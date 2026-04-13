#!/usr/bin/env python3
"""
sync_bank_entries.py  –  Sync bank CSV transactions into Odoo Accounting

Reads a Nordea-format bank statement CSV and creates journal entries in Odoo
so they appear in the Journaling overview and can be used for annual reports.

Usage:
    python sync_bank_entries.py [--config config.ini] [--dry-run]

Requirements:
    pip install python-dotenv   (optional, only if using .env files)
    No other external packages — uses Odoo's built-in XML-RPC API.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("odoo_sync")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BankTransaction:
    """One row from the bank CSV."""
    date: date
    amount: float
    sender: str
    recipient: str
    name: str
    balance: float
    currency: str
    invoice_number: str = ""

    @property
    def ref(self) -> str:
        """Stable reference used for duplicate detection."""
        raw = f"{self.date.isoformat()}|{self.amount}|{self.name}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    @property
    def label(self) -> str:
        return f"{self.date}  {self.amount:>+12.2f}  {self.name}"


# ---------------------------------------------------------------------------
# Account mapping rules  —  loaded from config.ini at runtime
# ---------------------------------------------------------------------------
# Rules live in [account_rules]: pattern = account_code  (first match wins)
# Fallbacks live in [account_fallbacks]: expense = <code>  income = <code>


@dataclass
class Config:
    """Parsed configuration values."""
    odoo_url: str = ""
    odoo_db: str = ""
    odoo_user: str = ""
    odoo_api_key: str = ""
    csv_file: str = ""
    bank_journal_name: str = "Bank"
    suspense_account_code: str = "999999"
    auto_post: bool = False
    dry_run: bool = False

    # Resolved at runtime
    uid: int = 0
    bank_journal_id: int = 0
    bank_account_id: int = 0
    suspense_account_id: int = 0
    currency_id: int = 0

    # Account code → Odoo account ID cache
    account_cache: dict = field(default_factory=dict)

    # Loaded from config.ini [account_rules] / [account_fallbacks]
    account_rules: list = field(default_factory=list)
    fallback_expense_code: str = "3690"
    fallback_income_code: str = "6520"


# ---------------------------------------------------------------------------
# CSV Parsing
# ---------------------------------------------------------------------------

def parse_csv(path: str) -> list[BankTransaction]:
    """Parse the Nordea bank statement CSV.

    Handles:
      - Semicolon or comma delimiters (auto-detected)
      - Danish number formats (comma as decimal separator)
      - Quoted fields
    """
    filepath = Path(path)
    if not filepath.exists():
        log.error("CSV file not found: %s", path)
        sys.exit(1)

    # Sniff delimiter
    with open(filepath, encoding="utf-8-sig") as f:
        sample = f.read(2048)
    delimiter = ";" if sample.count(";") > sample.count(",") else ","

    transactions: list[BankTransaction] = []

    with open(filepath, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)

        # Normalise header names (strip BOM, whitespace)
        if reader.fieldnames:
            reader.fieldnames = [h.strip().strip("\ufeff") for h in reader.fieldnames]

        for row in reader:
            try:
                raw_date = row.get("Bogføringsdato", "").strip()
                raw_amount = row.get("Beløb", "").strip()
                raw_balance = row.get("Saldo", "").strip()

                # Parse date – accept YYYY/MM/DD and YYYY-MM-DD
                dt = datetime.strptime(raw_date.replace("-", "/"), "%Y/%m/%d").date()

                # Parse amounts – handle Danish comma-decimal if needed
                amount = _parse_number(raw_amount)
                balance = _parse_number(raw_balance)

                # Invoice number column (may be spelled "Fakura nummer" or "Faktura nummer")
                invoice_nr = (
                    row.get("Fakura nummer", "")
                    or row.get("Faktura nummer", "")
                ).strip()

                txn = BankTransaction(
                    date=dt,
                    amount=amount,
                    sender=row.get("Afsender", "").strip(),
                    recipient=row.get("Modtager", "").strip(),
                    name=row.get("Navn", "").strip(),
                    balance=balance,
                    currency=row.get("Valuta", "DKK").strip(),
                    invoice_number=invoice_nr,
                )
                transactions.append(txn)
            except Exception as exc:
                log.warning("Skipping malformed row: %s — %s", row, exc)

    # Sort oldest-first (the CSV is newest-first)
    transactions.sort(key=lambda t: t.date)

    log.info("Parsed %d transactions from %s", len(transactions), path)
    return transactions


def _parse_number(raw: str) -> float:
    """Parse a number that may use comma or dot as decimal separator."""
    raw = raw.strip()
    if not raw:
        return 0.0
    # If both comma and dot are present, the last one is the decimal separator
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            # Comma is decimal: 1.234,56
            raw = raw.replace(".", "").replace(",", ".")
        else:
            # Dot is decimal: 1,234.56
            raw = raw.replace(",", "")
    elif "," in raw:
        # Could be decimal comma (1234,56) or thousands (1,234)
        parts = raw.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    return float(raw)


# ---------------------------------------------------------------------------
# Odoo JSON-RPC helpers
# ---------------------------------------------------------------------------

class OdooAPI:
    """Thin wrapper around Odoo's JSON-RPC /jsonrpc endpoint."""

    _req_id = 0

    def __init__(self, url: str, db: str, user: str, api_key: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self.uid: int = 0

    def _call(self, service: str, method: str, args: list) -> Any:
        OdooAPI._req_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "call",
            "id": OdooAPI._req_id,
            "params": {
                "service": service,
                "method": method,
                "args": args,
            },
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/jsonrpc",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        if body.get("error"):
            err = body["error"]
            msg = err.get("data", {}).get("message") or err.get("message", str(err))
            raise RuntimeError(f"Odoo RPC error: {msg}")
        return body.get("result")

    def authenticate(self) -> int:
        log.info("Connecting to %s (db=%s, user=%s) …", self.url, self.db, self.user)
        version = self._call("common", "version", [])
        log.info(
            "Odoo server version: %s",
            version.get("server_version", "unknown") if isinstance(version, dict) else version,
        )
        self.uid = self._call("common", "authenticate",
                              [self.db, self.user, self.api_key, {}])
        if not self.uid:
            log.error("Authentication failed. Check URL, database, user, and API key.")
            sys.exit(1)
        log.info("Authenticated as uid=%d", self.uid)
        return self.uid

    # Convenience wrappers --------------------------------------------------

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, list(args), kwargs])

    def search(self, model: str, domain: list, **kw: Any) -> list[int]:
        return self.execute(model, "search", domain, **kw)

    def search_read(self, model: str, domain: list, fields: list[str], **kw: Any) -> list[dict]:
        return self.execute(model, "search_read", domain, fields=fields, **kw)

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict]:
        return self.execute(model, "read", ids, fields=fields)

    def create(self, model: str, vals: dict) -> int:
        res = self.execute(model, "create", [vals])
        return res[0] if isinstance(res, list) else res

    def write(self, model: str, ids: list[int], vals: dict) -> bool:
        return self.execute(model, "write", ids, vals)


