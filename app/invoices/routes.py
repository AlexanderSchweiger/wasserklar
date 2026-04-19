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
from app.models import Invoice, InvoiceItem, Customer, WaterMeter, MeterReading, WaterTariff, Booking, Account, Property, OpenItem, Project, RealAccount, InvoiceCounter, AppSetting, BillingRun
from app.utils import next_invoice_number as _next_invoice_number
from app.settings_service import get_wg, send_mail, wg_settings
from app.invoices.design import get_design


def _current_design():
    """Liest das aktuell konfigurierte Rechnungsdesign aus den AppSettings."""
    return get_design(AppSetting.get("invoice.design", "classic"))


def _render_pdf_html(invoice):
    """Rendert die HTML-Vorlage für WeasyPrint mit aktuellem Design."""
    return render_template(
        "invoices/pdf_template.html",
        invoice=invoice,
        design=_current_design(),
    )


def _resolve_open_item_account_id(invoice, form_account_id=None):
    """Ermittelt das Buchungskonto für einen aus einer Rechnung erzeugten Offenen Posten.

    Priorität: Rechnungslauf-Konto > Formularwert (manuelle Rechnung).
    """
    if invoice.billing_run_id and invoice.billing_run and invoice.billing_run.account_id:
        return invoice.billing_run.account_id
    return form_account_id


def _create_or_update_open_item(invoice, account_id=None):
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
            account_id=account_id,
        )
        db.session.add(oi)
    else:
        oi.amount = invoice.total_amount
        oi.due_date = invoice.due_date
        oi.period_year = invoice.period_year
        if account_id is not None:
            oi.account_id = account_id


def _invoice_is_locked(invoice):
    """Gesperrt wenn Status nicht mehr Entwurf ist."""
    return invoice.status != Invoice.STATUS_DRAFT


def _get_document_format(override=None):
    """Gibt das konfigurierte Dokumentformat zurück ('pdf', 'docx' oder 'both').
    override kommt aus dem Request-Parameter ?fmt="""
    fmt = override or AppSetting.get("invoice.document_format", "pdf")
    return fmt if fmt in ("pdf", "docx", "both") else "pdf"


def _get_doc_dir(invoice):
    """Gibt den jahresspezifischen Unterordner für Rechnungsdokumente zurück und legt ihn an.

    Struktur: <PDF_DIR>/<Jahr>/ z.B. instance/pdfs/2024/
    """
    year = invoice.date.year if invoice.date else "misc"
    doc_dir = os.path.join(current_app.config["PDF_DIR"], str(year))
    os.makedirs(doc_dir, exist_ok=True)
    return doc_dir


def _versioned_path(doc_dir: str, invoice_number: str, ext: str) -> str:
    """Gibt einen eindeutigen Dateipfad zurück.

    Existiert bereits eine Datei mit dem Basisnamen, wird _V2, _V3, … angehängt,
    damit ältere Versionen erhalten bleiben.

    Beispiel: 2025-00042.pdf → 2025-00042_V2.pdf → 2025-00042_V3.pdf
    """
    base = os.path.join(doc_dir, f"{invoice_number}.{ext}")
    if not os.path.exists(base):
        return base
    v = 2
    while True:
        candidate = os.path.join(doc_dir, f"{invoice_number}_V{v}.{ext}")
        if not os.path.exists(candidate):
            return candidate
        v += 1


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _sync_open_item(invoice):
    """Passt den Betrag des verknüpften Offenen Postens an den neuen Rechnungsbetrag an.
    Gibt True zurück wenn ein Offener Posten aktualisiert wurde."""
    if invoice.open_item:
        invoice.open_item.amount = invoice.total_amount
        return True
    return False


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
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    doc_format = AppSetting.get("invoice.document_format", "pdf")
    if request.headers.get("HX-Request"):
        return render_template("invoices/_table.html", invoices=invoices, doc_format=doc_format)
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
        doc_format=doc_format,
        accounts=accounts,
    )


