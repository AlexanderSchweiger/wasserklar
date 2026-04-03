# Wassergenossenschaft Verwaltung

Flask + HTMX + SQLite Verwaltungssystem für Wassergenossenschaften.
Design: **AdminLTE 3** (Bootstrap 4 + Font Awesome)

**Funktionen:** Kundenverwaltung · Zählerablesungen (+ CSV/Excel-Import) · Rechnungsgenerierung (PDF, E-Mail) · Buchhaltung (EÜR, Offene Posten, Jahresbericht)

---

## Ersteinrichtung (lokal / Windows)

```bash
# 1. Virtuelle Umgebung erstellen
python -m venv .venv

# 2. Abhängigkeiten installieren (ohne WeasyPrint – kein GTK auf Windows benötigt)
.venv/Scripts/pip install -r requirements-dev.txt

# 3. Konfiguration anlegen
cp .env.example .env
#    → .env anpassen: WG_NAME, IBAN, E-Mail-Server, SECRET_KEY

# 4. Datenbank + Standard-Konten erstellen
.venv/Scripts/flask --app run init-db

# 5. Ersten Admin-Benutzer anlegen
.venv/Scripts/flask --app run create-admin

# 6. Dev-Server starten
.venv/Scripts/flask --app run run
#    → http://127.0.0.1:5000
```

---

## Deployment auf Synology NAS (Docker)

```bash
# 1. Konfiguration anlegen
cp .env.example .env
#    → .env anpassen (FLASK_ENV=production, SECRET_KEY, Mail-Daten)

# 2. Container bauen und starten
docker compose up -d --build

# 3. Datenbank initialisieren (einmalig)
docker compose exec wg flask --app run init-db

# 4. Admin-Benutzer anlegen (einmalig)
docker compose exec wg flask --app run create-admin
#    → http://NAS-IP:5000
```

Die SQLite-Datenbank und generierte PDFs liegen in `instance/` und werden als Docker-Volume gemountet – sie überleben Container-Neustarts und Updates.

---

## CLI-Befehle

| Befehl | Beschreibung |
|--------|-------------|
| `flask --app run init-db` | Tabellen erstellen + Standard-Konten anlegen |
| `flask --app run create-admin` | Admin-Benutzer interaktiv anlegen |
| `flask --app run run` | Entwicklungsserver starten |

---

## Hinweise

- **PDF-Export** benötigt WeasyPrint mit GTK3. Lokal unter Windows entfällt diese Funktion (Fehlermeldung statt Absturz). Im Docker-Container ist WeasyPrint vollständig enthalten.
- **E-Mail-Versand** erfordert einen konfigurierten SMTP-Server in `.env`.
- **SECRET_KEY** in `.env` muss für die Produktion durch einen langen, zufälligen Wert ersetzt werden.