# ---------------------------------------------------------------------------
# Accounting logic
# ---------------------------------------------------------------------------

def resolve_journal(api: OdooAPI, cfg: Config) -> None:
    """Find or create the bank journal; populate cfg with IDs."""

    # --- Currency -----------------------------------------------------------
    curr = api.search_read(
        "res.currency", [("name", "=", "DKK")], ["id", "name"], limit=1
    )
    if curr:
        cfg.currency_id = curr[0]["id"]
        log.info("Currency DKK → id %d", cfg.currency_id)
    else:
        log.warning("Currency DKK not found; will use company default.")
        cfg.currency_id = False  # type: ignore[assignment]

    # --- Bank journal -------------------------------------------------------
    journals = api.search_read(
        "account.journal",
        [("name", "=", cfg.bank_journal_name), ("type", "=", "bank")],
        ["id", "name", "default_account_id"],
        limit=1,
    )
    if journals:
        j = journals[0]
        cfg.bank_journal_id = j["id"]
        cfg.bank_account_id = j["default_account_id"][0] if j.get("default_account_id") else 0
        log.info("Found bank journal '%s' → id %d", j["name"], j["id"])
    else:
        # Try to find *any* bank journal
        journals = api.search_read(
            "account.journal",
            [("type", "=", "bank")],
            ["id", "name", "default_account_id"],
            limit=5,
        )
        if journals:
            j = journals[0]
            cfg.bank_journal_id = j["id"]
            cfg.bank_account_id = j["default_account_id"][0] if j.get("default_account_id") else 0
            log.info(
                "Journal '%s' not found. Using existing bank journal '%s' (id %d).",
                cfg.bank_journal_name, j["name"], j["id"],
            )
        else:
            log.error(
                "No bank journal found in Odoo. Please create one in "
                "Accounting → Configuration → Journals first."
            )
            sys.exit(1)

    # --- Suspense / counterpart account ------------------------------------
    accs = api.search_read(
        "account.account",
        [("code", "=", cfg.suspense_account_code)],
        ["id", "code", "name"],
        limit=1,
    )
    if accs:
        cfg.suspense_account_id = accs[0]["id"]
        log.info("Suspense account %s '%s' → id %d",
                 accs[0]["code"], accs[0]["name"], accs[0]["id"])
    else:
        # Fallback: try the journal's default account or a generic suspense
        accs = api.search_read(
            "account.account",
            [("code", "like", "9999%")],
            ["id", "code", "name"],
            limit=1,
        )
        if accs:
            cfg.suspense_account_id = accs[0]["id"]
            log.info("Fallback suspense account %s → id %d",
                     accs[0]["code"], accs[0]["id"])
        else:
            # Use the bank journal's default account as temporary counterpart
            if cfg.bank_account_id:
                cfg.suspense_account_id = cfg.bank_account_id
                log.warning(
                    "No suspense account found (code=%s). "
                    "Using bank journal default account %d as counterpart. "
                    "You should recategorise these in Odoo.",
                    cfg.suspense_account_code,
                    cfg.bank_account_id,
                )
            else:
                log.error(
                    "Cannot determine a counterpart account. "
                    "Set suspense_account_code in config.ini to a valid account code."
                )
                sys.exit(1)

    if not cfg.bank_account_id:
        log.error(
            "Bank journal %d has no default account. Configure the journal in Odoo first.",
            cfg.bank_journal_id,
        )
        sys.exit(1)

    # --- Pre-resolve all account codes used in mapping rules ---------------
    codes_needed: set[str] = {cfg.fallback_expense_code, cfg.fallback_income_code}
    for _, code in cfg.account_rules:
        codes_needed.add(code)

    for code in sorted(codes_needed):
        accs = api.search_read(
            "account.account", [("code", "=", code)], ["id", "code", "name"], limit=1,
        )
        if accs:
            cfg.account_cache[code] = accs[0]["id"]
            log.info("Account %s '%s' → id %d", accs[0]["code"], accs[0]["name"], accs[0]["id"])
        else:
            log.error("Account code %s not found in chart of accounts!", code)
            sys.exit(1)