@bp.route("/generate", methods=["GET", "POST"])
@login_required
def generate():
    """Massenrechnungslauf: Alle Kunden mit Ablesung für ein Jahr."""
    from app.accounting import services as acc_svc
    tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    if request.method == "POST":
        year = int(request.form["year"])
        tariff_id = int(request.form["tariff_id"])
        tariff = db.get_or_404(WaterTariff, tariff_id)
        due_days = int(request.form.get("due_days", 30))
        account_id_raw = request.form.get("account_id") or None
        if not account_id_raw:
            flash("Bitte ein Buchungskonto für den Rechnungslauf auswählen.", "danger")
            return render_template("invoices/generate.html", tariffs=tariffs, accounts=accounts, year=date.today().year)
        billing_account_id = int(account_id_raw)

        # Rechnungsdatum für den Rechnungslauf ist heute.
        invoice_date = date.today()
        fy_error = acc_svc.open_fiscal_year_error(invoice_date)
        if fy_error:
            flash(f"{fy_error} Rechnungslauf nicht möglich.", "danger")
            return redirect(url_for("invoices.generate"))

        # Standard-Wasser-Steuersatz nur in USt-pflichtigen Buchungsjahren anwenden
        water_tax = acc_svc.default_water_tax_rate(invoice_date.year)

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

        # Rechnungslauf anlegen (Tarif-Snapshot + Metadaten)
        billing_run = BillingRun(
            period_year=year,
            created_by_id=current_user.id,
            tariff_name=tariff.name,
            tariff_valid_from=tariff.valid_from,
            tariff_valid_to=tariff.valid_to,
            tariff_base_fee=tariff.base_fee,
            tariff_base_fee_label=tariff.base_fee_label or "Grundgebühr",
            tariff_additional_fee=tariff.additional_fee,
            tariff_additional_fee_label=tariff.additional_fee_label or "Zusatzgebühr",
            tariff_price_per_m3=tariff.price_per_m3,
            tariff_notes=tariff.notes,
            account_id=billing_account_id,
        )
        db.session.add(billing_run)
        db.session.flush()

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
            # None bedeutet: keine Gebühr, keine Rechnungsposition
            effective_base_fee = (
                prop.base_fee_override if prop.base_fee_override is not None
                else customer.base_fee_override if customer.base_fee_override is not None
                else tariff.base_fee
            )
            effective_additional_fee = (
                prop.additional_fee_override if prop.additional_fee_override is not None
                else customer.additional_fee_override if customer.additional_fee_override is not None
                else tariff.additional_fee
            )
            base_fee_label = tariff.base_fee_label or "Grundgebühr"
            additional_fee_label = tariff.additional_fee_label or "Zusatzgebühr"

            inv = Invoice(
                invoice_number=_next_invoice_number(year),
                customer_id=ownership.customer_id,
                property_id=prop.id,
                billing_run_id=billing_run.id,
                period_year=year,
                date=date.today(),
                due_date=date.today() + timedelta(days=due_days),
                status=Invoice.STATUS_DRAFT,
                created_by_id=current_user.id,
            )
            db.session.add(inv)
            db.session.flush()

            # Grundgebühr (nur wenn explizit hinterlegt, auch 0 erzeugt eine Position)
            if effective_base_fee is not None:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=base_fee_label,
                    quantity=1,
                    unit="Jahr",
                    unit_price=effective_base_fee,
                    amount=effective_base_fee,
                    tax_rate=water_tax,
                ))

            # Zusatzgebühr (nur wenn explizit hinterlegt, auch 0 erzeugt eine Position)
            if effective_additional_fee is not None:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=additional_fee_label,
                    quantity=1,
                    unit="Jahr",
                    unit_price=effective_additional_fee,
                    amount=effective_additional_fee,
                    tax_rate=water_tax,
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
                    tax_rate=water_tax,
                ))

            # Damit USt (sofern vorhanden) im Gesamtbetrag berücksichtigt wird:
            db.session.flush()
            inv.recalculate_total()
            created += 1

        billing_run.invoices_created = created
        billing_run.invoices_skipped = skipped

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Rechnungslauf – alle Änderungen wurden zurückgesetzt: {e}", "danger")
            return redirect(url_for("invoices.generate"))
        flash(f"{created} Rechnungen erstellt, {skipped} übersprungen.", "success")
        return redirect(url_for("invoices.billing_run_detail", run_id=billing_run.id))

    return render_template(
        "invoices/generate.html",
        tariffs=tariffs,
        accounts=accounts,
        year=date.today().year,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    """Direkte Rechnungs-Anlage (Entwurf, ohne vorherigen OpenItem).

    Nutzt denselben Positions-Editor wie der OpenItem→Rechnung-Flow und ist
    damit der primäre Weg zur manuellen Rechnungserstellung (siehe ADR-001).
    Kunde ist Pflichtfeld; das Quick-Create-Modal erlaubt die Neuanlage
    direkt aus diesem Formular heraus.
    """
    from app.accounting import services as acc_svc
    customers = Customer.query.order_by(Customer.name).all()
    tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()
    editor_accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    editor_projects = Project.query.filter_by(closed=False).order_by(Project.name).all()

    if request.method == "POST":
        customer_id_raw = request.form.get("customer_id", "").strip()
        if not customer_id_raw:
            flash("Bitte einen Kunden auswählen.", "danger")
            return redirect(url_for("invoices.new"))
        try:
            customer_id = int(customer_id_raw)
        except ValueError:
            flash("Ungültige Kunden-ID.", "danger")
            return redirect(url_for("invoices.new"))
        customer = db.session.get(Customer, customer_id)
        if not customer:
            flash("Kunde nicht gefunden.", "danger")
            return redirect(url_for("invoices.new"))

        try:
            inv_date = date.fromisoformat(request.form["date"])
        except (KeyError, ValueError):
            flash("Ungültiges Rechnungsdatum.", "danger")
            return redirect(url_for("invoices.new"))

        fy_error = acc_svc.open_fiscal_year_error(inv_date)
        if fy_error:
            flash(f"{fy_error} Rechnung wurde nicht erstellt.", "danger")
            return redirect(url_for("invoices.new"))

        is_vat_liable_year = acc_svc.is_year_vat_liable(inv_date.year)
        due_date = (
            date.fromisoformat(request.form["due_date"])
            if request.form.get("due_date") else None
        )
        notes = request.form.get("notes", "").strip()

        inv = Invoice(
            invoice_number=_next_invoice_number(inv_date.year),
            customer_id=customer.id,
            date=inv_date,
            due_date=due_date,
            status=Invoice.STATUS_DRAFT,
            notes=notes,
            created_by_id=current_user.id,
        )
        db.session.add(inv)
        db.session.flush()

        _apply_row_items_to_invoice(inv, request.form, is_vat_liable_year)
        inv.recalculate_total()

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(
                f"Fehler beim Erstellen der Rechnung – alle Änderungen wurden zurückgesetzt: {e}",
                "danger",
            )
            return redirect(url_for("invoices.new"))

        flash(f"Rechnung {inv.invoice_number} erstellt.", "success")
        return redirect(url_for("invoices.detail", invoice_id=inv.id))

    return render_template(
        "invoices/new.html",
        customers=customers,
        tariffs=tariffs,
        today=date.today(),
        editor_accounts=editor_accounts,
        editor_projects=editor_projects,
    )


def _apply_row_items_to_invoice(inv, form, is_vat_liable_year):
    """Fügt die vom Positions-Editor gesendeten Zeilen als ``InvoiceItem``
    zur Rechnung hinzu. Shared zwischen ``invoices.new`` und
    ``accounting.open_item_invoice`` (dort noch inline), damit die
    Parse-Logik nur einmal existiert.

    Leere ``row_account_id[]`` / ``row_project_id[]`` bleiben als NULL
    erhalten — die Vererbung auf ``open_item.account_id`` /
    ``billing_run.account_id`` passiert später im Service-Layer
    (``booking_group_from_invoice_payment``).
    """
    row_types = form.getlist("row_type[]")
    row_tariff_ids = form.getlist("row_tariff_id[]")
    row_consumptions = form.getlist("row_consumption_m3[]")
    row_descriptions = form.getlist("row_description[]")
    row_quantities = form.getlist("row_quantity[]")
    row_units = form.getlist("row_unit[]")
    row_unit_prices = form.getlist("row_unit_price[]")
    row_tax_rates = form.getlist("row_tax_rate[]")
    row_account_ids = form.getlist("row_account_id[]")
    row_project_ids = form.getlist("row_project_id[]")

    def _dec(lst, idx, default="0"):
        v = lst[idx].replace(",", ".") if idx < len(lst) and lst[idx].strip() else default
        try:
            return Decimal(v)
        except Exception:
            return Decimal(default)

    def _int_or_none(lst, idx):
        if idx >= len(lst):
            return None
        raw = (lst[idx] or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    water_tax = Decimal("10") if is_vat_liable_year else None

    for i, rtype in enumerate(row_types):
        # Dimensionen sind pro Zeile gleich, auch wenn eine Tarif-Zeile
        # mehrere Items erzeugt (Grundgebühr/Zusatz/Verbrauch).
        row_account_id = _int_or_none(row_account_ids, i)
        row_project_id = _int_or_none(row_project_ids, i)

        if rtype == "tariff":
            tariff_id_raw = row_tariff_ids[i] if i < len(row_tariff_ids) else ""
            if not tariff_id_raw:
                continue
            try:
                tariff_id = int(tariff_id_raw)
            except ValueError:
                continue
            tariff = db.session.get(WaterTariff, tariff_id)
            if not tariff:
                continue
            consumption = _dec(row_consumptions, i)
            if tariff.base_fee is not None:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=tariff.base_fee_label or "Grundgebühr",
                    quantity=Decimal("1"),
                    unit="Jahr",
                    unit_price=tariff.base_fee,
                    amount=tariff.base_fee,
                    tax_rate=water_tax,
                    account_id=row_account_id,
                    project_id=row_project_id,
                ))
            if tariff.additional_fee is not None:
                db.session.add(InvoiceItem(
                    invoice_id=inv.id,
                    description=tariff.additional_fee_label or "Zusatzgebühr",
                    quantity=Decimal("1"),
                    unit="Jahr",
                    unit_price=tariff.additional_fee,
                    amount=tariff.additional_fee,
                    tax_rate=water_tax,
                    account_id=row_account_id,
                    project_id=row_project_id,
                ))
            amount = (consumption * tariff.price_per_m3).quantize(Decimal("0.01"))
            db.session.add(InvoiceItem(
                invoice_id=inv.id,
                description=f"Wasserverbrauch ({consumption} m³ × {tariff.price_per_m3} €/m³)",
                quantity=consumption,
                unit="m³",
                unit_price=tariff.price_per_m3,
                amount=amount,
                tax_rate=water_tax,
                account_id=row_account_id,
                project_id=row_project_id,
            ))
        elif rtype == "water":
            consumption = _dec(row_consumptions, i)
            unit_price = _dec(row_unit_prices, i)
            desc = (
                row_descriptions[i].strip()
                if i < len(row_descriptions) and row_descriptions[i].strip()
                else f"Wasserverbrauch ({consumption} m³)"
            )
            amount = (consumption * unit_price).quantize(Decimal("0.01"))
            db.session.add(InvoiceItem(
                invoice_id=inv.id,
                description=desc,
                quantity=consumption,
                unit="m³",
                unit_price=unit_price,
                amount=amount,
                tax_rate=water_tax,
                account_id=row_account_id,
                project_id=row_project_id,
            ))
        else:  # free
            desc = row_descriptions[i].strip() if i < len(row_descriptions) else ""
            if not desc:
                continue
            qty = _dec(row_quantities, i, "1")
            unit = row_units[i] if i < len(row_units) and row_units[i].strip() else "Stk"
            unit_price = _dec(row_unit_prices, i)
            tax_rate = _dec(row_tax_rates, i) if is_vat_liable_year else Decimal("0")
            amount = (qty * unit_price).quantize(Decimal("0.01"))
            db.session.add(InvoiceItem(
                invoice_id=inv.id,
                description=desc,
                quantity=qty,
                unit=unit,
                unit_price=unit_price,
                amount=amount,
                tax_rate=tax_rate if tax_rate > 0 else None,
                account_id=row_account_id,
                project_id=row_project_id,
            ))


