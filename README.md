# Odoo Accounting Tools

Python scripts for managing Odoo Accounting (SaaS 19.2) via JSON-RPC.  
No external packages required — uses Python 3 stdlib only.

## Prerequisites

- Python 3.10+
- Odoo API key (Settings → Technical → API Keys)

## Configuration

All scripts read `config.ini` in the same directory.

```ini
# config.ini.example  –  Copy to config.ini and fill in your values.
# config.ini is listed in .gitignore and must never be committed.

[odoo]
url          = https://yourcompany.odoo.com
db           = yourdb
user         = admin@example.com
api_key      = your_api_key_here
company_name = Your Company ApS
language     = da_DK

[csv]
# Path to bank statement CSV, relative to this file.
file = ref/your-bank-statement.csv

[accounting]
# Name of the bank journal in Odoo (Accounting → Configuration → Journals)
bank_journal_name = Bank

# Account code used as counterpart when no mapping rule matches and as
# fallback when a looked-up code is missing.
suspense_account_code = 999999

# "true" to post/validate journal entries automatically; "false" for draft
auto_post = false

# "true" to preview only (no writes to Odoo)
dry_run = false

[loan]
# Loan parameters
principal         = 1000000.00
loan_date         = 2023-01-01
loan_name         = Shareholder loan
asset_name        = Loan collateral asset
initial_entry_ref = LOAN-INITIAL

# Interest rate components
diskonto_at_signing = 3.6
spread              = 4.0
effective_rate      = 7.6

# Odoo account codes, journal, and asset group
# Adjust to match your chart of accounts.
account_asset            = 6060
account_liability        = 7210
account_interest_expense = 3690
journal_code             = MISC
asset_group_id           = 1

[account_rules]
# Keyword rules for classifying bank transactions.
# Format: pattern = account_code
# Rules are applied in order; first match wins (case-insensitive).
# Add or remove rules to match your bank statement descriptions.
Auditor Name      = 2670
Bank package fee  = 3690
Bank setup fee    = 3690
Card fee          = 3690

[account_fallbacks]
# Fallback account codes when no keyword rule matches.
expense = 3690
income  = 6520
```

---

## Scripts

### `sync_bank_entries.py` — Bank CSV → Odoo journal entries

Parses a Nordea bank statement CSV and creates one journal entry per
transaction in the configured bank journal.  Duplicate detection is based on
a SHA-1 hash of (date, amount, description) stored as the move reference, so
re-running the script is safe.

If an `invoices/` directory is present and the CSV has a "Fakura nummer"
column, matching PDFs (`invoices/<number>.pdf`) are attached to the
corresponding journal entry.

**Account mapping** is controlled by `ACCOUNT_RULES` at the top of the
script.  The first matching rule wins; falls back to `3690` (expense) or
`6520` (income) when nothing matches.

```
python sync_bank_entries.py                  # sync all unsynced transactions
python sync_bank_entries.py --dry-run        # preview only
python sync_bank_entries.py --config path/to/other.ini
```

**CSV format** (Nordea Driftskonto export, semicolon-delimited):

| Column         | Description                         |
|----------------|-------------------------------------|
| Dato           | Transaction date (DD-MM-YYYY)       |
| Beløb          | Amount (Danish decimal: comma)      |
| Afsender       | Sender name                         |
| Modtager       | Recipient name                      |
| Navn           | Transaction description             |
| Saldo          | Running balance                     |
| Valuta         | Currency code (e.g. DKK)            |
| Fakura nummer  | Invoice number for PDF attachment   |

---

### `manage_loan.py` — Shareholder loan & asset (native Odoo models)

Creates a shareholder loan and the corresponding collateral asset using
Odoo's native `account.loan` and `account.asset` models so that they appear
under Accounting → Loans and Accounting → Assets.

The script also posts an initial recognition entry (DR 6060 / CR 7210)
idempotently (ref `LOAN-INITIAL`).

```
python manage_loan.py                # create loan + asset + recognition entry
python manage_loan.py --dry-run      # preview, no writes
python manage_loan.py --schedule     # print interest schedule only
python manage_loan.py --cleanup      # remove all loan/asset records created by this script
```

---

### `cleanup_entries.py` — Delete journal entries

Resets entries to draft → cancels → deletes.  Falls back to archiving or
zeroing amounts when Odoo's audit trail blocks deletion.

```
python cleanup_entries.py                         # delete all BANK- entries (asks for confirmation)
python cleanup_entries.py --dry-run               # preview only
python cleanup_entries.py --ids 229,230,231       # delete specific entries by ID
python cleanup_entries.py --ref-pattern "BANK-"   # delete by ref pattern
python cleanup_entries.py --all                   # delete ALL entries in the bank journal
python cleanup_entries.py --no-archive            # error instead of archiving as fallback
```

---

### `setup.py` — Upload or generate a company logo, set report language, align names

Uploads a logo to the Odoo company record (`res.company.logo`), which Odoo
uses on all printed/PDF reports — invoices, accounting reports, etc.

By default runs all three steps when called without flags:
1. **Logo** — uploads a logo file, or generates one from `company_name` in config.
2. **Language** — installs the target language and sets it on the company and all
   internal users so PDFs render in that language.
3. **Names** — renames `account.account` and `account.report.line` labels to match
   Danish annual report vocabulary (årsregnskabsloven class B).

If `--logo` is not given, the script fetches the company name from Odoo and
**generates a PNG automatically**: solid colour background (derived from the
company name) with white initials centered on it.  The generator uses only
Python stdlib — no external packages required.

```
python setup.py                                   # all three steps
python setup.py --logo logo.png                   # upload logo + language + names
python setup.py --skip-lang                       # logo + names only
python setup.py --skip-logo                       # language + names only
python setup.py --skip-names                      # logo + language only
python setup.py --lang en_US                      # override language
python setup.py --save preview.png                # generate, save locally, and upload
python setup.py --dry-run --name 'Xario' --save preview.png  # offline logo preview
python setup.py --show                            # check current logo + language
```

| Option | Description |
|--------|-------------|
| `--logo FILE` | Upload an existing PNG/JPEG/GIF/WebP instead of generating |
| `--name NAME` | Override the company name used for generation |
| `--save FILE` | Save the generated PNG locally for inspection |
| `--lang CODE` | Language code to activate (e.g. `da_DK`, `en_US`). Defaults to config |
| `--skip-logo` | Skip the logo step |
| `--skip-lang` | Skip the language step |
| `--skip-names` | Skip the name alignment step |
| `--show` | Print current logo status and company language |
| `--dry-run` | Preview without writing to Odoo |

---

### `cleanup_accounts.py` — Archive unused chart-of-account entries

Sets `active=False` on accounts that have no journal entry lines and are not
referenced by any journal, tax, loan, or asset configuration.  Archived
accounts are hidden from reports and dropdowns but not deleted.

```
python cleanup_accounts.py --dry-run    # preview what would be archived
python cleanup_accounts.py              # archive unused accounts
python cleanup_accounts.py --restore    # re-activate all archived accounts
```

---

## Directory layout

```
odoo/
├── config.ini              # credentials and settings
├── setup.py
├── sync_bank_entries.py
├── manage_loan.py
├── cleanup_entries.py
├── cleanup_accounts.py
├── invoices/               # optional — PDFs named <invoice_number>.pdf
│   ├── 1085288.pdf
│   └── ...
└── ref/
    └── Driftskonto sep 2023 - mar 2026.csv
```

## Security note

`config.ini` contains the Odoo API key — keep it out of version control.
Add `config.ini` to `.gitignore` if this repository is shared.
