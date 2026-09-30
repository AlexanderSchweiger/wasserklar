"""E-Rechnungs-Pflicht (Deutschland, B2B) und Versandweg.

``required_for`` beantwortet: Muss diese Rechnung nach deutschem Recht eine
E-Rechnung sein? Dann darf sie nicht mehr per Post oder als reines PDF zugestellt
werden (E_RECHNUNG_PLAN.md § 1.1). Alle Leitplanken fragen hier — Rechnungslauf
(Post-Versand), Statuswechsel, Rechnungsdetail, Kundenliste und Dashboard — damit
die Regel an genau einer Stelle steht.

Die Pflicht hat vier Voraussetzungen, alle zugleich:

* Mandant in Deutschland (AT: keine Inlandspflicht),
* Kunde ist als Unternehmer gekennzeichnet (``Customer.is_business``),
* der Mandant ist im Jahr der Rechnung umsatzsteuerpflichtig (Kleinunternehmer
  sind dauerhaft nicht ausstellungspflichtig, § 34a UStDV) und der Betrag liegt
  ueber 250 € brutto (Kleinbetragsrechnung),
* die Leistung endet am Stichtag oder spaeter (``einvoice.mandate_from``; § 27
  Abs. 38 UStG stellt auf den Leistungszeitpunkt ab, nicht auf das Rechnungsdatum).

Der Stichtag ist ein Setting: 1.1.2028 gilt fuer alle, 1.1.2027 fuer Mandanten mit
mehr als 800.000 € Gesamtumsatz im Vorjahr. Eine politische Verschiebung aendert
damit nur Daten.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import or_

from app import country
from app.einvoice.model import FORMAT_PEPPOL_UBL, FORMAT_XRECHNUNG, XML_ONLY_FORMATS
from app.extensions import db
from app.models import AppSetting, Customer, CustomerEmailConsentLog, Invoice

MANDATE_KEY = "einvoice.mandate_from"
MANDATE_ALL = date(2028, 1, 1)             # Pflicht fuer alle
MANDATE_LARGE_TURNOVER = date(2027, 1, 1)  # Vorjahresumsatz > 800.000 €
MANDATE_OPTIONS = (MANDATE_ALL, MANDATE_LARGE_TURNOVER)
SMALL_INVOICE_LIMIT = Decimal("250")       # Kleinbetragsrechnung, brutto
CONSENT_TEXT_VERSION = "einvoice-b2b"

REASON_REQUIRED = "Pflicht-E-Rechnung an Unternehmer"
REASON_XRECHNUNG = "XRechnung (Behörde)"
REASON_PEPPOL = "UBL-Rechnung (Bundesdienststelle)"
# Gruende, die nur ein XML (kein Ausdruck) zulassen — die Seite bietet dort den XML-Download an.
XML_ONLY_REASONS = (REASON_XRECHNUNG, REASON_PEPPOL)

# last_email_status-Werte, die „nicht zugestellt" bedeuten (ein Soft-Bounce ist nur
# verzoegert und zaehlt noch nicht).
_UNDELIVERED = (
    Invoice.EMAIL_STATUS_BOUNCED_HARD, Invoice.EMAIL_STATUS_SPAM, Invoice.EMAIL_STATUS_FAILED,
)


def applies():
    """Die deutsche B2B-Pflicht gilt nur fuer Mandanten in Deutschland."""
    return country.current_code() == country.COUNTRY_DE


def mandate_from():
    """Stichtag der Pflicht (Setting ``einvoice.mandate_from``, Standard 1.1.2028)."""
    raw = (AppSetting.get(MANDATE_KEY) or "").strip()
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return MANDATE_ALL
    return value if value in MANDATE_OPTIONS else MANDATE_ALL


def required_for(invoice):
    """True, wenn diese Rechnung eine E-Rechnung sein muss (Post/PDF ist dann unzulaessig)."""
    customer = invoice.customer
    if not applies() or customer is None or not customer.is_business:
        return False
    if invoice.status == Invoice.STATUS_CANCELLED or invoice.date is None:
        return False
    from app.accounting.services import is_year_vat_liable
    from app.invoices.services import service_period
    if not is_year_vat_liable(invoice.date.year):
        return False
    if abs(Decimal(str(invoice.total_amount or 0))) <= SMALL_INVOICE_LIMIT:
        return False
    period = service_period(invoice)
    served = period[1] if period else invoice.date
    return served >= mandate_from()


def electronic_reason(invoice):
    """Grund, warum die Rechnung nur elektronisch zugestellt werden darf, sonst ``None``.

    Neben der gesetzlichen Pflicht gilt das fuer jeden Kunden, der eine XRechnung oder
    UBL-Rechnung verlangt: Ein Ausdruck ist dort nicht der Beleg.
    """
    from app.einvoice import service
    customer = invoice.customer
    if customer is not None and customer.einvoice_format in XML_ONLY_FORMATS \
            and service.is_enabled():
        return REASON_PEPPOL if customer.einvoice_format == FORMAT_PEPPOL_UBL else REASON_XRECHNUNG
    if required_for(invoice):
        return REASON_REQUIRED
    return None


def split_post_invoices(invoices):
    """Teilt Rechnungen fuer den Post-Versand in ``(druckbar, gesperrt)``.

    Gesperrt sind Entwuerfe, die nur elektronisch zugestellt werden duerfen: Der
    Post-Versand setzt sie auf „Versendet", obwohl der Kunde nichts Zulaessiges
    bekommen haette. Bereits versendete Rechnungen bleiben druckbar (Nachdruck).
    """
    printable, blocked = [], []
    for invoice in invoices:
        if invoice.status == Invoice.STATUS_DRAFT and electronic_reason(invoice):
            blocked.append(invoice)
        else:
            printable.append(invoice)
    return printable, blocked


def delivery_problem(invoice):
    """Hinweis ``(Stufe, Text)`` zur Zustellung einer Pflicht-E-Rechnung, sonst ``None``.

    Das Sendeprotokoll ist der ``EmailEvent``-Verlauf der Rechnung (BMF 15.10.2024:
    wer die E-Rechnung ausgestellt und sich nachweislich um die Uebermittlung
    bemueht hat, hat seine Pflicht erfuellt).
    """
    if invoice.status == Invoice.STATUS_DRAFT or not required_for(invoice):
        return None
    if invoice.last_email_status in _UNDELIVERED:
        return ("danger", "E-Rechnung nicht zugestellt: Die E-Mail an "
                f"{invoice.email_recipient or 'den Kunden'} ist nicht angekommen "
                f"({invoice.last_email_status_de}). Bitte die Adresse im Kundenstamm prüfen "
                "und die Rechnung erneut versenden — Post ist hier nicht zulässig.")
    if not invoice.email_sent_at:
        return ("warning", "Pflicht-E-Rechnung noch nicht elektronisch zugestellt: Der Kunde ist "
                "Unternehmer, Papier oder ein reines PDF genügen nicht. Bitte per E-Mail "
                "versenden.")
    return None


def no_email_clause():
    """SQL-Bedingung: Kunde kann nicht per E-Mail bedient werden (``not wants_email``)."""
    return or_(Customer.email.is_(None), Customer.email == "",
               Customer.rechnung_per_email.is_(False))


def set_business(customer, value):
    """Setzt das Unternehmer-Kennzeichen.

    Wird es in Deutschland neu eingeschaltet und der Kunde hat eine E-Mail-Adresse,
    wird „Schriftverkehr per E-Mail" mit eingeschaltet — das Kunden-Mail-Gate
    (``Customer.wants_email``) bleibt die eine Wahrheit. Das Protokoll haelt fest,
    dass der Mandant das getan hat (keine Einwilligung des Kunden: die braucht eine
    B2B-E-Rechnung in Deutschland nicht; in AT bleibt der Opt-in unberuehrt).
    """
    was = bool(customer.is_business)
    customer.is_business = bool(value)
    if not value or was or not applies():
        return
    email = (customer.email or "").strip()
    if email and not customer.rechnung_per_email:
        customer.rechnung_per_email = True
        db.session.add(CustomerEmailConsentLog(
            customer=customer, action=CustomerEmailConsentLog.EINVOICE_B2B, email=email,
            consent_text_version=CONSENT_TEXT_VERSION))


def business_warning(customer):
    """Hinweistext, wenn ein Unternehmer-Kunde keine E-Mail bekommen kann, sonst ``None``."""
    if not customer.is_business or not applies() or customer.wants_email:
        return None
    return (f"„{customer.name}“ ist als Unternehmer gekennzeichnet, kann aber keine E-Mail "
            "bekommen (E-Mail-Adresse oder „Schriftverkehr per E-Mail“ fehlt). Eine E-Rechnung "
            "darf nach der Übergangszeit nur elektronisch zugestellt werden — Post oder ein "
            "reines PDF genügen nicht.")


def dashboard_summary():
    """Zahlen fuer die Dashboard-Karte, ``None`` wenn sie nicht erscheint.

    Nur fuer deutsche Mandanten, die im laufenden Jahr umsatzsteuerpflichtig sind
    (Kleinunternehmer sind nicht ausstellungspflichtig).
    """
    if not applies():
        return None
    from app.accounting.services import is_year_vat_liable
    if not is_year_vat_liable(date.today().year):
        return None
    base = Customer.query.filter(Customer.active.is_(True), Customer.is_customer.is_(True),
                                 Customer.is_business.is_(True))
    return {
        "mandate_from": mandate_from(),
        "business": base.count(),
        "no_email": base.filter(no_email_clause()).count(),
    }
