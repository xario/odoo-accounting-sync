#!/usr/bin/env python3
"""
manage_loan.py  –  Shareholder loan & asset in Odoo via native models

Uses Odoo's native account.loan and account.asset models (JSON-RPC) so that
entries appear under Accounting → Loans and Accounting → Assets.

Models a shareholder loan where the company holds shares as collateral.
Interest compounds annually at year-end.
All loan parameters are loaded from config.ini — see the [loan] section.

Usage:
    python manage_loan.py --dry-run          # preview
    python manage_loan.py                    # create loan + asset
    python manage_loan.py --schedule         # print interest schedule only
    python manage_loan.py --cleanup          # remove loan + asset records
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("loan")

# ---------------------------------------------------------------------------
# Loan parameters — all loaded from config.ini [loan] section at runtime
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# JSON-RPC client  (stdlib only — no external deps)
# ---------------------------------------------------------------------------
class OdooAPI:
    """Thin JSON-RPC 2.0 wrapper around Odoo's /jsonrpc endpoint."""

    _req_id = 0

    def __init__(self, url: str, db: str, user: str, api_key: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self.uid: int = 0

    # -- low level ----------------------------------------------------------

    def _call(self, service: str, method: str, args: list) -> any:
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

    # -- authentication -----------------------------------------------------

    def authenticate(self) -> int:
        self.uid = self._call("common", "authenticate",
                              [self.db, self.user, self.api_key, {}])
        if not self.uid:
            log.error("Authentication failed.")
            sys.exit(1)
        log.info("Authenticated as uid=%d", self.uid)
        return self.uid

    # -- ORM convenience ----------------------------------------------------

    def execute(self, model: str, method: str, *args, **kwargs) -> any:
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, list(args), kwargs])

    def search_read(self, model, domain, fields, **kw):
        return self.execute(model, "search_read", domain, fields=fields, **kw)

    def search(self, model, domain, **kw):
        return self.execute(model, "search", domain, **kw)

    def read(self, model, ids, fields):
        return self.execute(model, "read", ids, fields=fields)

    def create(self, model, vals) -> int:
        res = self.execute(model, "create", [vals])
        return res[0] if isinstance(res, list) else res

    def write(self, model, ids, vals):
        return self.execute(model, "write", ids, vals)

    def unlink(self, model, ids):
        return self.execute(model, "unlink", ids)

    def call(self, model, method, ids):
        """Call a button / action method (returns arbitrary result)."""
        return self.execute(model, method, ids)


# ---------------------------------------------------------------------------
# Interest schedule
# ---------------------------------------------------------------------------

@dataclass
class InterestPeriod:
    year: int
    start_date: date
    end_date: date
    days: int
    opening_balance: float
    rate: float          # percent
    interest: float
    closing_balance: float


def calculate_schedule(
    principal: float,
    loan_date: date,
    rate_pct: float,
    through_year: int,
) -> list[InterestPeriod]:
    """Compound-interest schedule: simple interest per period, credited at
    each 31 Dec so that the next period's base includes prior interest."""
    rate = rate_pct / 100.0
    schedule: list[InterestPeriod] = []
    balance = principal

    first_year = loan_date.year
    for year in range(first_year, through_year + 1):
        period_start = loan_date if year == first_year else date(year, 1, 1)
        period_end = date(year, 12, 31)

        if year == first_year:
            days = (period_end - period_start).days
        else:
            days = (period_end - date(year - 1, 12, 31)).days

        interest = round(balance * rate * days / 365, 2)
        closing = round(balance + interest, 2)

        schedule.append(InterestPeriod(
            year=year,
            start_date=period_start,
            end_date=period_end,
            days=days,
            opening_balance=balance,
            rate=rate_pct,
            interest=interest,
            closing_balance=closing,
        ))
        balance = closing

    return schedule


