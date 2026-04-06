import os
from datetime import date, timedelta
from decimal import Decimal

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_file, abort,
)
from flask_login import login_required, current_user

from app.invoices import bp
from app.extensions import db
from app.models import Invoice, InvoiceItem, Customer, WaterMeter, MeterReading, WaterTariff, Booking, Account, Property, OpenItem, Project, RealAccount, InvoiceCounter, AppSetting
from app.utils import next_invoice_number as _next_invoice_number
from app.settings_service import get_wg, send_mail


def _create_or_update_open_item(invoice):
    """Erzeugt oder aktualisiert den verknüpften OpenItem wenn eine Rechnung versendet wird."""
    oi = invoice.open_item
    if oi is None:
        oi = OpenItem(
            customer_id=invoice.customer_id,
            description=invoice.invoice_number,
            amount=invoice.total_amount,
            date=invoice.date,
            due_date=invoice.due_date,
            period_year=invoice.period_year,
            status=OpenItem.STATUS_OPEN,
            invoice_id=invoice.id,
        )
        db.session.add(oi)
    else:
        oi.amount = invoice.total_amount
        oi.due_date = invoice.due_date
        oi.period_year = invoice.period_year


def _invoice_is_locked(invoice):
    """Gesperrt wenn Status nicht mehr Entwurf ist."""
    return invoice.status != Invoice.STATUS_DRAFT


def _render_email_body(invoice):
    """Rendert den E-Mail-Text: DB-Vorlage wenn vorhanden, sonst statisches Template."""
    from jinja2 import Environment
    custom = AppSetting.get("email_body_template")
    if custom:
        return Environment().from_string(custom).render(
            name=invoice.customer.name,
            rechnungsnummer=invoice.invoice_number,
            buchungsjahr=invoice.period_year or "",
            betrag=f"{invoice.total_amount:.2f}",
            faelligkeitsdatum=(
                invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "—"
            ),
            iban=get_wg('iban'),
        )
    return render_template("invoices/email_body.txt", invoice=invoice)


@bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status", "")
    year_filter = request.args.get("year", "", type=str)
    date_from = request.args.get("date_from", date(date.today().year, 1, 1).isoformat()).strip()
    date_to = request.args.get("date_to", "").strip()
    q = request.args.get("q", "").strip()
    project_id_filter = request.args.get("project_id", "", type=str)
    nur_email = request.args.get("nur_email", "") == "1"

    query = (
        Invoice.query
        .join(Customer, Invoice.customer_id == Customer.id)
        .outerjoin(Property, Invoice.property_id == Property.id)
        .order_by(Invoice.date.desc())
    )
    if status_filter:
        query = query.filter(Invoice.status == status_filter)
    if year_filter:
        query = query.filter(Invoice.period_year == int(year_filter))
    if date_from:
        query = query.filter(Invoice.date >= date.fromisoformat(date_from))
    if date_to:
        query = query.filter(Invoice.date <= date.fromisoformat(date_to))
    if q:
        from sqlalchemy import or_
        query = query.filter(or_(
            Customer.name.ilike(f"%{q}%"),
            Invoice.invoice_number.ilike(f"%{q}%"),
            Property.object_number.ilike(f"%{q}%"),
            Property.strasse.ilike(f"%{q}%"),
        ))
    if project_id_filter:
        from sqlalchemy import exists
        query = query.filter(
            exists().where(
                (Booking.invoice_id == Invoice.id) &
                (Booking.project_id == int(project_id_filter))
            )
        )
    if nur_email:
        query = query.filter(Customer.rechnung_per_email == True)

    invoices = query.all()
    projects_for_filter = Project.query.order_by(Project.name).all()
    if request.headers.get("HX-Request"):
        return render_template("invoices/_table.html", invoices=invoices)
    return render_template(
        "invoices/index.html",
        invoices=invoices,
        statuses=Invoice.ALL_STATUSES,
        status_filter=status_filter,
        year_filter=year_filter,
        date_from=date_from,
        date_to=date_to,
        projects_for_filter=projects_for_filter,
        project_id_filter=project_id_filter,
        nur_email=nur_email,
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

            customer = db.session.get(Customer, ownership.customer_id)

            # Priorität: Objekt > Kunde > Tarif
            effective_base_fee = (
                prop.base_fee_override if prop.base_fee_override is not None
                else customer.base_fee_override if customer.base_fee_override is not None
                else (tariff.base_fee or Decimal("0"))
            )
            effective_additional_fee = (
                prop.additional_fee_override if prop.additional_fee_override is not None
                else customer.additional_fee_override if customer.additional_fee_override is not None
                else (tariff.additional_fee or Decimal("0"))
            )
            base_fee_label = tariff.base_fee_label or "Grundgebühr"
            additional_fee_label = tariff.additional_fee_label or "Zusatzgebühr"

            inv = Invoice(
                invoice_number=_next_invoice_number(year),
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
            if effective_base_fee:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=base_fee_label,
                    quantity=1,
                    unit="Jahr",
                    unit_price=effective_base_fee,
                    amount=effective_base_fee,
                ))

            # Zusatzgebühr
            if effective_additional_fee:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=additional_fee_label,
                    quantity=1,
                    unit="Jahr",
                    unit_price=effective_additional_fee,
                    amount=effective_additional_fee,
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
                effective_base_fee
                + effective_additional_fee
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
    real_accounts = RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()
    default_real_account = RealAccount.query.filter_by(is_default=True, active=True).first()
    return render_template("invoices/detail.html", invoice=invoice, real_accounts=real_accounts,
                           default_real_account=default_real_account)


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if _invoice_is_locked(invoice):
        flash("Diese Rechnung kann nicht mehr bearbeitet werden (nur Stornierung möglich).", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    if request.method == "POST":
        invoice.date = date.fromisoformat(request.form["date"])
        invoice.due_date = date.fromisoformat(request.form["due_date"]) if request.form.get("due_date") else None
        invoice.notes = request.form.get("notes", "")
        db.session.commit()
        flash("Rechnung gespeichert.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    return render_template("invoices/edit.html", invoice=invoice, customers=customers)


@bp.route("/bulk-action", methods=["POST"])
@login_required
def bulk_action():
    invoice_ids = request.form.getlist("invoice_ids", type=int)
    action = request.form.get("action", "")

    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "warning")
        return redirect(url_for("invoices.index"))

    invoices = Invoice.query.filter(Invoice.id.in_(invoice_ids)).all()

    if action == "delete":
        deletable = [inv for inv in invoices if inv.status == Invoice.STATUS_DRAFT]
        skipped = len(invoices) - len(deletable)
        for inv in deletable:
            if inv.open_item:
                db.session.delete(inv.open_item)
            db.session.delete(inv)
        db.session.commit()
        msg = f"{len(deletable)} Rechnung(en) gelöscht."
        if skipped:
            msg += f" {skipped} übersprungen (nur Entwürfe können gelöscht werden)."
        flash(msg, "success" if deletable else "warning")

    elif action in Invoice.ALL_STATUSES:
        changed = 0
        for inv in invoices:
            old_status = inv.status
            inv.status = action
            if action == Invoice.STATUS_SENT:
                _create_or_update_open_item(inv)
            elif action == Invoice.STATUS_CANCELLED and inv.open_item:
                inv.open_item.status = OpenItem.STATUS_PAID
            changed += 1
        db.session.commit()
        flash(f"{changed} Rechnung(en) auf '{action}' gesetzt.", "success")

    else:
        flash("Ungültige Aktion.", "danger")

    return redirect(url_for("invoices.index"))


@bp.route("/bulk-pdf-merged", methods=["POST"])
@login_required
def bulk_pdf_merged():
    """Alle markierten Rechnungen als zusammengeführtes PDF zum Download."""
    invoice_ids = request.form.getlist("invoice_ids", type=int)
    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "warning")
        return redirect(url_for("invoices.index"))
    try:
        from weasyprint import HTML
    except ImportError:
        flash("WeasyPrint ist nicht installiert. PDF-Export nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("invoices.index"))
    invoices = Invoice.query.filter(Invoice.id.in_(invoice_ids)).order_by(Invoice.invoice_number).all()
    pdf_dir = current_app.config["PDF_DIR"]
    os.makedirs(pdf_dir, exist_ok=True)
    rendered_docs = []
    for invoice in invoices:
        html_content = render_template("invoices/pdf_template.html", invoice=invoice)
        rendered_docs.append(HTML(string=html_content).render())
        # Cache für gesperrte Rechnungen aktualisieren
        if _invoice_is_locked(invoice):
            pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
            if not invoice.pdf_path or not os.path.exists(invoice.pdf_path):
                rendered_docs[-1].write_pdf(pdf_path)
                invoice.pdf_path = pdf_path
    db.session.commit()
    all_pages = [page for doc in rendered_docs for page in doc.pages]
    merged_path = os.path.join(pdf_dir, "_bulk_merged.pdf")
    rendered_docs[0].copy(all_pages).write_pdf(merged_path)
    return send_file(merged_path, as_attachment=True, download_name="Rechnungen_gesamt.pdf")


@bp.route("/bulk-pdf-zip", methods=["POST"])
@login_required
def bulk_pdf_zip():
    """Alle markierten Rechnungen als einzelne PDFs in einer ZIP-Datei."""
    import zipfile
    import io
    invoice_ids = request.form.getlist("invoice_ids", type=int)
    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "warning")
        return redirect(url_for("invoices.index"))
    try:
        from weasyprint import HTML
    except ImportError:
        flash("WeasyPrint ist nicht installiert. PDF-Export nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("invoices.index"))
    invoices = Invoice.query.filter(Invoice.id.in_(invoice_ids)).order_by(Invoice.invoice_number).all()
    pdf_dir = current_app.config["PDF_DIR"]
    os.makedirs(pdf_dir, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for invoice in invoices:
            pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
            # Cache nutzen wenn vorhanden und nicht Entwurf
            if _invoice_is_locked(invoice) and invoice.pdf_path and os.path.exists(invoice.pdf_path):
                zf.write(invoice.pdf_path, f"{invoice.invoice_number}.pdf")
            else:
                html_content = render_template("invoices/pdf_template.html", invoice=invoice)
                HTML(string=html_content).write_pdf(pdf_path)
                if _invoice_is_locked(invoice):
                    invoice.pdf_path = pdf_path
                zf.write(pdf_path, f"{invoice.invoice_number}.pdf")
    db.session.commit()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="Rechnungen.zip",
                     mimetype="application/zip")


@bp.route("/<int:invoice_id>/status", methods=["POST"])
@login_required
def set_status(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    new_status = request.form.get("status")
    if new_status not in Invoice.ALL_STATUSES:
        abort(400)
    old_status = invoice.status
    invoice.status = new_status

    if new_status == Invoice.STATUS_SENT:
        _create_or_update_open_item(invoice)
    elif new_status == Invoice.STATUS_CANCELLED and invoice.open_item:
        invoice.open_item.status = OpenItem.STATUS_PAID

    # Automatische Buchung bei Bezahlt-Markierung
    if new_status == Invoice.STATUS_PAID and old_status != Invoice.STATUS_PAID:
        acc = Account.query.filter_by(active=True).first()
        if acc:
            real_account_id_raw = request.form.get("real_account_id") or None
            real_account_id = int(real_account_id_raw) if real_account_id_raw else None
            # Buchungen pro Steuersatz aufteilen, damit USt-Voranmeldung korrekt ist
            from collections import defaultdict
            from decimal import Decimal as _D
            groups = defaultdict(lambda: _D("0"))
            for item in invoice.items:
                rate = item.tax_rate if item.tax_rate is not None else _D("0")
                groups[rate] += item.amount
            if not groups:
                groups[None] = invoice.total_amount
            for rate, amount in groups.items():
                db.session.add(Booking(
                    date=date.today(),
                    account_id=acc.id,
                    amount=amount,
                    description=f"Zahlung {invoice.invoice_number} – {invoice.customer.name}",
                    reference=invoice.invoice_number,
                    invoice_id=invoice.id,
                    real_account_id=real_account_id,
                    created_by_id=current_user.id,
                    tax_rate=rate if rate and rate > 0 else None,
                ))
        if invoice.open_item:
            invoice.open_item.status = OpenItem.STATUS_PAID

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

    acc = Account.query.filter_by(active=True).first()
    if not acc:
        flash("Kein aktives Konto gefunden.", "danger")
        return redirect(url_for("accounting.open_items"))

    real_account_id_raw = request.form.get("real_account_id") or None
    booking = Booking(
        date=date.today(),
        account_id=acc.id,
        amount=amount,
        description=f"Zahlung {invoice.invoice_number} – {invoice.customer.name}",
        reference=invoice.invoice_number,
        invoice_id=invoice.id,
        real_account_id=int(real_account_id_raw) if real_account_id_raw else None,
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

    if invoice.open_item:
        oi = invoice.open_item
        booking.open_item_id = oi.id
        if balance > Decimal("0"):
            oi.status = OpenItem.STATUS_PARTIAL
        elif balance == Decimal("0"):
            oi.status = OpenItem.STATUS_PAID
        else:
            oi.status = OpenItem.STATUS_CREDIT

    db.session.commit()
    return redirect(url_for("accounting.open_items"))


@bp.route("/<int:invoice_id>/pdf")
@login_required
def pdf(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    # Gesperrte Rechnungen: gecachte PDF ausliefern wenn vorhanden
    if _invoice_is_locked(invoice) and invoice.pdf_path and os.path.exists(invoice.pdf_path):
        return send_file(invoice.pdf_path, as_attachment=False,
                         download_name=f"{invoice.invoice_number}.pdf")
    try:
        from weasyprint import HTML
    except ImportError:
        flash("WeasyPrint ist nicht installiert. PDF-Export nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    html_content = render_template("invoices/pdf_template.html", invoice=invoice)
    pdf_dir = current_app.config["PDF_DIR"]
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
    HTML(string=html_content).write_pdf(pdf_path)
    # Nur für Nicht-Entwürfe persistieren
    if _invoice_is_locked(invoice):
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
)
    pdf_dir = current_app.config["PDF_DIR"]
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
    weasyprint.HTML(string=html_content).write_pdf(pdf_path)

    msg = Message(
        subject=f"Rechnung {invoice.invoice_number}",
        recipients=[invoice.customer.email],
        body=_render_email_body(invoice),
    )
    with open(pdf_path, "rb") as fp:
        msg.attach(f"{invoice.invoice_number}.pdf", "application/pdf", fp.read())

    send_mail(msg)
    invoice.status = Invoice.STATUS_SENT
    invoice.pdf_path = pdf_path
    _create_or_update_open_item(invoice)
    db.session.commit()
    flash(f"Rechnung an {invoice.customer.email} versendet.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/send-email-ajax", methods=["POST"])
@login_required
def send_email_ajax(invoice_id):
    """JSON-Variante für den Massenmail-Versand per JavaScript."""
    from flask import jsonify
    from flask_mail import Message
    try:
        import weasyprint
    except ImportError:
        return jsonify({"ok": False, "error": "WeasyPrint nicht verfügbar"}), 503

    test_mode = request.form.get("test_mode") == "1"

    invoice = db.get_or_404(Invoice, invoice_id)
    if not invoice.customer.email:
        return jsonify({"ok": False, "error": "Keine E-Mail-Adresse hinterlegt"}), 400
    if not invoice.customer.rechnung_per_email:
        return jsonify({"ok": False, "error": "E-Mail-Versand nicht aktiviert"}), 400

    if test_mode:
        recipient = current_user.email
        if not recipient:
            return jsonify({"ok": False, "error": "Kein Admin-E-Mail für Testmodus hinterlegt"}), 400
    else:
        recipient = invoice.customer.email

    try:
        html_content = render_template("invoices/pdf_template.html", invoice=invoice)
        pdf_dir = current_app.config["PDF_DIR"]
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, f"{invoice.invoice_number}.pdf")
        weasyprint.HTML(string=html_content).write_pdf(pdf_path)

        subject = f"Rechnung {invoice.invoice_number}"
        if test_mode:
            subject = f"[TEST – an: {invoice.customer.email}] {subject}"

        body = _render_email_body(invoice)
        if test_mode:
            body = f"[TESTMODUS – eigentlicher Empfänger: {invoice.customer.email}]\n\n{body}"

        msg = Message(subject=subject, recipients=[recipient], body=body)
        with open(pdf_path, "rb") as fp:
            msg.attach(f"{invoice.invoice_number}.pdf", "application/pdf", fp.read())

        send_mail(msg)
        if not test_mode:
            invoice.status = Invoice.STATUS_SENT
            invoice.pdf_path = pdf_path
            _create_or_update_open_item(invoice)
            db.session.commit()
        return jsonify({"ok": True, "invoice_number": invoice.invoice_number,
                        "email": recipient, "test_mode": test_mode})
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/email-einstellungen", methods=["GET", "POST"])
@login_required
def email_settings():
    """E-Mail-Vorlage für den Rechnungsversand konfigurieren (nur Admin)."""
    if not current_user.is_admin:
        flash("Kein Zugriff.", "danger")
        return redirect(url_for("invoices.index"))
    import pathlib
    if request.method == "POST":
        AppSetting.set("email_body_template", request.form.get("email_body_template", "").strip())
        db.session.commit()
        flash("E-Mail-Vorlage gespeichert.", "success")
        return redirect(url_for("invoices.email_settings"))
    current_body = AppSetting.get("email_body_template")
    if not current_body:
        tpl_path = pathlib.Path(current_app.root_path) / "templates" / "invoices" / "email_body.txt"
        current_body = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else ""
    return render_template("invoices/email_settings.html", current_body=current_body)


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
            base_fee_label=request.form.get("base_fee_label", "").strip() or "Grundgebühr",
            additional_fee=Decimal(request.form.get("additional_fee", "0").replace(",", ".")),
            additional_fee_label=request.form.get("additional_fee_label", "").strip() or "Zusatzgebühr",
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
        t.base_fee_label = request.form.get("base_fee_label", "").strip() or "Grundgebühr"
        t.additional_fee = Decimal(request.form.get("additional_fee", "0").replace(",", "."))
        t.additional_fee_label = request.form.get("additional_fee_label", "").strip() or "Zusatzgebühr"
        t.price_per_m3 = Decimal(request.form["price_per_m3"].replace(",", "."))
        t.notes = request.form.get("notes", "")
        db.session.commit()
        flash("Tarif aktualisiert.", "success")
        return redirect(url_for("invoices.tariffs"))
    return render_template("invoices/tariff_form.html", tariff=t)


# ---------------------------------------------------------------------------
# Rechnungsnummer-Zähler
# ---------------------------------------------------------------------------

@bp.route("/counters")
@login_required
def counters():
    from sqlalchemy import func
    # Alle Jahre aus bestehenden Rechnungen + vorhandenen Countern zusammenführen
    invoice_years = db.session.query(
        func.substr(Invoice.invoice_number, 1, 4).label("year"),
        func.count(Invoice.id).label("count"),
        func.max(Invoice.invoice_number).label("max_nr"),
    ).group_by(func.substr(Invoice.invoice_number, 1, 4)).all()

    all_counters = {c.year: c for c in InvoiceCounter.query.all()}

    rows = []
    for row in sorted(invoice_years, key=lambda r: r.year, reverse=True):
        try:
            y = int(row.year)
        except (TypeError, ValueError):
            continue
        counter = all_counters.get(y)
        rows.append({
            "year": y,
            "count": row.count,
            "max_nr": row.max_nr,
            "next_seq": counter.next_seq if counter else "–",
        })
    # Jahre mit Counter aber ohne Rechnungen ergänzen
    for y, counter in all_counters.items():
        if not any(r["year"] == y for r in rows):
            rows.append({
                "year": y,
                "count": 0,
                "max_nr": "–",
                "next_seq": counter.next_seq,
            })
    rows.sort(key=lambda r: r["year"], reverse=True)
    return render_template("invoices/counters.html", rows=rows)


@bp.route("/counters/<int:year>/reset", methods=["POST"])
@login_required
def counter_reset(year):
    from sqlalchemy import func
    mode = request.form.get("mode", "auto")
    counter = db.session.get(InvoiceCounter, year)

    if mode == "manual":
        try:
            new_seq = int(request.form.get("next_seq", 1))
            if new_seq < 1:
                raise ValueError
        except ValueError:
            flash("Ungültiger Wert für den Zähler.", "danger")
            return redirect(url_for("invoices.counters"))
    else:
        # auto: max vorhandene Nummer + 1
        prefix = f"{year}-"
        last = (
            Invoice.query
            .filter(Invoice.invoice_number.like(f"{prefix}%"))
            .order_by(Invoice.invoice_number.desc())
            .first()
        )
        if last:
            try:
                new_seq = int(last.invoice_number.split("-")[-1]) + 1
            except ValueError:
                new_seq = 1
        else:
            new_seq = 1

    if counter is None:
        counter = InvoiceCounter(year=year, next_seq=new_seq)
        db.session.add(counter)
    else:
        counter.next_seq = new_seq
    db.session.commit()
    flash(f"Zähler für {year} auf {new_seq:05d} zurückgesetzt.", "success")
    return redirect(url_for("invoices.counters"))
