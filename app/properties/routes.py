from datetime import date, timedelta

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_required
from sqlalchemy import case as sa_case, func as sa_func

from app.properties import bp
from app.extensions import db
from app.models import Property, PropertyOwnership, Customer
from app.pagination import paginate_query


# Erlaubte Sort-Keys der Objektliste (Mapping URL-Param -> ORDER-BY-Logik
# in ``_apply_property_sort``).
_SORT_KEYS = {"nr", "type", "address", "owner"}
_DEFAULT_SORT = "nr"


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", _DEFAULT_SORT)
    if sort not in _SORT_KEYS:
        sort = _DEFAULT_SORT
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"

    query = Property.query.filter_by(active=True)
    if q:
        query = query.filter(
            db.or_(
                Property.object_number.ilike(f"%{q}%"),
                Property.strasse.ilike(f"%{q}%"),
                Property.ort.ilike(f"%{q}%"),
            )
        )
    query = _apply_property_sort(query, sort, direction)

    pagination = paginate_query(query, page_key="properties")
    properties = pagination.items
    ctx = dict(
        properties=properties,
        pagination=pagination,
        q=q,
        sort=sort,
        dir=direction,
    )
    if request.headers.get("HX-Request"):
        return render_template("properties/_table.html", **ctx)
    return render_template("properties/index.html", **ctx)


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


def _apply_property_sort(query, sort: str, direction: str):
    """Haengt die ORDER-BY-Klausel passend zum gewaehlten Spalten-Sort an.

    Sekundaer immer nach object_number, damit gleiche Werte stabil sortiert
    sind. NULL-Werte (z.B. fehlende Objektnummer oder Objekte ohne aktuellen
    Besitzer) wandern in beiden Richtungen ans Ende — portabel ueber SQLite,
    MySQL/MariaDB und Postgres via ``IS NULL``-CASE-Sortier-Praefix (ANSI
    ``NULLS LAST`` wird von MySQL nicht unterstuetzt).
    """
    desc = direction == "desc"

    def order(col):
        return [
            sa_case((col.is_(None), 1), else_=0).asc(),
            col.desc() if desc else col.asc(),
        ]

    if sort == "type":
        return query.order_by(
            *order(Property.object_type),
            *order(Property.object_number),
        )
    if sort == "address":
        return query.order_by(
            *order(Property.ort),
            *order(Property.strasse),
            *order(Property.hausnummer),
            *order(Property.object_number),
        )
    if sort == "owner":
        # Pro Objekt nur ein Besitzer-Name fuer den Sort (min(name) als
        # Aggregat — pro Property gibt's per Datenmodell ohnehin nur einen
        # aktiven Eigentuemer, das min ist nur Schutz gegen Datenanomalien).
        # LEFT JOIN, damit Objekte ohne Besitzer nicht rausfallen — sie landen
        # via NULLS-LAST-CASE am Ende.
        sub = (
            db.session.query(
                PropertyOwnership.property_id.label("pid"),
                sa_func.min(Customer.name).label("owner_name"),
            )
            .join(Customer, Customer.id == PropertyOwnership.customer_id)
            .filter(PropertyOwnership.valid_to.is_(None))
            .group_by(PropertyOwnership.property_id)
            .subquery()
        )
        return (
            query.outerjoin(sub, sub.c.pid == Property.id)
            .order_by(*order(sub.c.owner_name), Property.object_number.asc())
        )
    # Default und sort == "nr"
    return query.order_by(*order(Property.object_number), Property.ort.asc())


def _property_from_form(prop):
    from decimal import Decimal
    prop.object_number = request.form.get("object_number", "").strip() or None
    prop.object_type = request.form.get("object_type", "").strip()
    prop.strasse = request.form.get("strasse", "").strip()
    prop.hausnummer = request.form.get("hausnummer", "").strip()
    prop.plz = request.form.get("plz", "").strip()
    prop.ort = request.form.get("ort", "").strip()
    prop.land = request.form.get("land", "Österreich").strip()
    prop.notes = request.form.get("notes", "").strip()
    raw_base = request.form.get("base_fee_override", "").strip().replace(",", ".")
    prop.base_fee_override = Decimal(raw_base) if raw_base else None
    raw_add = request.form.get("additional_fee_override", "").strip().replace(",", ".")
    prop.additional_fee_override = Decimal(raw_add) if raw_add else None
    return prop
