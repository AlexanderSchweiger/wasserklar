# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Wassergenossenschaft Verwaltung — a Flask web app for Austrian water cooperative management. All UI text, flash messages, and documentation are in **German**.

Stack: Flask 3.1, SQLAlchemy, Flask-Login, Flask-Mail, Flask-Migrate, WeasyPrint (PDF), pandas (CSV/Excel import), **Tabler 1.0.0 (Bootstrap 5)**, TomSelect 2.3.1, HTMX 2.0.4, SQLite.

## Common Commands

```bash
# Local dev setup (Windows, uses requirements-dev.txt which excludes WeasyPrint)
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env

# Initialize database (creates tables + seeds 4 default tax rates)
flask --app run init-db

# Create admin user (interactive prompts)
flask --app run create-admin

# Migrate existing database after model changes (production updates)
flask --app run upgrade-db

# Run dev server (http://127.0.0.1:5000)
flask --app run run

# Docker (production, z.B. Hetzner-Server)
docker compose up -d --build
docker compose exec wg flask --app run init-db
docker compose exec wg flask --app run create-admin
```

**No tests, linting, or formatter are configured.**

## Schema-Änderungen (neue Spalten)

Neue Datenbankspalten werden **ausschließlich** über den `upgrade-db`-Befehl in [`cli.py`](cli.py) verwaltet. Bei jeder Modelländerung, die eine neue Spalte hinzufügt:

1. Eintrag in `_add_col_if_missing(...)` **sowohl** im `init-db`- als auch im `upgrade-db`-Block ergänzen.
2. Beide Blöcke müssen identisch sein — `upgrade-db` ist der primäre Ort, `init-db` übernimmt dieselben Einträge damit Neuinstallationen ebenfalls funktionieren.
3. Die Funktion ist idempotent (bereits vorhandene Spalten werden übersprungen) — kein manuelles SQL nötig.

```python
_add_col_if_missing("tabelle", "spalte TYP DEFAULT wert", "spalte")
```

## Architecture

**App factory** in `app/__init__.py` (`create_app`). Entry point is `run.py` (`run:app` for gunicorn). Config loaded from `.env` via `config.py` (DevelopmentConfig / ProductionConfig selected by `FLASK_ENV`).

**Extensions** (`app/extensions.py`): `db`, `login_manager`, `mail`, `migrate`, `csrf` — instantiated once, initialized in factory.

### Blueprints (10 modules)

| Blueprint | Prefix | Purpose |
|-----------|--------|---------|
| `auth` | `/auth` | Login/logout, user CRUD (admin only) |
| `customers` | `/customers` | Customer CRUD, soft-delete (active flag) |
| `meters` | `/meters` | Meter CRUD, yearly readings, CSV/Excel import with column mapping |
| `invoices` | `/invoices` | Invoice generation/edit/PDF/email, tariff CRUD under `/invoices/tariffs` |
| `accounting` | `/accounting` | Accounts, bookings, open items, fiscal years, annual report, CSV export |
| `properties` | `/properties` | Property (Objekt/Liegenschaft) CRUD, ownership history |
| `projects` | `/projects` | Project tracking with associated bookings and open items |
| `import_csv` | `/import-csv` | Bulk CSV/Excel import wizard for customers, properties, meters, readings |
| `settings` | `/settings` | WG contact info and mail config (DB key-value store via `AppSetting`) |
| `main` | `/` | Dashboard (open invoices, missing readings, income/expense summary) |

Each blueprint: `app/<name>/__init__.py` (registers blueprint) + `app/<name>/routes.py` (all routes).

### Data Model (`app/models.py`)