def get_existing_refs(api: OdooAPI, cfg: Config) -> set[str]:
    """Return set of `ref` values already in Odoo for this journal,
    so we can skip duplicates."""
    existing = api.search_read(
        "account.move",
        [
            ("journal_id", "=", cfg.bank_journal_id),
            ("ref", "!=", False),
        ],
        ["ref"],
    )
    refs: set[str] = set()
    for move in existing:
        if move.get("ref"):
            # A ref may contain our hash tag, e.g. "BANK-abc123def456"
            refs.add(move["ref"])
    return refs


def classify_transaction(txn: BankTransaction, cfg: Config) -> str:
    """Return the account code for the counterpart line of this transaction.

    Checks cfg.account_rules in order; first match wins.
    Falls back to cfg.fallback_expense_code (negative) or cfg.fallback_income_code (positive).
    """
    name_lower = txn.name.lower()
    for pattern, code in cfg.account_rules:
        if pattern in name_lower:  # patterns are already lowercased by configparser
            return code

    # No rule matched — use sign-based fallback
    if txn.amount < 0:
        return cfg.fallback_expense_code
    else:
        return cfg.fallback_income_code


def build_move_vals(txn: BankTransaction, cfg: Config) -> dict:
    """Construct the vals dict for account.move + embedded lines.

    Each bank transaction produces one journal entry with two lines:
      1. Bank account line (the actual bank movement)
      2. Counterpart line (mapped to the correct income/expense/equity account)
    """
    ref = f"BANK-{txn.ref}"  # Our stable reference for dedup

    amount = txn.amount
    abs_amount = abs(amount)

    # Determine the correct counterpart account
    cp_account_code = classify_transaction(txn, cfg)
    cp_account_id = cfg.account_cache.get(cp_account_code, cfg.suspense_account_id)

    # Line 1 — Bank side
    if amount >= 0:
        bank_debit = abs_amount
        bank_credit = 0.0
    else:
        bank_debit = 0.0
        bank_credit = abs_amount

    # Line 2 — Counterpart (mirror)
    cp_debit = bank_credit
    cp_credit = bank_debit

    line_vals = [
        (0, 0, {
            "account_id": cfg.bank_account_id,
            "name": txn.name,
            "debit": bank_debit,
            "credit": bank_credit,
            **({"currency_id": cfg.currency_id} if cfg.currency_id else {}),
        }),
        (0, 0, {
            "account_id": cp_account_id,
            "name": txn.name,
            "debit": cp_debit,
            "credit": cp_credit,
            **({"currency_id": cfg.currency_id} if cfg.currency_id else {}),
        }),
    ]

    move_vals: dict[str, Any] = {
        "journal_id": cfg.bank_journal_id,
        "date": txn.date.isoformat(),
        "ref": ref,
        "narration": f"Imported from CSV — {txn.name} → account {cp_account_code}",
        "line_ids": line_vals,
        "move_type": "entry",
    }
    if cfg.currency_id:
        move_vals["currency_id"] = cfg.currency_id

    return move_vals


