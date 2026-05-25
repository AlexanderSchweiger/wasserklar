# Sample-Daten

Statische Beispiel-Dateien fuer manuelle Tests des Bank-Imports und anderer
Datei-basierter Importer. **Keine Test-Fixtures** im engeren Sinn — die liegen
unter [`tests/`](../tests/). Diese hier sind fuer den Developer/QA gedacht,
der den UI-Flow ("Bank-Auszug hochladen") nachstellen will.

## bank_statements/

Bank-Auszuege im Format **MT940**, **MT942** und **camt.053** zum Hochladen
unter `/bank-import`. Alle drei Dateien stellen denselben Inhalt dar:

| # | Datum | Gegenpartei | Betrag | Erwartetes Matching |
|---|---|---|---|---|
| 1 | 2025-09-02 | Karl Weidinger | +120,00 | OpenItem "Anschlussgebuehr Gartenzaehler Nachruestung" (Kunde Nr. 6) — voll |
| 2 | 2025-09-03 | Petra Voglhuber | +8,50 | OpenItem "Saeumniszuschlag manuell" (Kunde Nr. 13) — voll |
| 3 | 2025-09-04 | Brigitte Kogler | +218,46 | Rechnung RE-2024-0004 (via Referenz) — voll |
| 4 | 2025-09-07 | Edith Fischer | +200,00 | Rechnung RE-2024-0005 — **Teilzahlung** (Forderung 367,84) |
| 5 | 2025-09-08 | Hildegard Wagner | +163,02 | Rechnung RE-2024-0011 — voll |
| 6 | 2025-09-10 | Monika Leitner | +229,24 | Rechnung RE-2024-0019 — voll |
| 7 | 2025-09-12 | Energie AG Oberoesterreich | +285,40 | **Kein** Matching — fremde Buchung, soll Account-Override verlangen |

Zielkonto: **Girokonto Raika**, IBAN `AT12 3456 7890 1111 0000` (aus dem
Demo-Seed; siehe `flask --app run seed-demo`). Eroeffnungssaldo 8 500,00 €,
Schlusssaldo 9 724,62 €.

### Voraussetzung

Vorher `flask --app run seed-demo --yes` (mit `WASSERKLAR_ALLOW_DEMO_SEED=1`)
ausfuehren — die Open Items und Rechnungsnummern in den Dateien sind exakt
auf den deterministischen Demo-Datensatz abgestimmt.

### Regenerieren

Die drei Dateien werden von [`generate.py`](bank_statements/generate.py)
erzeugt. Bei Aenderung der Demo-Daten (oder anderer Betraege):

```
.venv/Scripts/python sample_data/bank_statements/generate.py
```

Schreibt `giro_2025-09.mt940.sta`, `giro_2025-09.mt942.sta` und
`giro_2025-09.camt053.xml` neu.