def print_schedule(principal: float, loan_date: date, schedule: list[InterestPeriod]) -> None:
    print()
    print(f"  Loan principal:  {principal:>14,.2f} DKK")
    print(f"  Loan date:       {loan_date}")
    print(f"  Rate:            {schedule[0].rate}% p.a. (fixed)")
    print()
    hdr = (f"  {'Year':<6}  {'Period':<25}  {'Days':>5}  "
           f"{'Opening':>14}  {'Interest':>12}  {'Closing':>14}")
    sep = f"  {'-'*6}  {'-'*25}  {'-'*5}  {'-'*14}  {'-'*12}  {'-'*14}"
    print(hdr)
    print(sep)
    total_int = 0.0
    for p in schedule:
        print(f"  {p.year:<6}  {str(p.start_date)+' → '+str(p.end_date):<25}  "
              f"{p.days:>5}  {p.opening_balance:>14,.2f}  "
              f"{p.interest:>12,.2f}  {p.closing_balance:>14,.2f}")
        total_int += p.interest
    print(sep)
    print(f"  {'Total':<6}  {'':<25}  {'':<5}  {'':<14}  "
          f"{total_int:>12,.2f}  {schedule[-1].closing_balance:>14,.2f}")
    print()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Cfg:
    url: str = ""
    db: str = ""
    user: str = ""
    api_key: str = ""
    dry_run: bool = False
    auto_post: bool = False
    effective_rate: float = 7.6

    # loan configuration (loaded from config.ini)
    principal: float = 0.0
    loan_date: date = date(2000, 1, 1)
    loan_name: str = ""
    asset_name: str = ""
    initial_entry_ref: str = "LOAN-INITIAL"
    journal_code: str = "MISC"
    account_asset_code: str = ""
    account_liability_code: str = ""
    account_interest_expense_code: str = ""
    asset_group_id: int = 1

    # resolved Odoo IDs (filled at runtime)
    journal_id: int = 0
    asset_account_id: int = 0
    liability_account_id: int = 0
    expense_account_id: int = 0
    currency_id: int = 0


def load_config(path: str, args: argparse.Namespace) -> Cfg:
    c = Cfg()
    ini = configparser.ConfigParser()
    if Path(path).exists():
        ini.read(path)

    c.url = ini.get("odoo", "url", fallback="").strip()
    c.db = ini.get("odoo", "db", fallback="").strip()
    c.user = ini.get("odoo", "user", fallback="").strip()
    c.api_key = ini.get("odoo", "api_key", fallback="").strip()

    c.auto_post = ini.get("accounting", "auto_post", fallback="false").strip().lower() == "true"
    c.dry_run = getattr(args, "dry_run", False)

    # Loan parameters from [loan] section
    c.effective_rate = float(ini.get("loan", "effective_rate", fallback="7.6"))
    c.principal = float(ini.get("loan", "principal", fallback="0.0"))
    c.loan_date = date.fromisoformat(ini.get("loan", "loan_date", fallback="2000-01-01"))
    c.loan_name = ini.get("loan", "loan_name", fallback="Shareholder loan")
    c.asset_name = ini.get("loan", "asset_name", fallback="Loan collateral asset")
    c.initial_entry_ref = ini.get("loan", "initial_entry_ref", fallback="LOAN-INITIAL")
    c.journal_code = ini.get("loan", "journal_code", fallback="MISC")
    c.account_asset_code = ini.get("loan", "account_asset", fallback="")
    c.account_liability_code = ini.get("loan", "account_liability", fallback="")
    c.account_interest_expense_code = ini.get("loan", "account_interest_expense", fallback="")
    c.asset_group_id = int(ini.get("loan", "asset_group_id", fallback="1"))
    log.info("Loan: %s  principal=%.2f  rate=%.2f%%", c.loan_date, c.principal, c.effective_rate)

    return c


