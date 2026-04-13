#!/usr/bin/env python3
"""
setup.py  –  One-shot Odoo setup: company logo + report language + name alignment

By default runs all three steps:
  1. Logo     – uploads a logo file, or generates one from the company name.
  2. Language – installs the target language and sets it on the company
               and all internal users so PDFs are produced in that language.
  3. Names    – renames account and report-line labels to match Danish annual
               report vocabulary (årsregnskabsloven class B).

Usage:
    python setup.py                             # logo (generated) + language
    python setup.py --logo logo.png             # use existing image + language
    python setup.py --save preview.png          # generate, save, upload + language
    python setup.py --skip-lang                 # logo only
    python setup.py --skip-logo                 # language only
    python setup.py --lang en_US                # override language from config
    python setup.py --dry-run                   # preview everything, no writes
    python setup.py --show                      # show current logo + language
    python setup.py --dry-run --name 'YourCompany' --save preview.png  # offline logo preview
"""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import json
import logging
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("setup")

SUPPORTED_TYPES = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",  # RIFF....WEBP
}


# ---------------------------------------------------------------------------
# JSON-RPC client (same pattern as other scripts in this project)
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
            "jsonrpc": "2.0",
            "method": "call",
            "id": OdooAPI._req_id,
            "params": {"service": service, "method": method, "args": args},
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

    def execute(self, model: str, method: str, *args, **kwargs):
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, list(args), kwargs])

    def search_read(self, model: str, domain: list, fields: list[str], **kw):
        return self.execute(model, "search_read", domain, fields=fields, **kw)

    def write(self, model: str, ids: list[int], vals: dict) -> bool:
        return self.execute(model, "write", ids, vals)

    def execute_lang(self, model: str, method: str, *args, lang: str = "da_DK", **kw):
        """Execute with an explicit language context so translated fields are written."""
        ctx = kw.pop("context", {})
        ctx["lang"] = lang
        kw["context"] = ctx
        return self._call("object", "execute_kw",
                          [self.db, self.uid, self.api_key,
                           model, method, list(args), kw])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_mime(data: bytes) -> str:
    for magic, mime in SUPPORTED_TYPES.items():
        if data[:len(magic)] == magic:
            return mime
    raise ValueError(
        "Unrecognised image format. Supported types: PNG, JPEG, GIF, WebP."
    )


def load_config(config_path: str) -> tuple[str, str, str, str, str, str]:
    ini = configparser.ConfigParser()
    ini.read(config_path)
    url          = ini.get("odoo", "url",          fallback="")
    db           = ini.get("odoo", "db",           fallback="")
    user         = ini.get("odoo", "user",         fallback="")
    api_key      = ini.get("odoo", "api_key",      fallback="").strip()
    company_name = ini.get("odoo", "company_name", fallback="").strip()
    language     = ini.get("odoo", "language",     fallback="").strip()
    return url, db, user, api_key, company_name, language


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def get_company(api: OdooAPI) -> dict:
    companies = api.search_read(
        "res.company", [],
        ["id", "name", "logo"],
        limit=1,
    )
    if not companies:
        log.error("No company found in Odoo.")
        sys.exit(1)
    return companies[0]


def show_current_logo(api: OdooAPI) -> None:
    company = get_company(api)
    has_logo = bool(company.get("logo"))
    size_kb = len(company["logo"]) * 3 // 4 // 1024 if has_logo else 0
    log.info("Company: %s (id=%d)", company["name"], company["id"])
    if has_logo:
        log.info("Logo is set (~%d KB base64-encoded)", size_kb)
    else:
        log.info("No logo is currently set.")


def upload_logo(api: OdooAPI, logo_path: Path, dry_run: bool) -> None:
    if not logo_path.exists():
        log.error("File not found: %s", logo_path)
        sys.exit(1)

    data = logo_path.read_bytes()
    mime = detect_mime(data)
    size_kb = len(data) / 1024
    log.info("Logo file: %s  (%.1f KB, %s)", logo_path, size_kb, mime)

    company = get_company(api)
    log.info("Target company: %s (id=%d)", company["name"], company["id"])

    if dry_run:
        log.info("DRY-RUN — would upload logo to company/%d", company["id"])
        return

    b64 = base64.b64encode(data).decode("ascii")
    api.write("res.company", [company["id"]], {"logo": b64})
    log.info("✓ Logo uploaded successfully to company/%d", company["id"])


# ---------------------------------------------------------------------------
# Language setup
# ---------------------------------------------------------------------------

