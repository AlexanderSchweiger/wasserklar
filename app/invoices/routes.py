import os
from datetime import date, timedelta
from decimal import Decimal

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_file, abort,
)
from flask_login import login_required, current_user

from app.invoices import bp
from app.extensions import db, mail
from app.models import Invoice, InvoiceItem, Customer, WaterMeter, MeterReading, WaterTariff, Booking, Account, Property
from app.utils import next_invoice_number as _next_invoice_number


@bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status", "")
    year_filter = request.args.get("year", "", type=str)
    q = request.args.get("q", "").strip()

    query = Invoice.query.join(Customer, Invoice.customer_id == Customer.id).order_by(Invoice.date.desc())
    if status_filter:
        query = query.filter(Invoice.status == status_filter)
    if year_filter:
        query = query.filter(Invoice.period_year == int(year_filter))
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%"))

    invoices = query.all()
    if request.headers.get("HX-Request"):
        return render_template("invoices/_table.html", invoices=invoices)
    return render_template(
        "invoices/index.html",
        invoices=invoices,
        statuses=Invoice.ALL_STATUSES,
        status_filter=status_filter,
        year_filter=year_filter,
    )


@bp.route("/generate", methods=["GET", "POST"])
@login_required
def generate():
    """Massenrechnungslauf: Alle Kunden mit Ablesung für ein Jahr."""
    tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()
    if request.method == "POST":
        year = int(request.form["year"])
        tariff_id = int(request.form["tariff_id"])
        tariff = db.get_or_404(WaterTariff, tariff_id)
        due_days = int(request.form.get("due_days", 30))

        # Alle Ablesungen für das Jahr holen (inkl. ausgebauter Zähler vom selben Jahr)
        readings = (
            MeterReading.query
            .filter_by(year=year)
            .join(WaterMeter)
            .join(Property)
            .filter(Property.active == True)
            .all()
        )

        # Nach Objekt gruppieren (ein Objekt kann mehrere Zähler im selben Jahr haben)
        from collections import defaultdict
        property_readings = defaultdict(list)
        for reading in readings:
            property_readings[reading.meter.property_id].append(reading)

        created = 0
        skipped = 0
        for property_id, prop_readings in property_readings.items():
            prop = prop_readings[0].meter.property
            ownership = prop.current_owner()
            if not ownership:
                skipped += 1
                continue

            # Bereits vorhanden?
            exists = Invoice.query.filter_by(
                property_id=prop.id, period_year=year
            ).first()
            if exists:
                skipped += 1
                continue

            inv = Invoice(
                invoice_number=_next_invoice_number(),
                customer_id=ownership.customer_id,
                property_id=prop.id,
                period_year=year,
                date=date.today(),
                due_date=date.today() + timedelta(days=due_days),
                status=Invoice.STATUS_DRAFT,
                created_by_id=current_user.id,
            )
            db.session.add(inv)
            db.session.flush()

            # Grundgebühr
            if tariff.base_fee:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description="Grundgebühr",
                    quantity=1,
                    unit="Jahr",
                    unit_price=tariff.base_fee,
                    amount=tariff.base_fee,
                ))

            # Verbrauchspositionen — bei Zählerwechsel je Zähler eine Zeile
            is_replacement = len(prop_readings) > 1
            total_consumption = Decimal("0")
            for reading in prop_readings:
                consumption = reading.consumption or Decimal("0")
                total_consumption += consumption
                meter = reading.meter

                if is_replacement:
                    if meter.installed_to:
                        date_hint = f"ausgebaut {meter.installed_to.strftime('%d.%m.%Y')}"
                    elif meter.installed_from and meter.installed_from.year == year:
                        date_hint = f"eingebaut {meter.installed_from.strftime('%d.%m.%Y')}"
                    else:
                        date_hint = f"ganzjährig"
                    desc = (
                        f"Wasserverbrauch {year} – Zähler {meter.meter_number}"
                        f" ({date_hint}, {consumption} m³)"
                    )
                else:
                    desc = (
                        f"Wasserverbrauch {year}"
                        f" ({consumption} m³ × {tariff.price_per_m3} €/m³)"
                    )

                amount = (consumption * tariff.price_per_m3).quantize(Decimal("0.01"))
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=desc,
                    quantity=consumption,
                    unit="m³",
                    unit_price=tariff.price_per_m3,
                    amount=amount,
                ))

            inv.total_amount = (
                (tariff.base_fee or Decimal("0"))
                + (total_consumption * tariff.price_per_m3).quantize(Decimal("0.01"))
            )
            created += 1

        db.session.commit()
        flash(f"{created} Rechnungen erstellt, {skipped} übersprungen.", "success")
        return redirect(url_for("invoices.index", year=year))

    return render_template(
        "invoices/generate.html",
        tariffs=tariffs,
        year=date.today().year,
    )


