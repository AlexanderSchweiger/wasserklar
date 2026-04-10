from flask import render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from app.main import bp
from app.extensions import db
from app.models import Invoice, Booking, WaterMeter, MeterReading
from app.accounting import services as acc_svc
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

    # Ausstehende Ablesungen dieses Jahr
    from app.models import WaterMeter, MeterReading, Property
    total_meters = WaterMeter.query.join(Property).filter(
        WaterMeter.active == True, Property.active == True
    ).count()
    read_this_year = db.session.query(MeterReading).join(WaterMeter).join(Property).filter(
        MeterReading.year == current_year,
        WaterMeter.active == True,
        Property.active == True,
    ).count()
    missing_readings = total_meters - read_this_year

    # Saldo laufendes Jahr (Stornopaare werden über den Service ausgeschlossen)
    _, _, year_income, year_expense, year_balance = acc_svc.year_income_expense(current_year)

    # Letzte Buchungen
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(5).all()

    return render_template(
        "main/dashboard.html",
        current_year=current_year,
        open_invoices=open_invoices,
        total_open=total_open,
        missing_readings=missing_readings,
        year_income=year_income,
        year_expense=year_expense,
        year_balance=year_balance,
        recent_bookings=recent_bookings,
    )
