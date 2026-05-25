# wasserklar — Wassergenossenschaft Verwaltung

Flask + HTMX Verwaltungssystem für Wassergenossenschaften.
Design: **Tabler 1.0.0** (Bootstrap 5 + Font Awesome)

**Funktionen:** Kundenverwaltung · Zählerablesungen (+ CSV/Excel-Import) · Rechnungsgenerierung (PDF, E-Mail) · Buchhaltung (EÜR, Offene Posten, Jahresbericht)

---

## Lokale Entwicklung

Lokale Flask-Instanz verbindet sich gegen den **Docker-Postgres-Container** — dieselbe Datenbank wie beim echten Deployment, kein separates SQLite nötig.

```bash
# 1. Konfiguration anlegen
cp .env.example .env
#    → POSTGRES_PASSWORD setzen (beliebig, muss nur konsistent sein)
#    → DATABASE_URL anpassen: "change-me" durch dasselbe Passwort ersetzen
#    → WG_NAME, IBAN, SECRET_KEY setzen

# 2. Postgres-Container starten (nur DB, ohne App)
docker compose up -d postgres

# 3. Virtuelle Umgebung + Abhängigkeiten
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
#    (requirements-dev.txt lässt WeasyPrint/GTK weg — läuft auf Windows)

# 4. Datenbank initialisieren
.venv/Scripts/flask --app run init-db

# 5. Ersten Admin-Benutzer anlegen
.venv/Scripts/flask --app run create-admin

# 6. Dev-Server starten (Port aus .env: FLASK_RUN_PORT=5002)
.venv/Scripts/python run.py
#    → http://127.0.0.1:5002
#
# Hinweis: Docker-Container belegt Port 5000. Nativer Flask-Debug läuft
# bewusst auf 5002, damit beide gleichzeitig laufen können ohne Verwechslung.
```

---

## Deployment (Docker)

Ein einziges `docker-compose.yml` bringt Postgres, App und Scheduler hoch.

```bash
# 1. Konfiguration anlegen
cp .env.example .env
#    → FLASK_ENV=production
#    → POSTGRES_PASSWORD (langer, zufälliger Wert!)
#    → DATABASE_URL: "change-me" durch dasselbe Passwort ersetzen
#    → SECRET_KEY (langer, zufälliger Wert!), Mail-Daten anpassen

# 2. Container bauen und starten
docker compose up -d --build

# 3. Datenbank initialisieren (einmalig bei Erstinstallation)
docker compose exec wkoss flask --app run init-db

# 4. Admin-Benutzer anlegen (einmalig bei Erstinstallation)
docker compose exec wkoss flask --app run create-admin
#    → https://deine-domain.at  (oder http://SERVER-IP:5000)
```

**Update auf eine neue Version** (ohne Datenverlust):
```bash
git pull                                              # 1. neuen Code holen
docker compose up -d --build                          # 2. Image neu bauen + Container neu starten
docker compose exec wkoss flask --app run upgrade-db  # 3. Schema-Migrations + Daten-Seeds nachziehen
```

`upgrade-db` ist **idempotent** und deckt alle Fälle ab:

- **Frisch installiert (Alembic-stamped)** → zieht alle neuen Migrations.
- **Pre-Alembic-Bestand** (z.B. erste Installation vor v1.0.0) → ergänzt fehlende Spalten via internem Fallback, stempelt auf die Initial-Revision und zieht danach alle nachfolgenden Migrations regulär durch.

Wenn `upgrade-db` mit `Unknown column …` o.ä. crasht, ist der Alembic-Stempel inkonsistent zur DB. Diagnose und manueller Fix:

```bash
# Zeigt aktuell gestempelte Revision
docker compose exec wkoss flask --app run db current

# Migrationsverlauf
docker compose exec wkoss flask --app run db history

# Auf eine Vorrevision zurücksetzen und nur fehlende Migrations ziehen:
docker compose exec wkoss flask --app run db stamp <vor-revision>
docker compose exec wkoss flask --app run db upgrade <ziel-revision>
```

---

## Automatisierte Tests

Die Tests laufen mit **pytest** gegen eine SQLite-In-Memory-Datenbank — kein laufender Server nötig.

```bash
# Alle Tests ausführen
.venv/Scripts/pytest tests/

# Mit Details (welcher Test läuft)
.venv/Scripts/pytest tests/ -v

# Nur Unit-Tests (keine DB, sehr schnell ~0,5 s)
.venv/Scripts/pytest tests/unit/ -v

# Mit Coverage-Report
.venv/Scripts/pytest tests/ --cov=app --cov-report=term-missing
```

### Teststruktur