@bp.route("/<int:invoice_id>")
@login_required
def detail(invoice_id):
    from app.accounting import services as acc_svc
    from app.models import TaxRate
    invoice = db.get_or_404(Invoice, invoice_id)
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()
    doc_format = AppSetting.get("invoice.document_format", "pdf")
    fy_vat_liable = acc_svc.is_year_vat_liable(invoice.date.year) if invoice.date else False
    tax_rates = TaxRate.query.order_by(TaxRate.rate).all() if fy_vat_liable else []
    # Tarife und Projekte werden nur im Entwurfs-Modus für den Positions-Editor benötigt.
    if invoice.status == Invoice.STATUS_DRAFT:
        tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()
        editor_projects = Project.query.filter_by(closed=False).order_by(Project.name).all()
    else:
        tariffs = []
        # Auch in Read-Only-Ansicht brauchen wir Projekte, damit bestehende
        # Items korrekt als hidden-Inputs gerendert werden (Backward-Compat).
        editor_projects = Project.query.order_by(Project.name).all()
    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        accounts=accounts,
        doc_format=doc_format,
        fy_vat_liable=fy_vat_liable,
        tax_rates=tax_rates,
        tariffs=tariffs,
        editor_accounts=accounts,
        editor_projects=editor_projects,
        tax_summary=invoice.tax_breakdown,
        invoice_gross_total=invoice.total_amount,
    )


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


