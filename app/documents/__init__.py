"""Belegablage: Dateien (Rechnungen, Kassenbons, Kontoauszuege, Scans) an Buchungen.

``storage``  — Ablage im Dateibaum des Mandanten, relative Schluessel
``service``  — Upload, Verknuepfung mit Buchungen, Regeln, Aufbewahrung

Die Routen liegen im ``accounting``-Blueprint (``app/accounting/documents.py``, Recht
``buchhaltung``). Regeln und Begruendungen: CLAUDE.md, Abschnitt „Belegablage".
"""
