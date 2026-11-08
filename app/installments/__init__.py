"""Teilrechnungen (Akonto/Abschlag) mit Jahresabrechnung und Gegenverrechnung.

Regeln an einer Stelle:

* ``schedule``   — Faelligkeiten aus der Einstellung auf eine Periode abbilden.
* ``amounts``    — Teilbetrag runden, brutto cent-genau auf Gruppen verteilen.
* ``plans``      — Teilbetragsplaene berechnen/speichern (Tarif-Engine).
* ``invoicing``  — Teilrechnungen aus Plan-Eintraegen erstellen.
* ``settlement`` — Gegenverrechnung in Jahres-/Schlussrechnung, Guthaben.
* ``terms``      — Einstellungen + Beschriftungen.

Die Funktionen flushen, committen aber nie — der Aufrufer entscheidet.
"""