@bp.route("/<int:invoice_id>/items/save", methods=["POST"])
@login_required
def items_save(invoice_id):
    """Speichert alle Rechnungspositionen eines Rechnungsentwurfs in einem
    einzigen Schritt. Ersetzt die frühere Kombination aus item_add/item_edit/
    item_delete und nutzt denselben Positions-Editor wie ``/invoices/new``.

    Nuclear-Replace: alle bisherigen Items werden gelöscht und aus den
    eingereichten Zeilen neu aufgebaut. Der zugeordnete Offene Posten wird
    anschließend auf den neuen Gesamtbetrag synchronisiert.
    """
    from app.accounting import services as acc_svc
    invoice = db.get_or_404(Invoice, invoice_id)
    if _invoice_is_locked(invoice):
        flash("Rechnung ist gesperrt und kann nicht mehr bearbeitet werden.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    is_vat_liable_year = acc_svc.is_year_vat_liable(
        invoice.date.year if invoice.date else date.today().year
    )

    # Alle bestehenden Positionen entfernen — der Editor liefert den kompletten
    # neuen Zustand inklusive der (als "free" gerenderten) bestehenden Items.
    # ADR-003: Mahngebühr-Items bleiben erhalten (werden nur vom Dunning-Service verwaltet).
    for old_item in list(invoice.items):
        if getattr(old_item, "is_dunning_fee", 0):
            continue
        db.session.delete(old_item)
    db.session.flush()

    _apply_row_items_to_invoice(invoice, request.form, is_vat_liable_year)
    invoice.recalculate_total()
    sync_message = _sync_open_item(invoice)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"Fehler beim Speichern – alle Änderungen wurden zurückgesetzt: {e}", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    flash("Rechnungspositionen gespeichert.", "success")
    if sync_message:
        flash("Offener Posten wurde auf den neuen Betrag aktualisiert.", "info")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


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
        try:
            for inv in deletable:
                if inv.open_item:
                    db.session.delete(inv.open_item)
                db.session.delete(inv)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Löschen – alle Änderungen wurden zurückgesetzt: {e}", "danger")
            return redirect(url_for("invoices.index"))
        msg = f"{len(deletable)} Rechnung(en) gelöscht."
        if skipped:
            msg += f" {skipped} übersprungen (nur Entwürfe können gelöscht werden)."
        flash(msg, "success" if deletable else "warning")

    elif action in Invoice.ALL_STATUSES:
        if action == Invoice.STATUS_DRAFT:
            flash("Rechnungen können nicht auf 'Entwurf' zurückgesetzt werden.", "danger")
            return redirect(url_for("invoices.index"))
        # Für Massen-Versenden: ein Buchungskonto für alle manuellen Rechnungen
        bulk_account_id_raw = request.form.get("account_id") or None
        bulk_account_id = int(bulk_account_id_raw) if bulk_account_id_raw else None
        if action == Invoice.STATUS_SENT:
            # Prüfen ob manuelle Rechnungen enthalten sind, für die ein Konto fehlt
            manual_invoices = [inv for inv in invoices if not inv.billing_run_id]
            if manual_invoices and not bulk_account_id:
                flash("Bitte ein Buchungskonto wählen für manuelle Rechnungen (ohne Rechnungslauf).", "danger")
                return redirect(url_for("invoices.index"))
        changed = 0
        try:
            for inv in invoices:
                inv.status = action
                if action == Invoice.STATUS_SENT:
                    account_id = _resolve_open_item_account_id(inv, bulk_account_id)
                    _create_or_update_open_item(inv, account_id=account_id)
                elif action == Invoice.STATUS_CANCELLED:
                    if inv.open_item:
                        inv.open_item.status = OpenItem.STATUS_PAID
                    # ADR-003: Storno → alle aktiven Mahnungen der Rechnung stornieren
                    from app.dunning.services import cancel_dunnings_for_invoice
                    cancel_dunnings_for_invoice(inv)
                changed += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Fehler beim Statuswechsel – alle Änderungen wurden zurückgesetzt: {e}", "danger")
            return redirect(url_for("invoices.index"))
        flash(f"{changed} Rechnung(en) auf '{action}' gesetzt.", "success")

    else:
        flash("Ungültige Aktion.", "danger")

    return redirect(url_for("invoices.index"))