def resolve_ids(api: OdooAPI, cfg: Cfg) -> None:
    """Look up journal, account, and currency IDs."""
    cur = api.search_read("res.currency", [("name", "=", "DKK")], ["id"], limit=1)
    cfg.currency_id = cur[0]["id"] if cur else 0

    jrn = api.search_read("account.journal",
                          [("code", "=", cfg.journal_code)], ["id", "name"], limit=1)
    if not jrn:
        log.error("Journal %s not found.", cfg.journal_code)
        sys.exit(1)
    cfg.journal_id = jrn[0]["id"]
    log.info("Journal: %s (id %d)", jrn[0]["name"], cfg.journal_id)

    for code, attr in [
        (cfg.account_asset_code, "asset_account_id"),
        (cfg.account_liability_code, "liability_account_id"),
        (cfg.account_interest_expense_code, "expense_account_id"),
    ]:
        recs = api.search_read("account.account", [("code", "=", code)],
                               ["id", "code", "name"], limit=1)
        if not recs:
            log.error("Account %s not found.", code)
            sys.exit(1)
        setattr(cfg, attr, recs[0]["id"])
        log.info("  %s  %s → id %d", recs[0]["code"], recs[0]["name"], recs[0]["id"])


# ---------------------------------------------------------------------------
# Native account.loan creation
# ---------------------------------------------------------------------------

def find_existing_loan(api: OdooAPI, cfg: Cfg) -> dict | None:
    recs = api.search_read("account.loan", [("name", "=", cfg.loan_name)],
                           ["id", "state", "amount_borrowed", "interest"], limit=1)
    return recs[0] if recs else None


def create_loan(api: OdooAPI, cfg: Cfg, schedule: list[InterestPeriod]) -> int:
    """Create account.loan with loan lines and confirm it.

    Trick: use the SAME account (7210) for both long_term and short_term.
    Then the generated journal entries for each interest-only line become:
        DR 3690 (expense)  /  CR 7210 (liability)
    which is exactly what we need for capitalised compound interest.

    The last line carries the bullet principal repayment so that
    sum(principals) == amount_borrowed (Odoo's hard constraint).
    """
    total_interest = round(sum(p.interest for p in schedule), 2)
    # Odoo requires: duration == number of loan lines (not months)
    duration = len(schedule)

    # Build line commands  [(0, 0, {...}), ...]
    line_cmds = []
    for i, p in enumerate(schedule):
        is_last = (i == len(schedule) - 1)
        line_cmds.append((0, 0, {
            "date": p.end_date.isoformat(),
            "principal": cfg.principal if is_last else 0.0,
            "interest": p.interest,
        }))

    vals = {
        "name": cfg.loan_name,
        "date": cfg.loan_date.isoformat(),
        "amount_borrowed": cfg.principal,
        "interest": total_interest,
        "duration": duration,
        "journal_id": cfg.journal_id,
        "long_term_account_id": cfg.liability_account_id,
        "short_term_account_id": cfg.liability_account_id,  # same account, interest-only lines
        "expense_account_id": cfg.expense_account_id,
        "asset_group_id": cfg.asset_group_id,
        "line_ids": line_cmds,
    }

    loan_id = api.create("account.loan", vals)
    log.info("CREATED  account.loan/%d  '%s'  (draft)", loan_id, cfg.loan_name)

    # Confirm → generates journal entries and moves to 'running' (or 'closed')
    try:
        api.call("account.loan", "action_confirm", [loan_id])
        rec = api.read("account.loan", [loan_id], ["state"])[0]
        log.info("  → confirmed (state=%s)", rec["state"])
    except Exception as exc:
        log.warning("  → confirm failed: %s", exc)
        log.warning("  Loan stays in draft — confirm manually in Odoo.")

    return loan_id


# ---------------------------------------------------------------------------
# Native account.asset creation
# ---------------------------------------------------------------------------

def find_existing_asset(api: OdooAPI, cfg: Cfg) -> dict | None:
    recs = api.search_read("account.asset", [("name", "=", cfg.asset_name)],
                           ["id", "state", "original_value"], limit=1)
    return recs[0] if recs else None


