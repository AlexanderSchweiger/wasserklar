from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required
from app.customers import bp
from app.extensions import db
from app.models import Customer


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    query = Customer.query.filter_by(active=True).order_by(Customer.name)
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%"))
    customers = query.all()
    # HTMX: nur Tabellen-Fragment zurückgeben
    if request.headers.get("HX-Request"):
        return render_template("customers/_table.html", customers=customers)
    return render_template("customers/index.html", customers=customers, q=q)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        c = _customer_from_form(Customer())
        db.session.add(c)
        db.session.commit()
        flash(f"Kunde '{c.name}' angelegt.", "success")
        return redirect(url_for("customers.index"))
    return render_template("customers/form.html", customer=None)


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    from app.models import Invoice
    invoices = Invoice.query.filter_by(customer_id=customer_id).order_by(
        Invoice.date.desc()
    ).all()
    return render_template("customers/detail.html", customer=customer, invoices=invoices)


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    if request.method == "POST":
        _customer_from_form(customer)
        db.session.commit()
        flash("Kunde aktualisiert.", "success")
        return redirect(url_for("customers.detail", customer_id=customer.id))
    return render_template("customers/form.html", customer=customer)


@bp.route("/<int:customer_id>/deactivate", methods=["POST"])
@login_required
def deactivate(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    customer.active = False
    db.session.commit()
    flash(f"Kunde '{customer.name}' archiviert.", "info")
    return redirect(url_for("customers.index"))


def _customer_from_form(customer):
    from datetime import date
    from decimal import Decimal
    customer.name = request.form.get("name", "").strip()
    customer.strasse = request.form.get("strasse", "").strip()
    customer.hausnummer = request.form.get("hausnummer", "").strip()
    customer.plz = request.form.get("plz", "").strip()
    customer.ort = request.form.get("ort", "").strip()
    customer.land = request.form.get("land", "Österreich").strip()
    customer.email = request.form.get("email", "").strip()
    customer.phone = request.form.get("phone", "").strip()
    customer.notes = request.form.get("notes", "").strip()
    ms = request.form.get("member_since", "")
    if ms:
        from datetime import datetime
        customer.member_since = datetime.strptime(ms, "%Y-%m-%d").date()
    raw_base = request.form.get("base_fee_override", "").strip().replace(",", ".")
    customer.base_fee_override = Decimal(raw_base) if raw_base else None
    raw_add = request.form.get("additional_fee_override", "").strip().replace(",", ".")
    customer.additional_fee_override = Decimal(raw_add) if raw_add else None
    return customer
