"""Eingangs-E-Rechnungen: Ablage, Lieferanten-Zuordnung und Buchungsvorschlag.

Das Original wird unveraendert abgelegt (``instance/incoming/<Jahr>/``, Geschwister von
``PDF_DIR`` — im SaaS je Mandant) und nie wieder angefasst; die gelesenen Daten stehen
als JSON an der Zeile. Aus dem Beleg entsteht eine Ausgabenbuchung je Steuersatz
(Brutto, mit Steuersatz — wie die Buchungen aus Ausgangsrechnungen): ein Steuersatz →
Einzelbuchung, mehrere → Sammelbuchung (ADR-002). Gutschriften (Typ 381) buchen positiv.
"""
import hashlib
import os
from collections import OrderedDict
from datetime import date
from decimal import Decimal

from flask import current_app
from sqlalchemy import func
from werkzeug.utils import secure_filename

from app import country
from app.accounting import services as acc_svc
from app.einvoice import incoming
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, IncomingInvoice, Project, RealAccount,
)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ROUNDING_TOLERANCE = Decimal("0.05")


class DuplicateUpload(Exception):
    """Dieselbe Datei ist schon erfasst; ``existing`` ist der vorhandene Beleg."""

    def __init__(self, existing):
        super().__init__(existing.original_name)
        self.existing = existing


class BookingError(Exception):
    """Der Buchungsvorschlag laesst sich nicht buchen; die Meldung ist fuer den Nutzer."""


def incoming_dir():
    """Ablageordner der Originale: Geschwister von ``PDF_DIR`` (im SaaS je Mandant)."""
    pdf_dir = current_app.config["PDF_DIR"].rstrip("/\\")
    return os.path.join(os.path.dirname(pdf_dir), "incoming")


# ---------------------------------------------------------------------------
# Ablage
# ---------------------------------------------------------------------------

def store_upload(filename, data, user_id):
    """Liest eine hochgeladene E-Rechnung, legt das Original ab und gibt den Beleg zurueck.

    Wirft ``incoming.IncomingError`` (nicht lesbar) oder ``DuplicateUpload``. Der Beleg
    ist noch nicht committet — das macht der Aufrufer.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise incoming.IncomingError("Die Datei ist größer als 15 MB.")
    digest = hashlib.sha256(data).hexdigest()
    existing = IncomingInvoice.query.filter_by(sha256=digest).first()
    if existing is not None:
        raise DuplicateUpload(existing)
    parsed = incoming.parse(data, filename)
    inv = parsed.invoice
    doc = IncomingInvoice(
        original_name=(filename or "beleg")[:255], sha256=digest, source_kind=parsed.source_kind,
        syntax=inv.syntax, guideline=(inv.guideline or "")[:255], number=(inv.number or "")[:100],
        issue_date=inv.issue_date, currency=(inv.currency or "EUR")[:3], type_code=(inv.type_code or "380")[:3],
        seller_name=(inv.seller.name or "")[:200], seller_vat_id=(inv.seller.vat_id or None),
        grand_total=inv.grand_total, data=inv.to_json(), created_by_id=user_id)
    db.session.add(doc)
    db.session.flush()
    folder = os.path.join(incoming_dir(), str(inv.issue_date.year if inv.issue_date else "ohne-datum"))
    os.makedirs(folder, exist_ok=True)
    safe = secure_filename(filename or "") or f"beleg.{parsed.source_kind}"
    path = os.path.join(folder, f"{doc.id}_{digest[:8]}_{safe}")
    with open(path, "wb") as fh:
        fh.write(data)
    doc.file_path = path
    return doc


def possible_duplicates(doc):
    """Andere Belege desselben Lieferanten mit derselben Rechnungsnummer (Hinweis, kein Verbot)."""
    if not doc.number:
        return []
    query = IncomingInvoice.query.filter(IncomingInvoice.id != doc.id,
                                         IncomingInvoice.number == doc.number,
                                         IncomingInvoice.status != IncomingInvoice.STATUS_DISCARDED)
    if doc.seller_vat_id:
        query = query.filter(IncomingInvoice.seller_vat_id == doc.seller_vat_id)
    else:
        query = query.filter(func.lower(IncomingInvoice.seller_name) == (doc.seller_name or "").lower())
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


def release(*, booking_id=None, group_id=None):
    """Gibt Belege frei, deren Buchung geloescht oder storniert wird: sie sind wieder „Neu".

    Sonst bliebe ein Beleg auf „Verbucht", obwohl es keine wirksame Buchung mehr gibt — und
    das Loeschen der Buchung scheiterte am Fremdschluessel. Gibt die Zahl der Belege zurueck.
    Der Aufrufer committet.
    """
    query = IncomingInvoice.query.filter(IncomingInvoice.status == IncomingInvoice.STATUS_BOOKED)
    if booking_id is not None:
        query = query.filter(IncomingInvoice.booking_id == booking_id)
    elif group_id is not None:
        query = query.filter(IncomingInvoice.booking_group_id == group_id)
    else:
        return 0
    docs = query.all()
    for doc in docs:
        doc.status = IncomingInvoice.STATUS_NEW
        doc.booking_id = None
        doc.booking_group_id = None
    if docs:
        db.session.flush()        # vor dem DELETE der Buchung: Fremdschluessel zuerst loesen
    return len(docs)


def document_of(*, booking_id=None, group_id=None):
    """Der Eingangsrechnungs-Beleg einer Buchung bzw. Sammelbuchung (oder ``None``)."""
    if booking_id is not None:
        return IncomingInvoice.query.filter_by(booking_id=booking_id).first()
    if group_id is not None:
        return IncomingInvoice.query.filter_by(booking_group_id=group_id).first()
    return None


def book(doc, *, supplier, account_id, booking_date, user_id, project_id=None, real_account_id=None):
    """Bucht den Beleg. Gibt die ``Booking`` bzw. ``BookingGroup`` zurueck (noch nicht committet)."""
    if doc.status != IncomingInvoice.STATUS_NEW:
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

    parsed = doc.parsed
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

    if len(rows) == 1:
        result = booking(rows[0], base)
        db.session.add(result)
        db.session.flush()
        doc.booking_id = result.id
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
        doc.booking_group_id = result.id
    doc.status = IncomingInvoice.STATUS_BOOKED
    doc.supplier_id = supplier.id
    return result