@bp.route("/bulk-pdf-merged", methods=["POST"])
@login_required
def bulk_pdf_merged():
    """Alle markierten Rechnungen als zusammengeführtes PDF zum Download."""
    import io as _io
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
    rendered_docs = []
    for invoice in invoices:
        html_content = _render_pdf_html(invoice)
        rendered_docs.append(HTML(string=html_content).render())
        if _invoice_is_locked(invoice):
            pdf_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "pdf")
            if not invoice.pdf_path or not os.path.exists(invoice.pdf_path):
                rendered_docs[-1].write_pdf(pdf_path)
                invoice.pdf_path = pdf_path
    db.session.commit()
    all_pages = [page for doc in rendered_docs for page in doc.pages]
    merged_path = os.path.join(current_app.config["PDF_DIR"], "_bulk_merged.pdf")
    os.makedirs(current_app.config["PDF_DIR"], exist_ok=True)
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for invoice in invoices:
            if _invoice_is_locked(invoice) and invoice.pdf_path and os.path.exists(invoice.pdf_path):
                zf.write(invoice.pdf_path, f"{invoice.invoice_number}.pdf")
            else:
                pdf_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "pdf")
                html_content = _render_pdf_html(invoice)
                HTML(string=html_content).write_pdf(pdf_path)
                if _invoice_is_locked(invoice):
                    invoice.pdf_path = pdf_path
                zf.write(pdf_path, f"{invoice.invoice_number}.pdf")
    db.session.commit()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="Rechnungen.zip",
                     mimetype="application/zip")


@bp.route("/bulk-docx-zip", methods=["POST"])
@login_required
def bulk_docx_zip():
    """Alle markierten Rechnungen als einzelne .docx-Dateien in einer ZIP-Datei."""
    import zipfile
    import io
    from app.invoices.document_service import generate_docx
    invoice_ids = request.form.getlist("invoice_ids", type=int)
    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "warning")
        return redirect(url_for("invoices.index"))
    invoices = Invoice.query.filter(Invoice.id.in_(invoice_ids)).order_by(Invoice.invoice_number).all()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for invoice in invoices:
            if _invoice_is_locked(invoice) and invoice.doc_path and os.path.exists(invoice.doc_path):
                zf.write(invoice.doc_path, f"{invoice.invoice_number}.docx")
            else:
                doc_data = generate_docx(invoice, wg_settings(), design=_current_design())
                if _invoice_is_locked(invoice):
                    doc_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "docx")
                    with open(doc_path, "wb") as f:
                        f.write(doc_data)
                    invoice.doc_path = doc_path
                zf.writestr(f"{invoice.invoice_number}.docx", doc_data)
    db.session.commit()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="Rechnungen_docx.zip",
                     mimetype="application/zip")


@bp.route("/bulk-docx-merged", methods=["POST"])
@login_required
def bulk_docx_merged():
    """Alle markierten Rechnungen als einzelnes zusammengeführtes .docx zum Download."""
    import io as _io
    from app.invoices.document_service import generate_docx, merge_docx_files
    invoice_ids = request.form.getlist("invoice_ids", type=int)
    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "warning")
        return redirect(url_for("invoices.index"))
    invoices = Invoice.query.filter(Invoice.id.in_(invoice_ids)).order_by(Invoice.invoice_number).all()
    sources = []
    for invoice in invoices:
        if _invoice_is_locked(invoice) and invoice.doc_path and os.path.exists(invoice.doc_path):
            sources.append(invoice.doc_path)
        else:
            doc_data = generate_docx(invoice, wg_settings(), design=_current_design())
            if _invoice_is_locked(invoice):
                doc_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "docx")
                with open(doc_path, "wb") as f:
                    f.write(doc_data)
                invoice.doc_path = doc_path
            sources.append(doc_data)
    db.session.commit()
    merged = merge_docx_files(sources)
    return send_file(_io.BytesIO(merged), as_attachment=True,
                     download_name="Rechnungen_gesamt.docx",
                     mimetype=_DOCX_MIME)


