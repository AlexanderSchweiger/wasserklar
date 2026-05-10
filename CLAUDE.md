# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Wassergenossenschaft Verwaltung — a Flask web app for Austrian water cooperative management. All UI text, flash messages, and documentation are in **German**.

Stack: Flask 3.1, SQLAlchemy 2.x, Flask-Login, Flask-Mail, Flask-Migrate, WeasyPrint (PDF), pandas (CSV/Excel import), **Tabler 1.0.0 (Bootstrap 5)**, TomSelect 2.3.1, HTMX 2.0.4. DB ist dialekt-portabel (SQLite / MySQL-MariaDB / Postgres) — siehe "Datenbank" unten.

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

## Tests

Test-Stack: **pytest 9 + pytest-flask 1.3**, Konfig in [pytest.ini](pytest.ini). Struktur:

- `tests/conftest.py` — `app` (session-scoped, `create_app("testing")`), `client` (function-scoped), `clean_db` (autouse, leert alle Tabellen NACH jedem Test).
- `tests/integration/conftest.py` — gemeinsame Fixtures fuer Integration-Tests (`user`, `customer`, `account`, `real_account`).
- `tests/unit/` — pure Funktionen ohne DB.
- `tests/integration/` — DB-beruehrend (Models, Services).
- `tests/http/` — Flask-test_client mit Login-Helper `_login(client, username, password)`.

`TestingConfig` in [config.py](config.py) setzt `SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"` + `WTF_CSRF_ENABLED = False`. Schema kommt aus `db.create_all()` (kein Alembic im Test-Loop), die SQLite-In-Memory-DB lebt nur fuer die Session.

```bash
# Alle Tests
.venv/Scripts/python -m pytest

# Nur eine Datei oder Klasse oder Test
.venv/Scripts/python -m pytest tests/http/test_meters_import_wizard.py
.venv/Scripts/python -m pytest tests/integration/test_meters_import_service.py::TestCommitImport
.venv/Scripts/python -m pytest -k "import and not preview" -v
```

**Stolperer**:

- **Werkzeug-3.x test_client teilt den CookieJar zwischen Instanzen.** Wenn ein vorheriger Test einen User eingeloggt hat (Cookie mit `_user_id`), bleibt der Login-State im naechsten test_client erhalten -- selbst wenn der `client`-Fixture function-scoped ist. Folge: `@login_required`-Routen geben 200 statt 302 zurueck. **Fix**: in Login-Required-Tests am Anfang `client.get("/auth/logout")` aufrufen. Beispiel: [test_meters_import_wizard.py::TestLoginRequired](tests/http/test_meters_import_wizard.py).
- **`Property.object_type` ist `nullable=False`** (Werte `'Haus'` / `'Garten'` / `'Sonstiges'`). Beim Anlegen von Property-Fixtures **immer** explizit setzen, sonst `IntegrityError: NOT NULL constraint failed: properties.object_type`.
- **`db.session.commit()` in Service-Funktionen** (z.B. `import_service.commit_import`) macht ein hartes Commit. Tests, die mit echten DB-Effekten arbeiten, koennen das nicht via Outer-Savepoint zurueckrollen -- sie *muessen* gegen die SQLite-In-Memory-Test-DB laufen, niemals gegen die Dev-DB. Der `clean_db`-Autouse-Fixture leert die Tabellen nach jedem Test, daher ist das in der Test-Suite sicher.

## Schema-Änderungen (Alembic)

Schema-Migrationen werden ueber **Alembic / Flask-Migrate** verwaltet. Die Migrations-History liegt in [`migrations/versions/`](migrations/versions/), `init-db` und `upgrade-db` rufen intern `flask db upgrade` auf.

Neue Spalte hinzufuegen:

```bash
# 1. Model in app/models.py aendern
# 2. Migration generieren (IMMER gegen leeres Postgres oder leeres SQLite —
#    nicht gegen die laufende Dev-DB, sonst greift der Diff nur Teilstuecke):
DATABASE_URL=sqlite:///temp_migration.db \
  flask --app run db migrate -m "[oss-v1.X.0] add column foo to bar"
# 3. Generierte Datei in migrations/versions/ pruefen — Alembic-Autogenerate
#    ist nicht perfekt, manchmal muessen Defaults / FK-Reihenfolge nachgezogen
#    werden.
# 4. Im Dev-Setup nachziehen: flask --app run upgrade-db
# 5. Temp-DB loeschen
```

