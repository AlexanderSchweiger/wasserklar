# Wassergenossenschaft Verwaltung

Flask + HTMX Verwaltungssystem für Wassergenossenschaften.
Design: **AdminLTE 3** (Bootstrap 4 + Font Awesome)

**Funktionen:** Kundenverwaltung · Zählerablesungen (+ CSV/Excel-Import) · Rechnungsgenerierung (PDF, E-Mail) · Buchhaltung (EÜR, Offene Posten, Jahresbericht)

---

## Environments

| Environment | FLASK_ENV     | Datenbank              | Deployment              |
|-------------|---------------|------------------------|-------------------------|
| dev         | `development` | `wgbuchhaltung_dev`    | lokal (Flask dev-server)|
| test        | `testing`     | `wgbuchhaltung_test`   | Docker                  |
| prod        | `production`  | `wgbuchhaltung_prod`   | Docker                  |

Jedes Environment hat eine eigene Konfigurationsdatei: `.env`, `.env.test`, `.env.prod`.

---

## Lokale Entwicklung (dev)

```bash
# 1. Virtuelle Umgebung erstellen
python -m venv .venv

# 2. Abhängigkeiten installieren (ohne WeasyPrint – kein GTK auf Windows benötigt)
.venv/Scripts/pip install -r requirements-dev.txt

# 3. Konfiguration anlegen
cp .env.example .env
#    → .env anpassen: DATABASE_URL (wgbuchhaltung_dev), WG_NAME, IBAN, SECRET_KEY
#    → FLASK_ENV=development bleibt gesetzt

# 4. Datenbank + Standard-Konten erstellen
.venv/Scripts/flask --app run init-db

# 5. Ersten Admin-Benutzer anlegen
.venv/Scripts/flask --app run create-admin

# 6. Dev-Server starten
.venv/Scripts/flask --app run run
#    → http://127.0.0.1:5000
```

---

## Test-Deployment (Docker)

Verbindet sich mit `wgbuchhaltung_test`. Läuft als produktionsähnliche Umgebung (DEBUG=False).

```bash
# 1. Konfiguration anlegen
cp .env.example .env.test
#    → FLASK_ENV=testing
#    → DATABASE_URL auf wgbuchhaltung_test setzen
#    → SECRET_KEY, Mail-Daten anpassen

# 2. Container bauen und starten
docker compose -f docker-compose.test.yml up -d --build

# 3. Datenbank initialisieren (einmalig)
docker compose -f docker-compose.test.yml exec wg flask --app run init-db

# 4. Admin-Benutzer anlegen (einmalig)
docker compose -f docker-compose.test.yml exec wg flask --app run create-admin
#    → http://SERVER-IP:5000
```

---

## Produktions-Deployment (Docker)

Verbindet sich mit `wgbuchhaltung_prod`. Vollständige Produktionskonfiguration (DEBUG=False).

```bash
# 1. Konfiguration anlegen
cp .env.example .env.prod
#    → FLASK_ENV=production
#    → DATABASE_URL auf wgbuchhaltung_prod setzen
#    → SECRET_KEY (langer, zufälliger Wert!), Mail-Daten anpassen

# 2. Container bauen und starten
docker compose -f docker-compose.prod.yml up -d --build

# 3. Datenbank initialisieren (einmalig bei Erstinstallation)
docker compose -f docker-compose.prod.yml exec wg flask --app run init-db

# 4. Admin-Benutzer anlegen (einmalig bei Erstinstallation)
docker compose -f docker-compose.prod.yml exec wg flask --app run create-admin
#    → https://deine-domain.at  (oder http://SERVER-IP:5000)

# Update (ohne Datenverlust):
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec wg flask --app run upgrade-db
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

| Befehl | Beschreibung |
|--------|-------------|
| `flask --app run init-db` | Tabellen erstellen + Standard-Konten anlegen |
| `flask --app run upgrade-db` | Neue Spalten hinzufügen (idempotent, für Updates) |
| `flask --app run create-admin` | Admin-Benutzer interaktiv anlegen |
| `flask --app run run` | Entwicklungsserver starten |

---

## Hinweise

- **PDF-Export** benötigt WeasyPrint mit GTK3. Lokal unter Windows entfällt diese Funktion (Fehlermeldung statt Absturz). Im Docker-Container ist WeasyPrint vollständig enthalten.
- **E-Mail-Versand** erfordert einen konfigurierten SMTP-Server in der jeweiligen `.env`-Datei.
- **SECRET_KEY** muss in `.env.test` und `.env.prod` durch einen langen, zufälligen Wert ersetzt werden.
- Generierte PDFs und Datenbankdaten liegen in `instance/` (als Docker-Volume gemountet — überleben Container-Neustarts).
- **Test und Prod gleichzeitig** können auf demselben Host betrieben werden: Test läuft auf Port **5001**, Prod auf Port **5000**. Die Docker-Projektnamen (`wg-test` / `wg-prod`) verhindern Container-Namenskonflikte.
