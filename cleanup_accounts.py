#!/usr/bin/env python3
"""
cleanup_accounts.py  –  Archive unused accounts in Odoo

Sets active=False on accounts that have no journal entry lines and are not
referenced by any journal or tax configuration.  This hides them from reports
and selection dropdowns without deleting data.

Usage:
    python cleanup_accounts.py --dry-run     # preview what would be archived
    python cleanup_accounts.py               # archive unused accounts
    python cleanup_accounts.py --restore     # re-activate all archived accounts
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cleanup")


# ---------------------------------------------------------------------------
# JSON-RPC client
# ---------------------------------------------------------------------------
class OdooAPI:
    _req_id = 0

    def __init__(self, url: str, db: str, user: str, api_key: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self.uid: int = 0

    def _call(self, service: str, method: str, args: list):
        OdooAPI._req_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": OdooAPI._req_id,
            "params": {"service": service, "method": method, "args": args},
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/jsonrpc", data=payload,
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
        self.uid = self._call("common", "authenticate",
                              [self.db, self.user, self.api_key, {}])
        if not self.uid:
            log.error("Authentication failed.")
            sys.exit(1)
        log.info("Authenticated as uid=%d", self.uid)
        return self.uid

    def execute(self, model, method, *args, **kwargs):
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, list(args), kwargs])

    def search_read(self, model, domain, fields, **kw):
        return self.execute(model, "search_read", domain, fields=fields, **kw)

    def write(self, model, ids, vals):
        return self.execute(model, "write", ids, vals)


# ---------------------------------------------------------------------------
# Determine which accounts are in use
# ---------------------------------------------------------------------------

def get_used_account_ids(api: OdooAPI) -> set[int]:
    """Return IDs of accounts that are actively referenced and must stay."""
    used: set[int] = set()

    # 1. Accounts with journal entry lines
    offset = 0
    while True:
        lines = api.search_read(
            "account.move.line", [],
            ["account_id"], limit=5000, offset=offset,
        )
        if not lines:
            break
        for ln in lines:
            aid = ln["account_id"]
            used.add(aid[0] if isinstance(aid, list) else aid)
        if len(lines) < 5000:
            break
        offset += len(lines)
    log.info("Accounts in journal entries: %d", len(used))

    # 2. Default / suspense accounts on journals
    journals = api.search_read("account.journal", [], [
        "default_account_id", "suspense_account_id",
        "profit_account_id", "loss_account_id",
    ])
    for j in journals:
        for fld in ("default_account_id", "suspense_account_id",
                     "profit_account_id", "loss_account_id"):
            v = j.get(fld)
            if v:
                used.add(v[0] if isinstance(v, list) else v)

    # 3. Tax repartition line accounts
    taxes = api.search_read("account.tax", [], [
        "invoice_repartition_line_ids", "refund_repartition_line_ids",
    ])
    rep_ids: list[int] = []
    for t in taxes:
        rep_ids.extend(t.get("invoice_repartition_line_ids", []))
        rep_ids.extend(t.get("refund_repartition_line_ids", []))
    if rep_ids:
        reps = api.search_read(
            "account.tax.repartition.line",
            [("id", "in", rep_ids)], ["account_id"],
        )
        for r in reps:
            v = r.get("account_id")
            if v:
                used.add(v[0] if isinstance(v, list) else v)

    # 4. Company-level property accounts (receivable, payable, income, expense defaults)
    try:
        props = api.search_read("ir.property", [
            ("fields_id.model", "=", "res.partner"),
            ("fields_id.name", "in", [
                "property_account_receivable_id",
                "property_account_payable_id",
            ]),
        ], ["value_reference"])
        for p in props:
            ref = p.get("value_reference", "")
            if ref and ref.startswith("account.account,"):
                try:
                    used.add(int(ref.split(",")[1]))
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass  # ir.property may not be accessible

    # 5. Loan and asset accounts
    for model, flds in [
        ("account.loan", ["long_term_account_id", "short_term_account_id", "expense_account_id"]),
        ("account.asset", ["account_asset_id", "account_depreciation_id", "account_depreciation_expense_id"]),
    ]:
        try:
            recs = api.search_read(model, [], flds)
            for rec in recs:
                for fld in flds:
                    v = rec.get(fld)
                    if v:
                        used.add(v[0] if isinstance(v, list) else v)
        except Exception:
            pass

    log.info("Total protected accounts: %d", len(used))
    return used


# ---------------------------------------------------------------------------
# Main actions
# ---------------------------------------------------------------------------

def archive_unused(api: OdooAPI, dry_run: bool) -> None:
    used_ids = get_used_account_ids(api)

    # Get all currently active accounts
    all_accounts = api.search_read(
        "account.account", [("active", "=", True)],
        ["id", "code", "name", "account_type"],
        order="code",
    )
    log.info("Active accounts: %d", len(all_accounts))

    to_archive = [a for a in all_accounts if a["id"] not in used_ids]
    to_keep = [a for a in all_accounts if a["id"] in used_ids]

    print()
    print(f"  Accounts to KEEP ({len(to_keep)}):")
    for a in to_keep:
        print(f"    {a['code']:>8}  {a['name']}")

    print()
    print(f"  Accounts to ARCHIVE ({len(to_archive)}):")
    # Group by account_type for readable output
    by_type: dict[str, list] = {}
    for a in to_archive:
        by_type.setdefault(a["account_type"], []).append(a)
    for atype in sorted(by_type):
        accs = by_type[atype]
        print(f"    [{atype}]  ({len(accs)} accounts)")
        for a in accs[:5]:
            print(f"      {a['code']:>8}  {a['name']}")
        if len(accs) > 5:
            print(f"      ... and {len(accs) - 5} more")
    print()

    if dry_run:
        log.info("DRY-RUN — no changes made.  %d accounts would be archived.", len(to_archive))
        return

    if not to_archive:
        log.info("Nothing to archive.")
        return

    ids = [a["id"] for a in to_archive]
    # Archive in batches of 100 to avoid potential size limits
    batch_size = 100
    archived = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        api.write("account.account", batch, {"active": False})
        archived += len(batch)
        log.info("  archived %d / %d", archived, len(ids))

    log.info("Done.  Archived %d accounts, kept %d.", len(to_archive), len(to_keep))


def restore_all(api: OdooAPI, dry_run: bool) -> None:
    """Re-activate all archived accounts."""
    archived = api.search_read(
        "account.account",
        [("active", "=", False)],
        ["id", "code", "name"],
        order="code",
        context={"active_test": False},
    )
    if not archived:
        log.info("No archived accounts found.")
        return

    log.info("Found %d archived accounts.", len(archived))
    if dry_run:
        for a in archived[:20]:
            print(f"  {a['code']:>8}  {a['name']}")
        if len(archived) > 20:
            print(f"  ... and {len(archived) - 20} more")
        log.info("DRY-RUN — no changes made.")
        return

    ids = [a["id"] for a in archived]
    batch_size = 100
    restored = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        api.write("account.account", batch, {"active": True})
        restored += len(batch)
        log.info("  restored %d / %d", restored, len(ids))

    log.info("Done.  Restored %d accounts.", len(archived))


# ---------------------------------------------------------------------------
# Config & CLI
# ---------------------------------------------------------------------------

def load_connection(config_path: str) -> tuple[str, str, str, str]:
    ini = configparser.ConfigParser()
    if Path(config_path).exists():
        ini.read(config_path)
    url = ini.get("odoo", "url", fallback="").strip()
    db = ini.get("odoo", "db", fallback="").strip()
    user = ini.get("odoo", "user", fallback="").strip()
    api_key = ini.get("odoo", "api_key", fallback="").strip()
    return url, db, user, api_key


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive unused accounts in Odoo to declutter reports.")
    parser.add_argument("--config", default="config.ini")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--restore", action="store_true",
                        help="Re-activate all archived accounts")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    base = Path(__file__).resolve().parent
    cfg = args.config if Path(args.config).is_absolute() else str(base / args.config)
    url, db, user, api_key = load_connection(cfg)

    api = OdooAPI(url, db, user, api_key)
    api.authenticate()

    if args.restore:
        restore_all(api, args.dry_run)
    else:
        archive_unused(api, args.dry_run)


if __name__ == "__main__":
    main()