**Dialect-Stolperer**: Erste Migration immer auf Postgres oder leerem SQLite generieren — niemals aus einer existierenden MariaDB-DB. Autogenerate erkennt dort einige Postgres-spezifische Typen (z.B. `Numeric(10,2)`) als Diff, weil MariaDB sie anders rendert. `render_as_batch` ist in `migrations/env.py` automatisch aktiv fuer SQLite (Pflicht fuer ALTER COLUMN).

**Multi-Tenant**: SaaS-Schicht setzt vor dem Subprocess `ALEMBIC_TENANT_SCHEMA=tenant_xxx` — `env.py` legt dann die `alembic_version`-Tabelle im Tenant-Schema ab statt in `public`. Pro Tenant eine Version-Zeile, partielle Rollouts moeglich.

Der alte `_SCHEMA_UPGRADE_COLUMNS`-Mechanismus in [cli.py](cli.py) ist deprecated — Funktion bleibt importable fuer eventuelle Bestandskunden-Migrations, wird aber nicht mehr aufgerufen. **Nicht mehr erweitern.**

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
- **Property** (Objekt/Liegenschaft) → has many **WaterMeter** and **PropertyOwnership**; also has fee overrides; `object_type` ist `NOT NULL` (Werte `'Haus'` / `'Garten'` / `'Sonstiges'`)
- **PropertyOwnership** — time-bounded Customer↔Property link (`valid_from` / `valid_to`); `valid_to=None` = currently active. **Mehrere parallele aktive Ownerships pro Property sind erlaubt** (Ehepaare, Erbengemeinschaften) — Code, der "den" aktuellen Eigentuemer abfragt, muss `.all()` oder `.first()` nehmen, nicht `.scalar()` (sonst `MultipleResultsFound`)
- **WaterMeter** → has many **MeterReading** (unique per meter+year); tracks `installed_from/to`, `initial_value`, `eichjahr`. **`meter_type`** (`'main'` / `'sub'`, default `'main'`, NOT NULL) klassifiziert Hauptz. vs. Subz.; **`parent_meter_id`** (FK self-ref, ondelete SET NULL) verlinkt Subz. auf Hauptz. (max. 1 Ebene, parent muss `meter_type='main'` sein — Validation in der Route, kein DB-Constraint, weil dialekt-portabel). Self-Reference und Nicht-Hauptz.-Parent werden in `meter_new`/`meter_edit` serverseitig gekappt + Flash-Warnung
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

### Ablesungen-Import-Wizard

`/meters/import` ist ein **3-stufiger Wizard** (Bug-Fix-Begruendung: ein einzelner POST-Handler, der erst Upload und dann Mapping verarbeitet, scheitert daran, dass das Mapping-Form keine Datei mehr mitsendet — verifizierter Bug, jetzt sauber getrennt):

| Endpoint | Zweck |
|---|---|
| `GET/POST /meters/import` | Upload + Mapping-Modus + Duplikat-Strategie. POST speichert das DataFrame als Pickle in `instance/meter_import_<uuid>.pkl`, Pfad in `session["meter_import_file"]`, Config in `session["meter_import_cfg"]`, redirect zu `/preview`. |
| `GET/POST /meters/import/preview` | Vorschau-Editor (editierbare Tabelle, Status-Highlighting). POST mit `action=refresh` baut die Vorschau mit der neu im Form gewaehlten Mapping-Config neu auf. POST mit `action=confirm` ruft `commit_import` und redirected zu `/result`. **Beide Actions lesen die Mapping-Config aus dem Form** — nicht nur aus der Session — sonst gingen Spalten-Selektionen beim direkten Confirm-Klick verloren. |
| `GET /meters/import/result` | Stats (`stats` aus Session). |

Heavy Lifting in [`app/meters/import_service.py`](app/meters/import_service.py): drei Mapping-Modi (`meter_number` / `customer_number` / `customer_name`), Auto-Detection von Zahlen- (`at_de`/`us`/`plain`) und Datumsformaten (`iso`/`de`/`us`/`excel_ts`) mit User-Override, `resolve_meter` mit Hauptzaehler-Bevorzugung bei Mehrdeutigkeit (Customer hat 1 Hauptz. + n Subz. → Hauptz. vorausgewaehlt). `parse_form_edits` parst die `rows[N][feld]`-Form-Keys (Werkzeug-Convention) zurueck in eine Liste und mergt User-Edits auf die frisch resolveten Zeilen.

