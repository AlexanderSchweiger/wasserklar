from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required
from app.customers import bp
from app.extensions import db
from app.models import Customer, PropertyOwnership


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    ohne_objekt = request.args.get("ohne_objekt", "0") == "1"

    # Subquery: Kunden-IDs mit laufender Eigentumsbeziehung
    owned_ids = db.session.query(PropertyOwnership.customer_id).filter(
        PropertyOwnership.valid_to.is_(None)
    ).subquery()

    query = Customer.query.filter_by(active=True).order_by(Customer.name)
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%"))
    if ohne_objekt:
        query = query.filter(~Customer.id.in_(owned_ids))
    else:
        query = query.filter(Customer.id.in_(owned_ids))
    customers = query.all()

    # Property-Map für Anzeige aufbauen
    if customers:
        ownerships = PropertyOwnership.query.filter(
            PropertyOwnership.valid_to.is_(None),
            PropertyOwnership.customer_id.in_([c.id for c in customers])
        ).all()
    else:
        ownerships = []
    property_map = {o.customer_id: o.property for o in ownerships}

    # HTMX: nur Tabellen-Fragment zurückgeben
    if request.headers.get("HX-Request"):
        return render_template("customers/_table.html", customers=customers,
                               property_map=property_map, ohne_objekt=ohne_objekt)
    return render_template("customers/index.html", customers=customers, q=q,
                           property_map=property_map, ohne_objekt=ohne_objekt)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        c = _customer_from_form(Customer())
        from sqlalchemy import func
        max_nr = db.session.query(func.max(Customer.customer_number)).scalar() or 0
        c.customer_number = max_nr + 1
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
    customer.rechnung_per_email = request.form.get("rechnung_per_email") == "1"
    customer.phone = request.form.get("phone", "").strip()
    customer.notes = request.form.get("notes", "").strip()
    ms = request.form.get("member_since", "")
    if ms:
        from datetime import datetime
        customer.member_since = datetime.strptime(ms, "%Y-%m-%d").date()
    customer.externe_kennung = request.form.get("externe_kennung", "").strip() or None
    raw_base = request.form.get("base_fee_override", "").strip().replace(",", ".")
    customer.base_fee_override = Decimal(raw_base) if raw_base else None
    raw_add = request.form.get("additional_fee_override", "").strip().replace(",", ".")
    customer.additional_fee_override = Decimal(raw_add) if raw_add else None
    return customer