def create_asset(api: OdooAPI, cfg: Cfg) -> int:
    """Create an account.asset for the shares (no depreciation).

    The asset just records that the company holds shares worth PRINCIPAL,
    appearing under Accounting → Assets.
    """
    vals = {
        "name": cfg.asset_name,
        "original_value": cfg.principal,
        "acquisition_date": cfg.loan_date.isoformat(),
        "prorata_date": cfg.loan_date.isoformat(),
        "prorata_computation_type": "none",
        "account_asset_id": cfg.asset_account_id,
        "asset_group_id": cfg.asset_group_id,
    }

    asset_id = api.create("account.asset", vals)
    log.info("CREATED  account.asset/%d  '%s'  (draft)", asset_id, cfg.asset_name)

    # Try to confirm / open the asset
    try:
        api.call("account.asset", "validate", [asset_id])
        rec = api.read("account.asset", [asset_id], ["state"])[0]
        log.info("  → validated (state=%s)", rec["state"])
    except Exception as exc:
        log.warning("  → validate failed: %s  (asset stays draft)", exc)

    return asset_id


# ---------------------------------------------------------------------------
# Initial recognition journal entry  (DR 6060 asset / CR 7210 liability)
# ---------------------------------------------------------------------------

def find_initial_entry(api: OdooAPI, cfg: Cfg) -> dict | None:
    recs = api.search_read(
        "account.move",
        [("ref", "=", cfg.initial_entry_ref), ("journal_id", "=", cfg.journal_id)],
        ["id", "state"], limit=1,
    )
    return recs[0] if recs else None


