# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Wassergenossenschaft Verwaltung — a Flask web app for water cooperatives and water utilities in **Austria and Germany** (Länderprofile, siehe unten). All UI text, flash messages, and documentation are in **German**.

Stack: Flask 3.1, SQLAlchemy 2.x, Flask-Login, Flask-Mail, Flask-Migrate (Alembic), WeasyPrint + pypdf (PDF), pandas/openpyxl (CSV/Excel-Import), mt-940 + lxml (Bankauszug-Import CAMT/MT940), pyshp + pyproj (Shapefile-/WLK-Import im Leitungsnetz-Modul), python-docx (Brief-/Export), **Tabler 1.0.0 (Bootstrap 5)**, TomSelect 2.3.1, HTMX 2.0.4, Leaflet (Leitungsnetz-Karte). DB ist dialekt-portabel (SQLite / MySQL-MariaDB / Postgres) — siehe "Datenbank" unten.

Die App ist deutlich ueber die reine Verwaltung hinausgewachsen: granulares **Rollen-/Rechte-System** (10 Bereiche, nicht mehr nur admin/user), **Mandant-Typ-Schalter** (Wassergenossenschaft/Versorger, `is_wg`-Gating), **Abrechnungsperioden** (`BillingPeriod`) statt Kalenderjahr-Verdrahtung, historisierte **Rechnungslaeufe** (`BillingRun`), **Mahnwesen** (`dunning`), **Bankauszug-Import** mit Zuordnungsvorschlaegen, **In-App-Benachrichtigungen**, **E-Mail-Event-Tracking** (Postmark) + **Sperrliste** (`EmailSuppression`), ein **Leitungsnetz-Modul** (Wasserleitungsplan auf Leaflet-Karte, frueher „Technik"), ein **Störungsjournal** (`incidents`) und eine **Schriftführung** (Sitzungen/Protokolle/Beschlüsse/Schriftverkehr, nur im WG-Modus). Details in den jeweiligen Abschnitten unten.

## Common Commands

```bash
# Local dev setup (Windows, uses requirements-dev.txt which excludes WeasyPrint)
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env

# Initialize database (Alembic-Upgrade + Seeds: Steuersaetze des Landes, Gebuehrenarten,
# Default-Mahnstufen, Default-Rollen (Admin + abgeleitete), eine aktive Abrechnungsperiode).
# Land des Mandanten: --country AT|DE (sonst DEFAULT_COUNTRY aus .env, sonst AT)
flask --app run init-db [--country DE]

# Create admin user (interactive prompts) — legt einen User mit der Admin-Rolle an
flask --app run create-admin

# Migrate existing database after model changes (production updates)
flask --app run upgrade-db

# Mail-Verschluesselungs-Key rotieren / DB-SMTP-Passwoerter zuruecksetzen
flask --app run rotate-mail-key        # re-encrypt mit neuem WASSERKLAR_MAIL_KEY (MultiFernet)
flask --app run reset-mail-passwords   # gespeicherte SMTP-Passwoerter leeren (Recovery)

# Datenexport/-import (Voll-Backup eines Mandanten als ZIP, siehe data_transfer)
flask --app run export-data --out backup.zip
flask --app run import-data --in backup.zip --mode merge

# Offene Vortages-Buchungen auf "Verbucht" setzen (Scheduler-Catch-up, taeglich 00:05)
flask --app run mark-posted

# Run dev server — Port 5002 (FLASK_RUN_PORT in .env), Docker belegt 5000
python run.py   # → http://127.0.0.1:5002

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

### Blueprints (17 modules)

| Blueprint | Prefix | Permission | Purpose |
|-----------|--------|------------|---------|
| `auth` | `/auth` | (login) | Login/logout, User-/Rollen-CRUD (siehe Rechte-System) |
| `customers` | `/customers` | `stammdaten` | Customer CRUD, soft-delete (active flag), Kundenauswertung |
| `properties` | `/properties` | `stammdaten` | Property (Objekt/Liegenschaft) CRUD, ownership history |
| `periods` | `/perioden` | `stammdaten` | **Abrechnungsperioden** (`BillingPeriod`) — eine ist immer aktiv |
| `meters` | `/meters` | `zaehler` | Zähler-CRUD, Ablesungen, **Zählertausch**, CSV/Excel-Import-Wizard |
| `invoices` | `/invoices` | `rechnungen_op` | Einzel- + **Massen-Rechnungslauf** (`BillingRun`), Edit/PDF/E-Mail, Tarife unter `/invoices/tariffs` |
| `dunning` | `/dunning` | `mahnwesen` | **Mahnwesen**: Mahnstufen, Mahnlauf, Mahnungen, Vorlagen |
| `accounting` | `/accounting` | `buchhaltung` | Konten, Buchungen, Umbuchungen, Bank & Kassa (`RealAccount`), Offene Posten, Buchungsjahre, EÜR/Jahresbericht, USt, **Belege** (Belegeingang + Belege an Buchungen, siehe „Belegablage“) |
| `projects` | `/projekte` | `buchhaltung` | Projekt-Kostenstellen mit zugeordneten Buchungen + Offenen Posten |
| `bank_import` | `/bank-import` | `buchhaltung` | **Bankauszug-Import** (CAMT/MT940) mit Zuordnungsvorschlaegen |
| `network` | `/network` | `network` | **Wasserleitungsplan** (Leaflet-Karte): mehrere benannte Pläne (`NetworkPlan`, Kopie→Merge), Anlagen/Features, Wartung/Prüfung, Elementliste, WLK-Shapefile-Import. (Blueprint/Permission `network`, UI-Label „Leitungsnetz", frueher `technik`.) |
| `incidents` | `/incidents` | `incidents` | **Störungs-/Rohrbruch-Journal**: Ereignisjournal mit Kartenpin (Leaflet, Point-only, GeoJSON-in-Text), Ursachenkategorie/Status/Schweregrad, Reparaturkosten/Wasserverlust/betroffene Anschlüsse, Fotos, CSV-Export + PDF-Jahresbericht. Foto-Ablage als Geschwister von `PDF_DIR` (`instance/incidents/`), nicht im data_transfer-ZIP (separates FS-Backup noetig). |
| `schriftfuehrung` | `/schriftfuehrung` | `schriftfuehrung` | **Schriftführung** (nur WG-Modus, `is_wassergenossenschaft`-Guard): Vorstandssitzungen + Hauptversammlungen (`Meeting`), Einladungsversand mit Anwesenheits-Tracking, Beschlüsse, Protokolle, Schriftverkehr-Archiv. Dokumente als Geschwister von `PDF_DIR`. |
| `import_csv` | `/import` | `stammdaten` | Stammdaten-Import-Wizard (Kunden/Objekte/Zähler) |
| `data_transfer` | `/data-transfer` | `verwaltung` | Voll-Export/-Import eines Mandanten (ZIP), registry-getrieben |
| `settings` | `/einstellungen` | `verwaltung` | WG-Kontakt + Mail-Config (DB-KV-Store via `AppSetting`) |
| `main` | `/` | (login) | Dashboard (offene Rechnungen, fehlende Ablesungen, Einnahmen/Ausgaben) |

Each blueprint: `app/<name>/__init__.py` (registers blueprint) + `app/<name>/routes.py` (all routes). Blueprints, deren komplette Routen-Menge unter genau einem Recht steht, registrieren `bp.before_request(require_blueprint_permission(PERM_X))` (siehe `app/network/__init__.py`); feiner granulierte Routen nutzen den `@permission_required(PERM_X)`-Decorator pro Route.

### Data Model (`app/models.py`)

Das Modell ist deutlich gewachsen (~58 Tabellen). Die Kerngruppen:

**Auth & Rechte:**
- **Role** + **RolePermission** — Rollen mit zugeordneten Rechten. Die Rolle `Admin` hat **implizit alle Rechte** (auch spaeter neu hinzukommende). Rechte sind feste Code-Konstanten in [app/auth/permissions.py](app/auth/permissions.py), keine eigene Tabelle. Siehe "Rechte-System" unten.
- **User** — `role_id` → **Role** (ersetzt das alte `role`-String-Feld), `active`-Flag; `User.has_permission(key)` ist der zentrale Check.
- **UserPreference** — per-User-Einstellungen (z.B. Default-Konto, Tabellen-Spalten).

**Stammdaten:**
- **Customer** → has many **PropertyOwnership**; individuelle Gebühren (`ChargeOverride`, je Gebührenart: eigener Betrag oder „entfällt") schlagen den Tarif; `wants_email` gatet jeglichen Kunden-Mailversand (siehe SaaS `invoice_optin`). **Name-Aufspaltung (v1.21.0)**: `name` bleibt das kombinierte **Sortier-/Listen-/Suchfeld** (Konvention „Nachname Vorname") und wird beim Speichern aus `last_name` + `first_name` abgeleitet; daneben gibt es `salutation` (Herr/Frau/Familie/leer) und `is_company`. Für Brief-/Rechnungsausgabe IMMER die berechneten Properties nutzen: `customer.letter_name` (Anschrift: „Vorname Nachname", „Familie X", Firmenname) und `customer.salutation_line` (Anrede). **Listen, Dropdowns, Suche und `order_by` weiterhin über `name`** — niemals nach `last_name` sortieren (bricht Firmen ohne Nachname + Altbestand, wo `letter_name`/`salutation_line` auf `name` zurückfallen). Quick-Create und Altimporte setzen nur `name`; der CLI-Befehl `flask split-customer-names` füllt `first_name`/`last_name`/`is_company` heuristisch vor.
- **Property** (Objekt/Liegenschaft) → has many **WaterMeter** and **PropertyOwnership**; auch individuelle Gebühren (`ChargeOverride`); `lat`/`lng`/`geocoded_at` (Lage — geokodiert oder auf der Detailseite manuell gesetzt); `object_type` ist `NOT NULL` (Werte `'Haus'` / `'Garten'` / `'Sonstiges'`)
- **PropertyOwnership** — time-bounded Customer↔Property link (`valid_from` / `valid_to`); `valid_to=None` = currently active. **Mehrere parallele aktive Ownerships pro Property sind erlaubt** (Ehepaare, Erbengemeinschaften) — Code, der "den" aktuellen Eigentuemer abfragt, muss `.all()` oder `.first()` nehmen, nicht `.scalar()` (sonst `MultipleResultsFound`)
- **WaterMeter** → has many **MeterReading** (unique per meter+year); tracks `installed_from/to`, `initial_value`, `eichjahr`. **`meter_type`** (`'main'` / `'sub'`, default `'main'`, NOT NULL) klassifiziert Hauptz. vs. Subz.; **`parent_meter_id`** (FK self-ref, ondelete SET NULL) verlinkt Subz. auf Hauptz. (max. 1 Ebene, parent muss `meter_type='main'` sein — Validation in der Route, kein DB-Constraint, weil dialekt-portabel). Self-Reference und Nicht-Hauptz.-Parent werden in `meter_new`/`meter_edit` serverseitig gekappt + Flash-Warnung
- **MeterReplacement** — explizites **Zählertausch-Event** (alt→neu-Paarung + Snapshot der Tausch-Metadaten); ersetzt die fruehere Datums-Heuristik (alter Zähler `active=False`, `installed_to == neuer.installed_from`), die bei zwei am selben Tag getauschten Zählern nicht aufloesbar war. `property_id` redundant gehalten → Per-Objekt-Abfragen ohne Join.
- **MeterReadingAccessCode** (erbt `EmailTrackableMixin`) — Login-Code fuers SaaS-Selbstablesungs-Portal (`/zaehlerstand`); im OSS definiert, von der SaaS-`self_service`-Schicht genutzt.
- **CustomerCounter** — per-year sequence counter for Kundennummern (analog `InvoiceCounter`).
- **CustomerWgProfile** / **PropertyWgProfile** / **WgFunction** — WG-spezifische 1:1-Profiltabellen (Mitglieds-Status + `member_until`; Anteile + m²; mehrwertige Vorstands-/Prüf-Funktionen), nur im Mandant-Typ Wassergenossenschaft relevant. Der Schalter ist die `AppSetting` `org.type` (`cooperative`/`utility`); der Context-Processor injiziert `is_wg` in alle Templates. Domäne/Regeln in [app/wg.py](app/wg.py).

**Abrechnung (Perioden statt Kalenderjahr):**
- **BillingPeriod** — **zentraler Gruppierungsschluessel** fuer Ablesungen, Zählertausche und Rechnungslaeufe; ersetzt die fruehere Kalenderjahr-Verdrahtung. `start_date`/`end_date` (z.B. Juni–Juni), `name` (z.B. "2025/26"). **Genau eine ist immer aktiv** — applikationsseitig erzwungen (`activate()` setzt alle anderen inaktiv; `BillingPeriod.current()`), kein portabler Partial-Index ueber alle drei Dialekte.
- **WaterTariff** — Name + Gültigkeit (Jahresbereich) + Liste von **Tarifpositionen** (`TariffComponent`, OSS v1.44.0; ersetzt die festen Spalten `price_per_m3`/`base_fee`/`additional_fee`). Je Position: Gebührenart (`ChargeType`), Rechnungstext, `amount` (NULL = „nur mit individuellem Betrag"), eigener `tax_rate`, Buchungskonto (nullable → siehe "Kontierung" unten) und optional `valid_from` (Stichtag im Abrechnungszeitraum → zeitanteilige Abgrenzung, z. B. bayerischer Wassercent ab 1.7.2026). Jeder Tarif hat genau eine `water`-Position (Verbrauch, Schätzung und Plankostenrechnung hängen daran). Die Rechnungspositionen baut **eine** Engine: [app/invoices/tariff_engine.py](app/invoices/tariff_engine.py) (Rechnungslauf, Tarif-Zeile im Positions-Editor, Schlussrechnung, Schätzkorrektur, Plankostenrechnung) — nie wieder eine eigene Gebühren-Kaskade schreiben.
- **ChargeType** (Gebührenart, `charge_types`) — Katalog: `key` (stabil: `water`, `water_levy` = Wassercent, `base_fee`, `additional_fee`, eigene `custom_*`), `calc_type` (`per_m3` | `flat`), `is_levy` (Abgabe an eine Behörde → in der Plankostenrechnung kein Ertrag), `overridable`, `active`. Systemarten seedet die Migration + `ensure_system_charge_types()` ([app/invoices/charges.py](app/invoices/charges.py)); der Wassercent ist nur in Ländern mit Abgabe (`LEVY_COUNTRIES` = DE) von Haus aus aktiv. **ChargeOverride** (`charge_overrides`) hängt an der Gebührenart (nicht am Tarif → überlebt Tarifwechsel) und an Kunde **oder** Objekt; Priorität **Objekt > Kunde > Tarif**; ein Override ersetzt nur den Betrag (Text/USt/Konto kommen aus dem Tarif), ohne Betrag heißt „entfällt". `InvoiceItem.charge_key` friert die Gebührenart ein — `Invoice.consumption` und die Schätzung erkennen den Verbrauch daran (`charge_key == "water"`, Fallback `unit == "m³"` nur für Altbelege), sonst würde eine zweite m³-Zeile (Wassercent) den Verbrauch doppelt zählen.
- **BillingRun** — **historisierter Massen-Rechnungslauf**: bei jeder Massenabrechnung gespeichert, haelt einen **Snapshot des verwendeten Tarifs** (`tariff_snapshot` = JSON der Positionen; Altläufe behalten ihre `tariff_*`-Spalten, `tariff_price_per_m3` bleibt der Wasserpreis), zaehlt `invoices_created`/`invoices_skipped`, kennt eine `sort_order` fuer die PDF-Reihenfolge. `Invoice.billing_run` verlinkt zurueck.
- **Invoice** (erbt `EmailTrackableMixin`) → linked to Customer + optional Property + optional **BillingRun**; has many **InvoiceItem**; statuses: Entwurf → Versendet → Bezahlt → Storniert / Guthaben; invoice_number format `YYYY-NNNNN` (via `InvoiceCounter`). E-Mail-Versand-Status wird ueber **EmailEvent** getrackt (siehe E-Mail-Tracking).
- **Storno-Rechnung / Gutschrift (v1.42.0):** Beim Stornieren einer versendeten oder bezahlten Rechnung fragt ein Dialog, ob ein Gegenbeleg ausgestellt wird. Der entsteht als **eigene** `Invoice` mit `invoice_kind='credit_note'`, eigener fortlaufender Nummer, gespiegelten (negativen) Positionen und `cancels_invoice_id` → Original (UStG-§11-Pflichtverweis); Original bleibt unveraendert. Logik in [app/invoices/credit_note.py](app/invoices/credit_note.py). Die Gutschrift entsteht als **Entwurf** — der Nutzer prueft und versendet sie selbst. Erst der Wechsel auf `Versendet` legt (wie bei jeder Rechnung) den Offenen Posten an: einen **negativen `OpenItem`** ueber den Rueckzahlbetrag, wenn die Rechnung bezahlt war, sonst gar keinen. Der Betrag wird **abgeleitet statt gespeichert** (`credit_note.refund_amount_for` liest die auf `Storniert` gesetzten Originalbuchungen) — das ist auch bei Teilzahlung korrekt, wo Belegsumme und Rueckzahlbetrag auseinanderfallen. Die Rueckueberweisung wird vom `bank_import` als negative Zeile automatisch diesem Posten zugeordnet (Vorzeichen wird ueberall mitgeprueft; `accounting.services.settlement_status` leitet die Status vorzeichenbewusst ab). Gutschriften sind aus Forderungs-Aggregaten (Dashboard, Perioden-Uebersicht, Mahnlauf) **ausgeschlossen** — die stornierte Originalrechnung ist dort schon ausgeblendet, sonst wuerde derselbe Betrag zweimal abgezogen.
- **InvoiceCounter** — per-year sequence counter for invoice numbers; auto-seeded from existing invoices if missing
- **Geschätzte Zählerstände + Korrekturposten (v1.28.0):** `MeterReading.is_estimated` und `InvoiceItem.is_estimated` markieren Schätzungen (Badge „geschätzt" in Listen/Detail/PDF). Schätz-Logik in [app/meters/estimation.py](app/meters/estimation.py): `estimate_meter_value` (letzter Stand + Ø-Verbrauch; Bulk-Route `/meters/readings/estimate-missing` + Button im Ablese-Formular). Wird ein echter Stand über `save_reading(is_estimated=False)` auf eine **abgerechnete** Schätzung gespeichert, legt `build_correction` einen vorzeichenbehafteten **`ReadingCorrection`** an (`amount`>0 Nachforderung, <0 Gutschrift; `remaining_amount` für Carry-forward). `apply_corrections_to_invoice` zieht offene Posten beim nächsten Rechnungslauf ein (pro Kunde eine Rechnung) — Gutschrift nur bis Rechnungsbetrag 0, Rest wandert weiter; rundungssicher konsistent mit `Invoice.recalculate_total` (`_item_gross`). Übersicht: `/invoices/corrections`. Der Abgleich sitzt zentral in `save_reading` → greift auch bei Import + SaaS-Self-Service.
- **Account** (Einnahme/Ausgabe-Konto) → has many **Booking**; optional 3-char `code`
- **RealAccount** — reales Geldkonto (IBAN, opening balance, Font Awesome `icon`); `is_default` marks the pre-selected account. **`account_type`** (`'bank'` / `'cash'`, default `'bank'`, NOT NULL + `server_default`) unterscheidet Bankkonto von **Bargeldkassa** — bewusst eine Spalte statt eines eigenen Models, damit Buchungen, `Transfer` (Bareinnahme → Bank), Jahresabschluss-Snapshot und Auswertungen unveraendert funktionieren. Der Typ steuert nur Beschriftung, IBAN-Sichtbarkeit, die Negativ-Warnung auf der Kontenliste und den Ausschluss aus dem `bank_import` (fuer Bargeld gibt es keinen Kontoauszug). Helfer: `ra.is_cash` / `ra.type_label`
- **RealAccountYearBalance** — snapshot of a RealAccount balance at fiscal year close
- **Booking** — links Account + optional Invoice/OpenItem/Project/Customer/RealAccount; `amount` positive = Einnahme, negative = Ausgabe; `storno_of_id` enables cancellation chain; statuses: Offen → Verbucht (on fiscal year close) / Storniert
- **Transfer** — direct bank-to-bank transfer between two RealAccounts; not counted in annual report
- **OpenItem** — manually tracked receivable/payable; statuses: Offen → Teilbezahlt → Bezahlt / Gutschrift; settled via Bookings
- **Project** — named cost/revenue center with optional 3-char `code` and `color`; bookings and open items can be assigned
- **TaxRate** — einstellbare Steuersätze (`active`; Standardsätze je Land geseedet: AT 0/10/13/20, DE 0/7/19; Verwaltung unter Einstellungen → Steuern). Code fragt **immer** `tax_service.tax_rates()` (aktive Sätze) bzw. `tax_service.water_tax_rate()` — nie hart codierte Sätze; used on Booking, InvoiceItem and TariffComponent
- **BookingGroup** — fasst zusammengehoerige Buchungen (z.B. aus einem Vorgang) zu einer Gruppe zusammen.
- **Document** / **DocumentLink** / **DocumentEvent** — Belegablage (Dateien an Buchungen), siehe Abschnitt „Belegablage“. Der Modellname kollidiert mit `python-docx` (`from docx import Document` in `invoices/` und `dunning/document_service.py`): nie beide in einem Modul importieren, sonst mit Alias.
- **FiscalYear** — year with start/end dates; `is_vat_liable` markiert USt-pflichtige Jahre (steuert USt-Voranmeldung + den `has_vat_fiscal_year`-Context-Flag); closing locks bookings (Offen → Verbucht) and snapshots RealAccount balances.
- **FiscalYearReopenLog** — Audit-Log fuer das Wieder-Oeffnen eines abgeschlossenen Buchungsjahres.
- **AppSetting** — generic key-value store (`AppSetting.get(key)` / `AppSetting.set(key, value)`); keys `wg.*` for cooperative contact info (inkl. `wg.vat_id` = UID-Nr./USt-IdNr., `wg.tax_number`), `mail.*` for SMTP config, `org.country` (Land, siehe Länderprofile), `tax.water_rate` (Standard-USt Wasser), `map.ortho_wms_*` (optionaler Luftbild-WMS), `invoice.small_business_note` (Kleinunternehmer-Hinweis).

**Mahnwesen (`dunning`):**
- **DunningPolicy** → has many **DunningStage** — Mahn-Regelwerk (Default via `init-db` geseedet): pro Stufe Frist in Tagen, Gebuehr, Vorlagentext.
- **DunningNotice** — einzelne Mahnung zu einer Rechnung; Status Aktiv → Zurückgesetzt / Storniert. Achtung Alembic-FK-Reihenfolge: `dunning_notices` referenziert `users` — siehe Initial-Migrations-Stolperer im Deploy-SETUP.

**Bankauszug-Import (`bank_import`):**
- **BankStatement** → has many **BankStatementLine** — importierter Kontoauszug (CAMT.053 / MT940, via `mt-940` + `lxml`). Zeilen werden gegen offene Rechnungen/Posten gematcht und als Buchung uebernommen.

**In-App-Benachrichtigungen:**
- **AdminNotification** + **AdminNotificationRead** — plattform-/system-seitige Hinweise im Glocken-Badge; `*Read` haelt den Gelesen-Status pro User. (Im SaaS gespeist vom Platform-Notification-Stream.)

**E-Mail-Tracking (`app/email_tracking.py`):**
- **EmailEvent** — Delivery-/Bounce-/Open-Events zu versendeten Mails. `EmailTrackableMixin` (von `Invoice` geerbt) verknuepft ein Modell mit seinen Events; im SaaS schreibt der Postmark-Webhook ueber die Platform diese Events ins Tenant-Schema.
- **InvoiceEmailOptInCode** + **CustomerEmailConsentLog** — Double-Opt-In fuer "Rechnung per E-Mail": Code-basierte Zustimmung + Consent-Audit-Log (DSGVO). Auto-Anlage des Codes passiert im SaaS (`invoice_optin`).
- **EmailSuppression** — pro-Tenant-**Sperrliste** fuer unzustellbare/abgelehnte Adressen (Quellen nach Schwere: manuell, permanenter SMTP-Fehler, Hard-Bounce, Spam-Beschwerde). Jeder Kunden-Mailversand wird vorab gegen diese Liste geprueft; im SaaS speist sie der Platform-Webhook, im OSS-Standalone der synchrone SMTP-Fehler. Eskalations-/Block-Logik in [app/email_suppression.py](app/email_suppression.py).

**Leitungsnetz-Modul (`network`, frueher `technik`, OSS v1.12.0):** Wasserleitungsplan als GeoJSON-Annotationen auf einer Leaflet-Karte (basemap.at bzw. basemap.de je Land), bewusst **kein PostGIS** (dialekt-portabel, Geometrie als Text).
- **NetworkPlan** — benannter Plan-Container; erlaubt mehrere parallele Pläne (operativer Hauptplan + Planungs-Sandkasten). Eine Kopie merkt sich `source_plan_id`, sodass `plan_merge` Änderungen zurueckspiegelt; nur Pläne mit `maintenance_enabled` UND `status='aktiv'` treiben die Dashboard-Erinnerung „Fällige Prüfungen".
- **NetworkFeature** — Punkt (Hydrant, Schieber, Quelle, Behaelter, Verteiler, Pumpe, Hausanschluss, Probenahmestelle) oder Linie (Versorgungs-/Haupt-/Ring-/Hausanschlussleitung); `geometry` haelt das GeoJSON-Geometry-Objekt als Text.
- **MaintenanceLog** — Wartungs-/Pruef-Eintraege zu einem Feature (WLK-Import schreibt hier rein).
- **SpringYield** (OSS v1.32.0) — **Quellschüttung**: Schüttungs-Messreihe (Durchfluss in l/s je `measurement_date`) zu einer `NetworkFeature` vom Typ `quelle`. Geschwister-Pattern zu `MaintenanceLog` (gleicher `feature_id`-FK, gleiche Audit-Spalten, ORM-Cascade). Erfassung/Löschen über das Schüttungs-Modal der Elementliste (`yield_add`/`yield_delete`, Route gatet hart auf `feature_type=='quelle'`, Button nur bei Quellen); die **Monitoring-Seite** `network.monitoring` zeichnet je Quelle des aktiven Plans (`current_plan()`) eine Linie (Chart.js 4.4.1 + date-fns-Adapter via `head_extra` + `hx-boost="false"`) plus Trockenheits-Kennzahlen (aktuell, Min 12 Mon., % vom Median). Kein Plan-Gate, kein `maintenance_enabled`-Gate.
- **WaterSample** + **LabResult** (OSS v1.34.0) — **Wasserproben / TWV-Beprobung**: ein Laborbefund (`WaterSample`) je Entnahme an einer `NetworkFeature` vom Typ `probenahme` buendelt mehrere Laborwerte (`LabResult`: Parameter, Wert, Einheit, Grenzwert, Ampel-Status). Geschwister-Pattern zu `SpringYield`. Der Parameter-Katalog + Bewertung liegt in [app/network/water_quality.py](app/network/water_quality.py) und gilt **je Land** (AT: TWV = Basiskatalog `PARAMETERS`; DE: TrinkwV 2023 = Basis + `_DE_OVERLAY` mit abweichenden Werten und Zusatzparametern wie Uran/PFAS; immer über `parameters()`, `assess`, `effective_limit` — nie `PARAMETERS` direkt iterieren; Schlüssel bleiben in beiden Ländern gleich, Grenzwerte mit Übergangsfrist wie Blei DE tragen `limit_steps` und gelten zum **Probenahmedatum**); Grenzwerte sind Code-Konstanten, pro Tenant ueber `AppSetting` (`water_quality.<param>.limit`) ueberschreibbar (`/network/water-quality/limits`). `unit`/`limit_text`/`status` werden auf `LabResult` zur Erfassungszeit **eingefroren** (Beleg-Stabilitaet). Erfassung ueber das Wasserprobe-Modal der Elementliste + inline im Karten-Panel (`sample_add`/`sample_delete`, Route gatet hart auf `feature_type=='probenahme'`); die **Wasserqualitaets-Seite** `network.water_quality` zeigt je Stelle den letzten Befund + Gesamt-Ampel, ein Trend-Diagramm (waehlbarer Parameter, `?param=`), CSV-Export und einen **Behoerdenbericht** (WeasyPrint, ImportError-sicher → `water_quality_print.html`-Fallback). Kein Plan-Gate.
- **FeaturePhoto** — Foto-Anhang zu einem Feature; Ablage als Geschwister von `PDF_DIR` (nicht in der DB). Shapefile-/WLK-Import in [app/network/wlk_import.py](app/network/wlk_import.py) (`pyshp` + `pyproj`, GK→WGS84).

**Störungsjournal (`incidents`):**
- **Incident** — Störungs-/Rohrbruch-Eintrag mit Kartenpin (GeoJSON-Point als Text), Ursachenkategorie/Status/Schweregrad, Reparaturkosten/Wasserverlust/betroffene Anschlüsse.
- **IncidentPhoto** — Foto-Anhang; Ablage als Geschwister von `PDF_DIR` (`instance/incidents/`), **nicht** im data_transfer-ZIP (separates FS-Backup noetig).

**Schriftführung (`schriftfuehrung`, nur WG-Modus):**
- **Meeting** — Vorstandssitzung (`board`) oder Hauptversammlung (`assembly`), Lebenszyklus `planning → invited → held`; dazu **MeetingAgendaItem** (Tagesordnung), **MeetingResolution** (Beschlüsse), **MeetingProtocol** (Protokoll), **MeetingAttendance** (Anwesenheit).
- **MeetingInvitation** (erbt `EmailTrackableMixin`) + **MeetingDeliveryLog** — Einladungsversand pro Empfänger + Zustell-Audit.
- **SchriftverkehrDocument** — eigenständiges Korrespondenz-Dokument (eingehend/ausgehend) im Jahr-Archiv; DB hält nur Metadaten, Datei im Schriftverkehr-Ordner (Geschwister von `PDF_DIR`).

### Kontierung (Konto pro Rechnungsposition, v1.43.0)

`Booking.account_id` ist `NOT NULL` — jede Zahlung braucht ein Buchungskonto. Woher es
kommt, entscheidet eine dreistufige Kaskade in
[`_split_invoice_by_dimensions`](app/accounting/services.py):

1. **`InvoiceItem.account_id`** — gesetzt vom Tarif (Rechnungslauf) oder von Hand im
   Positions-Editor.
2. **`OpenItem.account_id`** — nur bei **manuell** angelegten Posten (siehe unten).
3. **expliziter Fallback** — das im Bezahlt-Dialog bzw. beim OP-Ausgleich gewaehlte Konto.

**Zwei Quellen, strikt getrennt** (`OpenItem.account_id`):

* **Manueller Posten** — traegt sein eigenes Konto, im Anlage-/Bearbeiten-Modal waehlbar
  (`accounting/_open_item_form_body.html`). Leer = wird beim Ausgleichen abgefragt.
* **Posten aus einer Rechnung** — bekommt **nie** ein Konto (`create_or_update_open_item`
  hat dafuer gar keinen Parameter mehr). Eine Rechnung kann mehrere Konten betreffen; ein
  einzelnes Feld am Posten koennte das nicht abbilden und waere eine zweite,
  konkurrierende Wahrheit. Auch `open_item_pay` schreibt das Dialog-Konto in diesem Fall
  NICHT auf den Posten zurueck.

`_open_item_needs_account(item)` entscheidet pro Zeile, ob der Bezahlen/Rueckzahlen-Button
direkt bucht oder erst den Konto-Dialog oeffnet — bei Rechnungs-Posten ueber
`invoice_missing_account(item.invoice)`, bei manuellen ueber `item.account_id`.
Die frueher vorhandene **Massen-Kontosetzung** auf der OP-Seite ist entfallen (das Konto
gehoert an den Tarif bzw. die Position), ebenso das Konto-Dropdown in der Zeile.

Die Zahlung wird nach **`(account_id, project_id, tax_rate)`** gesplittet. Ergibt der Split
genau eine Zeile, entsteht eine **normale Einzelbuchung**; ab zwei Zeilen ein
`BookingGroup`-Header plus je Zeile ein Kind (**Sammelbuchung**, ADR-002). Einheitliches
Konto + Projekt + Steuersatz ⇒ bewusst *keine* Sammelbuchung.

Bleibt nach der Kaskade ein Split ohne Konto, wirft der Service `ValueError`. Damit der
Nutzer nicht in einen Rollback laeuft, prueft die UI vorher mit
`acc_svc.invoice_missing_account(invoice)`: nur dann zeigt der „Bezahlt"-Button den
Konto-Dialog (`payAccountModal`), sonst bucht er direkt. Die Sammelaktion in der
Rechnungsliste fragt das Konto einmal fuer den Stapel ab und **ueberspringt** Rechnungen,
die danach immer noch unkontiert sind, statt den ganzen Lauf zurueckzurollen.

Der Rechnungslauf (`/invoices/generate`) kontiert seine Positionen aus dem Tarif
(je Tarifposition ein eigenes Konto) und setzt optional **ein Projekt fuer
den ganzen Lauf** (`BillingRun.project_id`) auf alle Positionen. Ein Sweep am Ende jeder
Rechnung faengt auch die Positionen ab, die `apply_corrections_to_invoice` und
`cap_invoice_at_zero` nachtraeglich anhaengen.

**Historie:** Diese Struktur gab es schon einmal und wurde in v1.7.0 (Migration
`e3b1f7a2c9d4`) ausgebaut — nicht aus fachlichen Gruenden, sondern weil das Konto-Dropdown
als 9. Feld die Positionszeile sprengte (es hinterliess ein `col-md-0`). Seit v1.43.0 stehen
Konto und Projekt deshalb in einer **zweiten Zeile** („Kontierung") innerhalb derselben
Positions-Box; wer dort ein Feld ergaenzt, gehoert in diese zweite Zeile, nicht in die
Betragszeile.

### E-Rechnung (ZUGFeRD/Factur-X, EN 16931)

Jedes Rechnungs-PDF wird ein **Hybrid**: PDF/A-3b mit eingebettetem `factur-x.xml` (Profil „EN 16931"), für DE **und** AT, B2B **und** B2C, kein Plan-Gate. Modul [app/einvoice/](app/einvoice/) — eine Richtung: `mapper.build_einvoice(invoice)` (einzige Stelle, die OSS-Modelle liest) → `model.EInvoice` → `rules.check()` (deutsche Meldungen, leer = gültig) → `cii.serialize()` (deterministisch, XSD-Reihenfolge beachten!) → `pdf.write_facturx_pdf()` (WeasyPrint-`Attachment` + Factur-X-XMP). Eingehängt in `pdf_service.render_invoice_pdf` — fehlen Stammdaten oder ist `einvoice.enabled=false`, entsteht still ein normales PDF.

- **Startwert je Land:** `einvoice.enabled` ist ohne Eintrag „an" (Bestand bleibt so). **Neue** Mandanten bekommen ihn aus `CountryProfile.einvoice_default` (AT **aus**, DE **an**): OSS `init-db` (`seed_default_einvoice_setting`, nur ohne Eintrag **und** ohne Rechnung), SaaS `create-tenant`, Platform-Provisioner (`COUNTRY_SEEDS[...]["einvoice_enabled"]`, Drift-Test). Ist die E-Rechnung aus, blendet das Kundenformular Format/Leitweg-ID/UID/Peppol-Felder aus (`einvoice_enabled` als Context-Variable; `_apply_einvoice_fields` lässt die Bestandswerte dann stehen) — der DE-Unternehmer-Schalter bleibt, die gesetzliche Pflicht (`obligation.applies()`) hängt bewusst nicht am Schalter.
- **Einfrieren:** Gesperrte Rechnungen (und Entwürfe beim Versand, `freeze_einvoice=True` in Mail-/Post-Versand) legen ihr XML als `<PDF_DIR>/<Jahr>/<Nr>.xml` ab (`invoices.xml_path`, `einvoice_profile`); jedes spätere PDF bettet genau dieses XML ein. Grund: beim Hybrid geht das XML dem Bildteil vor. `xml_path` läuft im data_transfer-ZIP mit wie `pdf_path`.
- **Normalisierung im Mapper:** Mahngebühr-Positionen raus; Storno → Typ 381 mit positiven Werten + Verweis aufs Original; negativer Einzelpreis → negative Menge (BR-27); USt > 0 → S, ohne USt → E mit Begründung (Kleinunternehmer-Hinweis in nicht USt-pflichtigen Jahren, sonst `einvoice.exempt_reason`) — **nie O** (BR-O-* verbietet das neben S). Summen exakt wie `recalculate_total`; Abweichung zu `total_amount` = kein XML.
- **Stammdaten:** strukturierte Anschrift `wg.street`/`wg.postal_code`/`wg.city` (leer = aus `wg.address` zerlegt), `wg.contact_name`, `wg.register_number` (Kennung ohne UID, BR-CO-26) — alle in `_WG_MAP`, also **zwingend** als Inputs in `settings/index.html` (die Speicher-Schleife setzt fehlende Felder auf leer). Karte „E-Rechnung" im Tab „Rechnung" mit Checkliste (`service.readiness()`); Rechnungsdetail zeigt Status + XML-Download (`/invoices/<id>/einvoice.xml`).
- **XRechnung 3.0 (Behördenkunden, Leitweg-ID):** `Customer.einvoice_format='xrechnung'` wählt das zweite Profil (`PROFILE_XRECHNUNG = "xrechnung-3.0"` in `model.py`); `Customer.buyer_reference` ist BT-10 (Leitweg-ID oder Kundenreferenz, ohne Eintrag die Kundennummer → BR-DE-15), `Customer.vat_id` BT-48 (auch im ZUGFeRD). Kundenformular-Abschnitt „E-Rechnung" (`customers/routes.py:_apply_einvoice_fields`). Unterschiede zu ZUGFeRD:
  - **Reines XML, kein Hybrid:** `render_invoice_pdf` liefert für XRechnung ein ganz normales PDF (nie PDF/A-3); das XML trägt das PDF als **BG-24-Anhang** (`_visual_copy`, Typ 916, Base64). Mail-Versand (`send_email`, `send_email_ajax`) hängt `<Nr>.xml` statt PDF/Word an (`service.xrechnung_attachment`) und **bricht ab**, wenn sich die XRechnung nicht erzeugen lässt (`EInvoiceUnavailable`) — ein Behördenportal lehnt eine unvollständige Rechnung ohnehin ab.
  - **Profil bleibt eingefroren:** `profile_of(invoice)` nimmt bei vorhandenem XML `invoices.einvoice_profile`, nicht das aktuelle Kundenformat. Sonst bettete `render_invoice_pdf` nach einer Umstellung ein XRechnung-XML als `factur-x.xml` in ein ZUGFeRD-PDF.
  - **Zusatzregeln** (`rules._xrechnung_issues`, deutsche Meldungen): BR-DE-1 Zahlungsanweisung auch beim Storno-Beleg (der Mapper lässt sie nur bei ZUGFeRD weg), BR-DE-2/5/6/7 Ansprechpartner + Telefon + E-Mail des Verkäufers, BR-DE-8/9 PLZ + Ort des Käufers, BT-49 elektronische Adresse des Käufers (E-Mail, sonst die Leitweg-ID mit Schema `0204`), BR-DE-19 IBAN-Prüfziffer. Eine Lieferanschrift ohne PLZ/Ort entfällt (BR-DE-10/11).
  - **Leitweg-ID** ([einvoice/leitweg.py](app/einvoice/leitweg.py)): Format `Grob[-Fein]-Prüfziffer` (MOD 97-10, an zwei echten IDs belegt). Geprüft wird nur, was wie eine Leitweg-ID aussieht (Länderkennzeichen 01–16/99) — die Bestellreferenz eines Firmenkunden bleibt frei wählbar; bei Buchstaben in der Feinadressierung entfällt die Prüfziffer (Umrechnung nicht belegt).
  - **BT-49 beim ZUGFeRD** bleibt an `Customer.wants_email` gekoppelt (der Beleg nennt die Adresse sonst selbst nicht).
- **UBL 2.1 / Peppol BIS 3.0 (AT-Bund, e-Rechnung.gv.at)** — drittes Profil (`PROFILE_PEPPOL_UBL = "peppol-bis-3"`, Kundenformat `peppol_ubl`), [einvoice/ubl.py](app/einvoice/ubl.py). XRechnung und UBL bilden zusammen `XML_ONLY_PROFILES`/`XML_ONLY_FORMATS` (`model.py`): reines XML, PDF als Anhang (BG-24), Mail-Anhang `<Nr>.xml` (`service.xml_only_attachment`, Alias `xrechnung_attachment`), kein Post-Angebot (`obligation.electronic_reason` → `REASON_PEPPOL`). Nie wieder `== PROFILE_XRECHNUNG` schreiben, wo beide gemeint sind.
  - **Peppol kennt „EM" nicht als Adressschema** (`PEPPOL-EN16931-CL008`, Liste im Schematron): Verkäufer wird über die **UID** adressiert (AT `9914`, DE `9930`, andere Länder → Regelmeldung), Käufer über `Customer.peppol_id` (`Schema:Kennung`, z. B. `9915:b`, **nicht erraten** — der Wert kommt von der Dienststelle), sonst aus seiner USt-IdNr. XRechnung darf weiter `EM` und die Leitweg-ID mit `0204` nutzen.
  - Zusatzfelder am Kunden: `order_reference` (BT-13, AT-Bund Pflicht; steht auch im ZUGFeRD/XRechnung, wenn gepflegt), `supplier_number` (BT-29 = `seller.identifier`, nur UBL), `peppol_id`. e-Rechnung.gv.at mappt BT-13 auf `/Invoice/OrderReference/ID` und BT-29 auf `/Invoice/AccountingSupplierParty/Party/PartyIdentification[1]/ID`.
  - **Reihenfolge:** UBL-Elemente strikt nach `xsd:sequence` (Invoice ≠ CreditNote: Storno hat Wurzel `CreditNote`, `CreditNoteTypeCode`, `CreditedQuantity`, **kein** `DueDate`, dafür `PaymentMeans/PaymentDueDate`).
  - **Peppol-Schematron lokal ausführen (Mustang prüft bei UBL nur EN 16931 + XSD, nicht die Peppol-Regeln!):** `PEPPOL-EN16931-UBL.sch` aus `OpenPeppol/peppol-bis-invoice-3` (`rules/sch`) + die ISO-Skeleton-XSLTs aus `Schematron/schematron` (`trunk/schematron/code`: `iso_dsdl_include`, `iso_abstract_expand`, `iso_svrl_for_xslt2`) per Saxon (`java -cp Mustang-CLI.jar net.sf.saxon.Transform`) zu einem XSLT kompilieren und auf die Dump-XMLs loslassen; SVRL-Namespace ist `http://purl.oclc.org/dsdl/svrl`. Negativprobe: ohne BuyerReference/OrderReference muss `PEPPOL-EN16931-R003` kommen.
- **B2B-Pflicht-Workflow (nur DE)** — [einvoice/obligation.py](app/einvoice/obligation.py) ist die **einzige** Stelle, die die Pflicht kennt; Leitplanken fragen dort, nie eigene Bedingungen schreiben:
  - `Customer.is_business` = Unternehmer i. S. d. UStG (**nicht** `is_company`: ein Vermieter ist Person *und* Unternehmer). `required_for(invoice)` ist wahr, wenn DE ∧ Unternehmer ∧ im Rechnungsjahr USt-pflichtig (`is_year_vat_liable`, Kleinunternehmer dauerhaft frei) ∧ brutto **> 250 €** ∧ Leistungsende (`service_period`, sonst Rechnungsdatum) ≥ `einvoice.mandate_from` (Setting, nur `2028-01-01` Standard oder `2027-01-01`; unbekannte Werte fallen auf 2028). Maßgeblich ist der **Leistungszeitpunkt**, nicht das Rechnungsdatum (§ 27 Abs. 38 UStG).
  - `electronic_reason(invoice)` = `required_for` **oder** Kunde mit XRechnung (Ausdruck ist dort nicht der Beleg). Darauf bauen: `_invoice_overview` (Entwürfe ohne E-Mail-Fähigkeit landen in `electronic_only` statt `post_ids`; Gruppe „Nur elektronisch zustellbar" im Versenden-Dialog, Zähler `count_electronic`), `split_post_invoices` (Post-Versand druckt Entwürfe mit `electronic_reason` **nie** — OSS-Route `billing_run_post_bulk` **und** SaaS-Renderer `render_billing_run_post`, gleicher Helfer; bereits versendete Belege bleiben nachdruckbar), Warnung beim manuellen Wechsel auf „Versendet" (`set_status`), Detailblock + `delivery_problem` (Sendeprotokoll = `EmailEvent`-Verlauf/`last_email_status`; Soft-Bounce zählt noch nicht als nicht zugestellt), `_mail_document_format` (Unternehmer bekommen per Mail **immer** PDF, nie nur Word).
  - **Mail-Gate bleibt die eine Wahrheit:** `set_business()` schaltet beim Wechsel *aus → an*, nur in DE und nur mit E-Mail-Adresse, `rechnung_per_email` mit ein und schreibt ein `CustomerEmailConsentLog` mit **eigener Aktion `einvoice_b2b_enabled`** — bewusst nicht `opt_in_confirmed`, denn es ist keine Einwilligung des Kunden, sondern eine Einstellung des Mandanten (B2B braucht keine). Ein Kunde, der abbestellt hat (`is_business` schon an, `rechnung_per_email` aus), wird nie automatisch wieder eingeschaltet; in AT bleibt der Opt-in unberührt. Ohne Adresse gibt es nur eine Warnung (`business_warning`), keinen Fehler — Behörden mit Leitweg-ID haben oft keine.
  - Das Kundenformular zeigt den Schalter nur in DE; das Hidden-Feld `einvoice_b2b_fields=1` verhindert, dass ein Formular ohne Checkbox (AT) das Kennzeichen löscht. Kontaktliste: Filter `business=yes|no_email` (`no_email_clause()` = `not wants_email`), Dashboard-Karte `dashboard_summary()` (nur DE + USt-pflichtig).
  - **Import** ([imports/common.py](app/imports/common.py) `apply_einvoice_columns`, beide Wizards): USt-IdNr./Leitweg-ID/Rechnungsformat/Unternehmer gelten nur, wenn die Spalte **gemappt** ist (sonst bleibt der Bestand auch beim Aktualisieren stehen); ungültige Werte entfallen mit Warnung, der Rest der Zeile wird importiert. Spaltenhinweise mit `=`-Präfix (`=uid`, `=vat`) matchen nur exakt — sonst träfe „uid" jede GUID-Spalte und „vat" jedes „Privatkunde".
- **Eingangs-E-Rechnungen (Empfang, Phase 4)** — seit v1.45.0 ein **Sonderfall der Belegablage** (s. u.): ein `Document` mit `Document.einvoice` (`IncomingInvoice` = nur die aus dem Original gelesenen Daten), Parser [einvoice/incoming.py](app/einvoice/incoming.py) (flask-frei), Buchungsvorschlag [accounting/incoming_service.py](app/accounting/incoming_service.py), Routen [accounting/documents.py](app/accounting/documents.py) (Seite „Belege“ in der Seitenleiste; `/accounting/incoming` leitet um).
  - **Das Original ist unantastbar** (§ 14b UStG, 8 Jahre): wird Byte für Byte als `Document` abgelegt (`documents/JJJJ/MM/<id>_<sha8>.<ext>` im **Mandanten-Dateibaum** = Elternordner von `PDF_DIR`, im SaaS also je Mandant) und nie verändert; auch verworfene Belege behalten die Datei. Dublettenschutz über `sha256` (dieselbe Datei nie zweimal; gleiche Rechnungsnummer + Lieferant nur Hinweis). `data_transfer`: siehe „Belegablage“.
  - **Fremddateien sind nicht vertrauenswürdig:** der Parser lädt keine Entitäten, kein Netz und **lehnt jede DTD ab** (XXE/Billion-Laughs); Lieferantendaten werden nur über Jinja-Autoescape angezeigt; eingebettete Anhänge (BG-24) werden **nur dann inline** als PDF ausgeliefert, wenn der Inhalt mit `%PDF-` beginnt (die Art nennt der Absender), sonst `octet-stream` als Download, stets `nosniff`.
  - Lesen: CII (ZUGFeRD 2.x/Factur-X/XRechnung) und UBL (Invoice + CreditNote), auch als Hybrid-PDF (`pypdf` `reader.attachments`: `factur-x.xml`, `zugferd-invoice.xml`, `xrechnung.xml`, sonst jede XML mit bekanntem Wurzelelement). **Keine** neue Abhängigkeit (kein `factur-x`). ZUGFeRD 1.x → klare Fehlermeldung. Ein PDF ohne Anhang ist **keine** E-Rechnung (`NotAnEInvoice`) — die Belegablage legt es als normalen Beleg ab. Fremdwerk-Gegenprobe: `tests/fixtures/einvoice/kosit-*.xml` (offizielle KoSIT-Testsuite, dieselbe Rechnung als UBL und CII muss dasselbe ergeben).
  - **Buchungsvorschlag** (`incoming_service.book`): je Steuersatz der **Bruttobetrag** als Ausgabe (negativ, mit `tax_rate`), ein Satz → `Booking`, mehrere → `BookingGroup` (ADR-002); Gutschrift (381/261) positiv; Sätze mit 0 %/steuerfrei werden zusammengefasst; Rundungsdifferenz ≤ 5 ct geht in die erste Zeile, mehr ist ein Fehler (Datei widersprüchlich → manuell buchen). Datum nie in der Zukunft, nur in offenes Buchungsjahr (`open_fiscal_year_error`). Lieferant: erst USt-IdNr. (`Customer.vat_id`), dann Name; sonst Neuanlage aus den Rechnungsdaten — ein Kunde ohne Lieferant-Flag wird nie als Lieferant gewählt. Der neue Lieferant wird beim Fehler mit zurückgerollt.
  - **Beleg ↔ Buchung:** Die Buchungsseiten zeigen einen Rücksprung zum Beleg (`incoming_doc`). Die Verknüpfung ist ein `DocumentLink`; „verbucht“ ist **abgeleitet** (s. „Belegablage“), ein Storno braucht deshalb keinen Hook — der Beleg taucht von selbst wieder im Eingang auf.
  - **Nicht gebaut:** Offener Posten/Zahlungsziel-Verfolgung (Lieferantenrechnungen sind bewusst nur Ausgabenbuchungen wie bisher), Bank-Matching, automatische Kontovorschläge, Fremdwährungsumrechnung, Selbstfakturierung (Typ 389: Rollen vertauscht).
- **Validierung:** `tests/integration/test_einvoice.py` (ZUGFeRD, Golden-Fälle G1–G12) und `test_einvoice_xrechnung.py` (G6a–d) schreiben mit `EINVOICE_DUMP_DIR=…` jedes XML heraus → mit Mustang-CLI (`java -jar Mustang-CLI.jar --action validate --source <datei>`) prüfen. Mustang wendet bei der XRechnung das offizielle KoSIT-Schematron (`XR_30/XRechnung-CII-validation.xslt`) an; beim Profil EN 16931 sind die XRechnung-Hinweise (BR-DE-*, PEPPOL-EN16931-R001/R010) erwartet. Eine XRechnung muss **ohne** Hinweise durchlaufen. Plan und Phasen: `E_RECHNUNG_PLAN.md` im Workspace-Root.

### Belegablage (Belege an Buchungen, v1.45.0)

Jede Datei (Rechnung, Kassenbon, Kontoauszug, Scan, Foto) ist ein **`Document`** (`documents`); **`DocumentLink`** (`document_links`) verknüpft sie n:m mit einer **Buchung oder Sammelbuchung**, **`DocumentEvent`** protokolliert jede Aktion (`uploaded`/`linked`/`unlinked`/`filed`/`discarded`/`reopened`/`edited`/`deleted`; Snapshot von Name + SHA-256, `document_id` wird beim Löschen NULL — das Protokoll überlebt). Code: [app/documents/](app/documents/) (`storage.py`, `service.py`), Routen [app/accounting/documents.py](app/accounting/documents.py) (Blueprint `accounting`, Recht `buchhaltung`; Seite **Belege** = Belegeingang), Büroklammer + Modal [templates/documents/](app/templates/documents/) nach dem Notiz-Muster (`doc_pin` in der Buchungsliste, ein Pin je Einzel-/Sammelbuchung, Zahlen in EINER Abfrage über `document_counts_for`). Kein Plan-Gate (alle Tarife).

- **Datei unverändert.** Jeder Upload wird **Byte für Byte** abgelegt, nie umkodiert (GoBD: elektronisch empfangene Belege im empfangenen Format; AT § 131 Abs. 3 BAO „inhaltsgleich“). Erlaubt: PDF, JPEG, PNG, WebP, E-Rechnungs-XML — erkannt an den **Magic Bytes** (`service.sniff`), nie an der Endung; abgelehnt: HEIC, TIFF, SVG, HTML, passwortgeschützte/defekte PDFs (reine Rechte-Sperre ist ok), XML ohne E-Rechnung. **Verkleinert wird höchstens im Browser** (`wkDocShrink` in `documents/_modal.html`: Canvas, max. 2400 px, JPEG 0,85, nur über 3 MB/3000 px, Schalter „Fotos verkleinern“ an) — der Server kennt das Original nicht, das Protokoll vermerkt „verkleinert“ + Originalgröße. Deshalb braucht der Server **kein Pillow**.
- **Nur relative Schlüssel.** `documents.storage_key` (`documents/JJJJ/MM/<id>_<sha8>.<ext>`; migrierte Alt-Belege `incoming/…`) wird **nur** über `storage.path_for` aufgelöst: Regex per `fullmatch` (`$` trifft in Python auch vor `\n`!) **und** `resolve()` + `relative_to()` gegen die Mandanten-Wurzel (`file_safety.tenant_file_root`, bei jedem Aufruf neu — SaaS-Middleware und Job-Worker biegen `PDF_DIR` je Request/Mandant um). `put` schreibt atomar und überschreibt nie. Nie einen absoluten Pfad speichern, nie einen DB-Wert direkt öffnen.
- **„Verbucht“ ist abgeleitet, nicht gespeichert** (`booked_clause`/`is_booked`): wirksame Verknüpfung = Buchung nicht storniert und keine Storno-Gegenbuchung, bzw. Sammelbuchung aktiv. `Document.status` ist nur der Arbeitsstand `Neu`/`Abgelegt`/`Verworfen`. Ein Storno (auch die Rechnungs-Storno-Kaskade) braucht **keinen Hook**: die Verknüpfung bleibt als Nachweis, der Beleg ist wieder im Eingang. Reiter der Liste: `tab_filter` (Eingang/Verbucht/Abgelegt/Verworfen/Alle).
- **Verknüpfen/Lösen.** Verknüpft wird nur mit einer Einzelbuchung oder **Sammelbuchung (Header)** — nie mit einem Kind (das Bearbeiten der Sammelbuchung löscht und legt alle Kinder neu an) und nie mit einer stornierten. Verknüpfen ist **immer** erlaubt (auch im abgeschlossenen Jahr: es kommt nur etwas dazu), **Lösen** nur bei wirksamer Buchung im **offenen** Buchungsjahr (`unlink_blocker`). Wird eine Buchung **gelöscht**, ruft die Route **vor** dem `db.session.delete` `documents.service.on_booking_deleted` (`booking_delete`, `booking_group_delete`); **wer einen weiteren Weg zum Löschen von Buchungen ergänzt, muss das ebenfalls tun** (der ORM-Cascade `cascade="all"` an `Booking.document_links`/`BookingGroup.document_links` ist nur das Sicherheitsnetz, auch für SQLite ohne FK-Erzwingung). Bewusst **ohne** `delete-orphan`: ein Link hat genau einen von zwei Eltern, SQLAlchemy würde sonst jeden als verwaist behandeln.
- **Löschen/Aufbewahrung.** Gelöscht werden darf nur ein Beleg, der **nie** verknüpft oder abgelegt war (kein Ereignis `linked`/`filed`) — alles andere ist aufbewahrungspflichtig (AT § 132 BAO 7 Jahre, DE § 147 AO/§ 14b UStG 8 Jahre, für **jeden** Buchungsbeleg, B2B wie B2C). `retention_end` (nur Anzeige) = 31.12. des spätesten Jahres aus Buchungen/Belegdatum/Ablage + `CountryProfile.document_retention_years`; Hinweistext `document_retention_hint` (AT 22 Jahre Grundstücke, DE Verlängerung bei offenen Verfahren). Es gibt bewusst keinen Löschweg für Gebuchtes.
- **Grenzen** (`config.py`): `DOCUMENT_MAX_UPLOAD_MB` (15) gilt **je Request** — ein `before_request`-Hook in `create_app`, registriert **vor** `csrf.init_app` (CSRF liest das Formular schon), setzt `request.max_content_length` nur für `accounting.document_upload` (ein globales `MAX_CONTENT_LENGTH` bräche den Datenimport); der 413-Handler antwortet für `fetch` als JSON. `DOCUMENT_QUOTA_MB` (0 = unbegrenzt) bzw. der Resolver `app.extensions["documents.quota_resolver"]` (die SaaS hängt ihn an, **fail-open**); Belegung = `SUM(documents.size_bytes)`. `DOCUMENT_MIN_FREE_DISK_MB` sperrt Uploads bei knappem Platz (`storage.free_bytes`). Der Upload prüft in der Reihenfolge Größe → Typ → Dublette → Kapazität → schreiben → commit; schlägt der commit fehl, wird die Datei wieder entfernt.
- **Upload-Endpunkt** `accounting.document_upload`: eine Datei je Request (JS lädt nacheinander), `Accept: application/json` → JSON je Datei (`ok`, `id`, `duplicate`, `linked`, `warning`), sonst Flash + Redirect; optional `link_type`/`link_id` verknüpft sofort (Panel an einer Buchung). Dateien werden nur mit exaktem Content-Type + `nosniff` ausgeliefert (PDF/Bild inline, XML immer als Download, **kein** CSP-`sandbox` — Chrome zeigt PDFs sonst nicht an).
- **Buchungsformular:** `_parse_booking_form` liest `document_ids` (`parse_document_ids`), die Verknüpfung entsteht **nach** dem Flush der Buchung; die Auswahl (`documents/_picker.html`, versteckte `document_ids`) überlebt einen Validierungsfehler. Beim Bearbeiten werden Belege nur **hinzugefügt**, gelöst wird im Panel/auf der Belegseite. „Buchung aus diesem Beleg erstellen“ → `booking_new?document_id=` (`booking_prefill`, Rückkehr über `return_doc`).
- **data_transfer:** `Document`/`IncomingInvoice`/`DocumentLink`/`DocumentEvent` in Kategorie `buchungen`; Dateien reisen als `files/<storage_key>` (`ZIP_STORED`), der Schlüssel steht auch ohne Bundle im JSON. Beim Import wird `storage_key` **nie** übernommen (`registry.STORAGE_KEY_COLS` → NULL); `_restore_document_files` setzt ihn erst, wenn die Datei zur Prüfsumme passt (am Platz oder aus `files/`, nie überschreiben). Vollersatz behält den Schlüssel, Merge vergibt einen neuen aus der neuen ID. Jahresfilter: `_filtered_document_ids`; `DocumentLink` hat NULL im natürlichen Schlüssel (`_NULLABLE_KEYS`). Der Mandanten-Reset räumt `documents/`, `incoming/`, `incidents/`.
- **Sicherung:** die Dateien liegen im Instanz-Volume; ein Datei-Backup gibt es im OSS nicht (SaaS-Prod: Hetzner-Server-Backups + der Voll-Export des Mandanten, der die Belege mitnimmt).
- **Tests:** `tests/unit/test_document_{sniff,storage}.py`, `tests/integration/test_document_{service,transfer}.py` + `test_incoming_service.py`, `tests/http/test_documents.py` + `test_booking_documents.py`. Migration `c4f8a2d6b1e9` (baut `incoming_invoices` neu auf — die FKs der Vorgänger-Migration sind unbenannt, MariaDB kann die Spalten sonst nicht droppen).

### Rechte-System (Rollen & Permissions)

[app/auth/permissions.py](app/auth/permissions.py) definiert **10 Bereichs-Rechte** als Code-Konstanten (keine DB-Tabelle): `stammdaten`, `zaehler`, `buchhaltung`, `rechnungen_op`, `mahnwesen`, `auswertungen`, `network`, `incidents`, `schriftfuehrung`, `verwaltung`. Jeder Hauptmenuepunkt entspricht genau einem Recht.

- Rechte werden Rollen ueber `role_permissions` zugeordnet; `init-db` seedet eine **Admin-Rolle** (alle Rechte) plus abgeleitete Rollen.
- Die Rolle **`Admin`** hat **implizit jedes Recht** — auch spaeter neu hinzukommende (Check in `User.has_permission`).
- Durchsetzung: `@permission_required(PERM_X)` pro Route, oder `bp.before_request(require_blueprint_permission(PERM_X))` fuer ein ganzes Blueprint. Beide flashen + redirecten zum Dashboard statt 403 (konsistenter UX-Pfad). `ALL_PERMISSIONS` ist als Jinja-Global fuer das Rollen-Formular verfuegbar.
- **Migration von altem Code:** Routen, die frueher `current_user.role == "admin"` geprueft haben, muessen auf `has_permission(...)` umgestellt werden. Neue gated Routen IMMER mit Recht versehen, sonst sind sie fuer alle eingeloggten User offen.

### Settings & WG Context

`app/settings_service.py` provides DB-overrides-env for cooperative identity and mail config:
- `wg_settings()` is injected into **every template** as `{{ wg.name }}`, `{{ wg.iban }}`, `{{ wg.email }}`, etc.
- `apply_mail_settings()` runs at app start and after settings changes to update Flask-Mail state
- Mail-Passwort wird at rest mit **`WASSERKLAR_MAIL_KEY`** verschluesselt — bewusst **separat vom `SECRET_KEY`** (ein geleaktes Session-Secret soll nicht das SMTP-Passwort entschluesseln). Comma-separated Keys ⇒ Rotation via `MultiFernet` (erster Key = primary); `flask --app run rotate-mail-key` re-encryptet. Ohne den Key loggt die App beim Start eine Warnung und `send_mail()` wirft erst beim tatsaechlichen Versand (ist Absicht — Erststart ohne Mail-Konfig soll laufen).
- **`MAIL_PLATFORM_RELAY`** (default aus): wenn aktiv, laeuft der Versand ueber den `app.config`-SMTP statt ueber per-Tenant-`mail.*`-Overrides. OSS-Standalone: aus; SaaS schaltet das per `mail_overrides` kontextabhaengig (own_smtp / shared_relay / custom_postmark).

### Länderprofile (Österreich / Deutschland)

[app/country.py](app/country.py) ist die **Single Source of Truth** für alles, was sich zwischen den unterstützten Ländern unterscheidet (`PROFILES` = `AT`, `DE`): Standard-Steuersätze, USt für Wasser (10 / 7 %), Nacheichfrist (5 / 6 Jahre), Beschriftung der UID-Nr./USt-IdNr., Kleinunternehmer-Hinweis, Kartenlayer + Startausschnitt, Fachbegriffe (`term()`: Kassa/Kasse, Obmann/Vorsitzender, Jänner/Januar, die Zahlungsaufforderung auf dem Beleg „Wir ersuchen Sie … einzuzahlen"/„Wir bitten Sie … zu überweisen" über `pay_request`/`pay_verb` — auch die SaaS-Rechnungsdesigns nutzen sie, nie wieder hart codieren). Das Land des Mandanten steht in der `AppSetting` `org.country` (Fallback `DEFAULT_COUNTRY` aus der Config, zuletzt AT — bestehende Installationen verhalten sich also unverändert). Code, der einen länderabhängigen Default braucht, fragt `country.current_profile()` — **nie** ein hart codiertes „Österreich" oder „10 %".

- **Profile liefern nur Defaults.** Was der Mandant selbst pflegt (Steuersätze in `tax_rates`, `tax.water_rate`, Eichfrist …) hat Vorrang; die Profile greifen beim Seeden (`seed_default_*(db, country_code=…)`, `init-db --country`), bei „Länder-Defaults übernehmen" ([app/settings/country_defaults.py](app/settings/country_defaults.py), nie destruktiv: Sätze werden höchstens deaktiviert, der Wassercent nur eingeblendet) und als Fallback.
- **Adress-Land:** `country.is_foreign(land)` (Jinja-Test `foreign_country`) statt `!= 'Österreich'`; das Land steht nur bei Auslandsanschriften im Brief. Neue Adressen bekommen `country.home_country_name()`.
- **Karten:** `country.map_config()` → JSON in [_map_config.html](app/templates/_map_config.html) → [static/js/basemaps.js](app/static/js/basemaps.js) (basemap.at bzw. basemap.de + OSM, optional Luftbild-WMS des Mandanten). Keine Tile-URLs mehr in den Karten-JS-Dateien; `country.map_tile_hosts()` speist die CSP der SaaS-Hydranten-Freigabe.
- **Geocoding:** [app/properties/geocoding.py](app/properties/geocoding.py) ist der Dispatcher (AT: BEV-Adressregister, lokaler Index; DE: OpenStreetMap über einen Photon-Server, `GEOCODER_PHOTON_URL`, [photon_geocode.py](app/properties/photon_geocode.py)). Aufrufer (Abgleich-Dialog, SaaS-REST `/v1/geocode`) fangen nur `GeocodingError`. Ohne Photon-Server bleibt in DE die manuelle Lage (Detailseite → Karte). Streng geprüfte Treffer (Straße + Hausnummer, PLZ) — keine Straßen-Mittelpunkte.
- **Rechnungs-Pflichtangaben:** `invoices/services.py:invoice_legal_context()` (UID/USt-IdNr., Steuernummer, Leistungszeitraum, Kleinunternehmer-Hinweis) speist PDF **und** Word (`document_service`) sowie die SaaS-Designs — eine Quelle.
- **Fallen:** (1) `term()` ist per Context-Processor **und** als Jinja-**Global** registriert — per `{% from … import %}` geladene Makro-Dateien bekommen keinen Context-Processor-Kontext (`test_board_list_renders` fing das). (2) Die RBAC-Migration legt die Rolle „Kassier" an, bevor das Land feststeht; `seed_default_roles` benennt sie um („Kassierer"), solange niemand sie nutzt, und legt nie eine zweite an (`country.role_treasurer_names()`). (3) `seed_default_*` gehören im SaaS/Platform-Provisioning **nach** dem Setzen von `org.country`, sonst gilt AT.

### HTMX Pattern

Many routes check `request.headers.get("HX-Request")` and return partial HTML fragments (`_table.html`, `_row.html`, `_status_badge.html`) instead of full pages. This enables dynamic search/filter without full reloads.

`base.html` setzt `<body hx-boost="true">` — jede Navigation laeuft als HTMX-Request. Damit geboostete **Voll**-Navigationen nicht faelschlich als Fragment-Request behandelt werden (Sidebar wuerde verschwinden), entfernt ein `before_request`-Hook in [app/__init__.py](app/__init__.py) (`_strip_hx_request_on_boost`) den `HX-Request`-Header, wenn `HX-Boosted` gesetzt ist — die Route sieht dann einen normalen GET und rendert das volle Template.

**Seiten-spezifische JS-Libs (Leaflet im Leitungsnetz-Modul, TomSelect):** hx-boost tauscht nur `<body>` aus und fuehrt `<head>`-`<script>`-Tags nicht erneut aus. Seiten, die eine eigene Lib im `head_extra`-Block laden, muessen `hx-boost="false"` auf dem Link/Container setzen (harter Reload), sonst fehlt die Lib nach dem Boost. Inline-Init-Skripte zusaetzlich gegen fehlendes `window.<lib>` absichern.

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
- **Serienversand (Massenmail)**: nie eine eigene Sende-Schleife schreiben — `{% include "_bulk_send_helpers.html" %}` im scripts-Block und `await wkSerialSend({rows, url, csrf, params?, testMode?})` aufrufen. Der Helfer macht Drossel (`bulk_mail_delay_ms`), Testmodus-Limit (3), Status-Icons, Fortschrittsbalken statt globalem Spinner (`fetch(..., {spinner: false})`), Abbruch bei abgelaufener Sitzung; Endpoints antworten `{ok, error?, email?, test_mode?}`. Bewusst Inline-Partial statt `app.js`: `app.js` wird bei hx-boost nicht neu geladen, ein offener Tab haette nach einem Deploy den alten Stand.

## Datenbank

**Default ist SQLite** (`sqlite:///instance/wg.db`, siehe [config.py](config.py)) — wer ohne `.env`-Override startet, bekommt eine lokale Datei-DB. Die App ist aber **dialekt-portabel** und laeuft auch auf **MySQL/MariaDB** und **Postgres**: einfach `DATABASE_URL` in der `.env` setzen, z.B.:

```env
DATABASE_URL=mysql+pymysql://user:pass@host:3307/dbname?charset=utf8mb4
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

Lokales Dev (und das Docker-Standalone-Deployment) laeuft inzwischen typischerweise gegen den **Docker-Postgres-Container** (`docker compose up -d postgres`, `DATABASE_URL=postgresql://…@localhost:5432/…` in der `.env`, siehe [README.md](README.md)). **MariaDB/MySQL und SQLite bleiben unterstuetzte Ziele** — entsprechend muss neuer Query-/Migrations-Code weiterhin auf allen drei Dialekten kompilieren (die Portabilitaets-Stolperer unten sind real).

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

- **WeasyPrint** (PDF generation, email with PDF) requires GTK3 and only works inside the Docker container. `requirements-dev.txt` excludes it. Routes handle `ImportError` gracefully. Exakt gepinnt (**70.0**), bei jedem Security-Release nachziehen; `pydyf` kommt als Abhängigkeit mit (kein eigener Pin). Das Dockerfile braucht `libharfbuzz-subset0` (sonst fontTools-Fallback mit Warnung, künftig Pflicht).
- **Rechnungs-PDFs entstehen nur in [app/invoices/pdf_service.py](app/invoices/pdf_service.py)**: `render_invoice_pdf(invoice, for_email=…)` rendert, `write_invoice_pdf(invoice, bytes)` legt versioniert ab (`<PDF_DIR>/<Jahr>/<Nr>[_Vn].pdf`, setzt `pdf_path` bewusst nicht — ob archiviert wird, entscheidet der Aufrufer). Alle Wege (Einzel-PDF, ZIP, Sammel-PDF, Mail, Post-Versand) und die SaaS-Job-Renderer nutzen diese Stelle; nie wieder `HTML(...).write_pdf()` für Rechnungen an einer Aufrufstelle. Die alten Underscore-Namen (`_render_pdf_html`, `_get_doc_dir`, `_versioned_path`, `_current_design`) bleiben in `invoices/routes.py` als Aliase.
- **PDF-Vorlagen unter WeasyPrint ≥ 70** (per Vorher/Nachher-Rendervergleich 62.3 → 70 gefunden): (1) Flex-`gap` wird jetzt ausgewertet und `flex-wrap` bricht zu früh um → Kachel-/Kennzahlreihen als CSS-Grid (`grid-template-columns: repeat(3, 1fr); gap: …`), nicht als `flex-wrap`. (2) Tabellenzeilen können über einen Seitenumbruch geteilt werden → jede PDF-Vorlage mit Tabelle bekommt `tr { break-inside: avoid; }`. (3) Werte mit Anführungszeichen in `<style>` (z.B. `design.font_family` der SaaS-Designs) brauchen `| safe`, sonst escaped Jinja die Quotes und WeasyPrint verwirft die ganze Deklaration.
- **Templates** use Tabler 1.0.0 layout (`templates/base.html`) with all assets loaded from CDNs (Font Awesome 5, Tabler, TomSelect).
- **Cooperative identity** (name, address, IBAN, etc.) is configured via `AppSetting` (DB) with `.env` fallback (`WG_NAME`, `WG_ADDRESS`, etc.) and appears on invoices and in all templates.
- **Belegdateien** (`documents.storage_key`) sind **keine** Pfade, sondern relative Schlüssel — nur über `app/documents/storage.py` (`path_for`) auflösen (siehe „Belegablage“).
- **In der DB gespeicherte Dateipfade nie direkt öffnen/ausliefern** (`invoices.pdf_path/doc_path/xml_path`, `dunning_notices.*_path`, Protokoll-/Schriftverkehr-`file_path`): immer über [app/file_safety.py](app/file_safety.py) `safe_tenant_path` — nur Dateien im Mandanten-Dateibaum (Elternordner von `PDF_DIR`), sonst verhält es sich wie „Datei fehlt" (im SaaS sonst Cross-Tenant-Lesezugriff). Der data_transfer-Import übernimmt solche Spalten **nie** aus der ZIP (`registry.FILE_PATH_COLS`; neue Pfadspalte dort eintragen — Guard-Test in `tests/integration/test_data_transfer_path_safety.py`). Vom Mandanten pflegbare Jinja-Texte (Mail-/Mahnvorlagen) nur mit `jinja2.sandbox.SandboxedEnvironment` rendern — ein normales `Environment` ist RCE.
