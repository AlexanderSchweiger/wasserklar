import io
import csv
import base64
import calendar
import difflib
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    render_template, redirect, url_for, flash, request,
    Response, stream_with_context,
)
from flask_login import login_required, current_user
from sqlalchemy import extract, func, case

from app.accounting import bp
from app.extensions import db
from app.models import Account, Booking, Invoice, OpenItem, WaterTariff, Customer, InvoiceItem, Project, RealAccount, RealAccountYearBalance, FiscalYear, FiscalYearReopenLog, TaxRate, Transfer
from app.utils import next_invoice_number


@bp.route("/")
@login_required
def index():
    year = request.args.get("year", date.today().year, type=int)
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    return render_template("accounting/index.html", accounts=accounts, year=year)


# ---------------------------------------------------------------------------
# Kontenplan
# ---------------------------------------------------------------------------

@bp.route("/accounts")
@login_required
def accounts():
    all_accounts = Account.query.order_by(Account.name).all()
    return render_template("accounting/accounts.html", accounts=all_accounts)


def _validate_code(code_raw, model_cls, exclude_id=None):
    """Validiert ein 3-stelliges Kürzel (A-Z, 0-9). Gibt (code, fehlermeldung) zurück."""
    import re
    code = code_raw.strip().upper() or None
    if code:
        if not re.match(r'^[A-Z0-9]{3}$', code):
            return None, "Kürzel muss genau 3 Zeichen bestehen (Großbuchstaben A–Z oder Ziffern 0–9)."
        q = model_cls.query.filter(model_cls.code == code)
        if exclude_id is not None:
            q = q.filter(model_cls.id != exclude_id)
        existing = q.first()
        if existing:
            return None, f"Kürzel '{code}' wird bereits von '{existing.name}' verwendet."
    return code, None


@bp.route("/accounts/new", methods=["GET", "POST"])
@login_required
def account_new():
    if request.method == "POST":
        code, err = _validate_code(request.form.get("code", ""), Account)
        if err:
            flash(err, "danger")
            return render_template("accounting/account_form.html", account=None)
        a = Account(
            code=code,
            name=request.form["name"].strip(),
            description=request.form.get("description", ""),
        )
        db.session.add(a)
        db.session.commit()
        flash("Konto angelegt.", "success")
        return redirect(url_for("accounting.accounts"))
    return render_template("accounting/account_form.html", account=None)


@bp.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
def account_edit(account_id):
    a = db.get_or_404(Account, account_id)
    if request.method == "POST":
        code, err = _validate_code(request.form.get("code", ""), Account, exclude_id=a.id)
        if err:
            flash(err, "danger")
            return render_template("accounting/account_form.html", account=a)
        a.code = code
        a.name = request.form["name"].strip()
        a.description = request.form.get("description", "")
        a.active = "active" in request.form
        db.session.commit()
        flash("Konto aktualisiert.", "success")
        return redirect(url_for("accounting.accounts"))
    return render_template("accounting/account_form.html", account=a)


# ---------------------------------------------------------------------------
# Buchungen
# ---------------------------------------------------------------------------

def _auto_post_bookings():
    """Markiert alle 'Offen'-Buchungen mit Buchungsdatum < heute als 'Verbucht'."""
    today = date.today()
    Booking.query.filter(
        Booking.status == Booking.STATUS_OFFEN,
        Booking.date < today,
    ).update({"status": Booking.STATUS_VERBUCHT}, synchronize_session=False)
    db.session.commit()


def _locked_fiscal_year(booking_date):
    """Gibt das abgeschlossene Buchungsjahr zurück, wenn booking_date darin liegt.
    Es wird sowohl der Datumsbereich als auch das Kalenderjahr geprüft, damit
    irrtümlich falsch konfigurierte FY-Daten (z. B. end_date im Folgejahr) keinen
    falschen Sperrblock auslösen."""
    fy = FiscalYear.query.filter(
        FiscalYear.closed == True,
        FiscalYear.start_date <= booking_date,
        FiscalYear.end_date >= booking_date,
    ).first()
    if fy is not None and fy.year != booking_date.year:
        return None
    return fy


