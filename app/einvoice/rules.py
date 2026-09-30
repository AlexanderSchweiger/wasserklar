"""Pruefregeln fuer das EN-16931-Modell — mit Meldungen auf Deutsch.

Kein Nachbau des kompletten EN-/Factur-X-/XRechnung-Schematrons (das prueft der
Mustang-Validator in der Entwicklung, siehe tests/integration/test_einvoice.py),
sondern genau die Regeln, die unsere Daten verletzen koennen — fehlende
Stammdaten des Mandanten oder des Kunden. Leere Liste = gueltig.
"""
import re

from app.einvoice import leitweg
from app.einvoice.model import PROFILE_PEPPOL_UBL, PROFILE_XRECHNUNG

_VAT_PREFIX = re.compile(r"^[A-Z]{2}[A-Z0-9]+$")


def _iban_valid(iban):
    """IBAN-Pruefziffer nach ISO 7064 MOD 97-10 (BR-DE-19 prueft sie in der XRechnung)."""
    iban = re.sub(r"\s+", "", iban or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


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
    # BR-CO-9 auch fuer die USt-IdNr. des Kaeufers
    if buyer.vat_id and not _VAT_PREFIX.match(buyer.vat_id):
        issues.append("Die USt-IdNr./UID des Kunden muss mit dem Länderkürzel beginnen "
                      "(z. B. ATU12345678 oder DE123456789) — bitte im Kundenstamm korrigieren.")
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

    if e.profile == PROFILE_XRECHNUNG:
        issues.extend(_xrechnung_issues(e))
    elif e.profile == PROFILE_PEPPOL_UBL:
        issues.extend(_peppol_issues(e))
    return issues


def _peppol_issues(e):
    """Zusatzpflichten von UBL/Peppol BIS 3.0 fuer die Bundesdienststelle (e-Rechnung.gv.at)."""
    issues = []
    # Peppol kennt „E-Mail" nicht als Adressschema: Verkaeufer und Kaeufer brauchen eine
    # Peppol-registrierte Kennung (Verkaeufer: UID/USt-IdNr. AT 9914 bzw. DE 9930).
    if not e.seller.endpoint:
        issues.append("Die UBL-Rechnung adressiert die Genossenschaft über ihre UID/USt-IdNr. "
                      "(Österreich oder Deutschland) — bitte in den Einstellungen → Kontaktdaten "
                      "eintragen.")
    if not e.buyer.endpoint:
        issues.append("Für die UBL-Rechnung fehlt die elektronische Adresse des Kunden: die "
                      "Peppol-ID der Dienststelle (z. B. 9915:b) oder ihre USt-IdNr. im "
                      "Kundenstamm unter „E-Rechnung“ eintragen.")
    # e-Rechnung.gv.at: Auftragsreferenz (BT-13) und Lieferantennummer (BT-29) sind Pflicht
    if not e.order_reference:
        issues.append("Die Bundesdienststelle verlangt eine Auftragsreferenz (BT-13) — "
                      "im Kundenstamm unter „E-Rechnung“ eintragen.")
    if not e.seller.identifier:
        issues.append("Die Bundesdienststelle verlangt die Lieferantennummer der Genossenschaft "
                      "(BT-29) — im Kundenstamm unter „E-Rechnung“ eintragen.")
    return issues


def _xrechnung_issues(e):
    """Zusatzpflichten der XRechnung 3.0 (BR-DE-*) gegenueber der EN 16931."""
    issues = []
    seller, buyer = e.seller, e.buyer

    # BR-DE-1 / BR-DE-19: Zahlungsanweisung mit gueltiger IBAN
    if e.payment is None:
        issues.append("Die XRechnung braucht eine Zahlungsanweisung: Die IBAN der "
                      "Genossenschaft fehlt (Einstellungen → Kontaktdaten).")
    elif not _iban_valid(e.payment.iban):
        issues.append("Die IBAN der Genossenschaft ist ungültig (Prüfziffer stimmt nicht) — "
                      "bitte in den Einstellungen → Kontaktdaten prüfen.")

    # BR-DE-2 / -5 / -6 / -7: Ansprechpartner des Verkaeufers mit Telefon und E-Mail
    if not seller.contact_name:
        issues.append("Die XRechnung braucht einen Ansprechpartner der Genossenschaft "
                      "(Einstellungen → Rechnung → E-Rechnung).")
    if not seller.contact_phone:
        issues.append("Die XRechnung braucht eine Telefonnummer der Genossenschaft "
                      "(Einstellungen → Kontaktdaten).")
    if not seller.endpoint:
        issues.append("Die XRechnung braucht eine E-Mail-Adresse der Genossenschaft "
                      "(Einstellungen → Kontaktdaten).")

    # BR-DE-8 / -9: Anschrift des Kaeufers mit Ort und PLZ
    if not (buyer.address.postcode and buyer.address.city):
        issues.append("Die XRechnung braucht PLZ und Ort in der Anschrift des Kunden "
                      "(Kundenstamm).")
    # BT-49: elektronische Adresse des Kaeufers
    if not buyer.endpoint:
        issues.append("Für die XRechnung fehlt die elektronische Adresse des Kunden: "
                      "E-Mail-Adresse oder Leitweg-ID im Kundenstamm eintragen.")

    # BR-DE-15: Kaeuferreferenz (Leitweg-ID, sonst Kundennummer)
    if not e.buyer_reference:
        issues.append("Die XRechnung braucht eine Käuferreferenz: Leitweg-ID des Kunden "
                      "(oder zumindest eine Kundennummer) eintragen.")
    elif leitweg.looks_like(e.buyer_reference) and not leitweg.is_valid(e.buyer_reference):
        issues.append(f"Die Leitweg-ID „{e.buyer_reference}“ des Kunden hat eine ungültige "
                      "Prüfziffer — bitte mit der Behörde abgleichen.")
    return issues