- **User** — auth with role ("admin"/"user"), active flag
- **Customer** → has many **PropertyOwnership**; `base_fee_override` / `additional_fee_override` take priority over tariff
- **Property** (Objekt/Liegenschaft) → has many **WaterMeter** and **PropertyOwnership**; also has fee overrides
- **PropertyOwnership** — time-bounded Customer↔Property link (`valid_from` / `valid_to`); `valid_to=None` = currently active
- **WaterMeter** → has many **MeterReading** (unique per meter+year); tracks `installed_from/to`, `initial_value`, `eichjahr`
- **WaterTariff** — base_fee + additional_fee + price_per_m3, valid for year range; fee overrides on Customer/Property take priority
- **Invoice** → linked to Customer + optional Property; has many **InvoiceItem**; statuses: Entwurf → Versendet → Bezahlt → Storniert / Guthaben; invoice_number format `YYYY-NNNNN` (via `InvoiceCounter`)
- **InvoiceCounter** — per-year sequence counter for invoice numbers; auto-seeded from existing invoices if missing
- **Account** (Einnahme/Ausgabe-Konto) → has many **Booking**; optional 3-char `code`
- **RealAccount** — real bank account (IBAN, opening balance, Font Awesome `icon`); `is_default` marks the pre-selected account
- **RealAccountYearBalance** — snapshot of a RealAccount balance at fiscal year close
- **Booking** — links Account + optional Invoice/OpenItem/Project/Customer/RealAccount; `amount` positive = Einnahme, negative = Ausgabe; `storno_of_id` enables cancellation chain; statuses: Offen → Verbucht (on fiscal year close) / Storniert
- **Transfer** — direct bank-to-bank transfer between two RealAccounts; not counted in annual report
- **OpenItem** — manually tracked receivable/payable; statuses: Offen → Teilbezahlt → Bezahlt / Gutschrift; settled via Bookings
- **Project** — named cost/revenue center with optional 3-char `code` and `color`; bookings and open items can be assigned
- **TaxRate** — available tax rates (0 %, 10 %, 13 %, 20 % seeded by `init-db`); used on Booking and InvoiceItem
- **FiscalYear** — year with start/end dates; closing locks bookings (Offen → Verbucht) and snapshots RealAccount balances
- **AppSetting** — generic key-value store (`AppSetting.get(key)` / `AppSetting.set(key, value)`); keys `wg.*` for cooperative contact info, `mail.*` for SMTP config

Setting invoice status to "Bezahlt" auto-creates a Booking in the first active income account.

### Settings & WG Context

`app/settings_service.py` provides DB-overrides-env for cooperative identity and mail config:
- `wg_settings()` is injected into **every template** as `{{ wg.name }}`, `{{ wg.iban }}`, `{{ wg.email }}`, etc.
- `apply_mail_settings()` runs at app start and after settings changes to update Flask-Mail state
- Mail password is encrypted at rest (Fernet/AES, key derived from `SECRET_KEY`)

### HTMX Pattern

Many routes check `request.headers.get("HX-Request")` and return partial HTML fragments (`_table.html`, `_row.html`, `_status_badge.html`) instead of full pages. This enables dynamic search/filter without full reloads.

### Forms

All form handling uses raw `request.form` — no WTForms form classes, though Flask-WTF/CSRFProtect is active for CSRF tokens.

### Jinja2 Filters & Conventions

- `{{ value | de_number }}` — German number format (e.g. `1.250,90`); optional `decimals` and `signed` params
- `{{ wg.name }}` etc. — always available via context processor
- Enhanced `<select>` elements need `class="form-select tom-select"` (or `form-control tom-select`) to activate TomSelect
- **UI size convention**: filter bars use `form-control-sm` / `form-select-sm` / `btn-sm`; card-header action buttons use `btn-sm`; main form submit buttons and inputs use the default (non-sm) size

## Key Constraints

- **WeasyPrint** (PDF generation, email with PDF) requires GTK3 and only works inside the Docker container. `requirements-dev.txt` excludes it. Routes handle `ImportError` gracefully.
- **Templates** use Tabler 1.0.0 layout (`templates/base.html`) with all assets loaded from CDNs (Font Awesome 5, Tabler, TomSelect).
- **SQLite** database lives at `instance/wg.db`; generated PDFs go to `instance/pdfs/`.
- **Cooperative identity** (name, address, IBAN, etc.) is configured via `AppSetting` (DB) with `.env` fallback (`WG_NAME`, `WG_ADDRESS`, etc.) and appears on invoices and in all templates.