@bp.route("/<int:invoice_id>")
@login_required
def detail(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    return render_template("invoices/detail.html", invoice=invoice)


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    if request.method == "POST":
        invoice.date = date.fromisoformat(request.form["date"])
        invoice.due_date = date.fromisoformat(request.form["due_date"]) if request.form.get("due_date") else None
        invoice.notes = request.form.get("notes", "")
        db.session.commit()
        flash("Rechnung gespeichert.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    return render_template("invoices/edit.html", invoice=invoice, customers=customers)


@bp.route("/<int:invoice_id>/status", methods=["POST"])
@login_required
def set_status(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    new_status = request.form.get("status")
    if new_status not in Invoice.ALL_STATUSES:
        abort(400)
    old_status = invoice.status
    invoice.status = new_status

    # Automatische Buchung bei Bezahlt-Markierung
    if new_status == Invoice.STATUS_PAID and old_status != Invoice.STATUS_PAID:
        acc = Account.query.filter_by(type=Account.TYPE_INCOME, active=True).first()
        if acc:
            booking = Booking(
                date=date.today(),
                account_id=acc.id,
                amount=invoice.total_amount,
                description=f"Zahlung {invoice.invoice_number} – {invoice.customer.name}",
                reference=invoice.invoice_number,
                invoice_id=invoice.id,
                created_by_id=current_user.id,
            )
            db.session.add(booking)

    db.session.commit()
    flash(f"Status auf '{new_status}' gesetzt.", "success")
    if request.headers.get("HX-Request"):
        return render_template("invoices/_status_badge.html", invoice=invoice)
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/pay", methods=["POST"])
@login_required
def pay(invoice_id):
    """Zahlung (Teil- oder Vollzahlung) auf eine Rechnung buchen."""
    invoice = db.get_or_404(Invoice, invoice_id)
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
        description=f"Zahlung {invoice.invoice_number} – {invoice.customer.name}",
        reference=invoice.invoice_number,
        invoice_id=invoice.id,
        created_by_id=current_user.id,
    )
    db.session.add(booking)
    db.session.flush()

    from sqlalchemy import func
    paid_total = db.session.query(func.sum(Booking.amount)).filter(
        Booking.invoice_id == invoice.id
    ).scalar() or Decimal("0")
    balance = Decimal(str(invoice.total_amount)) - Decimal(str(paid_total))

    if balance > Decimal("0"):
        invoice.status = Invoice.STATUS_SENT
        flash(f"Teilzahlung von {amount:.2f} \u20ac gebucht. Offener Restbetrag: {balance:.2f} \u20ac", "success")
    elif balance == Decimal("0"):
        invoice.status = Invoice.STATUS_PAID
        flash(f"Rechnung {invoice.invoice_number} vollst\u00e4ndig bezahlt.", "success")
    else:
        invoice.status = Invoice.STATUS_CREDIT
        flash(f"\u00dcberzahlung von {abs(balance):.2f} \u20ac. Rechnung als Gutschrift markiert.", "info")

    db.session.commit()
    return redirect(url_for("accounting.open_items"))


@bp.route("/<int:invoice_id>/pdf")
@login_required
def pdf(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    try:
        from weasyprint import HTML
    except ImportError:
        flash("WeasyPrint ist nicht installiert. PDF-Export nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    html_content = render_template("invoices/pdf_template.html", invoice=invoice,
                                   config=current_app.config)
    pdf_dir = current_app.config["PDF_DIR"]
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
    HTML(string=html_content).write_pdf(pdf_path)
    invoice.pdf_path = pdf_path
    db.session.commit()
    return send_file(pdf_path, as_attachment=False,
                     download_name=f"{invoice.invoice_number}.pdf")


@bp.route("/<int:invoice_id>/send-email", methods=["POST"])
@login_required
def send_email(invoice_id):
    from flask_mail import Message
    try:
        import weasyprint
    except ImportError:
        flash("WeasyPrint ist nicht installiert. E-Mail-Versand nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    invoice = db.get_or_404(Invoice, invoice_id)
    if not invoice.customer.email:
        flash("Keine E-Mail-Adresse beim Kunden hinterlegt.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    html_content = render_template("invoices/pdf_template.html", invoice=invoice,
                                   config=current_app.config)
    pdf_dir = current_app.config["PDF_DIR"]
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
    weasyprint.HTML(string=html_content).write_pdf(pdf_path)

    msg = Message(
        subject=f"Rechnung {invoice.invoice_number}",
        recipients=[invoice.customer.email],
        body=render_template("invoices/email_body.txt", invoice=invoice,
                             config=current_app.config),
    )
    with open(pdf_path, "rb") as fp:
        msg.attach(f"{invoice.invoice_number}.pdf", "application/pdf", fp.read())

    mail.send(msg)
    invoice.status = Invoice.STATUS_SENT
    invoice.pdf_path = pdf_path
    db.session.commit()
    flash(f"Rechnung an {invoice.customer.email} versendet.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


# ---------------------------------------------------------------------------
# Tarife
# ---------------------------------------------------------------------------

@bp.route("/tariffs")
@login_required
def tariffs():
    all_tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()
    return render_template("invoices/tariffs.html", tariffs=all_tariffs)


@bp.route("/tariffs/new", methods=["GET", "POST"])
@login_required
def tariff_new():
    if request.method == "POST":
        t = WaterTariff(
            name=request.form["name"].strip(),
            valid_from=int(request.form["valid_from"]),
            valid_to=int(request.form["valid_to"]) if request.form.get("valid_to") else None,
            base_fee=Decimal(request.form.get("base_fee", "0").replace(",", ".")),
            price_per_m3=Decimal(request.form["price_per_m3"].replace(",", ".")),
            notes=request.form.get("notes", ""),
        )
        db.session.add(t)
        db.session.commit()
        flash("Tarif angelegt.", "success")
        return redirect(url_for("invoices.tariffs"))
    return render_template("invoices/tariff_form.html", tariff=None)


@bp.route("/tariffs/<int:tariff_id>/edit", methods=["GET", "POST"])
@login_required
def tariff_edit(tariff_id):
    t = db.get_or_404(WaterTariff, tariff_id)
    if request.method == "POST":
        t.name = request.form["name"].strip()
        t.valid_from = int(request.form["valid_from"])
        t.valid_to = int(request.form["valid_to"]) if request.form.get("valid_to") else None
        t.base_fee = Decimal(request.form.get("base_fee", "0").replace(",", "."))
        t.price_per_m3 = Decimal(request.form["price_per_m3"].replace(",", "."))
        t.notes = request.form.get("notes", "")
        db.session.commit()
        flash("Tarif aktualisiert.", "success")
        return redirect(url_for("invoices.tariffs"))
    return render_template("invoices/tariff_form.html", tariff=t)