def _attach_invoice(api: OdooAPI, move_id: int, invoice_number: str, cfg: Config) -> None:
    """Attach invoices/<invoice_number>.pdf to the journal entry."""
    base_dir = Path(cfg.csv_file).resolve().parent.parent  # up from ref/ to workspace
    pdf_path = base_dir / "invoices" / f"{invoice_number}.pdf"
    if not pdf_path.exists():
        log.warning("  Invoice PDF not found: %s", pdf_path)
        return

    pdf_data = pdf_path.read_bytes()
    attachment_vals = {
        "name": f"{invoice_number}.pdf",
        "type": "binary",
        "datas": base64.b64encode(pdf_data).decode("ascii"),
        "res_model": "account.move",
        "res_id": move_id,
        "mimetype": "application/pdf",
    }
    try:
        att_id = api.create("ir.attachment", attachment_vals)
        if isinstance(att_id, list):
            att_id = att_id[0]
        log.info("  → attached %s (attachment/%d)", pdf_path.name, att_id)
    except Exception as exc:
        log.warning("  → failed to attach %s: %s", pdf_path.name, exc)


def sync_transactions(
    api: OdooAPI,
    transactions: list[BankTransaction],
    cfg: Config,
) -> tuple[int, int, int]:
    """Sync transactions to Odoo. Returns (created, skipped, errors)."""

    existing_refs = get_existing_refs(api, cfg)
    log.info(
        "Found %d existing journal entries in journal %d",
        len(existing_refs), cfg.bank_journal_id,
    )

    created = 0
    skipped = 0
    errors = 0

    for txn in transactions:
        ref = f"BANK-{txn.ref}"

        if ref in existing_refs:
            # Attach invoice PDF to existing entry if missing
            if txn.invoice_number and not cfg.dry_run:
                moves = api.search_read(
                    "account.move",
                    [("ref", "=", ref), ("journal_id", "=", cfg.bank_journal_id)],
                    ["id"], limit=1,
                )
                if moves:
                    mid = moves[0]["id"]
                    existing_att = api.search_read(
                        "ir.attachment",
                        [("res_model", "=", "account.move"), ("res_id", "=", mid),
                         ("name", "=", f"{txn.invoice_number}.pdf")],
                        ["id"], limit=1,
                    )
                    if not existing_att:
                        _attach_invoice(api, mid, txn.invoice_number, cfg)
            log.debug("SKIP (exists): %s", txn.label)
            skipped += 1
            continue

        if cfg.dry_run:
            cp_code = classify_transaction(txn, cfg)
            log.info("DRY-RUN would create: %s  ref=%s  → acct %s", txn.label, ref, cp_code)
            created += 1
            continue

        vals = build_move_vals(txn, cfg)
        try:
            move_id = api.create("account.move", vals)
            # Odoo may return a list [id] or a plain int depending on version
            if isinstance(move_id, list):
                move_id = move_id[0]
            log.info("CREATED  move/%d:  %s", move_id, txn.label)

            if cfg.auto_post:
                try:
                    api.execute("account.move", "action_post", [move_id])
                    log.info("  → posted move/%d", move_id)
                except Exception as exc:
                    log.warning("  → could not post move/%d: %s", move_id, exc)

            # Attach invoice PDF if available
            if txn.invoice_number:
                _attach_invoice(api, move_id, txn.invoice_number, cfg)

            created += 1
            existing_refs.add(ref)  # prevent intra-batch duplicates

        except Exception as exc:
            log.error("FAILED to create entry for %s: %s", txn.label, exc)
            errors += 1

    return created, skipped, errors


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_config(config_path: str, args: argparse.Namespace) -> Config:
    """Load config from .ini file, with CLI overrides."""
    cfg = Config()

    ini = configparser.ConfigParser()
    if Path(config_path).exists():
        ini.read(config_path)
        log.info("Loaded config from %s", config_path)
    else:
        log.warning("Config file %s not found — using defaults + CLI args.", config_path)

    def get(section: str, key: str, default: str = "") -> str:
        return ini.get(section, key, fallback=default).strip()

    cfg.odoo_url = get("odoo", "url", "")
    cfg.odoo_db = get("odoo", "db", "")
    cfg.odoo_user = get("odoo", "user", "")
    cfg.odoo_api_key = get("odoo", "api_key", "")
    if not cfg.odoo_api_key:
        log.error("No api_key in [odoo] section of config.")
        sys.exit(1)

    base_dir = Path(config_path).resolve().parent if Path(config_path).exists() else Path.cwd()
    csv_rel = get("csv", "file", "ref/bank-statement.csv")
    csv_path = Path(csv_rel)
    if not csv_path.is_absolute():
        csv_path = base_dir / csv_path
    cfg.csv_file = str(csv_path)

    cfg.bank_journal_name = get("accounting", "bank_journal_name", "Bank")
    cfg.suspense_account_code = get("accounting", "suspense_account_code", "999999")
    cfg.auto_post = get("accounting", "auto_post", "false").lower() == "true"
    cfg.dry_run = get("accounting", "dry_run", "false").lower() == "true"

    # Account mapping rules from [account_rules] and [account_fallbacks]
    cfg.account_rules = []
    if ini.has_section("account_rules"):
        for pattern, code in ini.items("account_rules"):
            cfg.account_rules.append((pattern, code.strip()))
    cfg.fallback_expense_code = get("account_fallbacks", "expense", "3690")
    cfg.fallback_income_code = get("account_fallbacks", "income", "6520")

    # CLI overrides
    if getattr(args, "dry_run", False):
        cfg.dry_run = True

    return cfg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync bank CSV transactions into Odoo Accounting journal entries."
    )
    parser.add_argument(
        "--config", default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview mode — do not write anything to Odoo.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug-level logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Resolve config path relative to script location
    script_dir = Path(__file__).resolve().parent
    config_path = args.config
    if not Path(config_path).is_absolute():
        config_path = str(script_dir / config_path)

    cfg = load_config(config_path, args)

    if cfg.dry_run:
        log.info("*** DRY-RUN MODE — no changes will be written to Odoo ***")

    # 1. Parse CSV
    transactions = parse_csv(cfg.csv_file)
    if not transactions:
        log.info("No transactions found. Nothing to do.")
        return

    log.info(
        "Date range: %s → %s  |  %d transactions  |  Σ = %.2f",
        transactions[0].date, transactions[-1].date,
        len(transactions),
        sum(t.amount for t in transactions),
    )

    # 2. Connect to Odoo
    api = OdooAPI(cfg.odoo_url, cfg.odoo_db, cfg.odoo_user, cfg.odoo_api_key)
    cfg.uid = api.authenticate()

    # 3. Resolve journal + accounts
    resolve_journal(api, cfg)

    # 4. Sync
    created, skipped, errors = sync_transactions(api, transactions, cfg)

    # 5. Summary
    log.info("=" * 60)
    log.info("Sync complete.")
    log.info("  Created : %d", created)
    log.info("  Skipped : %d  (already in Odoo)", skipped)
    log.info("  Errors  : %d", errors)
    log.info("=" * 60)

    if created and not cfg.auto_post:
        log.info(
            "Entries were created as DRAFTS. Go to Accounting → Journals → %s "
            "to review and post them.",
            cfg.bank_journal_name,
        )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
