from flask import render_template
from flask_login import login_required
from sqlalchemy import extract, func, case
from app.main import bp
from app.extensions import db
from app.models import (Invoice, Booking, WaterMeter, MeterReading, Property,
                        BillingPeriod, DunningPolicy, DunningNotice)
from app.accounting import services as acc_svc
from app.dunning import services as dunning_svc
from app.settings_service import meter_replacement_interval
from datetime import date


@bp.route("/")
@login_required
def dashboard():
    current_year = date.today().year

    # Offene Posten
    open_invoices = Invoice.query.filter_by(status=Invoice.STATUS_SENT).count()
    total_open = db.session.query(
        db.func.sum(Invoice.total_amount)
    ).filter_by(status=Invoice.STATUS_SENT).scalar() or 0

    # Aktive Abrechnungsperiode: Zähler, Ablesungen, Gesamtverbrauch
    active_period = BillingPeriod.current()
    total_meters = WaterMeter.query.join(Property).filter(
        WaterMeter.active == True, Property.active == True
    ).count()
    read_in_period = 0
    period_consumption = 0
    if active_period is not None:
        read_in_period = db.session.query(MeterReading).join(WaterMeter).join(Property).filter(
            MeterReading.billing_period_id == active_period.id,
            WaterMeter.active == True,
            Property.active == True,
        ).count()
        period_consumption = db.session.query(
            func.sum(MeterReading.consumption)
        ).filter(MeterReading.billing_period_id == active_period.id).scalar() or 0
    missing_readings = total_meters - read_in_period
    read_percent = round(read_in_period / total_meters * 100) if total_meters else 0

    # Verbrauchs-Historie pro Abrechnungsperiode (letzte 8, chronologisch)
    consumption_rows = (
        db.session.query(BillingPeriod.name, func.sum(MeterReading.consumption))
        .join(MeterReading, MeterReading.billing_period_id == BillingPeriod.id)
        .group_by(BillingPeriod.id, BillingPeriod.name, BillingPeriod.start_date)
        .order_by(BillingPeriod.start_date.asc())
        .all()
    )
    consumption_history = [
        {"label": name, "value": float(total or 0)}
        for name, total in consumption_rows[-8:]
    ]

    # Offene Posten mit Mahnstufe, sortiert nach Fälligkeit (längst fällig zuerst).
    # NULL-due_date via portablem CASE-Präfix ans Ende (MySQL kennt kein NULLS LAST).
    open_invoices_list = (
        Invoice.query
        .filter(Invoice.status == Invoice.STATUS_SENT)
        .order_by(
            case((Invoice.due_date.is_(None), 1), else_=0).asc(),
            Invoice.due_date.asc(),
        )
        .all()
    )
    # Aktuelle Mahnstufe je Rechnung (höchstes aktives level_snapshot) in einem Query.
    level_map = dict(
        db.session.query(
            DunningNotice.invoice_id,
            func.max(DunningNotice.level_snapshot),
        )
        .filter(DunningNotice.status == DunningNotice.STATUS_AKTIV)
        .group_by(DunningNotice.invoice_id)
        .all()
    )
    # Rechnungen, die eine (weitere) Mahnung bräuchten — gleiche Logik wie /dunning/.
    default_policy = (
        DunningPolicy.query.filter_by(is_default=True).first()
        or DunningPolicy.query.filter_by(active=True).first()
    )
    needs_dunning_ids = set()
    if default_policy is not None:
        needs_dunning_ids = {
            inv.id for inv, _stage in dunning_svc.eligible_invoices_for_stage(default_policy)
        }
    open_item_rows = [
        {
            "invoice": inv,
            "level": level_map.get(inv.id, 0),
            "needs_dunning": inv.id in needs_dunning_ids,
        }
        for inv in open_invoices_list
    ]

    # Zähler, die heuer (oder überfällig) zu tauschen sind.
    # Fälligkeitsjahr = (Eichjahr, ersatzweise Einbaujahr) + Tauschintervall.
    interval = meter_replacement_interval()
    active_meters = (
        WaterMeter.query.join(Property)
        .filter(WaterMeter.active == True, Property.active == True)
        .all()
    )
    meters_to_swap = []
    for m in active_meters:
        base_year = m.eichjahr or (m.installed_from.year if m.installed_from else None)
        if base_year is None:
            continue
        due_year = base_year + interval
        if due_year <= current_year:
            meters_to_swap.append({
                "meter": m,
                "base_year": base_year,
                "based_on_eichjahr": m.eichjahr is not None,
                "due_year": due_year,
                "overdue": due_year < current_year,
            })
    meters_to_swap.sort(key=lambda r: r["due_year"])

    # Saldo laufendes Jahr (Stornopaare werden über den Service ausgeschlossen)
    _, _, year_income, year_expense, year_balance = acc_svc.year_income_expense(current_year)

    # Letzte Buchungen des aktuellen Wirtschaftsjahres
    recent_bookings = (
        Booking.query
        .filter(extract("year", Booking.date) == current_year)
        .order_by(Booking.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "main/dashboard.html",
        current_year=current_year,
        today=date.today(),
        active_period=active_period,
        open_invoices=open_invoices,
        total_open=total_open,
        total_meters=total_meters,
        missing_readings=missing_readings,
        period_consumption=period_consumption,
        read_percent=read_percent,
        consumption_history=consumption_history,
        open_item_rows=open_item_rows,
        meters_to_swap=meters_to_swap,
        meter_interval=interval,
        year_income=year_income,
        year_expense=year_expense,
        year_balance=year_balance,
        recent_bookings=recent_bookings,
    )