def _jan1_balance(ra, year):
    """Kontostand am 1.1. von Jahr `year` (Jahresanfangsstand).

    Sucht den zuletzt gespeicherten RealAccountYearBalance vor `year` als Basis.
    Wenn keiner vorhanden, wird ra.opening_balance als Basis verwendet.
    Lücken-Jahre (zwischen gespeichertem Jahr+1 und year-1) werden aufaddiert.
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
        add = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            extract("year", Booking.date) >= start_year,
            extract("year", Booking.date) < year,
        ).scalar() or Decimal("0")
        inc = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.to_real_account_id == ra.id,
            extract("year", Transfer.date) >= start_year,
            extract("year", Transfer.date) < year,
        ).scalar() or Decimal("0")
        out = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.from_real_account_id == ra.id,
            extract("year", Transfer.date) >= start_year,
            extract("year", Transfer.date) < year,
        ).scalar() or Decimal("0")
        base += Decimal(str(add)) + Decimal(str(inc)) - Decimal(str(out))

    return base


def _year_end_balance(ra, year):
    """Kontostand am 31.12. von Jahr `year` (Jahresabschlussstand)."""
    jan1 = _jan1_balance(ra, year)
    bkgs = db.session.query(func.sum(Booking.amount)).filter(
        Booking.real_account_id == ra.id,
        extract("year", Booking.date) == year,
    ).scalar() or Decimal("0")
    inc = db.session.query(func.sum(Transfer.amount)).filter(
        Transfer.to_real_account_id == ra.id,
        extract("year", Transfer.date) == year,
    ).scalar() or Decimal("0")
    out = db.session.query(func.sum(Transfer.amount)).filter(
        Transfer.from_real_account_id == ra.id,
        extract("year", Transfer.date) == year,
    ).scalar() or Decimal("0")
    return jan1 + Decimal(str(bkgs)) + Decimal(str(inc)) - Decimal(str(out))


def _current_balance(ra):
    """Aktueller Kontostand = letzter gespeicherter Jahresabschluss + alle Buchungen danach."""
    last = (RealAccountYearBalance.query
            .filter_by(real_account_id=ra.id)
            .order_by(RealAccountYearBalance.year.desc())
            .first())

    if last:
        base = Decimal(str(last.closing_balance))
        from_date = date(last.year + 1, 1, 1)
        bkgs = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            Booking.date >= from_date,
        ).scalar() or Decimal("0")
        inc = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.to_real_account_id == ra.id,
            Transfer.date >= from_date,
        ).scalar() or Decimal("0")
        out = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.from_real_account_id == ra.id,
            Transfer.date >= from_date,
        ).scalar() or Decimal("0")
    else:
        base = Decimal(str(ra.opening_balance))
        bkgs = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
        ).scalar() or Decimal("0")
        inc = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.to_real_account_id == ra.id,
        ).scalar() or Decimal("0")
        out = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.from_real_account_id == ra.id,
        ).scalar() or Decimal("0")

    return base + Decimal(str(bkgs)) + Decimal(str(inc)) - Decimal(str(out))


@bp.route("/bookings")
@login_required
def bookings():
    _auto_post_bookings()

    year = request.args.get("year", date.today().year, type=int)
    account_id = request.args.get("account_id", "", type=str)
    project_id = request.args.get("project_id", "", type=str)
    real_account_id = request.args.get("real_account_id", "", type=str)

    query = (
        Booking.query
        .filter(extract("year", Booking.date) == year)
        .order_by(
            Booking.date.desc(),
            func.coalesce(Booking.storno_of_id, Booking.id).desc(),
        )
    )
    if account_id:
        query = query.filter(Booking.account_id == int(account_id))
    if project_id:
        query = query.filter(Booking.project_id == int(project_id))
    if real_account_id:
        query = query.filter(Booking.real_account_id == int(real_account_id))

    bkgs = query.all()
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    projects = Project.query.order_by(Project.name).all()
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()

    closed_fys = FiscalYear.query.filter_by(closed=True).all()
    locked_booking_ids = set()
    for b in bkgs:
        for fy in closed_fys:
            if fy.start_date <= b.date <= fy.end_date:
                locked_booking_ids.add(b.id)
                break

    active_bkgs = [b for b in bkgs if b.status != Booking.STATUS_STORNIERT]
    total_amount = sum((b.amount for b in active_bkgs), Decimal("0"))

    def _tax(b):
        if not b.tax_rate or b.tax_rate == 0:
            return Decimal("0")
        rate = Decimal(str(b.tax_rate))
        return (abs(b.amount) * rate / (100 + rate)).quantize(Decimal("0.01"))

    total_vorsteuer = sum(
        _tax(b) for b in active_bkgs if b.amount < 0
    )
    total_ust = sum(
        _tax(b) for b in active_bkgs if b.amount > 0
    )

    table_ctx = dict(
        bookings=bkgs, year=year,
        total_amount=total_amount,
        total_vorsteuer=total_vorsteuer,
        total_ust=total_ust,
        locked_booking_ids=locked_booking_ids,
    )

    if request.headers.get("HX-Request"):
        return render_template("accounting/_bookings_table.html", **table_ctx)

    # Aktueller Kontostand aller aktiven Bankkonten für den Header-Banner
    real_account_saldi = {ra.id: _current_balance(ra) for ra in real_accounts}

    return render_template(
        "accounting/bookings.html",
        accounts=accounts, projects=projects,
        account_id=account_id, project_id=project_id,
        real_accounts=real_accounts, real_account_id=real_account_id,
        real_account_saldi=real_account_saldi,
        **table_ctx,
    )


@bp.route("/bookings/new", methods=["GET", "POST"])
@login_required
def booking_new():
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    active_projects = Project.query.filter_by(closed=False).order_by(Project.name).all()
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    tax_rates = TaxRate.query.order_by(TaxRate.rate).all()
    default_real_account = RealAccount.query.filter_by(is_default=True, active=True).first()
    today = date.today()

    def _render_new(keep_date="", **extra):
        return render_template(
            "accounting/booking_form.html",
            booking=None, accounts=accounts,
            projects=active_projects, real_accounts=real_accounts,
            customers=customers, tax_rates=tax_rates,
            default_real_account=default_real_account,
            today=today,
            keep_date=keep_date,
            **extra,
        )

    if request.method == "POST":
        booking_date = date.fromisoformat(request.form["date"])
        if booking_date > today:
            flash("Das Buchungsdatum darf nicht in der Zukunft liegen.", "danger")
            return _render_new(form_data=request.form, keep_date=request.form.get("date", ""))
        fy_locked = _locked_fiscal_year(booking_date)
        if fy_locked:
            flash(f"Das Buchungsjahr {fy_locked.year} ist abgeschlossen. Buchung wurde nicht gespeichert.", "danger")
            return _render_new(form_data=request.form, keep_date=request.form.get("date", ""))
        amount_raw = request.form.get("amount", "0").replace(",", ".")
        amount = Decimal(amount_raw)
        acc = db.get_or_404(Account, int(request.form["account_id"]))
        project_id_raw = request.form.get("project_id") or None
        real_account_id_raw = request.form.get("real_account_id") or None
        customer_id_raw = request.form.get("customer_id") or None
        tax_rate_raw = request.form.get("tax_rate", "0") or "0"
        try:
            tax_rate = Decimal(tax_rate_raw)
        except Exception:
            tax_rate = Decimal("0")
        b = Booking(
            date=booking_date,
            account_id=acc.id,
            amount=amount,
            description=request.form.get("description", "").strip(),
            reference=request.form.get("reference", "").strip(),
            project_id=int(project_id_raw) if project_id_raw else None,
            real_account_id=int(real_account_id_raw) if real_account_id_raw else None,
            customer_id=int(customer_id_raw) if customer_id_raw else None,
            tax_rate=tax_rate if tax_rate > 0 else None,
            created_by_id=current_user.id,
        )
        db.session.add(b)
        db.session.commit()
        flash("Buchung gespeichert.", "success")
        if request.form.get("action") == "weiteres":
            return redirect(url_for("accounting.booking_new", date=booking_date.isoformat()))
        return redirect(url_for("accounting.bookings"))
    keep_date = request.args.get("date", "")
    return _render_new(keep_date=keep_date)


@bp.route("/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@login_required
def booking_edit(booking_id):
    b = db.get_or_404(Booking, booking_id)
    if b.status == Booking.STATUS_STORNIERT:
        flash("Stornierte Buchungen können nicht bearbeitet werden.", "warning")
        return redirect(url_for("accounting.bookings"))
    fy_locked = _locked_fiscal_year(b.date)
    if fy_locked:
        flash(f"Das Buchungsjahr {fy_locked.year} ist abgeschlossen. Diese Buchung kann nicht bearbeitet werden.", "danger")
        return redirect(url_for("accounting.bookings"))
    is_verbucht = b.status == Booking.STATUS_VERBUCHT
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    active_projects = Project.query.filter_by(closed=False).order_by(Project.name).all()
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    tax_rates = TaxRate.query.order_by(TaxRate.rate).all()
    if request.method == "POST":
        acc = db.get_or_404(Account, int(request.form["account_id"]))
        project_id_raw = request.form.get("project_id") or None
        real_account_id_raw = request.form.get("real_account_id") or None
        customer_id_raw = request.form.get("customer_id") or None
        b.account_id = acc.id
        b.description = request.form.get("description", "").strip()
        b.project_id = int(project_id_raw) if project_id_raw else None
        b.real_account_id = int(real_account_id_raw) if real_account_id_raw else None
        b.customer_id = int(customer_id_raw) if customer_id_raw else None
        if not is_verbucht:
            amount_raw = request.form.get("amount", "0").replace(",", ".")
            b.amount = Decimal(amount_raw)
            b.date = date.fromisoformat(request.form["date"])
            b.reference = request.form.get("reference", "").strip()
            tax_rate_raw = request.form.get("tax_rate", "0") or "0"
            try:
                tax_rate = Decimal(tax_rate_raw)
            except Exception:
                tax_rate = Decimal("0")
            b.tax_rate = tax_rate if tax_rate > 0 else None
        db.session.commit()
        flash("Buchung aktualisiert.", "success")
        return redirect(url_for("accounting.bookings"))
    return render_template(
        "accounting/booking_form.html", booking=b, accounts=accounts,
        projects=active_projects, real_accounts=real_accounts, customers=customers,
        is_verbucht=is_verbucht, tax_rates=tax_rates,
    )


@bp.route("/bookings/<int:booking_id>/delete", methods=["POST"])
@login_required
def booking_delete(booking_id):
    b = db.get_or_404(Booking, booking_id)
    if b.status != Booking.STATUS_OFFEN:
        flash("Nur offene Buchungen können gelöscht werden.", "warning")
        return redirect(url_for("accounting.bookings"))
    fy_locked = _locked_fiscal_year(b.date)
    if fy_locked:
        flash(f"Das Buchungsjahr {fy_locked.year} ist abgeschlossen. Diese Buchung kann nicht gelöscht werden.", "danger")
        return redirect(url_for("accounting.bookings"))
    db.session.delete(b)
    db.session.commit()
    flash("Buchung gelöscht.", "info")
    return redirect(url_for("accounting.bookings"))


@bp.route("/bookings/<int:booking_id>/stornieren", methods=["GET", "POST"])
@login_required
def booking_stornieren(booking_id):
    b = db.get_or_404(Booking, booking_id)

    if b.status == Booking.STATUS_STORNIERT:
        flash("Diese Buchung ist bereits storniert.", "warning")
        return redirect(url_for("accounting.bookings"))
    if b.storno_of_id is not None:
        flash("Eine Storno-Buchung kann nicht erneut storniert werden.", "warning")
        return redirect(url_for("accounting.bookings"))
    fy_locked = _locked_fiscal_year(b.date)
    if fy_locked:
        flash(f"Das Buchungsjahr {fy_locked.year} ist abgeschlossen. Diese Buchung kann nicht storniert werden.", "danger")
        return redirect(url_for("accounting.bookings"))
    if b.date.year != date.today().year:
        flash("Buchungen aus Vorjahren können nicht storniert werden.", "warning")
        return redirect(url_for("accounting.bookings"))

    if request.method == "POST":
        reason = request.form.get("storno_reason", "").strip()
        if not reason:
            flash("Bitte einen Storno-Grund angeben.", "danger")
            return render_template("accounting/storno_form.html", booking=b)

        # Storno-Buchung anlegen (gleiches Datum wie Ursprungsbuchung)
        storno = Booking(
            date=b.date,
            account_id=b.account_id,
            amount=b.amount * -1,
            description=f"Storno: {b.description}",
            invoice_id=b.invoice_id,
            open_item_id=b.open_item_id,
            project_id=b.project_id,
            tax_rate=b.tax_rate,
            storno_of_id=b.id,
            storno_reason=reason,
            storno_date=date.today(),
            status=Booking.STATUS_VERBUCHT,
            created_by_id=current_user.id,
        )
        db.session.add(storno)

        # Ursprungsbuchung als storniert markieren
        b.status = Booking.STATUS_STORNIERT

        # Verknüpfte Rechnung stornieren
        if b.invoice_id:
            inv = db.session.get(Invoice, b.invoice_id)
            if inv and inv.status not in (Invoice.STATUS_CANCELLED,):
                inv.status = Invoice.STATUS_CANCELLED
                flash(
                    f"Rechnung {inv.invoice_number} wurde storniert. "
                    f"Bitte eine neue Rechnung ausstellen.",
                    "warning",
                )

        # Offenen Posten zurücksetzen wenn gewünscht
        if b.open_item_id and request.form.get("close_open_item"):
            oi = db.session.get(OpenItem, b.open_item_id)
            if oi:
                oi.status = OpenItem.STATUS_OPEN

        db.session.commit()
        flash("Buchung erfolgreich storniert.", "success")
        return redirect(url_for("accounting.bookings"))

    return render_template("accounting/storno_form.html", booking=b)


# ---------------------------------------------------------------------------
# Offene Posten
# ---------------------------------------------------------------------------

@bp.route("/open-items")
@login_required
def open_items():
    show_closed = request.args.get("show_closed", "0") == "1"
    amount_min_raw = request.args.get("amount_min", "").strip()
    amount_max_raw = request.args.get("amount_max", "").strip()
    customer_q = request.args.get("customer", "").strip()
    ref_q = request.args.get("ref", "").strip()  # Rechnungsnr. oder Beschreibung
    year_q = request.args.get("year", "").strip()

    item_q = OpenItem.query.join(Customer, OpenItem.customer_id == Customer.id)

    if not show_closed:
        item_q = item_q.filter(OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]))

    if customer_q:
        item_q = item_q.filter(Customer.name.ilike(f"%{customer_q}%"))
    if ref_q:
        item_q = item_q.filter(OpenItem.description.ilike(f"%{ref_q}%"))
    if year_q:
        try:
            item_q = item_q.filter(OpenItem.period_year == int(year_q))
        except ValueError:
            pass
    if amount_min_raw:
        try:
            item_q = item_q.filter(OpenItem.amount >= Decimal(amount_min_raw.replace(",", ".")))
        except Exception:
            pass
    if amount_max_raw:
        try:
            item_q = item_q.filter(OpenItem.amount <= Decimal(amount_max_raw.replace(",", ".")))
        except Exception:
            pass

    items = item_q.order_by(OpenItem.due_date).all()
    total_open = sum(item.open_balance for item in items)
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()

    return render_template(
        "accounting/open_items.html",
        items=items,
        total_open=total_open,
        today=date.today(),
        show_closed=show_closed,
        f_customer=customer_q,
        f_ref=ref_q,
        f_year=year_q,
        f_amount_min=amount_min_raw,
        f_amount_max=amount_max_raw,
        real_accounts=real_accounts,
    )


@bp.route("/open-items/new", methods=["GET", "POST"])
@login_required
def open_item_new():
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    if request.method == "POST":
        from decimal import Decimal
        amount_raw = request.form.get("amount", "0").replace(",", ".")
        item = OpenItem(
            customer_id=int(request.form["customer_id"]),
            description=request.form["description"].strip(),
            notes=request.form.get("notes", "").strip(),
            amount=Decimal(amount_raw),
            date=date.fromisoformat(request.form["date"]),
            due_date=date.fromisoformat(request.form["due_date"]) if request.form.get("due_date") else None,
            status=OpenItem.STATUS_OPEN,
            created_by_id=current_user.id,
        )
        db.session.add(item)
        db.session.commit()
        flash("Offener Posten angelegt.", "success")
        return redirect(url_for("accounting.open_items"))
    return render_template("accounting/open_item_form.html", item=None, customers=customers, today=date.today())


@bp.route("/open-items/<int:item_id>/pay", methods=["POST"])
@login_required
def open_item_pay(item_id):
    """Zahlung (Teil- oder Vollzahlung) auf einen manuellen offenen Posten buchen."""
    item = db.get_or_404(OpenItem, item_id)
    from decimal import Decimal
    amount_raw = request.form.get("amount", "0").replace(",", ".")
    try:
        amount = Decimal(amount_raw)
    except Exception:
        flash("Ungültiger Betrag.", "danger")
        return redirect(url_for("accounting.open_items"))
    if amount <= 0:
        flash("Betrag muss positiv sein.", "danger")
        return redirect(url_for("accounting.open_items"))

    acc = Account.query.filter_by(active=True).first()
    if not acc:
        flash("Kein aktives Konto gefunden.", "danger")
        return redirect(url_for("accounting.open_items"))

    real_account_id_raw = request.form.get("real_account_id") or None
    ref = item.invoice.invoice_number if item.invoice_id else f"OP-{item.id}"
    booking = Booking(
        date=date.today(),
        account_id=acc.id,
        amount=amount,
        description=f"Zahlung – {item.description} – {item.customer.name}",
        reference=ref,
        open_item_id=item.id,
        invoice_id=item.invoice_id,
        real_account_id=int(real_account_id_raw) if real_account_id_raw else None,
        created_by_id=current_user.id,
    )
    db.session.add(booking)
    db.session.flush()

    paid_total = db.session.query(func.sum(Booking.amount)).filter(
        Booking.open_item_id == item.id
    ).scalar() or Decimal("0")
    balance = Decimal(str(item.amount)) - Decimal(str(paid_total))

    if balance > Decimal("0"):
        item.status = OpenItem.STATUS_PARTIAL
        flash(f"Teilzahlung von {amount:.2f} \u20ac gebucht. Offener Restbetrag: {balance:.2f} \u20ac", "success")
    elif balance == Decimal("0"):
        item.status = OpenItem.STATUS_PAID
        flash("Offener Posten vollst\u00e4ndig bezahlt.", "success")
    else:
        item.status = OpenItem.STATUS_CREDIT
        flash(f"\u00dcberzahlung von {abs(balance):.2f} \u20ac. Offener Posten als Gutschrift markiert.", "info")

    # Verknüpfte Rechnung synchronisieren
    if item.invoice_id:
        inv = db.session.get(Invoice, item.invoice_id)
        if inv:
            if balance > Decimal("0"):
                inv.status = Invoice.STATUS_SENT
            elif balance == Decimal("0"):
                inv.status = Invoice.STATUS_PAID
            else:
                inv.status = Invoice.STATUS_CREDIT

    db.session.commit()
    return redirect(url_for("accounting.open_items"))


@bp.route("/open-items/<int:item_id>/invoice", methods=["GET", "POST"])
@login_required
def open_item_invoice(item_id):
    """Rechnung aus einem manuellen offenen Posten generieren."""
    item = db.get_or_404(OpenItem, item_id)
    tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()

    if request.method == "POST":
        from decimal import Decimal
        from datetime import timedelta
        from app.models import Invoice, InvoiceItem

        inv_date = date.fromisoformat(request.form["date"])
        due_date = date.fromisoformat(request.form["due_date"]) if request.form.get("due_date") else None
        notes = request.form.get("notes", "").strip()

        inv = Invoice(
            invoice_number=next_invoice_number(inv_date.year),
            customer_id=item.customer_id,
            date=inv_date,
            due_date=due_date,
            status=Invoice.STATUS_DRAFT,
            notes=notes,
            created_by_id=current_user.id,
        )
        db.session.add(inv)
        db.session.flush()

        row_types = request.form.getlist("row_type[]")
        row_tariff_ids = request.form.getlist("row_tariff_id[]")
        row_consumptions = request.form.getlist("row_consumption_m3[]")
        row_descriptions = request.form.getlist("row_description[]")
        row_quantities = request.form.getlist("row_quantity[]")
        row_units = request.form.getlist("row_unit[]")
        row_unit_prices = request.form.getlist("row_unit_price[]")
        row_tax_rates = request.form.getlist("row_tax_rate[]")

        for i, rtype in enumerate(row_types):
            def _dec(lst, idx, default="0"):
                v = lst[idx].replace(",", ".") if idx < len(lst) and lst[idx].strip() else default
                try:
                    return Decimal(v)
                except Exception:
                    return Decimal(default)

            if rtype == "tariff":
                tariff_id = int(row_tariff_ids[i]) if i < len(row_tariff_ids) and row_tariff_ids[i] else None
                if not tariff_id:
                    continue
                tariff = db.session.get(WaterTariff, tariff_id)
                if not tariff:
                    continue
                consumption = _dec(row_consumptions, i)
                if tariff.base_fee:
                    db.session.add(InvoiceItem(
                        invoice_id=inv.id,
                        description="Grundgebühr",
                        quantity=Decimal("1"),
                        unit="Jahr",
                        unit_price=tariff.base_fee,
                        amount=tariff.base_fee,
                    ))
                amount = (consumption * tariff.price_per_m3).quantize(Decimal("0.01"))
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=f"Wasserverbrauch ({consumption} m\u00b3 \u00d7 {tariff.price_per_m3} \u20ac/m\u00b3)",
                    quantity=consumption,
                    unit="m\u00b3",
                    unit_price=tariff.price_per_m3,
                    amount=amount,
                ))
            elif rtype == "water":
                consumption = _dec(row_consumptions, i)
                unit_price = _dec(row_unit_prices, i)
                desc = row_descriptions[i].strip() if i < len(row_descriptions) and row_descriptions[i].strip() else f"Wasserverbrauch ({consumption} m\u00b3)"
                amount = (consumption * unit_price).quantize(Decimal("0.01"))
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=desc,
                    quantity=consumption,
                    unit="m\u00b3",
                    unit_price=unit_price,
                    amount=amount,
                ))
            else:  # free
                desc = row_descriptions[i].strip() if i < len(row_descriptions) else ""
                if not desc:
                    continue
                qty = _dec(row_quantities, i, "1")
                unit = row_units[i] if i < len(row_units) and row_units[i].strip() else "Stk"
                unit_price = _dec(row_unit_prices, i)
                tax_rate = _dec(row_tax_rates, i)
                amount = (qty * unit_price).quantize(Decimal("0.01"))
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=desc,
                    quantity=qty,
                    unit=unit,
                    unit_price=unit_price,
                    amount=amount,
                    tax_rate=tax_rate if tax_rate > 0 else None,
                ))

        inv.recalculate_total()
        item.invoice_id = inv.id
        db.session.commit()
        flash(f"Rechnung {inv.invoice_number} erstellt.", "success")
        return redirect(url_for("invoices.detail", invoice_id=inv.id))

    return render_template(
        "accounting/open_item_invoice.html",
        item=item,
        tariffs=tariffs,
        today=date.today(),
    )


# ---------------------------------------------------------------------------
# Jahresabschluss / Berichte
# ---------------------------------------------------------------------------

@bp.route("/report")
@login_required
def report():
    year = request.args.get("year", date.today().year, type=int)
    real_account_id = request.args.get("real_account_id", 0, type=int)  # 0 = alle

    all_real_accounts = RealAccount.query.order_by(RealAccount.name).all()

    def _base_filter(q):
        q = q.filter(
            extract("year", Booking.date) == year,
            Booking.status != Booking.STATUS_STORNIERT,
        )
        if real_account_id:
            q = q.filter(Booking.real_account_id == real_account_id)
        return q

    rows = _base_filter(
        db.session.query(
            Account.name,
            func.sum(Booking.amount).label("total"),
        )
        .join(Booking, Booking.account_id == Account.id)
    ).group_by(Account.id).order_by(Account.name).all()

    income_rows = [(r.name, r.total) for r in rows if r.total is not None and r.total > 0]
    expense_rows = [(r.name, abs(r.total)) for r in rows if r.total is not None and r.total < 0]
    total_income = sum(r[1] for r in income_rows)
    total_expense = sum(r[1] for r in expense_rows)
    balance = total_income - total_expense

    # Projektübersicht
    project_rows = _base_filter(
        db.session.query(
            Project.id.label("project_id"),
            Project.name.label("project_name"),
            Account.name.label("account_name"),
            func.sum(Booking.amount).label("total"),
        )
        .select_from(Booking)
        .outerjoin(Project, Booking.project_id == Project.id)
        .join(Account, Booking.account_id == Account.id)
    ).group_by(Project.id, Project.name, Account.id, Account.name).order_by(
        case((Project.id == None, 1), else_=0),
        Project.name,
        Account.name,
    ).all()

    project_summary = {}
    for row in project_rows:
        key = row.project_id
        if key not in project_summary:
            project_summary[key] = {
                "name": row.project_name or "Ohne Projekt",
                "accounts": [],
                "income": Decimal("0"),
                "expense": Decimal("0"),
            }
        row_total = row.total or Decimal("0")
        project_summary[key]["accounts"].append(
            (row.account_name, row_total)
        )
        if row_total >= 0:
            project_summary[key]["income"] += row_total
        else:
            project_summary[key]["expense"] += abs(row_total)

    project_summary_list = [
        v for k, v in sorted(
            project_summary.items(),
            key=lambda x: (x[0] is None, x[1]["name"]),
        )
    ]

    # Kontenentwicklung Bankkonten
    konten_list = []
    for ra in all_real_accounts:
        jan1 = _jan1_balance(ra, year)
        dec31 = _year_end_balance(ra, year)
        bkgs = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            extract("year", Booking.date) == year,
            Booking.status != Booking.STATUS_STORNIERT,
        ).scalar() or Decimal("0")
        konten_list.append({
            "name": ra.name,
            "iban": ra.iban or "",
            "jan1": jan1,
            "bewegung": Decimal(str(bkgs)),
            "dec31": dec31,
        })

    return render_template(
        "accounting/report.html",
        year=year,
        real_account_id=real_account_id,
        all_real_accounts=all_real_accounts,
        income_rows=income_rows,
        expense_rows=expense_rows,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        project_summary_list=project_summary_list,
        konten_list=konten_list,
    )


@bp.route("/report/export/excel")
@login_required
def report_export_excel():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    import io as _io

    year = request.args.get("year", date.today().year, type=int)

    # ---- Hilfsfunktionen ----
    HDR_FILL = PatternFill("solid", fgColor="2F5496")
    HDR_FONT = Font(bold=True, color="FFFFFF")
    SUBHDR_FILL = PatternFill("solid", fgColor="BDD7EE")
    SUBHDR_FONT = Font(bold=True)
    TOTAL_FONT = Font(bold=True)
    EUR_FMT = '#,##0.00 "€"'
    DATE_FMT = "DD.MM.YYYY"
    thin = Side(style="thin")
    BORDER = Border(bottom=thin)

    def _hdr(ws, row, cols):
        for c, val in enumerate(cols, 1):
            cell = ws.cell(row=row, column=c, value=val)
            cell.font = HDR_FONT
            cell.fill = HDR_FILL
            cell.alignment = Alignment(horizontal="center")

    def _subhdr(ws, row, cols):
        for c, val in enumerate(cols, 1):
            cell = ws.cell(row=row, column=c, value=val)
            cell.font = SUBHDR_FONT
            cell.fill = SUBHDR_FILL

    def _autowidth(ws, min_w=10, max_w=50):
        for col in ws.columns:
            length = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_w, max(min_w, length + 2))

    def _eur(ws, row, col, val):
        cell = ws.cell(row=row, column=col, value=float(val))
        cell.number_format = EUR_FMT
        return cell

    def _total_row(ws, row, label, val, ncols):
        ws.cell(row=row, column=1, value=label).font = TOTAL_FONT
        c = _eur(ws, row, ncols, val)
        c.font = TOTAL_FONT
        c.border = BORDER

    # ---- Daten ermitteln ----
    all_real_accounts = RealAccount.query.order_by(RealAccount.name).all()

    def _bookings_q():
        return (
            Booking.query
            .filter(extract("year", Booking.date) == year)
            .filter(Booking.status != Booking.STATUS_STORNIERT)
            .order_by(Booking.date)
            .all()
        )

    bookings_year = _bookings_q()

    # Einnahmen/Ausgaben nach Konto
    rows_q = (
        db.session.query(Account.name, func.sum(Booking.amount).label("total"))
        .join(Booking, Booking.account_id == Account.id)
        .filter(extract("year", Booking.date) == year)
        .filter(Booking.status != Booking.STATUS_STORNIERT)
        .group_by(Account.id).order_by(Account.name).all()
    )
    income_rows = [(r.name, Decimal(str(r.total))) for r in rows_q if r.total is not None and r.total > 0]
    expense_rows = [(r.name, abs(Decimal(str(r.total)))) for r in rows_q if r.total is not None and r.total < 0]
    total_income = sum(r[1] for r in income_rows)
    total_expense = sum(r[1] for r in expense_rows)
    balance = total_income - total_expense

    # Kontenentwicklung
    konten_list = []
    for ra in all_real_accounts:
        jan1 = _jan1_balance(ra, year)
        dec31 = _year_end_balance(ra, year)
        bkgs = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            extract("year", Booking.date) == year,
            Booking.status != Booking.STATUS_STORNIERT,
        ).scalar() or Decimal("0")
        konten_list.append({
            "name": ra.name, "iban": ra.iban or "",
            "jan1": jan1, "bewegung": Decimal(str(bkgs)), "dec31": dec31,
        })

    # Projektübersicht
    project_rows_q = (
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
        .filter(Booking.status != Booking.STATUS_STORNIERT)
        .group_by(Project.id, Project.name, Account.id, Account.name)
        .order_by(case((Project.id == None, 1), else_=0), Project.name, Account.name)
        .all()
    )
    project_summary = {}
    for row in project_rows_q:
        key = row.project_id
        if key not in project_summary:
            project_summary[key] = {"name": row.project_name or "Ohne Projekt", "accounts": [], "income": Decimal("0"), "expense": Decimal("0")}
        rt = row.total or Decimal("0")
        project_summary[key]["accounts"].append((row.account_name, rt))
        if rt >= 0:
            project_summary[key]["income"] += rt
        else:
            project_summary[key]["expense"] += abs(rt)
    project_list = [v for k, v in sorted(project_summary.items(), key=lambda x: (x[0] is None, x[1]["name"]))]

    # ---- Workbook aufbauen ----
    wb = openpyxl.Workbook()

    # ================================================================
    # BLATT 1: Übersicht
    # ================================================================
    ws = wb.active
    ws.title = "Übersicht"
    r = 1
    ws.merge_cells(f"A{r}:D{r}")
    cell = ws.cell(row=r, column=1, value=f"Jahresbericht {year}")
    cell.font = Font(bold=True, size=14, color="FFFFFF")
    cell.fill = HDR_FILL
    cell.alignment = Alignment(horizontal="center")
    r += 2

    _subhdr(ws, r, ["Kennzahl", "", "Betrag"])
    r += 1
    _eur(ws, r, 3, total_income); ws.cell(row=r, column=1, value="Gesamteinnahmen"); r += 1
    _eur(ws, r, 3, total_expense); ws.cell(row=r, column=1, value="Gesamtausgaben"); r += 1
    c = _eur(ws, r, 3, balance)
    c.font = Font(bold=True, color="375623" if balance >= 0 else "9C0006")
    ws.cell(row=r, column=1, value="Saldo").font = TOTAL_FONT
    r += 2

    _subhdr(ws, r, ["Bankkonto", "IBAN", "Stand 1.1.", "Stand 31.12."])
    r += 1
    total_jan1 = Decimal("0"); total_dec31 = Decimal("0")
    for k in konten_list:
        ws.cell(row=r, column=1, value=k["name"])
        ws.cell(row=r, column=2, value=k["iban"])
        _eur(ws, r, 3, k["jan1"]); _eur(ws, r, 4, k["dec31"])
        total_jan1 += k["jan1"]; total_dec31 += k["dec31"]
        r += 1
    ws.cell(row=r, column=1, value="Gesamt").font = TOTAL_FONT
    c3 = _eur(ws, r, 3, total_jan1); c3.font = TOTAL_FONT; c3.border = BORDER
    c4 = _eur(ws, r, 4, total_dec31); c4.font = TOTAL_FONT; c4.border = BORDER
    r += 2

    # USt-Zahllast Jahresübersicht
    ust_rows_y, vst_rows_y = _ust_berechnen(year, 0)
    zahllast_y = sum(v["steuer"] for _, v in ust_rows_y) - sum(v["steuer"] for _, v in vst_rows_y)
    _subhdr(ws, r, ["USt/VSt", "", "Betrag"])
    r += 1
    _eur(ws, r, 3, sum(v["steuer"] for _, v in ust_rows_y)); ws.cell(row=r, column=1, value="Umsatzsteuer gesamt"); r += 1
    _eur(ws, r, 3, sum(v["steuer"] for _, v in vst_rows_y)); ws.cell(row=r, column=1, value="Vorsteuer gesamt"); r += 1
    c = _eur(ws, r, 3, zahllast_y); c.font = TOTAL_FONT
    ws.cell(row=r, column=1, value="Zahllast").font = TOTAL_FONT
    _autowidth(ws)

    # ================================================================
    # BLATT 2: Einnahmen & Ausgaben
    # ================================================================
    ws2 = wb.create_sheet("Einnahmen & Ausgaben")
    _hdr(ws2, 1, ["Konto", "Typ", "Betrag"])
    r2 = 2
    for name, amt in income_rows:
        ws2.cell(row=r2, column=1, value=name)
        ws2.cell(row=r2, column=2, value="Einnahme")
        _eur(ws2, r2, 3, amt); r2 += 1
    _total_row(ws2, r2, "Einnahmen gesamt", total_income, 3); r2 += 2
    for name, amt in expense_rows:
        ws2.cell(row=r2, column=1, value=name)
        ws2.cell(row=r2, column=2, value="Ausgabe")
        _eur(ws2, r2, 3, amt); r2 += 1
    _total_row(ws2, r2, "Ausgaben gesamt", total_expense, 3); r2 += 2
    ws2.cell(row=r2, column=1, value="Saldo").font = TOTAL_FONT
    c = _eur(ws2, r2, 3, balance); c.font = TOTAL_FONT; c.border = BORDER
    _autowidth(ws2)

    # ================================================================
    # BLATT 3: Buchungen
    # ================================================================
    ws3 = wb.create_sheet("Buchungen")
    _hdr(ws3, 1, ["Datum", "Bankkonto", "Konto", "Typ", "Beschreibung", "Belegnummer", "Projekt", "Kunde", "MwSt %", "MwSt Betrag", "Betrag", "Status"])
    r3 = 2
    for b in bookings_year:
        tax_amt = ""
        if b.tax_rate and b.tax_rate > 0:
            tax_amt = float((abs(b.amount) * Decimal(str(b.tax_rate)) / (100 + Decimal(str(b.tax_rate)))).quantize(Decimal("0.01")))
        ws3.cell(row=r3, column=1, value=b.date).number_format = DATE_FMT
        ws3.cell(row=r3, column=2, value=b.real_account.name if b.real_account else "")
        ws3.cell(row=r3, column=3, value=b.account.name)
        ws3.cell(row=r3, column=4, value="Einnahme" if b.amount >= 0 else "Ausgabe")
        ws3.cell(row=r3, column=5, value=b.description)
        ws3.cell(row=r3, column=6, value=b.reference or "")
        ws3.cell(row=r3, column=7, value=b.project.name if b.project else "")
        ws3.cell(row=r3, column=8, value=b.customer.name if b.customer else "")
        ws3.cell(row=r3, column=9, value=int(b.tax_rate) if b.tax_rate else "")
        if tax_amt != "":
            _eur(ws3, r3, 10, tax_amt)
        _eur(ws3, r3, 11, float(b.amount))
        ws3.cell(row=r3, column=12, value=b.status or "")
        r3 += 1
    _autowidth(ws3)

    # ================================================================
    # BLATT 4: Bankkonten-Entwicklung
    # ================================================================
    ws4 = wb.create_sheet("Bankkonten")
    _hdr(ws4, 1, ["Bankkonto", "IBAN", f"Stand 1.1.{year}", f"Bewegung {year}", f"Stand 31.12.{year}"])
    r4 = 2
    sum_jan1 = Decimal("0"); sum_dec31 = Decimal("0")
    for k in konten_list:
        ws4.cell(row=r4, column=1, value=k["name"])
        ws4.cell(row=r4, column=2, value=k["iban"])
        _eur(ws4, r4, 3, k["jan1"])
        _eur(ws4, r4, 4, k["bewegung"])
        _eur(ws4, r4, 5, k["dec31"])
        sum_jan1 += k["jan1"]; sum_dec31 += k["dec31"]
        r4 += 1
    ws4.cell(row=r4, column=1, value="Gesamt").font = TOTAL_FONT
    for col, val in [(3, sum_jan1), (4, sum_dec31 - sum_jan1), (5, sum_dec31)]:
        c = _eur(ws4, r4, col, val); c.font = TOTAL_FONT; c.border = BORDER
    _autowidth(ws4)

    # ================================================================
    # BLATT 5: Projekte
    # ================================================================
    ws5 = wb.create_sheet("Projekte")
    _hdr(ws5, 1, ["Projekt", "Konto", "Einnahmen", "Ausgaben", "Saldo"])
    r5 = 2
    for ps in project_list:
        start_r = r5
        for acc_name, amt in ps["accounts"]:
            ws5.cell(row=r5, column=1, value=ps["name"])
            ws5.cell(row=r5, column=2, value=acc_name)
            if amt >= 0:
                _eur(ws5, r5, 3, amt)
            else:
                _eur(ws5, r5, 4, abs(amt))
            r5 += 1
        # Projektsumme
        ws5.cell(row=r5, column=1, value=ps["name"]).font = TOTAL_FONT
        ws5.cell(row=r5, column=2, value="Gesamt").font = TOTAL_FONT
        c3 = _eur(ws5, r5, 3, ps["income"]); c3.font = TOTAL_FONT; c3.border = BORDER
        c4 = _eur(ws5, r5, 4, ps["expense"]); c4.font = TOTAL_FONT; c4.border = BORDER
        c5 = _eur(ws5, r5, 5, ps["income"] - ps["expense"]); c5.font = TOTAL_FONT; c5.border = BORDER
        r5 += 2
    _autowidth(ws5)

    # ================================================================
    # BLÄTTER 6-10: USt-Voranmeldungen Q1–Q4 + Gesamtjahr
    # ================================================================
    def _ust_sheet(ws_ust, label, date_from, date_to, ust_r, vst_r):
        ws_ust.cell(row=1, column=1, value="Zeitraum").font = SUBHDR_FONT
        ws_ust.cell(row=1, column=2, value=label)
        ws_ust.cell(row=2, column=1, value="Von").font = SUBHDR_FONT
        ws_ust.cell(row=2, column=2, value=date_from).number_format = DATE_FMT
        ws_ust.cell(row=3, column=1, value="Bis").font = SUBHDR_FONT
        ws_ust.cell(row=3, column=2, value=date_to).number_format = DATE_FMT

        _hdr(ws_ust, 5, ["Abschnitt", "Steuersatz %", "Bruttobetrag", "Nettobetrag", "Steuerbetrag"])
        ru = 6
        for rate, v in ust_r:
            ws_ust.cell(row=ru, column=1, value="Umsatzsteuer")
            ws_ust.cell(row=ru, column=2, value=rate)
            _eur(ws_ust, ru, 3, v["brutto"]); _eur(ws_ust, ru, 4, v["netto"]); _eur(ws_ust, ru, 5, v["steuer"])
            ru += 1
        for rate, v in vst_r:
            ws_ust.cell(row=ru, column=1, value="Vorsteuer")
            ws_ust.cell(row=ru, column=2, value=rate)
            _eur(ws_ust, ru, 3, v["brutto"]); _eur(ws_ust, ru, 4, v["netto"]); _eur(ws_ust, ru, 5, v["steuer"])
            ru += 1
        ru += 1
        total_u = sum(v["steuer"] for _, v in ust_r)
        total_v = sum(v["steuer"] for _, v in vst_r)
        zahllast = total_u - total_v
        for lbl, val in [("Umsatzsteuer gesamt", total_u), ("Vorsteuer gesamt", total_v), ("Zahllast", zahllast)]:
            ws_ust.cell(row=ru, column=1, value=lbl).font = TOTAL_FONT
            c = _eur(ws_ust, ru, 5, val); c.font = TOTAL_FONT; c.border = BORDER
            ru += 1
        _autowidth(ws_ust)

    for q in range(1, 5):
        df, dt = _ust_period(year, q)
        ur, vr = _ust_berechnen(year, q)
        ws_q = wb.create_sheet(f"USt Q{q}")
        _ust_sheet(ws_q, f"Q{q}/{year}", df, dt, ur, vr)

    df_y, dt_y = _ust_period(year, 0)
    ws_y = wb.create_sheet("USt Gesamtjahr")
    _ust_sheet(ws_y, str(year), df_y, dt_y, ust_rows_y, vst_rows_y)

    # ---- Ausgabe ----
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=jahresbericht_{year}.xlsx"},
    )


# ---------------------------------------------------------------------------
# Buchungen-Import
# ---------------------------------------------------------------------------

def _parse_at_number(raw):
    """Österreichisches Zahlenformat: Leerzeichen/Punkt als Tausender, Komma als Dezimal."""
    raw = str(raw).strip().replace('\xa0', '').replace(' ', '')
    if not raw or raw == 'nan':
        return None
    if ',' in raw and '.' in raw:
        raw = raw.replace('.', '')
    raw = raw.replace(',', '.')
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _ensure_umbuchungs_account(account_cache, kst_name):
    """Stellt sicher, dass ein Account für den Umbuchungs-KST im Cache vorhanden ist."""
    if kst_name not in account_cache:
        acc = Account.query.filter_by(name=kst_name).first()
        if not acc:
            acc = Account(name=kst_name)
            db.session.add(acc)
            db.session.flush()
        account_cache[kst_name] = acc


def _find_customer(name, customers, customer_name_map):
    """Exakte Suche nach Kunde anhand des Namens (Vor- und Nachname müssen übereinstimmen)."""
    if not name or not name.strip():
        return None
    name_lower = name.strip().lower()

    # Exakte Übereinstimmung
    if name_lower in customer_name_map:
        return customer_name_map[name_lower]

    # Umgekehrte Reihenfolge (Nachname Vorname → Vorname Nachname)
    parts = name.strip().split()
    if len(parts) == 2:
        reversed_name = f"{parts[1]} {parts[0]}".lower()
        if reversed_name in customer_name_map:
            return customer_name_map[reversed_name]

    return None


@bp.route("/bookings/import", methods=["GET", "POST"])
@login_required
def import_bookings():
    import pandas as pd

    if request.method == "POST":
        # ------------------------------------------------------------------
        # Stufe 1: Datei hochladen → Spaltenvorschau
        # ------------------------------------------------------------------
        if "file" in request.files and request.files["file"].filename:
            f = request.files["file"]
            try:
                raw_bytes = f.read()
                # Versuche UTF-8-sig, dann latin-1
                for enc in ("utf-8-sig", "latin-1"):
                    try:
                        df = pd.read_csv(
                            io.BytesIO(raw_bytes), sep=";", dtype=str,
                            encoding=enc, keep_default_na=False,
                        )
                        break
                    except Exception:
                        continue
                else:
                    flash("Fehler beim Lesen der Datei.", "danger")
                    return redirect(url_for("accounting.import_bookings"))
            except Exception as e:
                flash(f"Fehler beim Lesen der Datei: {e}", "danger")
                return redirect(url_for("accounting.import_bookings"))

            file_content_b64 = base64.b64encode(raw_bytes).decode("ascii")
            columns = list(df.columns)
            preview = df.head(5).to_dict(orient="records")

            # Spalten automatisch vorauswählen
            def _auto(names):
                for n in names:
                    if n in columns:
                        return n
                return ""

            auto = {
                "datum": _auto(["Datum"]),
                "kst": _auto(["KST"]),
                "ausgaben": _auto(["Ausgaben"]),
                "einnahmen": _auto(["Einnahmen"]),
                "konto": _auto(["Konto"]),
                "ktr": _auto(["KTR"]),
                "name": _auto(["Name"]),
                "beschreibung": _auto(["Beschreibung"]),
                "steuer": _auto(["Steuer"]),
            }
            return render_template(
                "accounting/import_bookings_mapping.html",
                columns=columns,
                preview=preview,
                file_content=file_content_b64,
                auto=auto,
            )

        # ------------------------------------------------------------------
        # Stufe 2: Mapping bestätigen → Buchungen anlegen
        # ------------------------------------------------------------------
        if request.form.get("confirm") == "1":
            file_content_b64 = request.form.get("file_content", "")
            if not file_content_b64:
                flash("Import-Daten fehlen, bitte Datei erneut hochladen.", "danger")
                return redirect(url_for("accounting.import_bookings"))

            try:
                raw_bytes = base64.b64decode(file_content_b64)
                for enc in ("utf-8-sig", "latin-1"):
                    try:
                        df = pd.read_csv(
                            io.BytesIO(raw_bytes), sep=";", dtype=str,
                            encoding=enc, keep_default_na=False,
                        )
                        break
                    except Exception:
                        continue
                else:
                    flash("Fehler beim Lesen der Import-Daten.", "danger")
                    return redirect(url_for("accounting.import_bookings"))
            except Exception as e:
                flash(f"Fehler: {e}", "danger")
                return redirect(url_for("accounting.import_bookings"))

            # Spaltenmapping aus Formular
            col_datum = request.form.get("col_datum", "")
            col_kst = request.form.get("col_kst", "")
            col_ausgaben = request.form.get("col_ausgaben", "")
            col_einnahmen = request.form.get("col_einnahmen", "")
            col_konto = request.form.get("col_konto", "")
            col_ktr = request.form.get("col_ktr", "")
            col_name = request.form.get("col_name", "")
            col_beschreibung = request.form.get("col_beschreibung", "")
            col_steuer = request.form.get("col_steuer", "")
            umbuchungs_kst = request.form.get("umbuchungs_kst", "").strip()

            if not col_datum or not col_kst:
                flash("Pflichtfelder Datum und KST (Konto) müssen zugeordnet sein.", "danger")
                return redirect(url_for("accounting.import_bookings"))

            # Kunden-Cache aufbauen
            alle_kunden = Customer.query.filter_by(active=True).all()
            customer_name_map = {c.name.lower(): c.id for c in alle_kunden}

            # Konto/Projekt/Bankkonto-Caches
            account_cache = {}      # name → Account
            project_cache = {}      # name → Project
            real_account_cache = {} # name → RealAccount

            results = {"ok": 0, "skip": 0, "matched": 0, "transfers": 0, "transfer_warnings": []}

            # Umbuchungs-Kandidaten: Liste von dicts mit geparsten Feldern
            transfer_candidates = []  # {"amount": Decimal, "date": date, "real_account_id": int|None, "description": str}

            for _, row in df.iterrows():
                def _col(c):
                    v = str(row.get(c, "")).strip() if c else ""
                    return v if v and v.lower() != "nan" else ""

                # Betrag bestimmen
                amount = None
                is_ausgabe = False
                ausgaben_raw = _col(col_ausgaben)
                einnahmen_raw = _col(col_einnahmen)

                if ausgaben_raw:
                    amount = _parse_at_number(ausgaben_raw)
                    is_ausgabe = True
                elif einnahmen_raw:
                    amount = _parse_at_number(einnahmen_raw)

                if amount is None:
                    results["skip"] += 1
                    continue

                # Ausgaben-Spalte: Betrag muss negativ sein
                if is_ausgabe and amount > 0:
                    amount = -amount

                # Datum parsen
                datum_raw = _col(col_datum)
                if not datum_raw:
                    results["skip"] += 1
                    continue
                try:
                    if "." in datum_raw:
                        from datetime import datetime as _dt
                        booking_date = _dt.strptime(datum_raw, "%d.%m.%Y").date()
                    else:
                        booking_date = date.fromisoformat(datum_raw)
                except Exception:
                    results["skip"] += 1
                    continue

                # Konto (KST) ermitteln
                kst_name = _col(col_kst)
                if not kst_name:
                    results["skip"] += 1
                    continue

                # Reales Bankkonto ermitteln / anlegen
                real_account_id = None
                konto_name = _col(col_konto)
                if konto_name:
                    if konto_name not in real_account_cache:
                        ra = RealAccount.query.filter_by(name=konto_name).first()
                        if not ra:
                            ra = RealAccount(name=konto_name)
                            db.session.add(ra)
                            db.session.flush()
                        real_account_cache[konto_name] = ra
                    real_account_id = real_account_cache[konto_name].id

                # Umbuchungs-Kandidat: in separate Liste stellen
                if umbuchungs_kst and kst_name == umbuchungs_kst:
                    beschreibung = _col(col_beschreibung)
                    import_name = _col(col_name)
                    parts = [p for p in [import_name, beschreibung] if p]
                    description = " – ".join(parts) if parts else "Umbuchung"
                    transfer_candidates.append({
                        "amount": amount,
                        "date": booking_date,
                        "real_account_id": real_account_id,
                        "description": description[:500],
                    })
                    continue

                if kst_name not in account_cache:
                    acc = Account.query.filter_by(name=kst_name).first()
                    if not acc:
                        acc = Account(name=kst_name)
                        db.session.add(acc)
                        db.session.flush()
                    account_cache[kst_name] = acc
                acc = account_cache[kst_name]

                # Projekt ermitteln / anlegen
                project_id = None
                ktr_name = _col(col_ktr)
                if ktr_name:
                    if ktr_name not in project_cache:
                        proj = Project.query.filter_by(name=ktr_name).first()
                        if not proj:
                            proj = Project(name=ktr_name)
                            db.session.add(proj)
                            db.session.flush()
                        project_cache[ktr_name] = proj
                    project_id = project_cache[ktr_name].id

                # Kunde suchen
                import_name = _col(col_name)
                customer_id = _find_customer(import_name, alle_kunden, customer_name_map)
                if customer_id:
                    results["matched"] += 1

                # Beschreibung
                beschreibung = _col(col_beschreibung)
                if customer_id:
                    description = beschreibung or import_name or "—"
                else:
                    parts = [p for p in [import_name, beschreibung] if p]
                    description = " – ".join(parts) if parts else "—"

                # Steuersatz
                tax_rate = None
                steuer_raw = _col(col_steuer)
                if steuer_raw:
                    try:
                        tr = Decimal(steuer_raw.replace(",", "."))
                        if tr > 0:
                            tax_rate = tr
                    except Exception:
                        pass

                b = Booking(
                    date=booking_date,
                    account_id=acc.id,
                    amount=amount,
                    description=description[:500],
                    real_account_id=real_account_id,
                    project_id=project_id,
                    customer_id=customer_id,
                    tax_rate=tax_rate,
                    created_by_id=current_user.id,
                    status=Booking.STATUS_OFFEN,
                )
                db.session.add(b)
                results["ok"] += 1

            # ------------------------------------------------------------------
            # Umbuchungs-Kandidaten paarweise matchen
            # Regel: Ausgabe (amount < 0) von Konto A + Einnahme (amount > 0) auf Konto B
            # mit gleichem abs(amount) → Transfer
            # ------------------------------------------------------------------
            if transfer_candidates:
                from collections import defaultdict
                # Gruppieren nach abs(amount)
                by_amount = defaultdict(lambda: {"ausgaben": [], "einnahmen": []})
                for tc in transfer_candidates:
                    key = abs(tc["amount"])
                    if tc["amount"] < 0:
                        by_amount[key]["ausgaben"].append(tc)
                    else:
                        by_amount[key]["einnahmen"].append(tc)

                for abs_amt, group in by_amount.items():
                    ausgaben = group["ausgaben"]
                    einnahmen = group["einnahmen"]

                    while ausgaben and einnahmen:
                        aus = ausgaben.pop(0)
                        ein = einnahmen.pop(0)

                        from_ra_id = aus["real_account_id"]
                        to_ra_id = ein["real_account_id"]

                        if from_ra_id is None or to_ra_id is None:
                            results["transfer_warnings"].append(
                                f"Umbuchung {abs_amt:.2f}: Bankkonto fehlt — als normale Buchung importiert."
                            )
                            # Fallback: als normale Buchungen anlegen
                            for tc_fb in [aus, ein]:
                                _ensure_umbuchungs_account(account_cache, umbuchungs_kst)
                                acc_fb = account_cache[umbuchungs_kst]
                                b_fb = Booking(
                                    date=tc_fb["date"],
                                    account_id=acc_fb.id,
                                    amount=tc_fb["amount"],
                                    description=tc_fb["description"],
                                    real_account_id=tc_fb["real_account_id"],
                                    created_by_id=current_user.id,
                                    status=Booking.STATUS_OFFEN,
                                )
                                db.session.add(b_fb)
                                results["ok"] += 1
                            continue

                        if from_ra_id == to_ra_id:
                            results["transfer_warnings"].append(
                                f"Umbuchung {abs_amt:.2f}: Ausgangs- und Zielkonto identisch — übersprungen."
                            )
                            results["skip"] += 2
                            continue

                        t = Transfer(
                            date=aus["date"],
                            amount=abs_amt,
                            description=aus["description"] or ein["description"],
                            from_real_account_id=from_ra_id,
                            to_real_account_id=to_ra_id,
                            created_by_id=current_user.id,
                        )
                        db.session.add(t)
                        results["transfers"] += 1

                    # Nicht gematchte Kandidaten → Warnung + normale Buchung
                    for tc_unmatched in ausgaben + einnahmen:
                        results["transfer_warnings"].append(
                            f"Umbuchung {abs_amt:.2f} ({tc_unmatched['date']}): "
                            f"Keine Gegenbuchung gefunden — als normale Buchung importiert."
                        )
                        _ensure_umbuchungs_account(account_cache, umbuchungs_kst)
                        acc_um = account_cache[umbuchungs_kst]
                        b_um = Booking(
                            date=tc_unmatched["date"],
                            account_id=acc_um.id,
                            amount=tc_unmatched["amount"],
                            description=tc_unmatched["description"],
                            real_account_id=tc_unmatched["real_account_id"],
                            created_by_id=current_user.id,
                            status=Booking.STATUS_OFFEN,
                        )
                        db.session.add(b_um)
                        results["ok"] += 1

            db.session.commit()
            msg = (
                f"Import abgeschlossen: {results['ok']} Buchungen importiert, "
                f"{results['skip']} übersprungen"
            )
            if results["transfers"]:
                msg += f", {results['transfers']} Umbuchungen erstellt"
            if results["matched"]:
                msg += f", {results['matched']} Kunden automatisch zugeordnet"
            msg += "."
            flash(msg, "success" if (results["ok"] or results["transfers"]) else "warning")
            for w in results["transfer_warnings"]:
                flash(w, "warning")
            return redirect(url_for("accounting.bookings"))

    return render_template("accounting/import_bookings.html")


# ---------------------------------------------------------------------------
# CSV-Export
# ---------------------------------------------------------------------------

@bp.route("/bookings/export")
@login_required
def export_csv():
    year = request.args.get("year", date.today().year, type=int)
    bookings = (
        Booking.query
        .filter(extract("year", Booking.date) == year)
        .order_by(Booking.date)
        .all()
    )

    def generate():
        output = io.StringIO()
        output.write("\ufeff")  # UTF-8 BOM für korrekte Darstellung in Excel
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Datum", "Bankkonto", "Konto", "Typ", "Beschreibung",
            "Belegnummer", "Projekt", "Kunde", "MwSt %", "MwSt Betrag", "Betrag", "Status",
        ])
        for b in bookings:
            tax_amount = ""
            if b.tax_rate and b.tax_rate > 0 and b.status != "Storniert":
                tax_amount = str(round(abs(b.amount) * b.tax_rate / (100 + b.tax_rate), 2)).replace(".", ",")
            writer.writerow([
                b.date.strftime("%d.%m.%Y"),
                b.real_account.name if b.real_account else "",
                b.account.name,
                "Einnahme" if b.amount >= 0 else "Ausgabe",
                b.description,
                b.reference or "",
                b.project.name if b.project else "",
                b.customer.name if b.customer else "",
                str(int(b.tax_rate)).replace(".", ",") if b.tax_rate else "",
                tax_amount,
                str(b.amount).replace(".", ","),
                b.status or "",
            ])
        return output.getvalue()

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=buchungen_{year}.csv"},
    )


# ---------------------------------------------------------------------------
# Umsatzsteuervoranmeldung / Umsatzsteuererklärung
# ---------------------------------------------------------------------------

def _ust_period(year, quartal):
    """Gibt (date_from, date_to) für ein Jahr/Quartal zurück."""
    if quartal in (1, 2, 3, 4):
        m_start = (quartal - 1) * 3 + 1
        m_end = quartal * 3
        return date(year, m_start, 1), date(year, m_end, calendar.monthrange(year, m_end)[1])
    return date(year, 1, 1), date(year, 12, 31)


def _ust_berechnen(year, quartal):
    """Berechnet USt/Vorsteuer-Gruppen. Gibt (ust_rows, vst_rows) zurück."""
    date_from, date_to = _ust_period(year, quartal)
    bookings = (
        Booking.query
        .filter(Booking.date >= date_from, Booking.date <= date_to)
        .filter(Booking.status != Booking.STATUS_STORNIERT)
        .filter(Booking.tax_rate.isnot(None), Booking.tax_rate > 0)
        .join(Booking.account)
        .order_by(Booking.date)
        .all()
    )

    def _tax(b):
        rate = Decimal(str(b.tax_rate))
        return (abs(b.amount) * rate / (100 + rate)).quantize(Decimal("0.01"))

    ust_rows = {}
    vst_rows = {}
    for b in bookings:
        tax = _tax(b)
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


@bp.route("/ust")
@login_required
def ust():
    year = request.args.get("year", date.today().year, type=int)
    quartal = request.args.get("quartal", 0, type=int)
    date_from, date_to = _ust_period(year, quartal)
    ust_rows, vst_rows = _ust_berechnen(year, quartal)
    total_ust = sum(v["steuer"] for _, v in ust_rows)
    total_vst = sum(v["steuer"] for _, v in vst_rows)
    zahllast = total_ust - total_vst
    ust_brutto = sum(v["brutto"] for _, v in ust_rows)
    ust_netto = sum(v["netto"] for _, v in ust_rows)
    vst_brutto = sum(v["brutto"] for _, v in vst_rows)
    vst_netto = sum(v["netto"] for _, v in vst_rows)
    return render_template(
        "accounting/ust.html",
        year=year, quartal=quartal,
        date_from=date_from, date_to=date_to,
        ust_rows=ust_rows, vst_rows=vst_rows,
        total_ust=total_ust, total_vst=total_vst, zahllast=zahllast,
        ust_brutto=ust_brutto, ust_netto=ust_netto,
        vst_brutto=vst_brutto, vst_netto=vst_netto,
    )


@bp.route("/ust/export")
@login_required
def export_ust_csv():
    year = request.args.get("year", date.today().year, type=int)
    quartal = request.args.get("quartal", 0, type=int)
    date_from, date_to = _ust_period(year, quartal)
    ust_rows, vst_rows = _ust_berechnen(year, quartal)
    total_ust = sum(v["steuer"] for _, v in ust_rows)
    total_vst = sum(v["steuer"] for _, v in vst_rows)
    zahllast = total_ust - total_vst

    label = f"Q{quartal}/{year}" if quartal else str(year)

    def fmt(d):
        return str(d.quantize(Decimal("0.01"))).replace(".", ",")

    def generate():
        output = io.StringIO()
        output.write("\ufeff")  # UTF-8 BOM
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Zeitraum", label])
        writer.writerow(["Von", date_from.strftime("%d.%m.%Y")])
        writer.writerow(["Bis", date_to.strftime("%d.%m.%Y")])
        writer.writerow([])
        writer.writerow(["Abschnitt", "Steuersatz %", "Bruttobetrag", "Nettobetrag", "Steuerbetrag"])
        for rate, v in ust_rows:
            writer.writerow(["Umsatzsteuer", rate, fmt(v["brutto"]), fmt(v["netto"]), fmt(v["steuer"])])
        for rate, v in vst_rows:
            writer.writerow(["Vorsteuer", rate, fmt(v["brutto"]), fmt(v["netto"]), fmt(v["steuer"])])
        writer.writerow([])
        writer.writerow(["Umsatzsteuer gesamt", "", "", "", fmt(total_ust)])
        writer.writerow(["Vorsteuer gesamt", "", "", "", fmt(total_vst)])
        writer.writerow(["Zahllast", "", "", "", fmt(zahllast)])
        return output.getvalue()

    filename = f"ust_{label.replace('/', '_')}.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Reale Bankkonten
# ---------------------------------------------------------------------------

@bp.route("/real-accounts")
@login_required
def real_accounts():
    year = request.args.get("year", date.today().year, type=int)
    accounts = RealAccount.query.order_by(RealAccount.name).all()

    # Saldo pro Konto basierend auf gespeichertem Jahresanfangsstand
    saldi = {}
    for ra in accounts:
        jan1 = _jan1_balance(ra, year)
        total = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            extract("year", Booking.date) == year,
        ).scalar() or Decimal("0")
        incoming = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.to_real_account_id == ra.id,
            extract("year", Transfer.date) == year,
        ).scalar() or Decimal("0")
        outgoing = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.from_real_account_id == ra.id,
            extract("year", Transfer.date) == year,
        ).scalar() or Decimal("0")
        year_total = Decimal(str(total)) + Decimal(str(incoming)) - Decimal(str(outgoing))
        saldi[ra.id] = {
            "jan1": jan1,
            "year_total": year_total,
            "balance": jan1 + year_total,
        }

    return render_template(
        "accounting/real_accounts.html",
        real_accounts=accounts, saldi=saldi, year=year,
    )


@bp.route("/real-accounts/new", methods=["GET", "POST"])
@login_required
def real_account_new():
    if request.method == "POST":
        opening_raw = request.form.get("opening_balance", "0").replace(",", ".")
        set_default = "is_default" in request.form
        if set_default:
            RealAccount.query.filter_by(is_default=True).update({"is_default": False})
        ra = RealAccount(
            name=request.form["name"].strip(),
            description=request.form.get("description", "").strip(),
            iban=request.form.get("iban", "").strip(),
            opening_balance=Decimal(opening_raw),
            icon=request.form.get("icon", "fa-university").strip() or "fa-university",
            is_default=set_default,
        )
        db.session.add(ra)
        db.session.commit()
        flash("Bankkonto angelegt.", "success")
        return redirect(url_for("accounting.real_accounts"))
    return render_template("accounting/real_account_form.html", real_account=None)


@bp.route("/real-accounts/<int:ra_id>/edit", methods=["GET", "POST"])
@login_required
def real_account_edit(ra_id):
    ra = db.get_or_404(RealAccount, ra_id)
    if request.method == "POST":
        opening_raw = request.form.get("opening_balance", "0").replace(",", ".")
        set_default = "is_default" in request.form
        if set_default:
            RealAccount.query.filter(RealAccount.id != ra.id, RealAccount.is_default == True).update({"is_default": False})
        ra.name = request.form["name"].strip()
        ra.description = request.form.get("description", "").strip()
        ra.iban = request.form.get("iban", "").strip()
        ra.opening_balance = Decimal(opening_raw)
        ra.active = "active" in request.form
        ra.icon = request.form.get("icon", "fa-university").strip() or "fa-university"
        ra.is_default = set_default
        db.session.commit()
        flash("Bankkonto aktualisiert.", "success")
        return redirect(url_for("accounting.real_accounts"))
    return render_template("accounting/real_account_form.html", real_account=ra)


# ---------------------------------------------------------------------------
# Umbuchungen
# ---------------------------------------------------------------------------

@bp.route("/transfers")
@login_required
def transfers():
    year = request.args.get("year", date.today().year, type=int)
    transfers_list = (
        Transfer.query
        .filter(extract("year", Transfer.date) == year)
        .order_by(Transfer.date.desc(), Transfer.id.desc())
        .all()
    )
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()
    years = db.session.query(extract("year", Transfer.date).label("y")).distinct().order_by("y").all()
    all_years = sorted({t.y for t in years} | {date.today().year}, reverse=True)
    return render_template(
        "accounting/transfers.html",
        transfers=transfers_list,
        year=year,
        all_years=all_years,
        real_accounts=real_accounts,
    )


@bp.route("/transfers/new", methods=["GET", "POST"])
@login_required
def transfer_new():
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()
    if request.method == "POST":
        transfer_date = date.fromisoformat(request.form["date"])
        locked = _locked_fiscal_year(transfer_date)
        if locked:
            flash(f"Das Buchungsjahr {locked.year} ist abgeschlossen. Umbuchung nicht möglich.", "danger")
            return render_template("accounting/transfer_form.html", real_accounts=real_accounts, today=date.today().isoformat())

        amount_raw = request.form["amount"].replace(",", ".")
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation:
            flash("Ungültiger Betrag.", "danger")
            return render_template("accounting/transfer_form.html", real_accounts=real_accounts, today=date.today().isoformat())

        if amount <= 0:
            flash("Der Betrag muss größer als 0 sein.", "danger")
            return render_template("accounting/transfer_form.html", real_accounts=real_accounts, today=date.today().isoformat())

        from_id = int(request.form["from_real_account_id"])
        to_id = int(request.form["to_real_account_id"])
        if from_id == to_id:
            flash("Ausgangs- und Zielkonto dürfen nicht gleich sein.", "danger")
            return render_template("accounting/transfer_form.html", real_accounts=real_accounts, today=date.today().isoformat())

        t = Transfer(
            date=transfer_date,
            amount=amount,
            description=request.form["description"].strip(),
            from_real_account_id=from_id,
            to_real_account_id=to_id,
            created_by_id=current_user.id,
        )
        db.session.add(t)
        db.session.commit()
        flash("Umbuchung gespeichert.", "success")
        return redirect(url_for("accounting.transfers"))

    return render_template("accounting/transfer_form.html", real_accounts=real_accounts, today=date.today().isoformat())


@bp.route("/transfers/<int:transfer_id>/delete", methods=["POST"])
@login_required
def transfer_delete(transfer_id):
    t = db.get_or_404(Transfer, transfer_id)
    locked = _locked_fiscal_year(t.date)
    if locked:
        flash(f"Das Buchungsjahr {locked.year} ist abgeschlossen. Löschen nicht möglich.", "danger")
        return redirect(url_for("accounting.transfers"))
    db.session.delete(t)
    db.session.commit()
    flash("Umbuchung gelöscht.", "success")
    return redirect(url_for("accounting.transfers"))


# ---------------------------------------------------------------------------
# Buchungsjahre
# ---------------------------------------------------------------------------

@bp.route("/fiscal-years")
@login_required
def fiscal_years():
    years = FiscalYear.query.order_by(FiscalYear.year.desc()).all()
    return render_template("accounting/fiscal_years.html", fiscal_years=years)


@bp.route("/fiscal-years/new", methods=["GET", "POST"])
@login_required
def fiscal_year_new():
    if request.method == "POST":
        year = int(request.form["year"])
        if FiscalYear.query.get(year):
            flash(f"Buchungsjahr {year} existiert bereits.", "warning")
            return redirect(url_for("accounting.fiscal_year_new"))
        fy = FiscalYear(
            year=year,
            start_date=date.fromisoformat(request.form["start_date"]),
            end_date=date.fromisoformat(request.form["end_date"]),
        )
        db.session.add(fy)
        db.session.commit()
        flash(f"Buchungsjahr {year} angelegt.", "success")
        return redirect(url_for("accounting.fiscal_years"))
    today = date.today()
    default_year = today.year
    default_start = date(default_year, 1, 1).isoformat()
    default_end = date(default_year, 12, 31).isoformat()
    return render_template(
        "accounting/fiscal_year_form.html",
        default_year=default_year,
        default_start=default_start,
        default_end=default_end,
    )


@bp.route("/fiscal-years/<int:year>/close", methods=["GET", "POST"])
@login_required
def fiscal_year_close(year):
    from datetime import datetime as _dt
    fy = db.get_or_404(FiscalYear, year)
    if fy.closed:
        flash(f"Buchungsjahr {year} ist bereits abgeschlossen.", "warning")
        return redirect(url_for("accounting.fiscal_years"))

    real_accs = RealAccount.query.order_by(RealAccount.name).all()

    if request.method == "POST":
        # Jahresabschlussstand pro Bankkonto speichern
        for ra in real_accs:
            dec31 = _year_end_balance(ra, year)
            existing = RealAccountYearBalance.query.filter_by(
                real_account_id=ra.id, year=year
            ).first()
            if existing:
                existing.closing_balance = dec31
            else:
                db.session.add(RealAccountYearBalance(
                    real_account_id=ra.id,
                    year=year,
                    closing_balance=dec31,
                ))
        fy.closed = True
        fy.closed_at = _dt.utcnow()
        fy.closed_by_id = current_user.id
        db.session.commit()
        flash(f"Buchungsjahr {year} wurde abgeschlossen.", "success")
        return redirect(url_for("accounting.fiscal_years"))

    # GET – Zusammenfassung berechnen
    summary = []
    for ra in real_accs:
        jan1 = _jan1_balance(ra, year)
        einnahmen = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            extract("year", Booking.date) == year,
            Booking.amount > 0,
        ).scalar() or Decimal("0")
        ausgaben = db.session.query(func.sum(Booking.amount)).filter(
            Booking.real_account_id == ra.id,
            extract("year", Booking.date) == year,
            Booking.amount < 0,
        ).scalar() or Decimal("0")
        transfers_in = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.to_real_account_id == ra.id,
            extract("year", Transfer.date) == year,
        ).scalar() or Decimal("0")
        transfers_out = db.session.query(func.sum(Transfer.amount)).filter(
            Transfer.from_real_account_id == ra.id,
            extract("year", Transfer.date) == year,
        ).scalar() or Decimal("0")
        dec31 = jan1 + Decimal(str(einnahmen)) + Decimal(str(ausgaben)) + Decimal(str(transfers_in)) - Decimal(str(transfers_out))
        summary.append({
            "ra": ra,
            "jan1": jan1,
            "einnahmen": Decimal(str(einnahmen)),
            "ausgaben": Decimal(str(ausgaben)),
            "transfers_netto": Decimal(str(transfers_in)) - Decimal(str(transfers_out)),
            "dec31": dec31,
        })

    return render_template(
        "accounting/fiscal_year_close_confirm.html",
        fiscal_year=fy,
        summary=summary,
    )


@bp.route("/fiscal-years/<int:year>/reopen", methods=["GET", "POST"])
@login_required
def fiscal_year_reopen(year):
    fy = db.get_or_404(FiscalYear, year)
    if not fy.closed:
        flash(f"Buchungsjahr {year} ist nicht abgeschlossen.", "warning")
        return redirect(url_for("accounting.fiscal_years"))
    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("Bitte einen Grund für die Wiederöffnung angeben.", "danger")
            return render_template("accounting/fiscal_year_reopen_form.html", fiscal_year=fy)
        log = FiscalYearReopenLog(
            fiscal_year_id=fy.year,
            reopened_by_id=current_user.id,
            reason=reason,
        )
        db.session.add(log)
        fy.closed = False
        fy.closed_at = None
        fy.closed_by_id = None
        db.session.commit()
        flash(f"Buchungsjahr {year} wurde wieder geöffnet.", "success")
        return redirect(url_for("accounting.fiscal_years"))
    return render_template("accounting/fiscal_year_reopen_form.html", fiscal_year=fy)