def show_current_language(api: OdooAPI) -> None:
    company = api.search_read("res.company", [], ["id", "name", "lang"], limit=1)[0]
    lang_code = company.get("lang") or "(not set)"
    langs = api.search_read("res.lang", [("code", "=", lang_code)], ["name", "active"])
    lang_name = langs[0]["name"] if langs else "unknown"
    log.info("Company language: %s  (%s)", lang_code, lang_name)


def setup_language(api: OdooAPI, lang_code: str, dry_run: bool) -> None:
    """Load translations for *lang_code*, then set it on the company and all users.

    Just setting active=True on res.lang makes the language selectable but does
    NOT import .po translation files for installed modules.  The
    base.language.install wizard (with lang_ids Many2many, Odoo 17+) both
    activates the language and loads all module translations so that account
    group names, report labels, etc. render in the target language.
    """
    # 1. Find the language record, including inactive ones.
    langs = api._call("object", "execute_kw", [
        api.db, api.uid, api.api_key,
        "res.lang", "search_read",
        [[("code", "=", lang_code)]],
        {"fields": ["id", "name", "active"], "context": {"active_test": False}},
    ])

    if not langs:
        log.error(
            "Language code '%s' not found in res.lang. "
            "Check the code (e.g. da_DK, en_US) and that it is a supported Odoo locale.",
            lang_code,
        )
        return

    lang = langs[0]

    if dry_run:
        log.info("DRY-RUN — would load translations for %s (%s).", lang_code, lang["name"])
    else:
        # Run the wizard — this activates the language AND loads all .po files.
        # Odoo 17+ uses lang_ids (Many2many) instead of the old lang (Char) field.
        log.info("Loading translations for %s (%s) …", lang_code, lang["name"])
        try:
            wizard_id = api.execute(
                "base.language.install", "create",
                [{"lang_ids": [[4, lang["id"]]], "overwrite": False}],
            )
            if isinstance(wizard_id, list):
                wizard_id = wizard_id[0]
            api.execute("base.language.install", "lang_install", [wizard_id])
            log.info("  ✓ Translations loaded for %s.", lang_code)
        except RuntimeError as exc:
            log.warning("  Wizard failed (%s).", exc)
            # Fallback: at least ensure the language record is active.
            if not lang["active"]:
                api.write("res.lang", [lang["id"]], {"active": True})
                log.info("  ✓ Language %s activated (translations may be incomplete).", lang_code)
            else:
                log.info("  Language already active (translations may be incomplete).")

    # 2. Set language on the company's partner (res.company has no lang field).
    companies = api._call("object", "execute_kw", [
        api.db, api.uid, api.api_key,
        "res.company", "search_read", [[]],
        {"fields": ["id", "name", "partner_id"], "limit": 1},
    ])
    if not companies:
        log.error("No company found.")
        return
    company = companies[0]
    partner_id = company["partner_id"][0]
    log.info("Setting company partner language to %s (partner/%d) …", lang_code, partner_id)
    if dry_run:
        log.info("  DRY-RUN — would set partner/%d lang to %s.", partner_id, lang_code)
    else:
        api.write("res.partner", [partner_id], {"lang": lang_code})
        log.info("  ✓ Company partner language set.")

    # 3. Set language on all internal users so report templates render correctly.
    user_ids = api.execute("res.users", "search", [["share", "=", False]])
    log.info("Setting language for %d internal user(s) …", len(user_ids))
    if dry_run:
        log.info("  DRY-RUN — would set lang on %d user(s).", len(user_ids))
    else:
        api.write("res.users", user_ids, {"lang": lang_code})
        log.info("  ✓ User languages set.")


# ---------------------------------------------------------------------------
# Account & report-line name alignment (Danish annual report vocabulary)
# ---------------------------------------------------------------------------

# account.account renames  {code: new_danish_name}
ACCOUNT_RENAMES: dict[str, str] = {
    "1010": "Salg af varer og tjenesteydelser",
    "1610": "Varekøb",
    "2670": "Revision og regnskabsbistand",
    "2720": "Øreafrunding / kassedifferencer",
    "3690": "Øvrige finansielle omkostninger",
    "6060": "Andre værdipapirer og kapitalandele",
    "6481": "Likvide beholdninger",
    "6482": "Likvide beholdninger – modkonto",
    "6520": "Anpartskapital",
    "7210": "Gæld til virksomhedsdeltagere og ledelse",
    "7680": "Salgsmoms",
    "7700": "Moms af varekøb i udlandet (EU og ikke-EU)",
    "7720": "Moms af tjenesteydelseskøb i udlandet (EU og ikke-EU)",
    "7740": "Købsmoms",
}