@bp.route("/<int:invoice_id>/status", methods=["POST"])
@login_required
def set_status(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    new_status = request.form.get("status")
    if new_status not in Invoice.ALL_STATUSES:
        abort(400)

    # Zurücksetzen auf Entwurf ist nicht erlaubt, sobald die Rechnung einmal
    # einen höheren Status hatte.
    if new_status == Invoice.STATUS_DRAFT and invoice.status != Invoice.STATUS_DRAFT:
        flash("Eine Rechnung kann nicht mehr auf 'Entwurf' zurückgesetzt werden. "
              "Zum Löschen bitte zuerst auf 'Storniert' setzen.", "danger")
        if request.headers.get("HX-Request"):
            return render_template("invoices/_status_badge.html", invoice=invoice)
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    try:
        old_status = invoice.status

        if new_status == Invoice.STATUS_SENT:
            form_account_id_raw = request.form.get("account_id") or None
            form_account_id = int(form_account_id_raw) if form_account_id_raw else None
            # Für manuelle Rechnungen ist das Buchungskonto Pflicht
            if not invoice.billing_run_id and not form_account_id:
                flash("Bitte ein Buchungskonto wählen, bevor die Rechnung auf 'Versendet' gesetzt wird.", "danger")
                if request.headers.get("HX-Request"):
                    return render_template("invoices/_status_badge.html", invoice=invoice)
                return redirect(url_for("invoices.detail", invoice_id=invoice.id))
            account_id = _resolve_open_item_account_id(invoice, form_account_id)
            invoice.status = new_status
            _create_or_update_open_item(invoice, account_id=account_id)
        else:
            invoice.status = new_status
            if new_status == Invoice.STATUS_CANCELLED and invoice.open_item:
                invoice.open_item.status = OpenItem.STATUS_PAID
            # ADR-003: Storno → alle aktiven Mahnungen der Rechnung stornieren
            if new_status == Invoice.STATUS_CANCELLED:
                from app.dunning.services import cancel_dunnings_for_invoice
                cancel_dunnings_for_invoice(invoice)

        # Automatische Buchung bei Bezahlt-Markierung
        if new_status == Invoice.STATUS_PAID and old_status != Invoice.STATUS_PAID:
            # Zahlung über Service-Layer erzeugen. Bei mehreren Dimensionen
            # (Konto/Projekt/Steuersatz) entsteht automatisch eine
            # Sammelbuchung (ADR-002), sonst eine Einzelbuchung.
            from app.accounting import services as acc_svc
            default_ra = RealAccount.query.filter_by(is_default=True, active=True).first() \
                or RealAccount.query.filter_by(active=True).first()
            real_account_id = default_ra.id if default_ra else None
            try:
                acc_svc.booking_group_from_invoice_payment(
                    invoice=invoice,
                    amount=invoice.total_amount,
                    payment_date=date.today(),
                    real_account_id=real_account_id,
                    created_by_id=current_user.id,
                    open_item=invoice.open_item,
                    reference=invoice.invoice_number,
                )
            except ValueError as ve:
                raise ve
            if invoice.open_item:
                invoice.open_item.status = OpenItem.STATUS_PAID

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"Fehler beim Statuswechsel – alle Änderungen wurden zurückgesetzt: {e}", "danger")
        if request.headers.get("HX-Request"):
            return render_template("invoices/_status_badge.html", invoice=invoice)
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

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

    # Offenes Buchungsjahr für das Zahlungsdatum prüfen
    from app.accounting import services as acc_svc
    payment_date = date.today()
    fy_error = acc_svc.open_fiscal_year_error(payment_date)
    if fy_error:
        flash(fy_error, "danger")
        return redirect(url_for("accounting.open_items"))

    # Standard-Bankkonto verwenden
    default_ra = RealAccount.query.filter_by(is_default=True, active=True).first() \
        or RealAccount.query.filter_by(active=True).first()
    real_account_id = default_ra.id if default_ra else None

    try:
        # Zahlung über Service-Layer erzeugen. Bei mehreren Dimensionen
        # (Konto/Projekt/Steuersatz) entsteht automatisch eine Sammelbuchung,
        # sonst eine einzelne Buchung (ADR-002).
        acc_svc.booking_group_from_invoice_payment(
            invoice=invoice,
            amount=amount,
            payment_date=payment_date,
            real_account_id=real_account_id,
            created_by_id=current_user.id,
            open_item=invoice.open_item,
            reference=invoice.invoice_number,
        )

        from sqlalchemy import func
        paid_total = db.session.query(func.sum(Booking.amount)).filter(
            Booking.invoice_id == invoice.id
        ).scalar() or Decimal("0")
        balance = Decimal(str(invoice.total_amount)) - Decimal(str(paid_total))

        if balance > Decimal("0"):
            invoice.status = Invoice.STATUS_SENT
        elif balance == Decimal("0"):
            invoice.status = Invoice.STATUS_PAID
        else:
            invoice.status = Invoice.STATUS_CREDIT

        if invoice.open_item:
            oi = invoice.open_item
            if balance > Decimal("0"):
                oi.status = OpenItem.STATUS_PARTIAL
            elif balance == Decimal("0"):
                oi.status = OpenItem.STATUS_PAID
            else:
                oi.status = OpenItem.STATUS_CREDIT

        db.session.commit()
    except ValueError as ve:
        db.session.rollback()
        flash(f"Fehler bei der Zahlung: {ve}", "danger")
        return redirect(url_for("accounting.open_items"))
    except Exception as e:
        db.session.rollback()
        flash(f"Fehler bei der Zahlung – alle Änderungen wurden zurückgesetzt: {e}", "danger")
        return redirect(url_for("accounting.open_items"))

    if balance > Decimal("0"):
        flash(f"Teilzahlung von {amount:.2f} \u20ac gebucht. Offener Restbetrag: {balance:.2f} \u20ac", "success")
    elif balance == Decimal("0"):
        flash(f"Rechnung {invoice.invoice_number} vollst\u00e4ndig bezahlt.", "success")
    else:
        flash(f"\u00dcberzahlung von {abs(balance):.2f} \u20ac. Rechnung als Gutschrift markiert.", "info")
    return redirect(url_for("accounting.open_items"))


