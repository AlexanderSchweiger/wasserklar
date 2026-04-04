# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Wassergenossenschaft Verwaltung — a Flask web app for Austrian water cooperative management. All UI text, flash messages, and documentation are in **German**.

Stack: Flask 3.1, SQLAlchemy, Flask-Login, Flask-Mail, Flask-Migrate, WeasyPrint (PDF), pandas (CSV/Excel import), AdminLTE 3 (Bootstrap 4), HTMX 2.0.4, SQLite.

## Common Commands

```bash
# Local dev setup (Windows, uses requirements-dev.txt which excludes WeasyPrint)
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env

# Initialize database (creates tables + seeds 7 default accounting accounts)
flask --app run init-db

# Create admin user (interactive prompts)
flask --app run create-admin

# Run dev server (http://127.0.0.1:5000)
flask --app run run

# Docker (production on Synology NAS)
docker compose up -d --build
docker compose exec wg flask --app run init-db
docker compose exec wg flask --app run create-admin
```

**No tests, linting, or formatter are configured.** No `migrations/` directory exists — schema is managed via `db.create_all()` in `init-db`.

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

### Blueprints (9 modules)

| Blueprint | Prefix | Purpose |
|-----------|--------|---------|
| `auth` | `/auth` | Login/logout, user CRUD (admin only) |
| `customers` | `/customers` | Customer CRUD, soft-delete (active flag) |
| `meters` | `/meters` | Meter CRUD, yearly readings, CSV/Excel import with column mapping |
| `invoices` | `/invoices` | Invoice generation/edit/PDF/email, tariff CRUD under `/invoices/tariffs` |
| `accounting` | `/accounting` | Accounts, bookings, open items, annual report, CSV export |
| `properties` | `/properties` | Property (Objekt/Liegenschaft) CRUD, ownership history |
| `projects` | `/projects` | Project tracking with associated bookings and open items |
| `import_csv` | `/import-csv` | Bulk CSV/Excel import wizard (upload → column mapping → execute) for customers, properties, meters, readings |
| `main` | `/` | Dashboard (open invoices, missing readings, income/expense summary) |

Each blueprint follows the pattern: `app/<name>/__init__.py` (registers blueprint) + `app/<name>/routes.py` (all routes).

### Data Model (`app/models.py`)

- **User** — auth with role ("admin"/"user"), active flag
- **Customer** → has many **PropertyOwnership** records; also has `base_fee_override` / `additional_fee_override`
- **Property** (Objekt/Liegenschaft) → has many **WaterMeter** and **PropertyOwnership**; also has fee overrides
- **PropertyOwnership** — time-bounded Customer↔Property link (`valid_from` / `valid_to`); `valid_to=None` means currently active
- **WaterMeter** → has many **MeterReading** (unique per meter+year, consumption = current − previous reading)
- **WaterTariff** — base_fee + additional_fee + price_per_m3, valid for year range; fee overrides on Customer/Property take priority
- **Invoice** → linked to Customer + optional Property; has many **InvoiceItem**; statuses: Entwurf → Versendet → Bezahlt → Storniert / Guthaben; invoice_number format "RE-YYYY-NNNN"
- **Account** (Einnahme/Ausgabe) → has many **Booking**
- **RealAccount** — real bank account (Girokonto etc.) with IBAN and opening balance; optionally linked to Bookings
- **Booking** — links Account + optional Invoice/OpenItem/Project/Customer/RealAccount; `storno_of_id` enables cancellation chain
- **OpenItem** — manually tracked receivable/payable; statuses: Offen → Teilbezahlt → Bezahlt / Gutschrift; settled via Bookings
- **Project** — named cost/revenue center; bookings and open items can be assigned to a project

Setting invoice status to "Bezahlt" auto-creates a Booking in the first active income account.

### HTMX Pattern

Many routes check `request.headers.get("HX-Request")` and return partial HTML fragments (`_table.html`, `_row.html`, `_status_badge.html`) instead of full pages. This enables dynamic search/filter without full reloads.

### Forms

All form handling uses raw `request.form` — no WTForms form classes, though Flask-WTF/CSRFProtect is active for CSRF tokens.

## Key Constraints

- **WeasyPrint** (PDF generation, email with PDF) requires GTK3 and only works inside the Docker container. `requirements-dev.txt` excludes it. Routes handle `ImportError` gracefully.
- **Templates** use AdminLTE 3 layout (`templates/base.html`) with all assets loaded from CDNs.
- **SQLite** database lives at `instance/wg.db`; generated PDFs go to `instance/pdfs/`.
- **Cooperative identity** (name, address, IBAN, etc.) is configured via env vars (`WG_NAME`, `WG_ADDRESS`, etc.) and appears on invoices.
