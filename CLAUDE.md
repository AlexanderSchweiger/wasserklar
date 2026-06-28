# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Wassergenossenschaft Verwaltung — a Flask web app for Austrian water cooperative management. All UI text, flash messages, and documentation are in **German**.

Stack: Flask 3.1, SQLAlchemy 2.x, Flask-Login, Flask-Mail, Flask-Migrate (Alembic), WeasyPrint + pypdf (PDF), pandas/openpyxl (CSV/Excel-Import), mt-940 + lxml (Bankauszug-Import CAMT/MT940), pyshp + pyproj (Shapefile-/WLK-Import im Leitungsnetz-Modul), python-docx (Brief-/Export), **Tabler 1.0.0 (Bootstrap 5)**, TomSelect 2.3.1, HTMX 2.0.4, Leaflet (Leitungsnetz-Karte). DB ist dialekt-portabel (SQLite / MySQL-MariaDB / Postgres) — siehe "Datenbank" unten.

Die App ist deutlich ueber die reine Verwaltung hinausgewachsen: granulares **Rollen-/Rechte-System** (10 Bereiche, nicht mehr nur admin/user), **Mandant-Typ-Schalter** (Wassergenossenschaft/Versorger, `is_wg`-Gating), **Abrechnungsperioden** (`BillingPeriod`) statt Kalenderjahr-Verdrahtung, historisierte **Rechnungslaeufe** (`BillingRun`), **Mahnwesen** (`dunning`), **Bankauszug-Import** mit Zuordnungsvorschlaegen, **In-App-Benachrichtigungen**, **E-Mail-Event-Tracking** (Postmark) + **Sperrliste** (`EmailSuppression`), ein **Leitungsnetz-Modul** (Wasserleitungsplan auf Leaflet-Karte, frueher „Technik"), ein **Störungsjournal** (`incidents`) und eine **Schriftführung** (Sitzungen/Protokolle/Beschlüsse/Schriftverkehr, nur im WG-Modus). Details in den jeweiligen Abschnitten unten.

## Common Commands

```bash
# Local dev setup (Windows, uses requirements-dev.txt which excludes WeasyPrint)
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
cp .env.example .env

# Initialize database (Alembic-Upgrade + Seeds: 4 Steuersaetze, Default-
# Mahnstufen, Default-Rollen (Admin + abgeleitete), eine aktive Abrechnungsperiode)
flask --app run init-db

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
| `accounting` | `/accounting` | `buchhaltung` | Konten, Buchungen, Umbuchungen, Bankkonten, Offene Posten, Buchungsjahre, EÜR/Jahresbericht, USt |
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
- **Customer** → has many **PropertyOwnership**; `base_fee_override` / `additional_fee_override` take priority over tariff; `wants_email` gatet jeglichen Kunden-Mailversand (siehe SaaS `invoice_optin`). **Name-Aufspaltung (v1.21.0)**: `name` bleibt das kombinierte **Sortier-/Listen-/Suchfeld** (Konvention „Nachname Vorname") und wird beim Speichern aus `last_name` + `first_name` abgeleitet; daneben gibt es `salutation` (Herr/Frau/Familie/leer) und `is_company`. Für Brief-/Rechnungsausgabe IMMER die berechneten Properties nutzen: `customer.letter_name` (Anschrift: „Vorname Nachname", „Familie X", Firmenname) und `customer.salutation_line` (Anrede). **Listen, Dropdowns, Suche und `order_by` weiterhin über `name`** — niemals nach `last_name` sortieren (bricht Firmen ohne Nachname + Altbestand, wo `letter_name`/`salutation_line` auf `name` zurückfallen). Quick-Create und Altimporte setzen nur `name`; der CLI-Befehl `flask split-customer-names` füllt `first_name`/`last_name`/`is_company` heuristisch vor.
- **Property** (Objekt/Liegenschaft) → has many **WaterMeter** and **PropertyOwnership**; also has fee overrides; `object_type` ist `NOT NULL` (Werte `'Haus'` / `'Garten'` / `'Sonstiges'`)
- **PropertyOwnership** — time-bounded Customer↔Property link (`valid_from` / `valid_to`); `valid_to=None` = currently active. **Mehrere parallele aktive Ownerships pro Property sind erlaubt** (Ehepaare, Erbengemeinschaften) — Code, der "den" aktuellen Eigentuemer abfragt, muss `.all()` oder `.first()` nehmen, nicht `.scalar()` (sonst `MultipleResultsFound`)
- **WaterMeter** → has many **MeterReading** (unique per meter+year); tracks `installed_from/to`, `initial_value`, `eichjahr`. **`meter_type`** (`'main'` / `'sub'`, default `'main'`, NOT NULL) klassifiziert Hauptz. vs. Subz.; **`parent_meter_id`** (FK self-ref, ondelete SET NULL) verlinkt Subz. auf Hauptz. (max. 1 Ebene, parent muss `meter_type='main'` sein — Validation in der Route, kein DB-Constraint, weil dialekt-portabel). Self-Reference und Nicht-Hauptz.-Parent werden in `meter_new`/`meter_edit` serverseitig gekappt + Flash-Warnung
- **MeterReplacement** — explizites **Zählertausch-Event** (alt→neu-Paarung + Snapshot der Tausch-Metadaten); ersetzt die fruehere Datums-Heuristik (alter Zähler `active=False`, `installed_to == neuer.installed_from`), die bei zwei am selben Tag getauschten Zählern nicht aufloesbar war. `property_id` redundant gehalten → Per-Objekt-Abfragen ohne Join.
- **MeterReadingAccessCode** (erbt `EmailTrackableMixin`) — Login-Code fuers SaaS-Selbstablesungs-Portal (`/zaehlerstand`); im OSS definiert, von der SaaS-`self_service`-Schicht genutzt.
- **CustomerCounter** — per-year sequence counter for Kundennummern (analog `InvoiceCounter`).
- **CustomerWgProfile** / **PropertyWgProfile** / **WgFunction** — WG-spezifische 1:1-Profiltabellen (Mitglieds-Status + `member_until`; Anteile + m²; mehrwertige Vorstands-/Prüf-Funktionen), nur im Mandant-Typ Wassergenossenschaft relevant. Der Schalter ist die `AppSetting` `org.type` (`cooperative`/`utility`); der Context-Processor injiziert `is_wg` in alle Templates. Domäne/Regeln in [app/wg.py](app/wg.py).

**Abrechnung (Perioden statt Kalenderjahr):**
- **BillingPeriod** — **zentraler Gruppierungsschluessel** fuer Ablesungen, Zählertausche und Rechnungslaeufe; ersetzt die fruehere Kalenderjahr-Verdrahtung. `start_date`/`end_date` (z.B. Juni–Juni), `name` (z.B. "2025/26"). **Genau eine ist immer aktiv** — applikationsseitig erzwungen (`activate()` setzt alle anderen inaktiv; `BillingPeriod.current()`), kein portabler Partial-Index ueber alle drei Dialekte.
- **WaterTariff** — base_fee + additional_fee + price_per_m3, valid for year range; fee overrides on Customer/Property take priority
- **BillingRun** — **historisierter Massen-Rechnungslauf**: bei jeder Massenabrechnung gespeichert, haelt einen **Snapshot des verwendeten Tarifs** (Kopie aller `tariff_*`-Felder), zaehlt `invoices_created`/`invoices_skipped`, kennt eine `sort_order` fuer die PDF-Reihenfolge. `Invoice.billing_run` verlinkt zurueck.
- **Invoice** (erbt `EmailTrackableMixin`) → linked to Customer + optional Property + optional **BillingRun**; has many **InvoiceItem**; statuses: Entwurf → Versendet → Bezahlt → Storniert / Guthaben; invoice_number format `YYYY-NNNNN` (via `InvoiceCounter`). E-Mail-Versand-Status wird ueber **EmailEvent** getrackt (siehe E-Mail-Tracking).
- **InvoiceCounter** — per-year sequence counter for invoice numbers; auto-seeded from existing invoices if missing
- **Geschätzte Zählerstände + Korrekturposten (v1.28.0):** `MeterReading.is_estimated` und `InvoiceItem.is_estimated` markieren Schätzungen (Badge „geschätzt" in Listen/Detail/PDF). Schätz-Logik in [app/meters/estimation.py](app/meters/estimation.py): `estimate_meter_value` (letzter Stand + Ø-Verbrauch; Bulk-Route `/meters/readings/estimate-missing` + Button im Ablese-Formular). Wird ein echter Stand über `save_reading(is_estimated=False)` auf eine **abgerechnete** Schätzung gespeichert, legt `build_correction` einen vorzeichenbehafteten **`ReadingCorrection`** an (`amount`>0 Nachforderung, <0 Gutschrift; `remaining_amount` für Carry-forward). `apply_corrections_to_invoice` zieht offene Posten beim nächsten Rechnungslauf ein (pro Kunde eine Rechnung) — Gutschrift nur bis Rechnungsbetrag 0, Rest wandert weiter; rundungssicher konsistent mit `Invoice.recalculate_total` (`_item_gross`). Übersicht: `/invoices/corrections`. Der Abgleich sitzt zentral in `save_reading` → greift auch bei Import + SaaS-Self-Service.
- **Account** (Einnahme/Ausgabe-Konto) → has many **Booking**; optional 3-char `code`
- **RealAccount** — real bank account (IBAN, opening balance, Font Awesome `icon`); `is_default` marks the pre-selected account
- **RealAccountYearBalance** — snapshot of a RealAccount balance at fiscal year close
- **Booking** — links Account + optional Invoice/OpenItem/Project/Customer/RealAccount; `amount` positive = Einnahme, negative = Ausgabe; `storno_of_id` enables cancellation chain; statuses: Offen → Verbucht (on fiscal year close) / Storniert
- **Transfer** — direct bank-to-bank transfer between two RealAccounts; not counted in annual report
- **OpenItem** — manually tracked receivable/payable; statuses: Offen → Teilbezahlt → Bezahlt / Gutschrift; settled via Bookings
- **Project** — named cost/revenue center with optional 3-char `code` and `color`; bookings and open items can be assigned
- **TaxRate** — available tax rates (0 %, 10 %, 13 %, 20 % seeded by `init-db`); used on Booking and InvoiceItem
- **BookingGroup** — fasst zusammengehoerige Buchungen (z.B. aus einem Vorgang) zu einer Gruppe zusammen.
- **FiscalYear** — year with start/end dates; `is_vat_liable` markiert USt-pflichtige Jahre (steuert USt-Voranmeldung + den `has_vat_fiscal_year`-Context-Flag); closing locks bookings (Offen → Verbucht) and snapshots RealAccount balances.
- **FiscalYearReopenLog** — Audit-Log fuer das Wieder-Oeffnen eines abgeschlossenen Buchungsjahres.
- **AppSetting** — generic key-value store (`AppSetting.get(key)` / `AppSetting.set(key, value)`); keys `wg.*` for cooperative contact info, `mail.*` for SMTP config.

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

**Leitungsnetz-Modul (`network`, frueher `technik`, OSS v1.12.0):** Wasserleitungsplan als GeoJSON-Annotationen auf einer Leaflet-Karte (basemap.at), bewusst **kein PostGIS** (dialekt-portabel, Geometrie als Text).
- **NetworkPlan** — benannter Plan-Container; erlaubt mehrere parallele Pläne (operativer Hauptplan + Planungs-Sandkasten). Eine Kopie merkt sich `source_plan_id`, sodass `plan_merge` Änderungen zurueckspiegelt; nur Pläne mit `maintenance_enabled` UND `status='aktiv'` treiben die Dashboard-Erinnerung „Fällige Prüfungen".
- **NetworkFeature** — Punkt (Hydrant, Schieber, Quelle, Behaelter, Verteiler, Pumpe, Hausanschluss, Probenahmestelle) oder Linie (Versorgungs-/Haupt-/Ring-/Hausanschlussleitung); `geometry` haelt das GeoJSON-Geometry-Objekt als Text.
- **MaintenanceLog** — Wartungs-/Pruef-Eintraege zu einem Feature (WLK-Import schreibt hier rein).
- **SpringYield** (OSS v1.32.0) — **Quellschüttung**: Schüttungs-Messreihe (Durchfluss in l/s je `measurement_date`) zu einer `NetworkFeature` vom Typ `quelle`. Geschwister-Pattern zu `MaintenanceLog` (gleicher `feature_id`-FK, gleiche Audit-Spalten, ORM-Cascade). Erfassung/Löschen über das Schüttungs-Modal der Elementliste (`yield_add`/`yield_delete`, Route gatet hart auf `feature_type=='quelle'`, Button nur bei Quellen); die **Monitoring-Seite** `network.monitoring` zeichnet je Quelle des aktiven Plans (`current_plan()`) eine Linie (Chart.js 4.4.1 + date-fns-Adapter via `head_extra` + `hx-boost="false"`) plus Trockenheits-Kennzahlen (aktuell, Min 12 Mon., % vom Median). Kein Plan-Gate, kein `maintenance_enabled`-Gate.
- **WaterSample** + **LabResult** (OSS v1.34.0) — **Wasserproben / TWV-Beprobung**: ein Laborbefund (`WaterSample`) je Entnahme an einer `NetworkFeature` vom Typ `probenahme` buendelt mehrere Laborwerte (`LabResult`: Parameter, Wert, Einheit, Grenzwert, Ampel-Status). Geschwister-Pattern zu `SpringYield`. Der TWV-Parameter-Katalog + Bewertung liegt in [app/network/water_quality.py](app/network/water_quality.py) (`PARAMETERS`, `assess`, `effective_limit`); Grenzwerte sind Code-Konstanten, pro Tenant ueber `AppSetting` (`water_quality.<param>.limit`) ueberschreibbar (`/network/water-quality/limits`). `unit`/`limit_text`/`status` werden auf `LabResult` zur Erfassungszeit **eingefroren** (Beleg-Stabilitaet). Erfassung ueber das Wasserprobe-Modal der Elementliste + inline im Karten-Panel (`sample_add`/`sample_delete`, Route gatet hart auf `feature_type=='probenahme'`); die **Wasserqualitaets-Seite** `network.water_quality` zeigt je Stelle den letzten Befund + Gesamt-Ampel, ein Trend-Diagramm (waehlbarer Parameter, `?param=`), CSV-Export und einen **Behoerdenbericht** (WeasyPrint, ImportError-sicher → `water_quality_print.html`-Fallback). Kein Plan-Gate.
- **FeaturePhoto** — Foto-Anhang zu einem Feature; Ablage als Geschwister von `PDF_DIR` (nicht in der DB). Shapefile-/WLK-Import in [app/network/wlk_import.py](app/network/wlk_import.py) (`pyshp` + `pyproj`, GK→WGS84).

**Störungsjournal (`incidents`):**
- **Incident** — Störungs-/Rohrbruch-Eintrag mit Kartenpin (GeoJSON-Point als Text), Ursachenkategorie/Status/Schweregrad, Reparaturkosten/Wasserverlust/betroffene Anschlüsse.
- **IncidentPhoto** — Foto-Anhang; Ablage als Geschwister von `PDF_DIR` (`instance/incidents/`), **nicht** im data_transfer-ZIP (separates FS-Backup noetig).

**Schriftführung (`schriftfuehrung`, nur WG-Modus):**
- **Meeting** — Vorstandssitzung (`board`) oder Hauptversammlung (`assembly`), Lebenszyklus `planning → invited → held`; dazu **MeetingAgendaItem** (Tagesordnung), **MeetingResolution** (Beschlüsse), **MeetingProtocol** (Protokoll), **MeetingAttendance** (Anwesenheit).
- **MeetingInvitation** (erbt `EmailTrackableMixin`) + **MeetingDeliveryLog** — Einladungsversand pro Empfänger + Zustell-Audit.
- **SchriftverkehrDocument** — eigenständiges Korrespondenz-Dokument (eingehend/ausgehend) im Jahr-Archiv; DB hält nur Metadaten, Datei im Schriftverkehr-Ordner (Geschwister von `PDF_DIR`).

Setting invoice status to "Bezahlt" auto-creates a Booking in the first active income account.

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

- **WeasyPrint** (PDF generation, email with PDF) requires GTK3 and only works inside the Docker container. `requirements-dev.txt` excludes it. Routes handle `ImportError` gracefully.
- **Templates** use Tabler 1.0.0 layout (`templates/base.html`) with all assets loaded from CDNs (Font Awesome 5, Tabler, TomSelect).
- **Cooperative identity** (name, address, IBAN, etc.) is configured via `AppSetting` (DB) with `.env` fallback (`WG_NAME`, `WG_ADDRESS`, etc.) and appears on invoices and in all templates.