@bp.route("/<int:invoice_id>/pdf")
@login_required
def pdf(invoice_id):
    import io as _io
    invoice = db.get_or_404(Invoice, invoice_id)
    fmt = _get_document_format(request.args.get("fmt"))

    if fmt == "docx":
        # Gecachte .docx ausliefern wenn vorhanden
        if _invoice_is_locked(invoice) and invoice.doc_path and os.path.exists(invoice.doc_path):
            return send_file(invoice.doc_path, as_attachment=True,
                             download_name=f"{invoice.invoice_number}.docx",
                             mimetype=_DOCX_MIME)
        from app.invoices.document_service import generate_docx
        doc_data = generate_docx(invoice, wg_settings(), design=_current_design())
        if _invoice_is_locked(invoice):
            doc_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "docx")
            with open(doc_path, "wb") as f:
                f.write(doc_data)
            invoice.doc_path = doc_path
            db.session.commit()
        return send_file(_io.BytesIO(doc_data), as_attachment=True,
                         download_name=f"{invoice.invoice_number}.docx",
                         mimetype=_DOCX_MIME)

    # ── PDF (Standard) ────────────────────────────────────────────────────
    # Gesperrte Rechnungen: gecachte PDF ausliefern wenn vorhanden
    if _invoice_is_locked(invoice) and invoice.pdf_path and os.path.exists(invoice.pdf_path):
        return send_file(invoice.pdf_path, as_attachment=False,
                         download_name=f"{invoice.invoice_number}.pdf")
    try:
        from weasyprint import HTML
    except ImportError:
        flash("WeasyPrint ist nicht installiert. PDF-Export nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    html_content = _render_pdf_html(invoice)
    pdf_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "pdf")
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
    import io as _io
    from flask_mail import Message

    invoice = db.get_or_404(Invoice, invoice_id)
    if not invoice.customer.email:
        flash("Keine E-Mail-Adresse beim Kunden hinterlegt.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    fmt = _get_document_format()

    msg = Message(
        subject=f"Rechnung {invoice.invoice_number}",
        recipients=[invoice.customer.email],
        body=_render_email_body(invoice),
    )

    doc_data = None
    pdf_path = None

    if fmt in ("docx", "both"):
        from app.invoices.document_service import generate_docx
        doc_data = generate_docx(invoice, wg_settings(), design=_current_design())
        msg.attach(f"{invoice.invoice_number}.docx", _DOCX_MIME, doc_data)

    if fmt in ("pdf", "both"):
        try:
            import weasyprint
            html_content = _render_pdf_html(invoice)
            pdf_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "pdf")
            weasyprint.HTML(string=html_content).write_pdf(pdf_path)
            with open(pdf_path, "rb") as fp:
                msg.attach(f"{invoice.invoice_number}.pdf", "application/pdf", fp.read())
        except ImportError:
            if fmt == "pdf":
                flash("WeasyPrint ist nicht installiert. E-Mail-Versand nur im Docker-Container verfügbar.", "danger")
                return redirect(url_for("invoices.detail", invoice_id=invoice_id))
            # bei 'both': PDF-Anhang überspringen, .docx wurde bereits angehängt

    if not msg.attachments:
        flash("Kein Dokument konnte generiert werden.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    send_mail(msg)
    invoice.status = Invoice.STATUS_SENT

    if doc_data and _invoice_is_locked(invoice):
        doc_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "docx")
        with open(doc_path, "wb") as f:
            f.write(doc_data)
        invoice.doc_path = doc_path
    if pdf_path:
        invoice.pdf_path = pdf_path

    form_account_id_raw = request.form.get("account_id") or None
    form_account_id = int(form_account_id_raw) if form_account_id_raw else None
    account_id = _resolve_open_item_account_id(invoice, form_account_id)
    _create_or_update_open_item(invoice, account_id=account_id)
    db.session.commit()
    flash(f"Rechnung an {invoice.customer.email} versendet.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/send-email-ajax", methods=["POST"])
