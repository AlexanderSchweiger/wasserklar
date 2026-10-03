"""Aufbewahrungsfristen im Dokumentenregister — je Bereich und Art eine Klasse, die Jahre je Land.

Die Frist wird **berechnet, nie gespeichert**: 31.12. des Jahres ``spaetestes Bezugsdatum +
Jahre``. Keine Rechtsberatung — die Hinweistexte stehen im Laenderprofil (``app/country.py``).

* ``tax`` — Buchungsbelege und Doppel der Ausgangsrechnungen: AT 7 (§ 132 BAO), DE 8 Jahre
  (§ 147 AO / § 14b UStG). Bezug bei Belegen: Belegdatum, Ablage und jede verknuepfte Buchung (auch
  eine stornierte); bei Ausgangsrechnungen das Rechnungsdatum.
* ``business_letter`` — Mahnungen und Schriftverkehr: AT 7, DE 6 Jahre. Bezug: Mahn- bzw.
  Dokumentdatum, sonst das Ablagedatum.
* ``permanent`` — Protokolle (und damit die Beschluesse): dauerhaft, nie im Fristablauf.
"""
from datetime import date, datetime

from sqlalchemy import and_, false, or_

from app import country
from app.models import (
    Booking, BookingGroup, Document, DocumentLink, DunningNotice, Invoice, Meeting,
)

CLASS_TAX = "tax"
CLASS_BUSINESS_LETTER = "business_letter"
CLASS_PERMANENT = "permanent"

_CLASS_BY_KIND = {
    Document.KIND_SALES_INVOICE: CLASS_TAX,
    Document.KIND_SALES_CREDIT: CLASS_TAX,
    Document.KIND_DUNNING_NOTICE: CLASS_BUSINESS_LETTER,
    Document.KIND_CORRESPONDENCE: CLASS_BUSINESS_LETTER,
    Document.KIND_PROTOCOL: CLASS_PERMANENT,
}

PERMANENT_HINT = ("Protokolle und Beschlüsse sind Unterlagen der Genossenschaft und werden dauerhaft "
                  "aufbewahrt — sie lassen sich nicht löschen.")


def retention_class(doc):
    if doc.area == Document.AREA_ACCOUNTING:
        return CLASS_TAX
    return _CLASS_BY_KIND.get(doc.kind, CLASS_TAX)


def years_for(cls, profile=None):
    """Jahre der Klasse im Land des Mandanten (``None`` = dauerhaft)."""
    profile = profile or country.current_profile()
    if cls == CLASS_PERMANENT:
        return None
    if cls == CLASS_BUSINESS_LETTER:
        return profile.business_letter_retention_years
    return profile.document_retention_years


def hint(doc):
    cls = retention_class(doc)
    if cls == CLASS_PERMANENT:
        return PERMANENT_HINT
    profile = country.current_profile()
    if cls == CLASS_BUSINESS_LETTER:
        return profile.business_letter_retention_hint
    return profile.document_retention_hint


def _reference_dates(doc):
    links = []
    for row in doc.links:
        if row.booking is not None:
            links.append(row.booking.date)
        elif row.booking_group is not None:
            links.append(row.booking_group.date)
        elif row.invoice is not None:
            links.append(row.invoice.date)
        elif row.dunning_notice is not None:
            links.append(row.dunning_notice.issued_date)
        elif row.meeting is not None:
            links.append(row.meeting.meeting_date)
    created = doc.created_at.date() if doc.created_at else None
    if doc.area == Document.AREA_ACCOUNTING:
        # Beleg: auch die Ablage zaehlt (ein spaet abgelegter Beleg verlaengert die Frist)
        return [d for d in [doc.document_date, created, *links] if d]
    # Erzeugtes Dokument / Schriftverkehr: das Datum des Vorgangs; die Ablage nur ohne Datum
    dated = [d for d in [doc.document_date, *links] if d]
    return dated or ([created] if created else [])


def retention_end(doc):
    """Ende der Aufbewahrungsfrist (``date``) oder ``None`` bei dauerhafter Aufbewahrung."""
    years = years_for(retention_class(doc))
    if years is None:
        return None
    dates = _reference_dates(doc)
    latest = max(dates) if dates else date.today()
    return date(latest.year + years, 12, 31)


def is_expired(doc, today=None):
    end = retention_end(doc)
    return end is not None and end < (today or date.today())


# ---------------------------------------------------------------------------
# Als Abfrage (Vorauswahl fuer die Fristablauf-Liste)
# ---------------------------------------------------------------------------

def _start(years, today):
    return date(today.year - years, 1, 1)


def _accounting_clause(start):
    recent_link = Document.links.any(or_(
        DocumentLink.booking.has(Booking.date >= start),
        DocumentLink.booking_group.has(BookingGroup.date >= start)))
    return and_(Document.created_at < datetime(start.year, 1, 1),
                or_(Document.document_date.is_(None), Document.document_date < start),
                ~recent_link)


def _dated_clause(start):
    recent_link = Document.links.any(or_(
        DocumentLink.invoice.has(Invoice.date >= start),
        DocumentLink.dunning_notice.has(DunningNotice.issued_date >= start),
        DocumentLink.meeting.has(Meeting.meeting_date >= start)))
    dated = or_(Document.document_date < start,
                and_(Document.document_date.is_(None), Document.created_at < datetime(start.year, 1, 1)))
    return and_(dated, ~recent_link)


def expired_clause(today=None, areas=None):
    """SQL-Ausdruck: die Frist des Dokuments ist abgelaufen (Vorauswahl; ``retention_end`` bestaetigt).

    Gleiche Regel wie ``retention_end``, aber als Abfrage — die Zaehler bleiben billig. ``areas``
    begrenzt auf Bereiche (``None`` = alle). Dauerhafte Dokumente (Protokolle) erscheinen nie.
    """
    today = today or date.today()
    profile = country.current_profile()
    tax = _start(profile.document_retention_years, today)
    letter = _start(profile.business_letter_retention_years, today)
    parts = {
        Document.AREA_ACCOUNTING: and_(Document.area == Document.AREA_ACCOUNTING, _accounting_clause(tax)),
        Document.AREA_INVOICES: and_(Document.area == Document.AREA_INVOICES, _dated_clause(tax)),
        Document.AREA_DUNNING: and_(Document.area == Document.AREA_DUNNING, _dated_clause(letter)),
        Document.AREA_RECORDS: and_(Document.area == Document.AREA_RECORDS,
                                    Document.kind == Document.KIND_CORRESPONDENCE, _dated_clause(letter)),
    }
    chosen = [clause for area, clause in parts.items() if areas is None or area in areas]
    return or_(*chosen) if chosen else false()
