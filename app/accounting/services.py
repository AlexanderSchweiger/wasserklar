"""
Zentraler Buchhaltungs-Service.

Sämtliche Berechnungen rund um Konten, Kontostände, Jahresabschluss und
Umsatzsteuer-Voranmeldung sind hier gebündelt. Routen sollen ausschließlich
auf diese Funktionen zugreifen, um Doppelimplementierungen und divergierende
Storno-Behandlung zu vermeiden.

Storno-Regel
------------
Eine Stornierung erzeugt zwei Buchungen, die zusammen Null ergeben:

* die Originalbuchung mit ``status = "Storniert"``
* die Stornogegenbuchung mit ``storno_of_id = original.id`` (Betrag negiert,
  Status "Verbucht")

Beide gehören zum selben Storno-Paar. Frühere Implementierungen filterten
nur ``status != "Storniert"`` und zählten dadurch ausschließlich die
Gegenbuchung mit – das ergab fälschlicherweise ``-original.amount`` statt
``0``. Korrekte Behandlung: **beide** Hälften ignorieren oder **beide**
mitzählen. Dieser Service ignoriert beide.
"""

from datetime import date
from decimal import Decimal
import calendar

from sqlalchemy import extract, func

from app.extensions import db
from app.models import (
    Booking, Transfer, RealAccount, RealAccountYearBalance, FiscalYear,
)


# ---------------------------------------------------------------------------
# Storno-Filter
# ---------------------------------------------------------------------------

def storno_filter():
    """SQLAlchemy-Filterausdruck, der beide Hälften eines Storno-Paares ausschließt."""
    return db.and_(
        Booking.status != Booking.STATUS_STORNIERT,
        Booking.storno_of_id.is_(None),
    )


def apply_storno_filter(query):
    """Wendet den Storno-Filter auf eine Booking-Query an."""
    return query.filter(storno_filter())


def is_effective_booking(booking):
    """True, wenn die Buchung in Summenberechnungen einfließen soll."""
    if booking is None:
        return False
    if booking.status == Booking.STATUS_STORNIERT:
        return False
    if booking.storno_of_id is not None:
        return False
    return True


# ---------------------------------------------------------------------------
# Allgemeine Helfer
# ---------------------------------------------------------------------------

def auto_post_bookings():
    """Markiert alle 'Offen'-Buchungen mit Datum < heute als 'Verbucht'."""
    today = date.today()
    Booking.query.filter(
        Booking.status == Booking.STATUS_OFFEN,
        Booking.date < today,
    ).update({"status": Booking.STATUS_VERBUCHT}, synchronize_session=False)
    db.session.commit()


def locked_fiscal_year(booking_date):
    """Gibt das abgeschlossene Buchungsjahr zurück, in das ``booking_date`` fällt.

    Es wird sowohl der Datumsbereich als auch das Kalenderjahr geprüft, damit
    irrtümlich falsch konfigurierte FY-Daten (z. B. end_date im Folgejahr) keinen
    falschen Sperrblock auslösen.
    """
    fy = FiscalYear.query.filter(
        FiscalYear.closed == True,  # noqa: E712
        FiscalYear.start_date <= booking_date,
        FiscalYear.end_date >= booking_date,
    ).first()
    if fy is not None and fy.year != booking_date.year:
        return None
    return fy