@login_required
def send_email_ajax(invoice_id):
    """JSON-Variante für den Massenmail-Versand per JavaScript."""
    from flask import jsonify
    from flask_mail import Message

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

    fmt = _get_document_format()

    try:
        subject = f"Rechnung {invoice.invoice_number}"
        if test_mode:
            subject = f"[TEST – an: {invoice.customer.email}] {subject}"
        body = _render_email_body(invoice)
        if test_mode:
            body = f"[TESTMODUS – eigentlicher Empfänger: {invoice.customer.email}]\n\n{body}"
        msg = Message(subject=subject, recipients=[recipient], body=body)

        doc_data = None
        pdf_path = None

        if fmt in ("docx", "both"):
            from app.invoices.document_service import generate_docx
            doc_data = generate_docx(invoice, wg_settings(), design=_current_design())
            msg.attach(f"{invoice.invoice_number}.docx", _DOCX_MIME, doc_data)

        if fmt in ("pdf", "both"):
            try:
                import weasyprint
                html_content = _render_pdf_html(invoice)
                pdf_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "pdf")
                weasyprint.HTML(string=html_content).write_pdf(pdf_path)
                with open(pdf_path, "rb") as fp:
                    msg.attach(f"{invoice.invoice_number}.pdf", "application/pdf", fp.read())
            except ImportError:
                if fmt == "pdf":
                    return jsonify({"ok": False, "error": "WeasyPrint nicht verfügbar"}), 503
                # bei 'both': PDF-Anhang überspringen, .docx wurde bereits angehängt

        if not msg.attachments:
            return jsonify({"ok": False, "error": "Kein Dokument konnte generiert werden"}), 500

        send_mail(msg)

        if not test_mode:
            invoice.status = Invoice.STATUS_SENT
            if doc_data and _invoice_is_locked(invoice):
                doc_path = _versioned_path(_get_doc_dir(invoice), invoice.invoice_number, "docx")
                with open(doc_path, "wb") as f:
                    f.write(doc_data)
                invoice.doc_path = doc_path
            if pdf_path:
                invoice.pdf_path = pdf_path
            ajax_account_id_raw = request.form.get("account_id") or None
            ajax_account_id = int(ajax_account_id_raw) if ajax_account_id_raw else None
            account_id = _resolve_open_item_account_id(invoice, ajax_account_id)
            _create_or_update_open_item(invoice, account_id=account_id)
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
# Rechnungsläufe – Übersicht, Detail, Löschen
# ---------------------------------------------------------------------------

@bp.route("/billing-runs")
@login_required
def billing_runs():
    runs = BillingRun.query.order_by(BillingRun.created_at.desc()).all()
    return render_template("invoices/billing_runs.html", runs=runs)


@bp.route("/billing-runs/<int:run_id>")
@login_required
def billing_run_detail(run_id):
    run = db.get_or_404(BillingRun, run_id)
    invoices = run.invoices.order_by(Invoice.invoice_number).all()
    doc_format = AppSetting.get("invoice.document_format", "pdf")
    return render_template("invoices/billing_run_detail.html", run=run, invoices=invoices, doc_format=doc_format)


@bp.route("/billing-runs/<int:run_id>/delete", methods=["POST"])
@login_required
def billing_run_delete(run_id):
    run = db.get_or_404(BillingRun, run_id)
    invoices = run.invoices.all()

    deletable = [inv for inv in invoices if inv.status == Invoice.STATUS_DRAFT]
    locked = [inv for inv in invoices if inv.status != Invoice.STATUS_DRAFT]

    if locked:
        # Teilweise gesperrt: Feedback-Seite zeigen (kein Löschen)
        flash(
            f"{len(locked)} Rechnung(en) können nicht gelöscht werden (nicht mehr im Entwurfsstatus). "
            f"Bitte diese Rechnungen manuell stornieren oder entfernen, bevor der Rechnungslauf gelöscht werden kann.",
            "warning",
        )
        return redirect(url_for("invoices.billing_run_detail", run_id=run_id))

    # Alle löschbar
    for inv in deletable:
        db.session.delete(inv)
    db.session.delete(run)
    db.session.commit()
    flash(f"Rechnungslauf und {len(deletable)} Rechnung(en) wurden gelöscht.", "success")
    return redirect(url_for("invoices.billing_runs"))


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
        def _fee_or_none(field):
            v = request.form.get(field, "").strip().replace(",", ".")
            return Decimal(v) if v else None

        t = WaterTariff(
            name=request.form["name"].strip(),
            valid_from=int(request.form["valid_from"]),
            valid_to=int(request.form["valid_to"]) if request.form.get("valid_to") else None,
            base_fee=_fee_or_none("base_fee"),
            base_fee_label=request.form.get("base_fee_label", "").strip() or "Grundgebühr",
            additional_fee=_fee_or_none("additional_fee"),
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
        def _fee_or_none(field):
            v = request.form.get(field, "").strip().replace(",", ".")
            return Decimal(v) if v else None

        t.name = request.form["name"].strip()
        t.valid_from = int(request.form["valid_from"])
        t.valid_to = int(request.form["valid_to"]) if request.form.get("valid_to") else None
        t.base_fee = _fee_or_none("base_fee")
        t.base_fee_label = request.form.get("base_fee_label", "").strip() or "Grundgebühr"
        t.additional_fee = _fee_or_none("additional_fee")
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
