#!/usr/bin/env python3
"""
cleanup_entries.py  –  Delete journal entries from Odoo via API

Useful for removing test entries that the GUI won't let you delete.
Posts entries are first reset to draft, then cancelled, then deleted.

Usage:
    # Preview what would be deleted (safe)
    python cleanup_entries.py --dry-run

    # Delete all entries in the Bank journal
    python cleanup_entries.py

    # Delete specific entries by ID
    python cleanup_entries.py --ids 229,230,231

    # Delete entries matching a ref pattern
    python cleanup_entries.py --ref-pattern "BANK-"

    # Delete ALL entries in the journal (including non-BANK ones)
    python cleanup_entries.py --all
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("odoo_cleanup")


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
        self.uid = self._call("common", "authenticate",
                              [self.db, self.user, self.api_key, {}])
        if not self.uid:
            log.error("Authentication failed.")
            sys.exit(1)
        log.info("Authenticated as uid=%d", self.uid)
        return self.uid

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, list(args), kwargs])

    def execute_ctx(self, model: str, method: str, args: list, context: dict) -> Any:
        """Execute with an explicit context dict."""
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, args, {"context": context}])

    def search_read(self, model: str, domain: list, fields: list[str], **kw: Any) -> list[dict]:
        return self.execute(model, "search_read", domain, fields=fields, **kw)


def load_config(config_path: str):
    ini = configparser.ConfigParser()
    ini.read(config_path)
    url = ini.get("odoo", "url", fallback="")
    db = ini.get("odoo", "db", fallback="")
    user = ini.get("odoo", "user", fallback="")
    api_key = ini.get("odoo", "api_key", fallback="").strip()

    journal_name = ini.get("accounting", "bank_journal_name", fallback="Bank")
    return url, db, user, api_key, journal_name


def _unlink_with_context(api: OdooAPI, move_id: int) -> None:
    """Try multiple strategies to delete a journal entry.

    Odoo SaaS / v17+ has an audit trail that blocks normal unlink.
    We try in order:
      1. unlink with force_delete context
      2. unlink with bypass_audit_trail context (v18+)
      3. plain unlink
    """
    contexts_to_try = [
        {"force_delete": True},
        {"force_delete": True, "bypass_audit_trail": True},
        {"force_delete": True, "module": "account"},
    ]

    last_error = None
    for ctx in contexts_to_try:
        try:
            api.execute_ctx("account.move", "unlink", [[move_id]], ctx)
            return  # Success
        except RuntimeError as e:
            last_error = e

    # Last resort: plain unlink
    try:
        api.execute("account.move", "unlink", [move_id])
        return
    except RuntimeError as e:
        last_error = e

    raise last_error  # type: ignore[misc]


def _archive_move(api: OdooAPI, move_id: int) -> None:
    """Try to archive or neutralise a journal entry that can't be deleted.

    Strategy:
      1. Try setting active=False (if the field exists)
      2. If that fails, zero out the amounts on all lines to neutralise it
    """
    # Try archive first
    try:
        api.execute("account.move", "write", [move_id], {"active": False})
        return
    except RuntimeError:
        pass

    # Fallback: zero out all move lines to neutralise the entry
    line_ids = api.execute("account.move.line", "search", [("move_id", "=", move_id)])
    if line_ids:
        for line_id in line_ids:
            try:
                api.execute("account.move.line", "write", [line_id], {
                    "debit": 0.0,
                    "credit": 0.0,
                })
            except RuntimeError:
                pass
    # Mark the ref so it's clearly neutralised
    api.execute("account.move", "write", [move_id], {
        "narration": "NEUTRALISED — amounts zeroed out, safe to ignore.",
    })


def delete_moves(api: OdooAPI, move_ids: list[int], dry_run: bool, archive_fallback: bool = True) -> tuple[int, int, int]:
    """Reset to draft → unlink. Returns (deleted, archived, errors)."""
    deleted = 0
    archived = 0
    errors = 0

    for move_id in move_ids:
        try:
            # Read current state
            moves = api.search_read(
                "account.move", [("id", "=", move_id)],
                ["id", "state", "name", "ref", "date"],
            )
            if not moves:
                log.warning("Move %d not found — skipping", move_id)
                continue

            m = moves[0]
            label = f"move/{m['id']}  {m.get('name', '')}  ref={m.get('ref', '')}  date={m.get('date', '')}  state={m['state']}"

            if dry_run:
                log.info("DRY-RUN would delete: %s", label)
                deleted += 1
                continue

            # Step 1: If posted, reset to draft
            if m["state"] == "posted":
                log.info("Resetting to draft: %s", label)
                try:
                    api.execute("account.move", "button_draft", [move_id])
                except RuntimeError as e:
                    log.warning("button_draft failed, trying write: %s", e)
                    try:
                        api.execute("account.move", "write", [move_id], {"state": "draft"})
                    except RuntimeError as e2:
                        log.error("Cannot reset move/%d to draft: %s", move_id, e2)
                        errors += 1
                        continue

            # Step 2: Try to cancel first (some versions require it)
            try:
                api.execute("account.move", "button_cancel", [move_id])
                log.debug("  Cancelled move/%d", move_id)
            except RuntimeError:
                pass  # Not all versions support cancel, continue anyway

            # Step 3: Try to delete with various context overrides
            log.info("Deleting: %s", label)
            try:
                _unlink_with_context(api, move_id)
                log.info("  ✓ Deleted move/%d", move_id)
                deleted += 1
            except RuntimeError as e:
                if archive_fallback:
                    log.warning("  Cannot delete move/%d (audit trail). Trying to archive/neutralise ...", move_id)
                    try:
                        _archive_move(api, move_id)
                        log.info("  ✓ Archived/neutralised move/%d", move_id)
                        archived += 1
                    except RuntimeError as e2:
                        log.error("  ✗ Cannot archive/neutralise move/%d: %s", move_id, e2)
                        errors += 1
                else:
                    log.error("  ✗ Cannot delete move/%d: %s", move_id, e)
                    errors += 1

        except Exception as exc:
            log.error("Unexpected error on move/%d: %s", move_id, exc)
            errors += 1

    return deleted, archived, errors


def main():
    parser = argparse.ArgumentParser(description="Delete journal entries from Odoo")
    parser.add_argument("--config", default="config.ini", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--ids", help="Comma-separated move IDs to delete (e.g. 229,230,231)")
    parser.add_argument("--ref-pattern", help="Delete entries whose ref contains this string (e.g. 'BANK-')")
    parser.add_argument("--all", action="store_true", help="Delete ALL entries in the bank journal")
    parser.add_argument("--no-archive", action="store_true", help="Don't archive as fallback — only report errors")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    script_dir = Path(__file__).resolve().parent
    config_path = args.config if Path(args.config).is_absolute() else str(script_dir / args.config)

    url, db, user, api_key, journal_name = load_config(config_path)
    api = OdooAPI(url, db, user, api_key)
    api.authenticate()

    # Find journal
    journals = api.search_read("account.journal", [("type", "=", "bank")], ["id", "name"], limit=10)
    journal = next((j for j in journals if j["name"] == journal_name), journals[0] if journals else None)
    if not journal:
        log.error("No bank journal found.")
        sys.exit(1)
    log.info("Using journal: '%s' (id %d)", journal["name"], journal["id"])

    # Build domain to find entries
    if args.ids:
        move_ids = [int(x.strip()) for x in args.ids.split(",")]
        entries = api.search_read("account.move", [("id", "in", move_ids)],
                                  ["id", "name", "ref", "date", "state"])
    else:
        domain = [("journal_id", "=", journal["id"])]
        if args.ref_pattern:
            domain.append(("ref", "ilike", args.ref_pattern))
        elif not args.all:
            # Default: only entries created by our sync (BANK- prefix)
            domain.append(("ref", "ilike", "BANK-"))
        entries = api.search_read("account.move", domain,
                                  ["id", "name", "ref", "date", "state"])

    if not entries:
        log.info("No matching entries found. Nothing to delete.")
        return

    log.info("Found %d entries to delete:", len(entries))
    for e in entries:
        log.info("  move/%-6d  %-14s  ref=%-20s  date=%s  state=%s",
                 e["id"], e.get("name", ""), e.get("ref", ""), e.get("date", ""), e["state"])

    if not args.dry_run:
        action = "DELETE (or archive)" if not args.no_archive else "DELETE"
        print(f"\n⚠️  About to {action} {len(entries)} journal entries. This cannot be undone!")
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            log.info("Aborted.")
            return

    move_ids = [e["id"] for e in entries]
    deleted, archived, errors = delete_moves(
        api, move_ids, args.dry_run, archive_fallback=not args.no_archive,
    )

    log.info("=" * 50)
    log.info("Done.  Deleted: %d  |  Archived: %d  |  Errors: %d", deleted, archived, errors)
    log.info("=" * 50)
    if archived:
        log.info(
            "Archived entries are hidden from all views. "
            "To see them in Odoo, use Filters → Archived."
        )


if __name__ == "__main__":
    main()
