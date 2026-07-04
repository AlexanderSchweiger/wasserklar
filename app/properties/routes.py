import json
import re
from datetime import date, timedelta

from flask import render_template, redirect, url_for, flash, request, make_response, session, current_app
from flask_login import login_required
from sqlalchemy import case as sa_case, exists, func as sa_func

from app.properties import bp
from app.extensions import db
from app.models import Property, PropertyOwnership, Customer, NetworkFeature, PropertyWgProfile
from app.pagination import paginate_query
from app.imports import common as import_common
from app.properties import import_service


# Erlaubte Sort-Keys der Objektliste (Mapping URL-Param -> ORDER-BY-Logik
# in ``_apply_property_sort``).
_SORT_KEYS = {"nr", "type", "address", "owner", "shares"}
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
    shares_filter = request.args.get("shares", "all")
    if shares_filter not in ("all", "with", "without"):
        shares_filter = "all"

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
    # WG-Filter: Liegenschaften mit / ohne Anteilen. Per Subquery (IN/NOT IN),
    # damit es sich nicht mit dem optionalen shares-Sort-JOIN beisst. NOT IN
    # deckt sowohl "kein Profil" als auch "Profil mit 0 Anteilen" ab.
    if shares_filter in ("with", "without"):
        with_shares = db.session.query(PropertyWgProfile.property_id).filter(
            PropertyWgProfile.shares > 0
        )
        if shares_filter == "with":
            query = query.filter(Property.id.in_(with_shares))
        else:
            query = query.filter(~Property.id.in_(with_shares))

    query = _apply_property_sort(query, sort, direction)

    pagination = paginate_query(query, page_key="properties")
    properties = pagination.items

    # WG-Profile (Anteile/m2) der sichtbaren Objekte vorladen (N+1 vermeiden).
    if properties:
        _pids = [p.id for p in properties]
        _wg = PropertyWgProfile.query.filter(
            PropertyWgProfile.property_id.in_(_pids)
        ).all()
    else:
        _wg = []
    wg_property_map = {w.property_id: w for w in _wg}

    ctx = dict(
        properties=properties,
        pagination=pagination,
        q=q,
        sort=sort,
        dir=direction,
        shares_filter=shares_filter,
        wg_property_map=wg_property_map,
    )
    if request.headers.get("HX-Request"):
        return render_template("properties/_table.html", **ctx)

    # BEV-Geocoding: Index-Info (built_at/Anzahl) fuer den Abgleich-Dialog.
    from app.properties import bev_geocode
    ctx["bev_index_info"] = bev_geocode.index_info(current_app.config["BEV_INDEX_PATH"])
    ctx["property_count"] = Property.query.filter_by(active=True).count()
    return render_template("properties/index.html", **ctx)


@bp.route("/fix-housenumbers", methods=["POST"])
@login_required
def fix_housenumbers():
    """Extrahiert Hausnummern aus dem Straßenfeld für alle Objekte,
    bei denen das Hausnummer-Feld leer ist.

    Alles ab der ersten Ziffer in ``strasse`` wird nach ``hausnummer``
    verschoben; der Rest (ohne nachfolgende Leerzeichen) bleibt in ``strasse``.
    """
    candidates = Property.query.filter(
        Property.active.is_(True),
        Property.strasse.isnot(None),
        Property.strasse != "",
        (Property.hausnummer.is_(None)) | (Property.hausnummer == ""),
    ).all()

    changed = 0
    skipped = []
    for prop in candidates:
        m = re.search(r"\d", prop.strasse)
        if m:
            pos = m.start()
            hausnummer = prop.strasse[pos:].strip()
            if len(hausnummer) > 20:
                skipped.append(f'{prop.label()} – „{prop.strasse}"')
                continue
            prop.hausnummer = hausnummer
            prop.strasse = prop.strasse[:pos].strip()
            changed += 1

    if changed:
        db.session.commit()
    if changed:
        flash(f"Hausnummern korrigiert: {changed} Objekt{'e' if changed != 1 else ''} aktualisiert.", "success")
    if skipped:
        skipped_list = "; ".join(skipped)
        flash(
            f"{len(skipped)} Objekt{'e' if len(skipped) != 1 else ''} übersprungen "
            f"(Hausnummer-Teil zu lang, bitte manuell korrigieren): {skipped_list}",
            "warning",
        )
    if not changed and not skipped:
        flash("Keine Objekte gefunden, bei denen eine Hausnummer in der Straße stand.", "info")

    return redirect(url_for("properties.index"))


