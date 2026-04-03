from datetime import date, timedelta

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required

from app.properties import bp
from app.extensions import db
from app.models import Property, PropertyOwnership, Customer


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    query = Property.query.filter_by(active=True).order_by(Property.object_number, Property.ort)
    if q:
        query = query.filter(
            db.or_(
                Property.object_number.ilike(f"%{q}%"),
                Property.strasse.ilike(f"%{q}%"),
                Property.ort.ilike(f"%{q}%"),
            )
        )
    properties = query.all()
    if request.headers.get("HX-Request"):
        return render_template("properties/_table.html", properties=properties)
    return render_template("properties/index.html", properties=properties, q=q)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        prop = _property_from_form(Property())
        db.session.add(prop)
        db.session.commit()
        flash(f"Objekt '{prop.label()}' angelegt.", "success")
        return redirect(url_for("properties.index"))
    return render_template("properties/form.html", property=None)


@bp.route("/<int:property_id>")
@login_required
def detail(property_id):
    prop = db.get_or_404(Property, property_id)
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    from app.models import Invoice
    invoices = Invoice.query.filter_by(property_id=property_id).order_by(
        Invoice.date.desc()
    ).all()
    return render_template(
        "properties/detail.html",
        property=prop,
        customers=customers,
        invoices=invoices,
    )


@bp.route("/<int:property_id>/edit", methods=["GET", "POST"])
@login_required
def edit(property_id):
    prop = db.get_or_404(Property, property_id)
    if request.method == "POST":
        _property_from_form(prop)
        db.session.commit()
        flash("Objekt aktualisiert.", "success")
        return redirect(url_for("properties.detail", property_id=prop.id))
    return render_template("properties/form.html", property=prop)


@bp.route("/<int:property_id>/deactivate", methods=["POST"])
@login_required
def deactivate(property_id):
    prop = db.get_or_404(Property, property_id)
    prop.active = False
    db.session.commit()
    flash(f"Objekt '{prop.label()}' archiviert.", "info")
    return redirect(url_for("properties.index"))


@bp.route("/<int:property_id>/ownerships/new", methods=["GET", "POST"])
@login_required
def ownership_new(property_id):
    prop = db.get_or_404(Property, property_id)
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    if request.method == "POST":
        customer_id = int(request.form["customer_id"])
        valid_from_str = request.form.get("valid_from", "")
        if not valid_from_str:
            flash("Bitte ein Startdatum angeben.", "danger")
            return render_template("properties/ownership_form.html",
                                   property=prop, customers=customers)
        valid_from = date.fromisoformat(valid_from_str)

        # Bestehenden aktiven Besitzer beenden
        current = prop.current_owner()
        if current:
            current.valid_to = valid_from - timedelta(days=1)

        ownership = PropertyOwnership(
            property_id=prop.id,
            customer_id=customer_id,
            valid_from=valid_from,
            valid_to=None,
        )
        db.session.add(ownership)
        db.session.commit()
        flash("Besitzer zugewiesen.", "success")
        return redirect(url_for("properties.detail", property_id=prop.id))
    return render_template("properties/ownership_form.html",
                           property=prop, customers=customers,
                           today=date.today())


@bp.route("/<int:property_id>/ownerships/<int:ownership_id>/end", methods=["POST"])
@login_required
def ownership_end(property_id, ownership_id):
    ownership = db.get_or_404(PropertyOwnership, ownership_id)
    valid_to_str = request.form.get("valid_to", "")
    if valid_to_str:
        ownership.valid_to = date.fromisoformat(valid_to_str)
    else:
        ownership.valid_to = date.today()
    db.session.commit()
    flash("Besitzverhältnis beendet.", "info")
    return redirect(url_for("properties.detail", property_id=property_id))


def _property_from_form(prop):
    prop.object_number = request.form.get("object_number", "").strip() or None
    prop.object_type = request.form.get("object_type", "").strip()
    prop.strasse = request.form.get("strasse", "").strip()
    prop.hausnummer = request.form.get("hausnummer", "").strip()
    prop.plz = request.form.get("plz", "").strip()
    prop.ort = request.form.get("ort", "").strip()
    prop.land = request.form.get("land", "Österreich").strip()
    prop.notes = request.form.get("notes", "").strip()
    return prop