def create_initial_entry(api: OdooAPI, cfg: Cfg) -> int:
    """Create the journal entry that puts the shares on the balance sheet.

    DR  6060  Amounts owed by participants  (asset — the shares)
    CR  7210  Debt to participants          (liability — the loan)

    The loan module handles interest entries, but NOT the initial principal
    recognition, so we create a plain journal entry for that.
    """
    vals = {
        "journal_id": cfg.journal_id,
        "date": cfg.loan_date.isoformat(),
        "ref": cfg.initial_entry_ref,
        "move_type": "entry",
        "line_ids": [
            (0, 0, {
                "account_id": cfg.asset_account_id,
                "name": cfg.asset_name,
                "debit": cfg.principal,
                "credit": 0.0,
            }),
            (0, 0, {
                "account_id": cfg.liability_account_id,
                "name": cfg.loan_name,
                "debit": 0.0,
                "credit": cfg.principal,
            }),
        ],
    }
    move_id = api.create("account.move", vals)
    log.info("CREATED  move/%d: Initial loan recognition  %.2f DKK", move_id, cfg.principal)

    # Auto-post
    try:
        api.call("account.move", "action_post", [move_id])
        log.info("  → posted move/%d", move_id)
    except Exception as exc:
        log.warning("  → could not post: %s", exc)

    return move_id


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup(api: OdooAPI, cfg: Cfg) -> None:
    """Remove existing loan and asset records."""
    # Loan
    loans = api.search_read("account.loan", [("name", "=", cfg.loan_name)],
                            ["id", "state"])
    for loan in loans:
        lid = loan["id"]
        state = loan["state"]
        try:
            if state == "running":
                api.call("account.loan", "action_cancel", [lid])
                log.info("  loan/%d cancelled", lid)
            if state in ("running", "cancelled", "closed"):
                api.call("account.loan", "action_set_to_draft", [lid])
                log.info("  loan/%d → draft", lid)
            api.unlink("account.loan", [lid])
            log.info("DELETED  account.loan/%d", lid)
        except Exception as exc:
            log.error("  Could not delete loan/%d: %s", lid, exc)

    # Asset
    assets = api.search_read("account.asset", [("name", "=", cfg.asset_name)],
                             ["id", "state"])
    for asset in assets:
        aid = asset["id"]
        try:
            # Try resetting to draft first (method may or may not exist)
            if asset["state"] != "draft":
                for m in ("set_to_draft", "action_set_to_draft", "button_draft"):
                    try:
                        api.call("account.asset", m, [aid])
                        break
                    except Exception:
                        pass
            api.unlink("account.asset", [aid])
            log.info("DELETED  account.asset/%d", aid)
        except Exception as exc:
            log.error("  Could not delete asset/%d: %s", aid, exc)

    # Initial recognition journal entry
    entries = api.search_read(
        "account.move",
        [("ref", "=", cfg.initial_entry_ref), ("journal_id", "=", cfg.journal_id)],
        ["id", "state"],
    )
    for entry in entries:
        eid = entry["id"]
        try:
            if entry["state"] == "posted":
                api.call("account.move", "button_draft", [eid])
            api.unlink("account.move", [eid])
            log.info("DELETED  move/%d (initial recognition)", eid)
        except Exception as exc:
            log.error("  Could not delete move/%d: %s", eid, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create shareholder loan & asset in Odoo using native models.")
    parser.add_argument("--config", default="config.ini")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--schedule", action="store_true",
                        help="Print interest schedule (no Odoo connection)")
    parser.add_argument("--through-year", type=int, default=None,
                        help="Calculate through this year (default: current)")
    parser.add_argument("--cleanup", action="store_true",
                        help="Remove existing loan + asset records")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    base = Path(__file__).resolve().parent
    cfg_path = args.config if Path(args.config).is_absolute() else str(base / args.config)
    cfg = load_config(cfg_path, args)

    through = args.through_year or date.today().year
    schedule = calculate_schedule(cfg.principal, cfg.loan_date, cfg.effective_rate, through)

    if args.schedule:
        print_schedule(cfg.principal, cfg.loan_date, schedule)
        return

    print_schedule(cfg.principal, cfg.loan_date, schedule)

    # ---- Connect ----------------------------------------------------------
    api = OdooAPI(cfg.url, cfg.db, cfg.user, cfg.api_key)
    api.authenticate()
    resolve_ids(api, cfg)

    # ---- Cleanup mode -----------------------------------------------------
    if args.cleanup:
        cleanup(api, cfg)
        return

    if cfg.dry_run:
        log.info("*** DRY-RUN MODE ***")

    # ---- Initial recognition entry (DR 6060 / CR 7210) -------------------
    existing_entry = find_initial_entry(api, cfg)
    if existing_entry:
        log.info("SKIP  move/%d initial recognition already exists (state=%s)",
                 existing_entry["id"], existing_entry["state"])
    elif cfg.dry_run:
        log.info("DRY-RUN  would create initial recognition entry  %.2f DKK",
                 cfg.principal)
    else:
        create_initial_entry(api, cfg)

    # ---- Asset ------------------------------------------------------------
    existing_asset = find_existing_asset(api, cfg)
    if existing_asset:
        log.info("SKIP  account.asset/%d already exists (state=%s)",
                 existing_asset["id"], existing_asset["state"])
    elif cfg.dry_run:
        log.info("DRY-RUN  would create account.asset '%s'  %.2f DKK",
                 cfg.asset_name, cfg.principal)
    else:
        create_asset(api, cfg)

    # ---- Loan -------------------------------------------------------------
    existing_loan = find_existing_loan(api, cfg)
    if existing_loan:
        log.info("SKIP  account.loan/%d already exists (state=%s)",
                 existing_loan["id"], existing_loan["state"])
    elif cfg.dry_run:
        total_int = sum(p.interest for p in schedule)
        log.info("DRY-RUN  would create account.loan '%s'", cfg.loan_name)
        log.info("  principal=%.2f  total_interest=%.2f  lines=%d",
                 cfg.principal, total_int, len(schedule))
        for i, p in enumerate(schedule):
            is_last = (i == len(schedule) - 1)
            log.info("  line %d: %s  principal=%10.2f  interest=%10.2f",
                     i + 1, p.end_date,
                     cfg.principal if is_last else 0.0, p.interest)
    else:
        create_loan(api, cfg, schedule)

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