@bp.route("/geocode-bev", methods=["POST"])
@login_required
def geocode_bev():
    """Gleicht die Adressen der Liegenschaften gegen den BEV-Index ab und setzt
    ihre Koordinaten (Voraussetzung fuer die Hausanschluss-Zuordnung im
    Leitungsnetz).

    Standardlauf ist idempotent (nur Liegenschaften ohne Koordinate). Mit
    ``mode=all`` werden alle neu abgeglichen — sinnvoll nach einem
    Index-Refresh (``flask bev-refresh``).
    """
    from app.properties import bev_geocode

    only_missing = request.form.get("mode") != "all"
    try:
        result = bev_geocode.geocode_properties(only_missing=only_missing)
    except bev_geocode.BevImportError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("properties.index"))

    if result["total"] == 0:
        flash("Keine Liegenschaften zum Abgleichen gefunden "
              "(alle bereits geocodet — für einen Neu-Abgleich „Alle neu“ wählen).", "info")
    else:
        nf = len(result["not_found"])
        category = "success" if result["geocoded"] else "warning"
        msg = (f"BEV-Abgleich: {result['geocoded']} von {result['total']} "
               f"Liegenschaften geocodet.")
        if nf:
            sample = ", ".join(result["not_found"][:8])
            if nf > 8:
                sample += " …"
            msg += (f" {nf} ohne Treffer: {sample} — diese Adressen bitte prüfen "
                    f"(Schreibweise/Hausnummer) oder den Index aktualisieren.")
        flash(msg, category)
    return redirect(url_for("properties.index"))


