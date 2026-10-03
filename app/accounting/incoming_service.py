"""Eingangs-E-Rechnungen: Lieferanten-Zuordnung und Buchungsvorschlag.

Die Datei selbst (unveraendert), der Arbeitsstand und die Verknuepfung mit Buchungen
gehoeren zur Belegablage (``app/documents``): ein ``Document`` mit ``Document.einvoice``
(``IncomingInvoice`` = die aus dem Original gelesenen Daten). Hier steht, was nur fuer
E-Rechnungen gilt: Lieferant finden/anlegen und der Buchungsvorschlag. Aus dem Beleg
entsteht eine Ausgabenbuchung je Steuersatz (Brutto, mit Steuersatz — wie die Buchungen
aus Ausgangsrechnungen): ein Steuersatz → Einzelbuchung, mehrere → Sammelbuchung
(ADR-002). Gutschriften (Typ 381) buchen positiv.
"""
from collections import OrderedDict
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app import country
from app.accounting import services as acc_svc
from app.documents import service as documents_svc
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Document, IncomingInvoice, Project, RealAccount,
)

ROUNDING_TOLERANCE = Decimal("0.05")


class BookingError(Exception):
    """Der Buchungsvorschlag laesst sich nicht buchen; die Meldung ist fuer den Nutzer."""


def possible_duplicates(doc):
    """Andere Belege desselben Lieferanten mit derselben Rechnungsnummer (Hinweis, kein Verbot)."""
    inv = doc.einvoice
    if inv is None or not inv.number:
        return []
    query = (Document.query.join(IncomingInvoice, IncomingInvoice.document_id == Document.id)
             .filter(Document.id != doc.id, IncomingInvoice.number == inv.number,
                     Document.status != Document.STATUS_DISCARDED))
    if inv.seller_vat_id:
        query = query.filter(IncomingInvoice.seller_vat_id == inv.seller_vat_id)
    else:
        query = query.filter(func.lower(IncomingInvoice.seller_name) == (inv.seller_name or "").lower())
    return query.all()


# ---------------------------------------------------------------------------
# Lieferant
# ---------------------------------------------------------------------------

def suggest_supplier(parsed):
    """Bekannten Lieferanten finden: zuerst ueber die USt-IdNr., dann ueber den Namen."""
    suppliers = Customer.query.filter(Customer.is_supplier.is_(True), Customer.active.is_(True))
    if parsed.seller.vat_id:
        match = suppliers.filter(Customer.vat_id == parsed.seller.vat_id).first()
        if match is not None:
            return match
    name = (parsed.seller.name or "").strip().lower()
    if name:
        return suppliers.filter(func.lower(Customer.name) == name).first()
    return None


def _country_name(code):
    profile = country.PROFILES.get((code or "").upper())
    return profile.name if profile else (code or "")


def create_supplier(parsed):
    """Legt aus den Verkaeuferdaten der Rechnung einen Lieferanten an (noch nicht committet)."""
    seller = parsed.seller
    supplier = Customer(
        name=seller.name[:200] or "Unbekannter Lieferant", is_company=True, is_customer=False,
        is_supplier=True, active=True, strasse=seller.street[:200] or None,
        plz=seller.postcode[:10] or None, ort=seller.city[:100] or None,
        land=_country_name(seller.country) or None, email=seller.email, phone=seller.phone,
        vat_id=seller.vat_id)
    db.session.add(supplier)
    db.session.flush()
    return supplier


# ---------------------------------------------------------------------------
# Buchungsvorschlag
# ---------------------------------------------------------------------------

