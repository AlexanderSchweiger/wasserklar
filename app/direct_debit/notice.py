"""Zahlungsart einer Rechnung und der gedruckte Lastschrift-Hinweis.

Eine Rechnung an einen Kontakt mit Bankeinzug zeigt statt der
Zahlungsaufforderung („Wir ersuchen Sie … einzuzahlen“) einen Hinweis, dass der
Betrag abgebucht wird — mit Betrag, Datum, Glaeubiger-ID und Mandatsreferenz.
So ist die Rechnung zugleich die SEPA-Vorabankuendigung (Pre-Notification).

* **Entwurf:** Zahlungsart live vom Kontakt.
* **Ab „Versendet“:** eingefroren an der Rechnung (``freeze_payment``, aufgerufen
  von ``invoices.services.create_or_update_open_item`` — der einen Stelle, die
  jeder Versandweg durchlaeuft). Altbestand ohne Snapshot gilt als Ueberweisung:
  so wurde er auch gedruckt.

Gutschriften und Belege ueber 0 € oder weniger sind nie eine Lastschrift.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.banking import (
    creditor_id_valid, iban_is_sepa, iban_valid, mask_iban, normalize_creditor_id,
    normalize_iban, sepa_id_valid,
)
from app.models import AppSetting, Customer, Invoice

CREDITOR_ID_KEY = "sepa.creditor_id"
MANDATE_PREFIX_KEY = "sepa.mandate_prefix"
NOTICE_TITLE_KEY = "sepa.notice_title"
NOTICE_TEXT_KEY = "sepa.notice_text"
SETTING_KEYS = (CREDITOR_ID_KEY, MANDATE_PREFIX_KEY, NOTICE_TITLE_KEY, NOTICE_TEXT_KEY)

# Fuer die Platzhalter-Hilfe unter dem Textfeld (Einstellungen).
NOTICE_PLACEHOLDERS = [
    ("betrag", "Rechnungsbetrag, z. B. „123,45 €“"),
    ("abbuchungstermin", "„am 15.11.2026“ (ohne Fälligkeit: „zum Fälligkeitstermin“)"),
    ("abbuchungsdatum", "nur das Datum (TT.MM.JJJJ)"),
    ("iban", "IBAN des Kunden, gekürzt (AT61 •••• … 3201)"),
    ("kontoinhaber", "Kontoinhaber"),
    ("mandatsreferenz", "Mandatsreferenz"),
    ("glaeubiger_id", "Gläubiger-ID der Genossenschaft"),
    ("rechnungsnummer", "Rechnungsnummer"),
    ("wg_name", "Name der Genossenschaft"),
]


@dataclass
class DebitInfo:
    """Lastschrift-Angaben einer Rechnung (Snapshot oder live vom Kontakt)."""
    mandate_reference: str | None
    iban: str | None
    holder: str
    collection_date: date | None


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

def creditor_id():
    """Glaeubiger-ID der Genossenschaft (normalisiert, leer = nicht hinterlegt)."""
    return normalize_creditor_id(AppSetting.get(CREDITOR_ID_KEY))


def mandate_prefix():
    return (AppSetting.get(MANDATE_PREFIX_KEY) or "").strip()


def suggested_mandate_reference(customer):
    """Vorschlag fuer eine neue Mandatsreferenz: Praefix + Kunden-/Mitgliedsnummer."""
    if not customer.customer_number:
        return None
    return f"{mandate_prefix()}{customer.customer_number}"[:35]


def mandate_reference_hint(customer):
    """Platzhalter fuers Formularfeld: der Vorschlag oder eine Erklaerung."""
    ref = suggested_mandate_reference(customer) if customer is not None else None
    if ref:
        return f"automatisch: {ref}"
    prefix = mandate_prefix()
    return f"automatisch: {prefix}<Kundennummer>" if prefix else "automatisch: Kundennummer"


def mandate_reference_taken(reference, exclude_id=None):
    """True, wenn ein anderer Kontakt diese Mandatsreferenz schon traegt."""
    from app.extensions import db
    query = db.session.query(Customer.id).filter(Customer.mandate_reference == reference)
    if exclude_id is not None:
        query = query.filter(Customer.id != exclude_id)
    return query.first() is not None


def ensure_mandate_reference(customer):
    """Vergibt bei Bankeinzug ohne Referenz Praefix + Kundennummer (eindeutig, ggf.
    mit Zaehler). Ohne Kundennummer bleibt das Feld leer (Kontakt gilt dann als
    unvollstaendig). Kein commit."""
    if not customer.is_direct_debit or customer.mandate_reference:
        return
    base = suggested_mandate_reference(customer)
    if not base or not sepa_id_valid(base):
        return
    candidate, n = base, 1
    while mandate_reference_taken(candidate, exclude_id=customer.id):
        n += 1
        candidate = f"{base[:32]}-{n}"
    customer.mandate_reference = candidate


# ---------------------------------------------------------------------------
# Kontakt
# ---------------------------------------------------------------------------

def customer_debit_issues(customer):
    """Was fuer einen Bankeinzug fehlt oder ungueltig ist (deutsche Meldungen).

    Leere Liste = der Kontakt kann eingezogen werden. Fuer Kontakte mit
    Ueberweisung immer leer.
    """
    if not customer.is_direct_debit:
        return []
    issues = []
    if not customer.iban:
        issues.append("IBAN fehlt")
    elif not iban_valid(customer.iban):
        issues.append("IBAN ungültig")
    elif not iban_is_sepa(customer.iban):
        issues.append("IBAN liegt außerhalb des SEPA-Raums")
    if not customer.mandate_reference:
        issues.append("Mandatsreferenz fehlt")
    elif not sepa_id_valid(customer.mandate_reference):
        issues.append("Mandatsreferenz enthält unzulässige Zeichen")
    if not customer.mandate_signed_on:
        issues.append("Datum der Mandatsunterschrift fehlt")
    return issues


def incomplete_clause():
    """SQL-Bedingung „Bankeinzug, aber Angaben fehlen“ (IBAN/Mandat/Datum leer).

    Die Pruefziffer kann SQL nicht rechnen — eine formal falsche IBAN laesst das
    Formular aber gar nicht zu, der Import verwirft sie.
    """
    from sqlalchemy import or_
    return (Customer.payment_method == Customer.PAYMENT_DIRECT_DEBIT) & or_(
        Customer.iban.is_(None), Customer.iban == "",
        Customer.mandate_reference.is_(None), Customer.mandate_reference == "",
        Customer.mandate_signed_on.is_(None),
    )


# ---------------------------------------------------------------------------
# Rechnung
# ---------------------------------------------------------------------------

def _never_debit(invoice):
    return invoice.is_credit_note or Decimal(str(invoice.total_amount or 0)) <= 0


def debit_info(invoice):
    """``DebitInfo`` fuer eine Rechnung, die per Lastschrift eingezogen wird, sonst ``None``."""
    if invoice is None or _never_debit(invoice):
        return None
    customer = invoice.customer
    if invoice.status != Invoice.STATUS_DRAFT:
        if invoice.payment_method != Customer.PAYMENT_DIRECT_DEBIT:
            return None
        return DebitInfo(
            mandate_reference=invoice.debit_mandate_reference,
            iban=invoice.debit_iban,
            holder=customer.debit_holder if customer else "",
            collection_date=invoice.due_date,
        )
    if customer is None or not customer.is_direct_debit:
        return None
    return DebitInfo(
        mandate_reference=customer.mandate_reference,
        iban=normalize_iban(customer.iban) or None,
        holder=customer.debit_holder,
        collection_date=invoice.due_date,
    )


def is_debit_invoice(invoice):
    return debit_info(invoice) is not None


def freeze_payment(invoice):
    """Friert Zahlungsart + Mandat ein, sobald die Rechnung den Entwurf verlaesst.

    Idempotent: ein vorhandener Snapshot bleibt (auch nach Stammdatenaenderung).
    Kein commit.
    """
    if invoice.payment_method is not None:
        return
    customer = invoice.customer
    if _never_debit(invoice) or customer is None or not customer.is_direct_debit:
        invoice.payment_method = Customer.PAYMENT_TRANSFER
        return
    invoice.payment_method = Customer.PAYMENT_DIRECT_DEBIT
    invoice.debit_mandate_reference = customer.mandate_reference
    invoice.debit_iban = normalize_iban(customer.iban) or None


def _money(value):
    v = Decimal(str(value or 0))
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def notice_context(invoice, info=None):
    """Platzhalter-Werte fuer den Hinweistext (siehe ``NOTICE_PLACEHOLDERS``)."""
    from app.settings_service import get_wg
    info = info or debit_info(invoice)
    when = info.collection_date if info else None
    return {
        "betrag": _money(invoice.total_amount),
        "abbuchungsdatum": when.strftime("%d.%m.%Y") if when else "",
        "abbuchungstermin": f"am {when.strftime('%d.%m.%Y')}" if when else "zum Fälligkeitstermin",
        "iban": mask_iban(info.iban) if info and info.iban else "",
        "kontoinhaber": info.holder if info else "",
        "mandatsreferenz": (info.mandate_reference if info else "") or "",
        "glaeubiger_id": creditor_id(),
        "rechnungsnummer": invoice.invoice_number or "",
        "wg_name": get_wg("name") or "",
    }


def render_notice_text(template, context):
    """Rendert den (vom Mandanten gepflegten) Hinweistext in der Jinja-Sandbox.

    Ein normales ``Environment`` waere RCE (siehe Mahnwesen). Wirft bei
    Syntaxfehlern — die Einstellungsseite nutzt das zur Pruefung beim Speichern.
    """
    from jinja2.sandbox import SandboxedEnvironment
    return SandboxedEnvironment(autoescape=False).from_string(template or "").render(**context)


def default_title():
    from app.country import term
    return term("debit_title")


def default_text():
    from app.country import term
    return term("debit_text")


def debit_notice(invoice):
    """Inhalt des Lastschrift-Hinweises fuer PDF/Word/SaaS-Designs, sonst ``None``.

    ``{"title", "text", "creditor_id", "mandate_reference", "iban_masked",
    "collection_date"}``. Ein kaputter eigener Text faellt still auf den
    Laender-Standard zurueck — die Rechnung darf daran nie scheitern.
    """
    info = debit_info(invoice)
    if info is None:
        return None
    ctx = notice_context(invoice, info)
    custom = (AppSetting.get(NOTICE_TEXT_KEY) or "").strip()
    try:
        text = render_notice_text(custom or default_text(), ctx)
    except Exception:
        text = render_notice_text(default_text(), ctx)
    return {
        "title": (AppSetting.get(NOTICE_TITLE_KEY) or "").strip() or default_title(),
        "text": text.strip(),
        "creditor_id": ctx["glaeubiger_id"],
        "mandate_reference": ctx["mandatsreferenz"],
        "iban_masked": ctx["iban"],
        "collection_date": info.collection_date,
    }


def creditor_id_issue(value):
    """Meldung fuer eine ungueltige Glaeubiger-ID (leer ist erlaubt)."""
    value = normalize_creditor_id(value)
    if value and not creditor_id_valid(value):
        return ("Die Gläubiger-ID ist ungültig (Format z. B. AT61ZZZ01234567890, "
                "Prüfziffer stimmt nicht).")
    return None