@bp.route("/bulk-set-address", methods=["POST"])
@login_required
def bulk_set_address():
    """Setzt PLZ, Ort und/oder Land für alle aktiven Liegenschaften auf einen
    gemeinsamen Wert. Gedacht für die Import-Nachbesserung, wenn diese Felder
    beim Import nicht (richtig) befüllt wurden.

    Nur ausgefüllte Eingabefelder werden angewendet — ein leer gelassenes Feld
    lässt die Spalte unberührt (kein versehentliches Leeren). Mit ``mode=empty``
    werden nur Liegenschaften ohne Wert in der jeweiligen Spalte gesetzt, mit
    ``mode=all`` (Default) alle.
    """
    plz = request.form.get("plz", "").strip()
    ort = request.form.get("ort", "").strip()
    land = request.form.get("land", "").strip()
    only_empty = request.form.get("mode") == "empty"

    labels = {"plz": "PLZ", "ort": "Ort", "land": "Land"}
    fields = {k: v for k, v in (("plz", plz), ("ort", ort), ("land", land)) if v}
    if not fields:
        flash("Es wurde kein Wert eingegeben — bitte mindestens PLZ, Ort oder Land ausfüllen.", "warning")
        return redirect(url_for("properties.index"))

    properties = Property.query.filter_by(active=True).all()
    changed = 0
    for prop in properties:
        touched = False
        for attr, value in fields.items():
            current = getattr(prop, attr)
            if only_empty and current not in (None, ""):
                continue
            if current != value:
                setattr(prop, attr, value)
                touched = True
        if touched:
            changed += 1

    if changed:
        db.session.commit()

    feldtext = ", ".join(labels[k] for k in fields)
    if changed:
        flash(
            f"{feldtext} bei {changed} Liegenschaft{'en' if changed != 1 else ''} gesetzt.",
            "success",
        )
    else:
        flash(
            "Keine Liegenschaft geändert (Werte waren bereits gesetzt oder es gibt keine passenden Objekte).",
            "info",
        )
    return redirect(url_for("properties.index"))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    is_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        prop = _property_from_form(Property())
        db.session.add(prop)
        db.session.commit()
        if is_modal:
            resp = make_response("", 204)
            resp.headers["HX-Trigger"] = json.dumps({
                "closePropertyEditModal": True,
                "propertyEdited": {"property_id": prop.id, "created": True},
            })
            return resp
        flash(f"Objekt '{prop.label()}' angelegt.", "success")
        return redirect(url_for("properties.index"))

    if is_modal:
        return render_template("properties/_property_edit_form_body.html", property=Property())
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

    from app.network import vocab as network_vocab

    network_features = NetworkFeature.query.filter_by(
        property_id=property_id
    ).order_by(NetworkFeature.id).all()

    network_features_display = []
    for nf in network_features:
        geom = None
        if nf.geometry:
            try:
                geom = json.loads(nf.geometry)
            except (ValueError, TypeError):
                pass
        type_vocab = (
            network_vocab.POINT_TYPES.get(nf.feature_type)
            if nf.geometry_kind == "point"
            else network_vocab.LINE_TYPES.get(nf.feature_type)
        ) or {}
        network_features_display.append({
            "id": nf.id,
            "name": nf.name,
            "type_label": type_vocab.get("label", nf.feature_type),
            "icon": type_vocab.get("icon", "fa-map-marker-alt"),
            "color": type_vocab.get("color", "#868e96"),
            "geometry_kind": nf.geometry_kind,
            "geometry": geom,
            "lat": nf.lat,
            "lng": nf.lng,
        })

    return render_template(
        "properties/detail.html",
        property=prop,
        customers=customers,
        invoices=invoices,
        open_items_pag=open_items_pag,
        bookings_pag=bookings_pag,
        has_active_owner=bool(active_customer_ids),
        today=date.today(),
        network_features_display=network_features_display,
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


@bp.route("/<int:property_id>/row")
@login_required
def row(property_id):
    """Liefert genau eine Liegenschafts-Tabellenzeile als HTML-Fragment.

    Wird nach dem Modal-Speichern (HX-Trigger ``propertyEdited``) auf der
    Liegenschaftsliste per HTMX nachgeladen und an Ort und Stelle in die
    Tabelle getauscht, statt die ganze Seite neu zu laden — so bleiben Filter,
    Suche und Pagination erhalten. Auf der Detailseite gibt es keine solche
    Zeile; dort greift weiterhin der Default-Reload (siehe
    ``_property_modal_scripts.html``).
    """
    prop = db.get_or_404(Property, property_id)
    profile = PropertyWgProfile.query.filter_by(property_id=property_id).first()
    wg_property_map = {property_id: profile} if profile else {}
    return render_template(
        "properties/_row.html", prop=prop, wg_property_map=wg_property_map,
    )


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
    if sort == "shares":
        # coalesce(shares,0): Objekte ohne WG-Profil zaehlen als 0 Anteile.
        shares_col = sa_func.coalesce(PropertyWgProfile.shares, 0)
        return (
            query.outerjoin(PropertyWgProfile, PropertyWgProfile.property_id == Property.id)
            .order_by(shares_col.desc() if desc else shares_col.asc(),
                      Property.object_number.asc())
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

    # WG-Felder (Anteile/m2) nur im Genossenschafts-Modus — im Versorger-Modus
    # fehlen sie im Formular, bestehende Werte bleiben unangetastet.
    from app.settings_service import is_wassergenossenschaft
    if is_wassergenossenschaft():
        profile = prop.ensure_wg_profile()
        raw_shares = request.form.get("wg_shares", "").strip()
        try:
            profile.shares = int(raw_shares) if raw_shares else 0
        except ValueError:
            profile.shares = 0
        raw_area = request.form.get("area_m2", "").strip()
        try:
            profile.area_m2 = int(raw_area) if raw_area else None
        except ValueError:
            profile.area_m2 = None
    return prop


# ---------------------------------------------------------------------------
# Objekte-Import-Wizard (3 Routen, analog Kunden-Import)
# ---------------------------------------------------------------------------
# Die Session-Keys haben einen property_import_-Prefix, um Kollisionen mit
# anderen Import-Wizards zu vermeiden.

_PI_FILE_KEY = "property_import_file"
_PI_CFG_KEY = "property_import_cfg"
_PI_RESULT_KEY = "property_import_result"


def _abort_to_property_import_upload(
    reason: str = "Sitzung abgelaufen — bitte erneut hochladen.",
    category: str = "warning",
):
    flash(reason, category)
    path = session.pop(_PI_FILE_KEY, None)
    if path:
        import_common.delete_dataframe(path)
    session.pop(_PI_CFG_KEY, None)
    return redirect(url_for("properties.import_upload"))


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_upload():
    """Schritt 1: Datei hochladen + Duplikat-Modus wählen."""
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Bitte eine Datei auswählen.", "warning")
            return redirect(url_for("properties.import_upload"))

        try:
            df = import_common.read_table(f)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("properties.import_upload"))

        # Alten Pickle bereinigen
        old_path = session.pop(_PI_FILE_KEY, None)
        if old_path:
            import_common.delete_dataframe(old_path)

        path = import_common.save_dataframe(df, prefix="property_import_")
        session[_PI_FILE_KEY] = path

        cfg = import_service.PropertyImportConfig.from_form(request.form)
        session[_PI_CFG_KEY] = cfg.to_dict()

        return redirect(url_for("properties.import_preview"))

    return render_template("properties/import.html")


@bp.route("/import/preview", methods=["GET", "POST"])
@login_required
def import_preview():
    """Schritt 2: Spalten zuordnen, Vorschau prüfen, Import ausführen."""
    path = session.get(_PI_FILE_KEY)
    df = import_common.load_dataframe(path) if path else None
    if df is None:
        return _abort_to_property_import_upload()

    columns = list(df.columns)

    if request.method == "POST":
        # Config immer aus dem Form übernehmen (beide Aktionen: refresh + confirm)
        cfg = import_service.PropertyImportConfig.from_form(request.form)
        session[_PI_CFG_KEY] = cfg.to_dict()

        if request.form.get("action") == "confirm":
            baseline = import_service.build_preview_rows(df, cfg)
            merged = import_service.apply_edits(request.form, baseline)
            stats = import_service.commit(merged, cfg)

            # Aufräumen
            path_to_delete = session.pop(_PI_FILE_KEY, None)
            if path_to_delete:
                import_common.delete_dataframe(path_to_delete)
            session.pop(_PI_CFG_KEY, None)
            session[_PI_RESULT_KEY] = stats.to_dict()

            total = stats.created + stats.updated
            category = "success" if total > 0 and not stats.errors else "warning"
            flash(
                f"Import abgeschlossen: {stats.created} angelegt, "
                f"{stats.updated} aktualisiert, {stats.skipped} übersprungen.",
                category,
            )
            return redirect(url_for("properties.import_result"))

        # action=refresh: Vorschau neu rendern (durch Fall-Through zu GET-Pfad)
    else:
        cfg = import_service.PropertyImportConfig.from_dict(session.get(_PI_CFG_KEY))
        # Auto-suggest leere Felder beim ersten Aufruf
        suggested = import_service.suggest_config(columns)
        if not cfg.col_object_number:
            cfg.col_object_number = suggested.col_object_number
        if not cfg.col_object_type:
            cfg.col_object_type = suggested.col_object_type
        if not cfg.col_strasse:
            cfg.col_strasse = suggested.col_strasse
        if not cfg.col_hausnummer:
            cfg.col_hausnummer = suggested.col_hausnummer
        if not cfg.col_plz:
            cfg.col_plz = suggested.col_plz
        if not cfg.col_ort:
            cfg.col_ort = suggested.col_ort
        if not cfg.col_land:
            cfg.col_land = suggested.col_land
        if not cfg.col_notes:
            cfg.col_notes = suggested.col_notes
        if not cfg.col_owner_customer_number:
            cfg.col_owner_customer_number = suggested.col_owner_customer_number

    rows = import_service.build_preview_rows(df, cfg)

    counts = {
        "count_new": sum(1 for r in rows if r.status == import_common.ROW_NEW),
        "count_update": sum(1 for r in rows if r.status == import_common.ROW_UPDATE),
        "count_exists": sum(1 for r in rows if r.status == import_common.ROW_EXISTS),
        "count_error": sum(1 for r in rows if r.status == import_common.ROW_ERROR),
    }

    return render_template(
        "properties/import_preview.html",
        cfg=cfg,
        columns=columns,
        rows=rows,
        counts=counts,
    )


@bp.route("/import/result")
@login_required
def import_result():
    """Schritt 3: Ergebnis anzeigen."""
    stats = session.pop(_PI_RESULT_KEY, None)
    if stats is None:
        return redirect(url_for("properties.index"))
    return render_template("properties/import_result.html", stats=stats)
