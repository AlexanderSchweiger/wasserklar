import io
import csv
from datetime import date
from decimal import Decimal

from flask import (
    render_template, redirect, url_for, flash, request,
    Response, stream_with_context,
)
from flask_login import login_required, current_user
from sqlalchemy import extract, func

from app.accounting import bp
from app.extensions import db
from app.models import Account, Booking, Invoice, OpenItem, WaterTariff, Customer, InvoiceItem
from app.utils import next_invoice_number


@bp.route("/")
@login_required
def index():
    year = request.args.get("year", date.today().year, type=int)
    accounts = Account.query.filter_by(active=True).order_by(Account.type, Account.name).all()
    return render_template("accounting/index.html", accounts=accounts, year=year)


# ---------------------------------------------------------------------------
# Kontenplan
# ---------------------------------------------------------------------------

@bp.route("/accounts")
@login_required
def accounts():
    all_accounts = Account.query.order_by(Account.type, Account.name).all()
    return render_template("accounting/accounts.html", accounts=all_accounts)


@bp.route("/accounts/new", methods=["GET", "POST"])
@login_required
def account_new():
    if request.method == "POST":
        a = Account(
            name=request.form["name"].strip(),
            type=request.form["type"],
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
        a.name = request.form["name"].strip()
        a.type = request.form["type"]
        a.description = request.form.get("description", "")
        a.active = "active" in request.form
        db.session.commit()
        flash("Konto aktualisiert.", "success")
        return redirect(url_for("accounting.accounts"))
    return render_template("accounting/account_form.html", account=a)


# ---------------------------------------------------------------------------
# Buchungen
# ---------------------------------------------------------------------------

@bp.route("/bookings")
@login_required
def bookings():
    year = request.args.get("year", date.today().year, type=int)
    account_id = request.args.get("account_id", "", type=str)

    query = (
        Booking.query
        .filter(extract("year", Booking.date) == year)
        .order_by(Booking.date.desc())
    )
    if account_id:
        query = query.filter(Booking.account_id == int(account_id))

    bkgs = query.all()
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()

    if request.headers.get("HX-Request"):
        return render_template(
            "accounting/_bookings_table.html", bookings=bkgs, year=year,
        )
    return render_template(
        "accounting/bookings.html",
        bookings=bkgs, accounts=accounts, year=year,
        account_id=account_id,
    )


@bp.route("/bookings/new", methods=["GET", "POST"])
@login_required
def booking_new():
    accounts = Account.query.filter_by(active=True).order_by(Account.type, Account.name).all()
    if request.method == "POST":
        amount_raw = request.form.get("amount", "0").replace(",", ".")
        amount = Decimal(amount_raw)
        acc = db.get_or_404(Account, int(request.form["account_id"]))
        # Ausgaben → negativer Betrag
        if acc.type == Account.TYPE_EXPENSE and amount > 0:
            amount = -amount

        b = Booking(
            date=date.fromisoformat(request.form["date"]),
            account_id=acc.id,
            amount=amount,
            description=request.form.get("description", "").strip(),
            reference=request.form.get("reference", "").strip(),
            created_by_id=current_user.id,
        )
        db.session.add(b)
        db.session.commit()
        flash("Buchung gespeichert.", "success")
        return redirect(url_for("accounting.bookings"))
    return render_template("accounting/booking_form.html", booking=None, accounts=accounts)


@bp.route("/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@login_required
def booking_edit(booking_id):
    b = db.get_or_404(Booking, booking_id)
    accounts = Account.query.filter_by(active=True).order_by(Account.type, Account.name).all()
    if request.method == "POST":
        amount_raw = request.form.get("amount", "0").replace(",", ".")
        amount = Decimal(amount_raw)
        acc = db.get_or_404(Account, int(request.form["account_id"]))
        if acc.type == Account.TYPE_EXPENSE and amount > 0:
            amount = -amount
        b.date = date.fromisoformat(request.form["date"])
        b.account_id = acc.id
        b.amount = amount
        b.description = request.form.get("description", "").strip()
        b.reference = request.form.get("reference", "").strip()
        db.session.commit()
        flash("Buchung aktualisiert.", "success")
        return redirect(url_for("accounting.bookings"))
    return render_template("accounting/booking_form.html", booking=b, accounts=accounts)


@bp.route("/bookings/<int:booking_id>/delete", methods=["POST"])
@login_required
def booking_delete(booking_id):
    b = db.get_or_404(Booking, booking_id)
    db.session.delete(b)
    db.session.commit()
    flash("Buchung gelöscht.", "info")
    return redirect(url_for("accounting.bookings"))


# ---------------------------------------------------------------------------
# Offene Posten
# ---------------------------------------------------------------------------

@bp.route("/open-items")
@login_required
def open_items():
    invoices = (
        Invoice.query
        .filter(Invoice.status.in_([Invoice.STATUS_SENT, Invoice.STATUS_CREDIT]))
        .order_by(Invoice.due_date)
        .all()
    )
    manual_items = (
        OpenItem.query
        .filter(OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL, OpenItem.STATUS_CREDIT]))
        .order_by(OpenItem.due_date)
        .all()
    )
    from decimal import Decimal
    invoice_total = sum(inv.open_balance for inv in invoices)
    manual_total = sum(item.open_balance for item in manual_items)
    return render_template(
        "accounting/open_items.html",
        invoices=invoices,
        manual_items=manual_items,
        invoice_total=invoice_total,
        manual_total=manual_total,
        today=date.today(),
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

    acc = Account.query.filter_by(type=Account.TYPE_INCOME, active=True).first()
    if not acc:
        flash("Kein aktives Einnahmenkonto gefunden.", "danger")
        return redirect(url_for("accounting.open_items"))

    booking = Booking(
        date=date.today(),
        account_id=acc.id,
        amount=amount,
        description=f"Zahlung – {item.description} – {item.customer.name}",
        reference=f"OP-{item.id}",
        open_item_id=item.id,
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
            invoice_number=next_invoice_number(),
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

    rows = (
        db.session.query(
            Account.name,
            Account.type,
            func.sum(Booking.amount).label("total"),
        )
        .join(Booking, Booking.account_id == Account.id)
        .filter(extract("year", Booking.date) == year)
        .group_by(Account.id)
        .order_by(Account.type, Account.name)
        .all()
    )

    income_rows = [(r.name, r.total) for r in rows if r.type == Account.TYPE_INCOME]
    expense_rows = [(r.name, abs(r.total)) for r in rows if r.type == Account.TYPE_EXPENSE]
    total_income = sum(r[1] for r in income_rows)
    total_expense = sum(r[1] for r in expense_rows)
    balance = total_income - total_expense

    return render_template(
        "accounting/report.html",
        year=year,
        income_rows=income_rows,
        expense_rows=expense_rows,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
    )


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
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Datum", "Konto", "Typ", "Betrag", "Beschreibung", "Belegnummer"])
        for b in bookings:
            writer.writerow([
                b.date.isoformat(),
                b.account.name,
                b.account.type,
                str(b.amount).replace(".", ","),
                b.description,
                b.reference or "",
            ])
        return output.getvalue()

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=buchungen_{year}.csv"},
    )