| Verzeichnis | Inhalt | DB? |
|-------------|--------|-----|
| `tests/unit/` | Reine Berechnungsfunktionen (Storno-Filter, Split-Logik, MwSt, Quartale) | nein |
| `tests/integration/` | Sammelbuchung, Storno, Kontostand, Rechnungsnummer | ja (SQLite) |
| `tests/http/` | Login-Schutz, Auth-Flow, HTMX-Partials | ja (SQLite) |

---

## CLI-Befehle

Alle Befehle werden mit `flask --app run <befehl>` aufgerufen (lokal: `.venv/Scripts/flask --app run <befehl>`; Docker: `docker compose exec wkoss flask --app run <befehl>`).

### Schema & Initialisierung

| Befehl | Beschreibung |
|--------|-------------|
| `init-db` | Tabellen via Alembic anlegen + Defaults seeden (Steuersätze, Mahnrichtlinie, Abrechnungsperiode). Einmalig bei Erstinstallation. |
| `upgrade-db` | Schema-Migrations auf head ziehen + fehlende Defaults nachseeden. Idempotent — einziges Kommando für Updates. |

### Benutzerverwaltung

| Befehl | Beschreibung |
|--------|-------------|
| `create-admin` | Admin-Benutzer interaktiv anlegen (Benutzername, E-Mail, Passwort per Prompt). |

### Entwicklung & Test

| Befehl | Beschreibung |
|--------|-------------|
| `seed-testdata` | Testdaten einfügen: 6 Kunden, Objekte, Zähler, Ablesungen 2021–2025, 24 Rechnungen, Buchungen, Offene Posten. Läuft nur wenn die DB leer ist (Schutz vor doppeltem Seeden). |
| `clear-db` | Tabellen leeren (Struktur bleibt erhalten). Ohne Flag: Bewegungsdaten; Benutzer und Einstellungen bleiben. Mit `--full`: alles inkl. Benutzer und Einstellungen (Seed-Defaults werden danach neu eingespielt). Bestätigung: `CLEAR`. |
| `reset-db` | **Alle Daten und Tabellen** löschen und DB neu initialisieren. Fragt zur Bestätigung nach dem Wort `RESET`. |

### Betrieb

| Befehl | Beschreibung |
|--------|-------------|
| `mark-posted` | Alle Buchungen mit Status `Offen` und Datum vor heute auf `Verbucht` setzen. Nützlich nach manuellem Jahresabschluss oder Datenimport. |

### Daten-Export / Import

| Befehl | Optionen | Beschreibung |
|--------|----------|-------------|
| `export-data` | `--out <datei.zip>` (Pflicht) | Exportiert alle Tabellen in eine ZIP-Datei (gleiches Format wie der UI-Export unter `/data-transfer`). |
| | `--include stammdaten,buchungen,mahnwesen,einstellungen` | Komma-separierte Kategorien (Standard: alle). |
| | `--years 2023,2024` | Nur Buchungen dieser Jahre exportieren (Standard: alle Jahre). |
| | `--no-pdfs` | PDF-Anhänge nicht mit-bundlen. |
| `import-data` | `--in <datei.zip>` (Pflicht) | Importiert eine zuvor exportierte ZIP-Datei. |
| | `--mode replace\|merge` | `replace`: Vollersatz (Standard). `merge`: Nur fehlende Records einfügen. |
| | `--update-existing` | Im Merge-Modus: bestehende Records aktualisieren. |
| | `--yes` | Bestätigungs-Prompt überspringen (für Scripting). |

---

## Hinweise

- **PDF-Export** benötigt WeasyPrint mit GTK3. Lokal unter Windows entfällt diese Funktion (Fehlermeldung statt Absturz). Im Docker-Container ist WeasyPrint vollständig enthalten.
- **E-Mail-Versand** erfordert einen konfigurierten SMTP-Server in der `.env`-Datei.
- **SECRET_KEY** und **POSTGRES_PASSWORD** müssen für Produktiv-Deployments durch lange, zufällige Werte ersetzt werden.
- Generierte PDFs und Datenbankdaten liegen in `instance/` bzw. im Docker-Volume `postgres_data` (überleben Container-Neustarts).

---

## Lizenz

Copyright © 2026 Alexander Schweiger — dual-lizenziert unter **AGPL-3.0** (Open Source mit Copyleft) und einer separaten kommerziellen Lizenz für die Betreiberin von [wasserklar.at](https://wasserklar.at).

Wer diese Software als Netzwerk-Dienst (SaaS) betreibt, **muss** den eigenen Quellcode unter AGPL-3.0 offenlegen — es sei denn, es liegt eine separate kommerzielle Lizenz vom Copyright-Inhaber vor. Details siehe [NOTICE.md](NOTICE.md) und [LICENSE](LICENSE).