# account.report.line renames  {code: new_danish_name}
REPORT_LINE_RENAMES: dict[str, str] = {
    # Balance Sheet — Assets
    "TA":    "Aktiver",
    "FA":    "Anlægsaktiver",
    "PNCA":  "Finansielle anlægsaktiver",
    "CA":    "Omsætningsaktiver",
    "BA":    "Likvide beholdninger",
    "REC":   "Tilgodehavender",
    "PRE":   "Periodiserede omkostninger",
    "CAS":   "Øvrige omsætningsaktiver",
    # Balance Sheet — Liabilities & Equity
    "EQ":                     "Egenkapital",
    "UNAFFECTED_EARNINGS":    "Overført resultat (udelt)",
    "CURR_YEAR_EARNINGS":     "Årets resultat (udelt)",
    "PREV_YEAR_EARNINGS":     "Overført resultat, primo (udelt)",
    "RETAINED_EARNINGS":      "Overført resultat",
    "CURR_RETAINED_EARNINGS": "Årets resultat",
    "PREV_RETAINED_EARNINGS": "Overført resultat, primo",
    "L":     "Gældsforpligtelser",
    "NL":    "Langfristede gældsforpligtelser",
    "CL":    "Kortfristede gældsforpligtelser",
    "CL1":   "Kortfristede gældsforpligtelser",
    "CL2":   "Leverandører af varer og tjenesteydelser",
    "CL3":   "Kreditgæld",
    "LE":    "Passiver",
    "OS":    "Eventualforpligtelser mv.",
    # P&L
    "REV":   "Nettoomsætning",
    "COS":   "Vareforbrug",
    "GRP":   "Bruttoresultat/-tab",
    "EXP":   "Driftsomkostninger",
    "INC":   "Resultat af ordinær primær drift",
    "OIN":   "Øvrige driftsindtægter",
    "OEXP":  "Øvrige finansielle omkostninger",
    "NEP":   "Årets resultat",
    "ALLOC": "Forslag til resultatdisponering",
    "NEPAL": "Resultat efter resultatdisponering",
    # Executive summary
    "EXEC_SUMMARY_NA": "Nettoaktiver",
    "EXEC_COS":        "Vareforbrug",
    "EXEC_NEP":        "Årets resultat",
}


def align_names(api: OdooAPI, dry_run: bool, lang: str = "da_DK") -> None:
    """Rename accounts and report lines to match Danish annual report vocabulary."""
    # --- Accounts ---
    accounts = api.execute("account.account", "search_read",
        [("active", "=", True)], fields=["id", "code", "name"])
    by_code = {a["code"]: a for a in accounts}
    acc_changed = 0
    for code, new_name in ACCOUNT_RENAMES.items():
        if code not in by_code:
            log.debug("  Account %s not found — skipping", code)
            continue
        acc = by_code[code]
        if acc["name"] == new_name:
            continue
        log.info("  account %s:  %r  →  %r", code, acc["name"], new_name)
        if not dry_run:
            api.execute_lang("account.account", "write", [acc["id"]], {"name": new_name}, lang=lang)
        acc_changed += 1
    log.info("Accounts: %d rename(s)%s", acc_changed, " (dry-run)" if dry_run else "")

    # --- Report lines ---
    lines = api.execute_lang("account.report.line", "search_read", [],
        fields=["id", "code", "name"], limit=500, lang=lang)
    by_line_code = {l["code"]: l for l in lines if l.get("code")}
    line_changed = 0
    for code, new_name in REPORT_LINE_RENAMES.items():
        if code not in by_line_code:
            log.debug("  Report line %s not found — skipping", code)
            continue
        line = by_line_code[code]
        if line["name"] == new_name:
            continue
        log.info("  report.line %s (%d):  %r  →  %r", code, line["id"], line["name"], new_name)
        if not dry_run:
            api.execute_lang("account.report.line", "write", [line["id"]], {"name": new_name}, lang=lang)
        line_changed += 1
    log.info("Report lines: %d rename(s)%s", line_changed, " (dry-run)" if dry_run else "")


# ---------------------------------------------------------------------------
# Logo generator (pure Python, no external dependencies)
# ---------------------------------------------------------------------------

_CANVAS_SIZE = 400  # output image size in pixels (square)
_CHAR_W = 5         # font character width in font-pixels
_CHAR_H = 7         # font character height in font-pixels
_GAP = 2            # gap between characters in font-pixels

