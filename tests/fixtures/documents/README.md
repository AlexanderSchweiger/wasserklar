# Test-Belege (Belegablage, Textauslese)

Erfundene Lieferantenbelege **mit Textebene** für `tests/unit/test_document_extract.py`. Alle
Namen, UIDs und IBANs sind fiktiv (die IBANs haben eine gültige Prüfziffer, aber die Bankleitzahl
99999 gibt es nicht). Sie decken die Layouts ab, an denen die Heuristiken in
`app/documents/extract.py` hängen:

| Datei | Layout | Erwartung |
|---|---|---|
| `tabellenkopf.pdf` | Spaltenköpfe „Rechnungsnummer / Rechnungsdatum“ mit den Werten **darunter**, Positionstabelle mit Spalte „Gesamt“, Skonto-Zeile, eigene UID als Empfänger | Nr. `2026-0123`, Datum 14.03.2026 (nicht Liefer-/Fälligkeitsdatum), brutto 1.234,56 (nicht Skonto-Betrag), UID `ATU12345678` (nicht die eigene `ATU87654321`) |
| `zeilen.pdf` | „Rechnungsnummer: …“ in einer Zeile, Datum „12. Jänner 2026“, „Zu zahlen“ | Nr. `RE-2026-0042`, Datum 12.01.2026, 98,40 |
| `kassenbon.pdf` | Bon (80 mm), „SUMME EUR“, Bar/Rückgeld | Art Quittung, Nr. `004711`, 03.02.2026, 23,45 (nicht 25,00) |
| `gutschrift.pdf` | „Gutschrift Nr. …“, USt-IdNr. DE | Art Gutschrift, `GS-2026-07`, 20.04.2026, 120,00 |
| `invoice-en.pdf` | englisch, Betrag „1,234.50“ | `INV-1001`, 2026-03-05, 1234.50 |

Erzeugt mit Chrome (`--headless=new --print-to-pdf`) aus einfachen HTML-Seiten. Zum Neuerzeugen
das Layout in HTML nachbauen — die Dateien sind klein (je 35–70 KB, eingebettete Schrift).
`.gitignore` lässt sie über `!tests/fixtures/documents/*.pdf` durch.