def booking_rows(parsed):
    """Buchungszeilen je Steuersatz: ``[{"rate", "gross", "label"}]``.

    ``gross`` hat das Buchungsvorzeichen (Ausgabe negativ, Gutschrift positiv). Zeilen
    mit demselben Satz (z. B. steuerfrei und 0 %) werden zusammengefasst. Eine
    Rundungsdifferenz bis 5 Cent zum Gesamtbetrag geht in die erste Zeile; eine groessere
    Abweichung ist ein Fehler — die Datei ist dann in sich widerspruechlich.
    """
    sign = Decimal("1") if parsed.is_credit_note else Decimal("-1")
    grouped = OrderedDict()
    for vat in parsed.vat:
        rate = vat.rate if vat.rate and vat.rate > 0 else Decimal("0")
        grouped[rate] = grouped.get(rate, Decimal("0")) + vat.taxable + vat.tax
    if not grouped:
        grouped[Decimal("0")] = parsed.grand_total
    rows = [{"rate": rate, "gross": sign * gross,
             "label": f"{rate.normalize():f} % USt" if rate > 0 else "ohne USt"}
            for rate, gross in grouped.items()]
    difference = sign * parsed.grand_total - sum((r["gross"] for r in rows), Decimal("0"))
    if difference:
        if abs(difference) > ROUNDING_TOLERANCE:
            raise BookingError(
                f"Die Steuerbeträge der Datei ergeben {abs(sum((r['gross'] for r in rows), Decimal('0')))} €, "
                f"der Gesamtbetrag lautet {parsed.grand_total} €. Bitte den Beleg beim Lieferanten klären "
                "und manuell buchen.")
        rows[0]["gross"] += difference
    return rows


def default_booking_date(parsed):
    """Rechnungsdatum, nie in der Zukunft (Buchungen duerfen nicht vordatiert werden)."""
    today = date.today()
    return min(parsed.issue_date, today) if parsed.issue_date else today


def book(doc, *, supplier, account_id, booking_date, user_id, project_id=None, real_account_id=None):
    """Bucht den Beleg. Gibt die ``Booking`` bzw. ``BookingGroup`` zurueck (noch nicht committet).

    ``doc`` ist das ``Document`` mit E-Rechnungsdaten; die Buchung wird per
    ``documents.service.link`` verknuepft (damit ist der Beleg „verbucht").
    """
    inv_row = doc.einvoice
    if inv_row is None:
        raise BookingError("Dieser Beleg enthält keine E-Rechnungsdaten — bitte von Hand buchen.")
    if doc.status == Document.STATUS_DISCARDED or documents_svc.is_booked(doc):
        raise BookingError("Dieser Beleg ist bereits verbucht oder verworfen.")
    if supplier is None:
        raise BookingError("Bitte einen Lieferanten wählen.")
    account = db.session.get(Account, account_id) if account_id else None
    if account is None or not account.active:
        raise BookingError("Bitte ein Konto wählen.")
    if booking_date > date.today():
        raise BookingError("Das Buchungsdatum darf nicht in der Zukunft liegen.")
    fy_error = acc_svc.open_fiscal_year_error(booking_date)
    if fy_error:
        raise BookingError(fy_error)
    if project_id is not None and db.session.get(Project, project_id) is None:
        raise BookingError("Das gewählte Projekt gibt es nicht.")
    if real_account_id is not None and db.session.get(RealAccount, real_account_id) is None:
        raise BookingError("Das gewählte Bankkonto gibt es nicht.")

    parsed = inv_row.parsed
    rows = booking_rows(parsed)
    number = parsed.number or "ohne Nummer"
    base = f"{supplier.name}: {'Gutschrift' if parsed.is_credit_note else 'Rechnung'} {number}"[:440]
    reference = (parsed.number or None) and parsed.number[:100]

    def booking(row, description, group_id=None):
        return Booking(
            date=booking_date, account_id=account.id, amount=row["gross"], description=description[:500],
            reference=reference, project_id=project_id, real_account_id=real_account_id,
            customer_id=supplier.id, tax_rate=row["rate"] if row["rate"] > 0 else None,
            group_id=group_id, created_by_id=user_id)

    try:
        if len(rows) == 1:
            result = booking(rows[0], base)
            db.session.add(result)
            db.session.flush()
            documents_svc.link(doc, booking=result, user_id=user_id)
        else:
            result = BookingGroup(date=booking_date, description=base, reference=reference,
                                  customer_id=supplier.id, total_amount=Decimal("0"),
                                  status=BookingGroup.STATUS_AKTIV, created_by_id=user_id)
            db.session.add(result)
            db.session.flush()
            for row in rows:
                db.session.add(booking(row, f"{base} ({row['label']})", group_id=result.id))
            db.session.flush()
            acc_svc.recompute_group_total(result.id)
            documents_svc.link(doc, group=result, user_id=user_id)
    except documents_svc.DocumentError as exc:
        raise BookingError(str(exc)) from exc
    doc.supplier_id = supplier.id
    return result