# 5×7 bitmap font.  Each character: 7 ints, each int is a 5-bit row mask
# (bit 4 = leftmost pixel).
_FONT: dict[str, list[int]] = {
    ' ': [0b00000]*7,
    'A': [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    'B': [0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110],
    'C': [0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110],
    'D': [0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110],
    'E': [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
    'F': [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000],
    'G': [0b01110, 0b10001, 0b10000, 0b10011, 0b10001, 0b10001, 0b01110],
    'H': [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    'I': [0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    'J': [0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100],
    'K': [0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001],
    'L': [0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111],
    'M': [0b10001, 0b11011, 0b10101, 0b10001, 0b10001, 0b10001, 0b10001],
    'N': [0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001],
    'O': [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    'P': [0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000],
    'Q': [0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101],
    'R': [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
    'S': [0b01110, 0b10001, 0b10000, 0b01110, 0b00001, 0b10001, 0b01110],
    'T': [0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
    'U': [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    'V': [0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b01010, 0b00100],
    'W': [0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001],
    'X': [0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001],
    'Y': [0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100],
    'Z': [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111],
}

# Pleasant background colours (deterministic choice from company name hash).
_PALETTE: list[tuple[int, int, int]] = [
    (41,  128, 185),  # blue
    (39,  174, 96),   # green
    (142, 68,  173),  # purple
    (192, 57,  43),   # red
    (230, 126, 34),   # orange
    (26,  188, 156),  # teal
    (44,  62,  80),   # dark navy
    (22,  160, 133),  # sea green
]


def _get_initials(name: str) -> str:
    """Return up to 2 uppercase initials from a company name."""
    # Strip common legal suffixes so they don't crowd the initials.
    _SKIP = {"APS", "A/S", "AS", "INC", "LLC", "LTD", "GMBH", "AB", "BV", "NV"}
    words = [w for w in name.upper().split() if any(c.isalpha() for c in w)]
    filtered = [w for w in words if w.rstrip(".") not in _SKIP]
    words = filtered if filtered else words
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2]
    return "".join(w[0] for w in words[:2])


def _pick_color(name: str) -> tuple[int, int, int]:
    idx = int(hashlib.md5(name.lower().encode()).hexdigest(), 16) % len(_PALETTE)
    return _PALETTE[idx]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def make_logo_png(company_name: str) -> bytes:
    """Generate a _CANVAS_SIZE × _CANVAS_SIZE RGB PNG with company initials."""
    initials = _get_initials(company_name)
    bg = _pick_color(company_name)
    fg = (255, 255, 255)

    n = len(initials)
    text_w_fp = n * _CHAR_W + max(0, n - 1) * _GAP
    target = int(_CANVAS_SIZE * 0.72)
    scale = min(target // text_w_fp, target // _CHAR_H)

    text_w = text_w_fp * scale
    text_h = _CHAR_H * scale
    ox = (_CANVAS_SIZE - text_w) // 2
    oy = (_CANVAS_SIZE - text_h) // 2

    # Allocate pixel grid filled with background colour.
    rows: list[list[tuple[int, int, int]]] = [[bg] * _CANVAS_SIZE for _ in range(_CANVAS_SIZE)]

    for char_idx, ch in enumerate(initials):
        glyph = _FONT.get(ch, [0b11111] * _CHAR_H)
        char_ox = ox + char_idx * (_CHAR_W + _GAP) * scale
        for row_idx, row_bits in enumerate(glyph):
            for col_idx in range(_CHAR_W):
                if (row_bits >> (_CHAR_W - 1 - col_idx)) & 1:
                    py = oy + row_idx * scale
                    px = char_ox + col_idx * scale
                    for dy in range(scale):
                        for dx in range(scale):
                            if 0 <= py + dy < _CANVAS_SIZE and 0 <= px + dx < _CANVAS_SIZE:
                                rows[py + dy][px + dx] = fg

    # Encode scanlines: each row prefixed with filter byte 0 (None).
    raw = bytearray()
    for row in rows:
        raw += b"\x00"
        for r, g, b in row:
            raw.append(r)
            raw.append(g)
            raw.append(b)

    # PNG: signature + IHDR + IDAT + IEND
    ihdr = struct.pack(">IIBBBBB", _CANVAS_SIZE, _CANVAS_SIZE, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=6))
        + _png_chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure Odoo: upload/generate company logo and set report language.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python setup.py                              # generate logo + set language\n"
            "  python setup.py --logo logo.png             # upload logo + set language\n"
            "  python setup.py --skip-lang                 # logo only\n"
            "  python setup.py --skip-logo                 # language only\n"
            "  python setup.py --lang en_US                # override language\n"
            "  python setup.py --dry-run                   # preview, no writes\n"
            "  python setup.py --show                      # show current logo + language\n"
            "  python setup.py --dry-run --name 'YourCompany' --save preview.png\n"
        ),
    )
    # Logo options
    logo_grp = parser.add_argument_group("logo")
    logo_grp.add_argument(
        "--logo", metavar="FILE",
        help="Existing image to upload (PNG, JPEG, GIF, WebP). "
             "Omit to generate from company name.",
    )
    logo_grp.add_argument(
        "--name", metavar="NAME",
        help="Company name for logo generation (overrides config and Odoo lookup).",
    )
    logo_grp.add_argument(
        "--save", metavar="FILE",
        help="Save the generated PNG locally (e.g. preview.png).",
    )
    logo_grp.add_argument(
        "--skip-logo", action="store_true",
        help="Skip the logo step entirely.",
    )
    # Language options
    lang_grp = parser.add_argument_group("language")
    lang_grp.add_argument(
        "--lang", metavar="CODE",
        help="Language code to install and activate (e.g. da_DK, en_US). "
             "Defaults to the value in config.ini [odoo] language.",
    )
    lang_grp.add_argument(
        "--skip-lang", action="store_true",
        help="Skip the language step entirely.",
    )
    # Name alignment options
    names_grp = parser.add_argument_group("names")
    names_grp.add_argument(
        "--skip-names", action="store_true",
        help="Skip the account/report-line name alignment step.",
    )
    # General options
    parser.add_argument(
        "--show", action="store_true",
        help="Show current logo status and company language, then exit.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be done without writing anything to Odoo.",
    )
    parser.add_argument("--config", default="config.ini",
                        help="Path to config.ini (default: config.ini).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    script_dir = Path(__file__).resolve().parent
    config_path = args.config if Path(args.config).is_absolute() else str(script_dir / args.config)
    url, db, user, api_key, cfg_company_name, cfg_language = load_config(config_path)

    lang_code = args.lang or cfg_language

    # Only skip the Odoo connection for a pure offline logo preview.
    offline = bool((args.name or cfg_company_name) and args.dry_run
                   and not args.show and args.skip_lang and args.skip_names)
    api: OdooAPI | None = None
    if not offline:
        api = OdooAPI(url, db, user, api_key)
        api.authenticate()

    # ── Show mode ────────────────────────────────────────────────────────
    if args.show:
        show_current_logo(api)  # type: ignore[arg-type]
        show_current_language(api)  # type: ignore[arg-type]
        return

    # ── Logo step ────────────────────────────────────────────────────────
    if not args.skip_logo:
        if args.logo:
            upload_logo(api, Path(args.logo), dry_run=args.dry_run)  # type: ignore[arg-type]
        else:
            if args.name:
                company_name = args.name
                company_id: int | None = None
            elif cfg_company_name:
                company_name = cfg_company_name
                company_id = None
            else:
                company = get_company(api)  # type: ignore[arg-type]
                company_name = company["name"]
                company_id = company["id"]

            log.info("Generating logo for '%s' …", company_name)
            png_data = make_logo_png(company_name)
            log.info(
                "Generated %d-byte PNG (%d×%d px, initials: %s)",
                len(png_data), _CANVAS_SIZE, _CANVAS_SIZE, _get_initials(company_name),
            )

            if args.save:
                save_path = Path(args.save)
                save_path.write_bytes(png_data)
                log.info("Saved to %s", save_path)

            if args.dry_run:
                log.info("DRY-RUN — logo not uploaded.")
                if not args.save:
                    log.info("  Use --save logo.png to save the image for inspection.")
            else:
                if company_id is None:
                    company_id = get_company(api)["id"]  # type: ignore[index]
                b64 = base64.b64encode(png_data).decode("ascii")
                api.write("res.company", [company_id], {"logo": b64})  # type: ignore[union-attr]
                log.info("✓ Logo uploaded to company/%d", company_id)
                log.info("  Appears on all PDF reports (invoices, accounting, etc.).")

    # ── Language step ─────────────────────────────────────────────────────
    if not args.skip_lang:
        if not lang_code:
            log.warning(
                "No language configured. Set 'language' in config.ini [odoo] "
                "or pass --lang CODE to enable this step."
            )
        else:
            setup_language(api, lang_code, dry_run=args.dry_run)  # type: ignore[arg-type]

    # ── Name alignment step ─────────────────────────────────────────────
    if not args.skip_names:
        log.info("--- Aligning account and report names ---")
        align_names(api, dry_run=args.dry_run, lang=lang_code or "da_DK")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
