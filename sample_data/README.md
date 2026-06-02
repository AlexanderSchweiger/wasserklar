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

## stammdaten_import/

Excel-Dateien zum Ausprobieren der **Stammdaten-Importe** (Kunden / Objekte /
Zaehler). Jede Datei enthaelt **alle Felder**, die der jeweilige Import
unterstuetzt, und ist so aufgebaut, dass das Spalten-Mapping im
Import-Assistenten **automatisch** korrekt erkannt wird. Anders als die
Bank-Dateien sind diese **nicht** an den Demo-Seed gebunden — sie importieren
in eine beliebige (auch leere) Datenbank.

| Datei | Import | Erreichbar ueber |
|---|---|---|
| `kunden_import_beispiel.xlsx`  | Kunden (8 Kontakte)        | Kundenliste → „Importieren" |
| `objekte_import_beispiel.xlsx` | Objekte (6 Liegenschaften) | Objektliste → „Importieren" |
| `zaehler_import_beispiel.xlsx` | Zaehler (7 Wasserzaehler)  | Zaehlerliste → „Importieren" |

**Empfohlene Reihenfolge** (die Dateien verweisen aufeinander):

1. **Kunden** zuerst.
2. **Objekte** danach — Spalte *Besitzer (Kunden-Nr.)* verweist auf die
   Kundennummern `1001`–`1006`.
3. **Zaehler** zuletzt — Spalte *Objekt-Nr.* verweist auf `OBJ-001`–`OBJ-006`.
   Werden die Zaehler vorher importiert, sind die Zeilen mangels Objekt *Fehler*.

Format oesterreichisch: Datum `TT.MM.JJJJ`, Dezimalzahlen mit Komma
(`1250,000`), alle Zellen als Text. Absichtlich enthaltene Sonderfaelle:
Kunden mit *Name* bzw. *Nachname+Vorname*; Objekt-*Typ* leer (→ `Haus`) und
`Stall` (→ `Sonstiges`); ein *Subzaehler* und ein Zaehler mit leerem *Typ*
(→ `Hauptzaehler`). Erklaerung aller Importe:
[`../docs/import-anleitung.html`](../docs/import-anleitung.html).

### Alles-in-einem: `stammdaten_komplett_2025.xlsx`

Zusaetzlich liegt hier `stammdaten_komplett_2025.xlsx` — die **Alles-in-einem-
Variante** fuer den **kombinierten Import** ueber das Menue
*Stammdaten importieren*. Eine breite Tabelle (1 Zeile je Zaehler) legt in einem
Lauf Kunden, Objekte, Zaehler **und** den letzten Zaehlerstand (Spalte
*Stand 2025*) an — Alternative zu den drei Einzeldateien oben. Die fehlende
Abrechnungsperiode 2025 wird dabei automatisch erzeugt. (Hinweis: der kombinierte
Import fasst die Objekt-Typen vereinfacht zusammen — `Garten`/`Stall` → `Sonstiges`,
alles andere → `Haus`.)

## ablesungen_tausch/

Excel-Dateien fuer die **Messwert-Importe** (Ablesungen + Zaehlertausch) als ein
zusammenhaengendes Zwei-Jahres-Szenario. Sie bauen auf den Zaehlern
`ZN-10001`–`ZN-10007` aus den Stammdaten auf — **zuerst die Stammdaten
importieren** (Ordner `stammdaten_import/`).

| Datei | Import | Erreichbar ueber |
|---|---|---|
| `zaehlerablesungen_2025.xlsx` | Ablesungen → Periode 2025 | Menue *Ablesungen importieren* |
| `zaehlertausch_2026.xlsx`     | 3 Zaehlertaeusche im Jahr 2026 | Menue *Zaehlertausch-Import* |
| `zaehlerablesungen_2026.xlsx` | Ablesungen → Periode 2026 | Menue *Ablesungen importieren* |

**Reihenfolge & Story:**

1. **Stammdaten** importieren, damit die Zaehler existieren.
2. Abrechnungsperiode **2025** anlegen, dann `zaehlerablesungen_2025.xlsx`
   importieren (Modus *nach Zaehlernummer*, Periode 2025). Staende auf den
   Originalzaehlern zum 31.12.2025.
3. Abrechnungsperiode **2026** anlegen, dann `zaehlertausch_2026.xlsx`: drei
   Zaehler werden im Lauf 2026 getauscht
   (`ZN-10003`→`ZN-20003`, `ZN-10005`→`ZN-20005`, `ZN-10007`→`ZN-20007`).
4. `zaehlerablesungen_2026.xlsx`: Jahresstaende 2026 — fuer die getauschten
   Objekte auf den **neuen** Zaehlern, fuer die uebrigen auf den Originalen.

Die Ablesungs-Dateien haben die Spalten *Zaehlernummer*, *Zaehlerstand* und
*Ablesedatum*; beim Import jeweils Modus *nach Zaehlernummer* und die passende
Periode waehlen. Wer Schritt 1+2 ueberspringen will, kann stattdessen
`stammdaten_import/stammdaten_komplett_2025.xlsx` nehmen — der enthaelt die
Stammdaten und den Stand 2025 bereits.