Der zweite Import-Wizard im Repo, `app/import_csv/` (Stammdaten — Kunden/Objekte/Zaehler), folgt dem gleichen Pickle-Pattern und ist die Vorlage gewesen.

### Jinja2 Filters & Conventions

- `{{ value | de_number }}` — German number format (e.g. `1.250,90`); optional `decimals` and `signed` params
- `{{ wg.name }}` etc. — always available via context processor
- Enhanced `<select>` elements need `class="form-select tom-select"` (or `form-control tom-select`) to activate TomSelect
- **UI size convention**: filter bars use `form-control-sm` / `form-select-sm` / `btn-sm`; card-header action buttons use `btn-sm`; main form submit buttons and inputs use the default (non-sm) size

## Datenbank

**Default ist SQLite** (`sqlite:///instance/wg.db`, siehe [config.py](config.py)) — wer ohne `.env`-Override startet, bekommt eine lokale Datei-DB. Die App ist aber **dialekt-portabel** und laeuft auch auf **MySQL/MariaDB** und **Postgres**: einfach `DATABASE_URL` in der `.env` setzen, z.B.:

```env
DATABASE_URL=mysql+pymysql://user:pass@host:3307/dbname?charset=utf8mb4
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

Lokal laeuft typischerweise **MariaDB** (Netzwerk-Host) — entsprechend muss neuer Query-/Migrations-Code auf allen drei Dialekten kompilieren.

**Portabilitaets-Stolperer**, die in der Vergangenheit zugeschlagen haben:

- **`NULLS LAST` / `NULLS FIRST`**: ANSI-SQL, von Postgres/SQLite (≥ 3.30) unterstuetzt, **nicht** von MySQL/MariaDB. SQLAlchemy's `col.asc().nulls_last()` rendert direkt zu `NULLS LAST` und kracht auf MySQL. Portable Loesung: ein CASE-Praefix, z.B. in [app/customers/routes.py](app/customers/routes.py:`_apply_customer_sort`):
  ```python
  sa_case((col.is_(None), 1), else_=0).asc(),  # NULLs ans Ende
  col.asc(),                                    # eigentlicher Sort
  ```
- **`ilike`**: Postgres-spezifisch. SQLAlchemy mappt das auf MySQL implizit zu `LIKE` (das dort by default case-insensitive ist) und auf SQLite zu `LIKE` mit case-insensitive collation — funktioniert in allen drei, aber nicht aus demselben Grund. OK so lange man ASCII-Strings vergleicht.
- **Boolean-Spalten**: SQLite hat keinen nativen Bool-Typ (Integer 0/1), MySQL hat `TINYINT(1)`, Postgres hat `BOOLEAN`. SQLAlchemy abstrahiert das — `Column.is_(True)` ist robust, `== 1`/`== True` funktioniert je nach Dialekt unterschiedlich.
- **`upgrade-db` / `_add_col_if_missing`** in [cli.py](cli.py) geht ueber `PRAGMA table_info` (SQLite-only). Auf MySQL/Postgres muss eine andere Spaltenpruefung her — wer dort eine Migration nachzieht, sollte `inspect(db.engine).get_columns(...)` aus `sqlalchemy` nutzen statt PRAGMA.

`instance/wg.db` und `instance/pdfs/` sind weiterhin die SQLite-Default-Pfade; bei MySQL/Postgres ist nur `instance/pdfs/` relevant.

## Key Constraints

- **WeasyPrint** (PDF generation, email with PDF) requires GTK3 and only works inside the Docker container. `requirements-dev.txt` excludes it. Routes handle `ImportError` gracefully.
- **Templates** use Tabler 1.0.0 layout (`templates/base.html`) with all assets loaded from CDNs (Font Awesome 5, Tabler, TomSelect).
- **Cooperative identity** (name, address, IBAN, etc.) is configured via `AppSetting` (DB) with `.env` fallback (`WG_NAME`, `WG_ADDRESS`, etc.) and appears on invoices and in all templates.
