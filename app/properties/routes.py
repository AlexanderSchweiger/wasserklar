from datetime import date, timedelta

import json

from flask import render_template, redirect, url_for, flash, request, make_response
from flask_login import login_required
from sqlalchemy import case as sa_case, exists, func as sa_func

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
        owner_match = exists().where(
            PropertyOwnership.property_id == Property.id,
            PropertyOwnership.valid_to.is_(None),
            PropertyOwnership.customer_id == Customer.id,
            Customer.name.ilike(f"%{q}%"),
        )
        query = query.filter(
            db.or_(
                Property.object_number.ilike(f"%{q}%"),
                Property.strasse.ilike(f"%{q}%"),
                Property.ort.ilike(f"%{q}%"),
                owner_match,
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
    from app.models import Invoice, OpenItem, Booking

    prop = db.get_or_404(Property, property_id)
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    invoices = Invoice.query.filter_by(property_id=property_id).order_by(
        Invoice.date.desc()
    ).all()

    # Aktive Eigentuemer — laut Datenmodell sind mehrere parallele Ownerships
    # erlaubt (Ehepaare, Erbengemeinschaften). Offene Posten und Buchungen
    # aller aktuell aktiven Eigentuemer-Kunden zusammenziehen.
    active_customer_ids = [
        o.customer_id
        for o in PropertyOwnership.query.filter_by(
            property_id=property_id, valid_to=None
        ).all()
    ]

    open_items_pag = _mini_paginate(
        OpenItem.query.filter(
            OpenItem.customer_id.in_(active_customer_ids),
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        ).order_by(OpenItem.date.asc(), OpenItem.id.asc()),
        page_param="op_page",
        per_page=5,
    ) if active_customer_ids else None

    bookings_pag = _mini_paginate(
        Booking.query.filter(Booking.customer_id.in_(active_customer_ids))
        .order_by(Booking.date.desc(), Booking.id.desc()),
        page_param="bk_page",
        per_page=5,
    ) if active_customer_ids else None

    return render_template(
        "properties/detail.html",
        property=prop,
        customers=customers,
        invoices=invoices,
        open_items_pag=open_items_pag,
        bookings_pag=bookings_pag,
        has_active_owner=bool(active_customer_ids),
        today=date.today(),
    )


def _mini_paginate(query, *, page_param: str, per_page: int):
    """Schlanke Pagination fuer mehrere Listen auf einer Seite.

    ``paginate_query`` aus ``app.pagination`` belegt fix den URL-Param
    ``page`` — auf der Properties-Detail-Seite teilen sich aber zwei Listen
    (offene Posten + Buchungen) eine URL. Daher hier ein separater Param pro
    Liste (``op_page`` / ``bk_page``).
    """
    try:
        page = max(1, int(request.args.get(page_param, 1)))
    except (TypeError, ValueError):
        page = 1
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    # SimpleNamespace statt dict — sonst kollidiert ``.items`` in Jinja mit
    # der dict.items()-Methode (Attribut-Lookup hat Vorrang vor Item-Lookup).
    from types import SimpleNamespace
    return SimpleNamespace(
        items=items,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
        param=page_param,
        has_prev=page > 1,
        has_next=page < pages,
        first_index=0 if total == 0 else (page - 1) * per_page + 1,
        last_index=min(page * per_page, total),
    )


@bp.route("/<int:property_id>/edit", methods=["GET", "POST"])
@login_required
def edit(property_id):
    prop = db.get_or_404(Property, property_id)
    is_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        _property_from_form(prop)
        db.session.commit()
        if is_modal:
            resp = make_response("", 204)
            resp.headers["HX-Trigger"] = json.dumps({
                "closePropertyEditModal": True,
                "propertyEdited": {"property_id": prop.id},
            })
            return resp
        flash("Objekt aktualisiert.", "success")
        return redirect(url_for("properties.detail", property_id=prop.id))

    if is_modal:
        return render_template("properties/_property_edit_form_body.html", property=prop)
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
    is_modal = bool(request.headers.get("X-From-Modal"))

    def _render_form(template: str):
        return render_template(
            template, property=prop, ownership=None,
            customers=customers, today=date.today(),
        )

    if request.method == "GET" and is_modal:
        return _render_form("properties/_ownership_edit_form_body.html")

    if request.method == "POST":
        customer_id = int(request.form["customer_id"])
        valid_from_str = request.form.get("valid_from", "")
        if not valid_from_str:
            flash("Bitte ein Startdatum angeben.", "danger")
            if is_modal:
                return _render_form("properties/_ownership_edit_form_body.html")
            return _render_form("properties/ownership_form.html")
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
        if is_modal:
            resp = make_response("", 204)
            resp.headers["HX-Trigger"] = json.dumps({
                "closeOwnershipEditModal": True,
                "ownershipEdited": {
                    "ownership_id": ownership.id,
                    "property_id": prop.id,
                    "created": True,
                },
            })
            return resp
        return redirect(url_for("properties.detail", property_id=prop.id))
    return _render_form("properties/ownership_form.html")


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


@bp.route("/<int:property_id>/ownerships/<int:ownership_id>/edit", methods=["GET", "POST"])
@login_required
def ownership_edit(property_id, ownership_id):
    ownership = db.get_or_404(PropertyOwnership, ownership_id)
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    is_modal = bool(request.headers.get("X-From-Modal"))

    def _render_form(template: str):
        return render_template(
            template, property=ownership.property,
            ownership=ownership, customers=customers,
            today=date.today(),
        )

    if request.method == "GET" and is_modal:
        return _render_form("properties/_ownership_edit_form_body.html")

    if request.method == "POST":
        customer_id = int(request.form["customer_id"])
        valid_from_str = request.form.get("valid_from", "").strip()
        if not valid_from_str:
            flash("Bitte ein Startdatum angeben.", "danger")
            if is_modal:
                return _render_form("properties/_ownership_edit_form_body.html")
            return _render_form("properties/ownership_edit.html")
        valid_to_str = request.form.get("valid_to", "").strip()
        valid_from = date.fromisoformat(valid_from_str)
        valid_to = date.fromisoformat(valid_to_str) if valid_to_str else None
        if valid_to and valid_to < valid_from:
            flash("Das Bis-Datum darf nicht vor dem Von-Datum liegen.", "danger")
            if is_modal:
                return _render_form("properties/_ownership_edit_form_body.html")
            return _render_form("properties/ownership_edit.html")
        ownership.customer_id = customer_id
        ownership.valid_from = valid_from
        ownership.valid_to = valid_to
        db.session.commit()
        flash("Besitzverhältnis aktualisiert.", "success")
        if is_modal:
            resp = make_response("", 204)
            resp.headers["HX-Trigger"] = json.dumps({
                "closeOwnershipEditModal": True,
                "ownershipEdited": {
                    "ownership_id": ownership.id,
                    "property_id": property_id,
                },
            })
            return resp
        return redirect(url_for("properties.detail", property_id=property_id))
    return _render_form("properties/ownership_edit.html")


@bp.route("/<int:property_id>/ownerships/<int:ownership_id>/delete", methods=["POST"])
@login_required
def ownership_delete(property_id, ownership_id):
    ownership = db.get_or_404(PropertyOwnership, ownership_id)
    db.session.delete(ownership)
    db.session.commit()
    flash("Besitzverhältnis gelöscht.", "info")
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
