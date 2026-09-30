"""Pruefregeln fuer das EN-16931-Modell — mit Meldungen auf Deutsch.

Kein Nachbau des kompletten EN-/Factur-X-Schematrons (das prueft der
Mustang-Validator in der Entwicklung, siehe tests/integration/test_einvoice.py),
sondern genau die Regeln, die unsere Daten verletzen koennen — fehlende
Stammdaten des Mandanten oder des Kunden. Leere Liste = gueltig.
"""
import re

_VAT_PREFIX = re.compile(r"^[A-Z]{2}[A-Z0-9]+$")


def check(e):
    """Liste deutscher Meldungen; leer, wenn die E-Rechnung erzeugt werden kann."""
    issues = list(e.mapping_errors)
    seller, buyer = e.seller, e.buyer

    if not seller.name:
        issues.append("Der Name der Genossenschaft fehlt (Einstellungen → Kontaktdaten).")
    if not (seller.address.postcode and seller.address.city):
        issues.append("PLZ und Ort der Genossenschaft fehlen "
                      "(Einstellungen → Rechnung → E-Rechnung).")
    # BR-CO-26: Verkaeuferkennung (BT-29/30/31)
    if not (seller.vat_id or seller.legal_id):
        issues.append("Es fehlt eine Kennung der Genossenschaft: UID/USt-IdNr. oder "
                      "Register-/Firmenbuchnummer (Einstellungen → Rechnung → E-Rechnung).")
    # BR-S-02 / BR-E-02: steuerliche Registrierung des Verkaeufers
    if e.lines and not (seller.vat_id or seller.tax_number):
        issues.append("UID/USt-IdNr. oder Steuernummer der Genossenschaft fehlt "
                      "(Einstellungen → Kontaktdaten → Steuerliche Angaben).")
    # BR-CO-9: USt-IdNr. mit Laenderpraefix
    if seller.vat_id and not _VAT_PREFIX.match(seller.vat_id):
        issues.append("Die UID/USt-IdNr. muss mit dem Länderkürzel beginnen "
                      "(z. B. ATU12345678 oder DE123456789).")

    if not buyer.name:
        issues.append("Der Name des Kunden fehlt.")
    # BR-11: Land des Kaeufers
    if not buyer.address.country_code:
        issues.append("Das Land in der Anschrift des Kunden ist unbekannt — bitte im "
                      "Kundenstamm korrigieren (z. B. „Österreich“ oder „Deutschland“).")

    # BR-16 / BR-25
    if not e.lines:
        issues.append("Die Rechnung hat keine Positionen.")
    for line in e.lines:
        if not line.name:
            issues.append(f"Position {line.line_id} hat keine Bezeichnung.")
    return issues