def booking_tax(booking):
    """Berechnet den USt-Anteil einer Buchung (immer als positiver Wert)."""
    if not booking.tax_rate or booking.tax_rate == 0:
        return Decimal("0")
    rate = Decimal(str(booking.tax_rate))
    return (abs(booking.amount) * rate / (100 + rate)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Bankkonto-Salden
# ---------------------------------------------------------------------------

def _sum_bookings(ra_id, *date_filters):
    q = db.session.query(func.sum(Booking.amount)).filter(
        Booking.real_account_id == ra_id,
        *date_filters,
    )
    q = apply_storno_filter(q)
    return q.scalar() or Decimal("0")


def _sum_transfers_in(ra_id, *date_filters):
    return db.session.query(func.sum(Transfer.amount)).filter(
        Transfer.to_real_account_id == ra_id,
        *date_filters,
    ).scalar() or Decimal("0")


def _sum_transfers_out(ra_id, *date_filters):
    return db.session.query(func.sum(Transfer.amount)).filter(
        Transfer.from_real_account_id == ra_id,
        *date_filters,
    ).scalar() or Decimal("0")


def jan1_balance(ra, year):
    """Kontostand am 1.1. des angegebenen Jahres (Jahresanfangsstand).

    Sucht den zuletzt gespeicherten ``RealAccountYearBalance`` vor ``year`` als
    Basis. Wenn keiner vorhanden, wird ``ra.opening_balance`` verwendet.
    Lückenjahre (zwischen gespeichertem Jahr+1 und year-1) werden aufaddiert.
    """
    prev = (RealAccountYearBalance.query
            .filter_by(real_account_id=ra.id)
            .filter(RealAccountYearBalance.year < year)
            .order_by(RealAccountYearBalance.year.desc())
            .first())

    if prev:
        base = Decimal(str(prev.closing_balance))
        start_year = prev.year + 1
    else:
        base = Decimal(str(ra.opening_balance))
        start_year = 1

    if start_year < year:
        bookings = _sum_bookings(
            ra.id,
            extract("year", Booking.date) >= start_year,
            extract("year", Booking.date) < year,
        )
        inc = _sum_transfers_in(
            ra.id,
            extract("year", Transfer.date) >= start_year,
            extract("year", Transfer.date) < year,
        )
        out = _sum_transfers_out(
            ra.id,
            extract("year", Transfer.date) >= start_year,
            extract("year", Transfer.date) < year,
        )
        base += Decimal(str(bookings)) + Decimal(str(inc)) - Decimal(str(out))

    return base


def year_end_balance(ra, year):
    """Kontostand am 31.12. des angegebenen Jahres (Jahresabschlussstand)."""
    base = jan1_balance(ra, year)
    bookings = _sum_bookings(ra.id, extract("year", Booking.date) == year)
    inc = _sum_transfers_in(ra.id, extract("year", Transfer.date) == year)
    out = _sum_transfers_out(ra.id, extract("year", Transfer.date) == year)
    return base + Decimal(str(bookings)) + Decimal(str(inc)) - Decimal(str(out))


def current_balance(ra):
    """Aktueller Kontostand: letzter gespeicherter Jahresabschluss + alle Bewegungen danach."""
    last = (RealAccountYearBalance.query
            .filter_by(real_account_id=ra.id)
            .order_by(RealAccountYearBalance.year.desc())
            .first())

    if last:
        base = Decimal(str(last.closing_balance))
        from_date = date(last.year + 1, 1, 1)
        bookings = _sum_bookings(ra.id, Booking.date >= from_date)
        inc = _sum_transfers_in(ra.id, Transfer.date >= from_date)
        out = _sum_transfers_out(ra.id, Transfer.date >= from_date)
    else:
        base = Decimal(str(ra.opening_balance))
        bookings = _sum_bookings(ra.id)
        inc = _sum_transfers_in(ra.id)
        out = _sum_transfers_out(ra.id)

    return base + Decimal(str(bookings)) + Decimal(str(inc)) - Decimal(str(out))


def year_movements(ra_id, year):
    """Bewegungen eines Bankkontos für ein Jahr.

    Liefert ``(income, expense, year_total)``:

    * ``income``  – Summe positiver Buchungen + eingehender Umbuchungen
    * ``expense`` – Summe negativer Buchungen (positiv) + ausgehender Umbuchungen
    * ``year_total`` – Saldo (income - expense bzw. Buchungssumme + Umbuchungssaldo)

    Stornopaare werden ignoriert.
    """
    income_book = _sum_bookings(
        ra_id,
        extract("year", Booking.date) == year,
        Booking.amount > 0,
    )
    expense_book = _sum_bookings(
        ra_id,
        extract("year", Booking.date) == year,
        Booking.amount < 0,
    )
    incoming = _sum_transfers_in(ra_id, extract("year", Transfer.date) == year)
    outgoing = _sum_transfers_out(ra_id, extract("year", Transfer.date) == year)

    income = Decimal(str(income_book)) + Decimal(str(incoming))
    expense = abs(Decimal(str(expense_book))) + Decimal(str(outgoing))
    year_total = (
        Decimal(str(income_book)) + Decimal(str(expense_book))
        + Decimal(str(incoming)) - Decimal(str(outgoing))
    )
    return income, expense, year_total


def year_booking_total(ra_id, year):
    """Reine Buchungssumme (ohne Umbuchungen) eines Bankkontos im Jahr."""
    return _sum_bookings(ra_id, extract("year", Booking.date) == year)


# ---------------------------------------------------------------------------
# Jahres-Auswertung (Einnahmen/Ausgaben nach Konto, Projektübersicht)
# ---------------------------------------------------------------------------

def year_account_totals(year, real_account_id=None):
    """Liefert ``[(account_name, total), ...]`` gruppiert je Erfolgskonto."""
    from app.models import Account

    q = (
        db.session.query(
            Account.name.label("name"),
            func.sum(Booking.amount).label("total"),
        )
        .join(Booking, Booking.account_id == Account.id)
        .filter(extract("year", Booking.date) == year)
    )
    q = apply_storno_filter(q)
    if real_account_id:
        q = q.filter(Booking.real_account_id == real_account_id)
    return q.group_by(Account.id).order_by(Account.name).all()


def year_income_expense(year, real_account_id=None):
    """Liefert ``(income_rows, expense_rows, total_income, total_expense, balance)``."""
    rows = year_account_totals(year, real_account_id=real_account_id)
    income_rows = [(r.name, Decimal(str(r.total))) for r in rows if r.total is not None and r.total > 0]
    expense_rows = [(r.name, abs(Decimal(str(r.total)))) for r in rows if r.total is not None and r.total < 0]
    total_income = sum((r[1] for r in income_rows), Decimal("0"))
    total_expense = sum((r[1] for r in expense_rows), Decimal("0"))
    balance = total_income - total_expense
    return income_rows, expense_rows, total_income, total_expense, balance


def year_project_summary(year, real_account_id=None):
    """Projektübersicht: Einnahmen/Ausgaben pro Projekt+Konto."""
    from sqlalchemy import case
    from app.models import Account, Project

    q = (
        db.session.query(
            Project.id.label("project_id"),
            Project.name.label("project_name"),
            Account.name.label("account_name"),
            func.sum(Booking.amount).label("total"),
        )
        .select_from(Booking)
        .outerjoin(Project, Booking.project_id == Project.id)
        .join(Account, Booking.account_id == Account.id)
        .filter(extract("year", Booking.date) == year)
    )
    q = apply_storno_filter(q)
    if real_account_id:
        q = q.filter(Booking.real_account_id == real_account_id)
    rows = q.group_by(Project.id, Project.name, Account.id, Account.name).order_by(
        case((Project.id == None, 1), else_=0),  # noqa: E711
        Project.name,
        Account.name,
    ).all()

    summary = {}
    for row in rows:
        key = row.project_id
        if key not in summary:
            summary[key] = {
                "name": row.project_name or "Ohne Projekt",
                "accounts": [],
                "income": Decimal("0"),
                "expense": Decimal("0"),
            }
        rt = row.total or Decimal("0")
        summary[key]["accounts"].append((row.account_name, rt))
        if rt >= 0:
            summary[key]["income"] += rt
        else:
            summary[key]["expense"] += abs(rt)

    return [v for k, v in sorted(summary.items(), key=lambda x: (x[0] is None, x[1]["name"]))]


def year_bookings(year, real_account_id=None):
    """Liefert alle effektiven Buchungen eines Jahres (Stornopaare ausgeschlossen)."""
    q = Booking.query.filter(extract("year", Booking.date) == year)
    q = apply_storno_filter(q)
    if real_account_id:
        q = q.filter(Booking.real_account_id == real_account_id)
    return q.order_by(Booking.date).all()


# ---------------------------------------------------------------------------
# Umsatzsteuer
# ---------------------------------------------------------------------------

def ust_period(year, quartal):
    """``(date_from, date_to)`` für Jahr/Quartal. ``quartal == 0`` → Gesamtjahr."""
    if quartal in (1, 2, 3, 4):
        m_start = (quartal - 1) * 3 + 1
        m_end = quartal * 3
        return date(year, m_start, 1), date(year, m_end, calendar.monthrange(year, m_end)[1])
    return date(year, 1, 1), date(year, 12, 31)


def ust_compute(year, quartal):
    """Berechnet USt/Vorsteuer-Gruppen für einen Zeitraum.

    Liefert ``(ust_rows, vst_rows)`` als sortierte Listen
    ``[(rate, {brutto, netto, steuer}), ...]``.

    Stornopaare werden ignoriert.
    """
    date_from, date_to = ust_period(year, quartal)

    q = (
        Booking.query
        .filter(Booking.date >= date_from, Booking.date <= date_to)
        .filter(Booking.tax_rate.isnot(None), Booking.tax_rate > 0)
    )
    q = apply_storno_filter(q)
    bookings = q.join(Booking.account).order_by(Booking.date).all()

    ust_rows = {}
    vst_rows = {}
    for b in bookings:
        tax = booking_tax(b)
        brutto = abs(b.amount)
        netto = brutto - tax
        target = ust_rows if b.amount > 0 else vst_rows
        rate_key = int(b.tax_rate)
        if rate_key not in target:
            target[rate_key] = {"brutto": Decimal("0"), "steuer": Decimal("0"), "netto": Decimal("0")}
        target[rate_key]["brutto"] += brutto
        target[rate_key]["steuer"] += tax
        target[rate_key]["netto"] += netto
    return sorted(ust_rows.items()), sorted(vst_rows.items())


def ust_totals(year, quartal):
    """Liefert kompakte Summen-Übersicht für USt-Voranmeldung.

    Returns dict with: ``ust_rows, vst_rows, total_ust, total_vst, zahllast,
    ust_brutto, ust_netto, vst_brutto, vst_netto, date_from, date_to``.
    """
    date_from, date_to = ust_period(year, quartal)
    ust_rows, vst_rows = ust_compute(year, quartal)
    total_ust = sum((v["steuer"] for _, v in ust_rows), Decimal("0"))
    total_vst = sum((v["steuer"] for _, v in vst_rows), Decimal("0"))
    return {
        "date_from": date_from,
        "date_to": date_to,
        "ust_rows": ust_rows,
        "vst_rows": vst_rows,
        "total_ust": total_ust,
        "total_vst": total_vst,
        "zahllast": total_ust - total_vst,
        "ust_brutto": sum((v["brutto"] for _, v in ust_rows), Decimal("0")),
        "ust_netto": sum((v["netto"] for _, v in ust_rows), Decimal("0")),
        "vst_brutto": sum((v["brutto"] for _, v in vst_rows), Decimal("0")),
        "vst_netto": sum((v["netto"] for _, v in vst_rows), Decimal("0")),
    }


# ---------------------------------------------------------------------------
# Jahresabschluss
# ---------------------------------------------------------------------------

def fiscal_year_close_summary(year):
    """Vorschau-Summen für den Jahresabschluss (alle aktiven Bankkonten).

    Liefert eine Liste mit Eintrag pro Konto: ``ra``, ``jan1``, ``einnahmen``,
    ``ausgaben`` (negativ), ``transfers_netto``, ``dec31``.
    Stornopaare werden ignoriert.
    """
    real_accs = RealAccount.query.order_by(RealAccount.name).all()
    summary = []
    for ra in real_accs:
        jan1 = jan1_balance(ra, year)
        einnahmen = _sum_bookings(
            ra.id,
            extract("year", Booking.date) == year,
            Booking.amount > 0,
        )
        ausgaben = _sum_bookings(
            ra.id,
            extract("year", Booking.date) == year,
            Booking.amount < 0,
        )
        transfers_in = _sum_transfers_in(ra.id, extract("year", Transfer.date) == year)
        transfers_out = _sum_transfers_out(ra.id, extract("year", Transfer.date) == year)
        dec31 = (
            jan1
            + Decimal(str(einnahmen))
            + Decimal(str(ausgaben))
            + Decimal(str(transfers_in))
            - Decimal(str(transfers_out))
        )
        summary.append({
            "ra": ra,
            "jan1": jan1,
            "einnahmen": Decimal(str(einnahmen)),
            "ausgaben": Decimal(str(ausgaben)),
            "transfers_netto": Decimal(str(transfers_in)) - Decimal(str(transfers_out)),
            "dec31": dec31,
        })
    return summary
