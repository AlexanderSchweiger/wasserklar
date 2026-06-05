import json
import re

from flask import (render_template, redirect, url_for, flash, request, jsonify,
                   session, make_response)
from flask_login import login_required
from sqlalchemy import case as sa_case, func as sa_func, or_
from sqlalchemy.exc import IntegrityError

from app.customers import bp
from app.customers.duplicate_check import find_similar_customers
from app.extensions import db
from app.models import Customer, Property, PropertyOwnership, CustomerWgProfile, WgFunction
from app.wg import STATUS_LABELS, FUNCTION_LABELS
from app.pagination import paginate_query
from app.utils import bump_customer_counter_to, next_customer_number
from app.imports import common as import_common
from app.customers import import_service


# Pflichtfelder auf Formular-Ebene. Adressfelder sind seit dem
# Lieferanten-Rollout bewusst alle optional — der Name reicht (z.B. Behoerden,
# Online-Dienste, ad-hoc-Lieferanten ohne vollstaendige Adresse).
REQUIRED_ADDRESS_FIELDS = [
    ("name", "Name"),
]

# Erlaubte Werte des type-Filters in der Liste.
_TYPE_FILTERS = {"customer", "supplier", "all"}

# Erlaubte Sort-Keys der Kontaktliste (Mapping URL-Param -> ORDER-BY-Logik
# in ``_apply_customer_sort``).
_SORT_KEYS = {"nr", "name", "type", "address", "object", "email"}
_DEFAULT_SORT = "name"


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "all")
    if type_filter not in _TYPE_FILTERS:
        type_filter = "all"
    sort = request.args.get("sort", _DEFAULT_SORT)
    if sort not in _SORT_KEYS:
        sort = _DEFAULT_SORT
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    country_filter = request.args.get("country", "").strip()
    status_filter = request.args.get("status", "").strip()
    func_filter = request.args.get("func", "").strip()

    query = Customer.query.filter_by(active=True)
    if type_filter == "customer":
        query = query.filter(Customer.is_customer.is_(True))
    elif type_filter == "supplier":
        query = query.filter(Customer.is_supplier.is_(True))
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%"))
    if country_filter:
        query = query.filter(Customer.land == country_filter)
    # WG-Filter: Status (fehlendes Profil = Mitglied, Default) + Funktion.
    if status_filter in STATUS_LABELS:
        query = query.outerjoin(
            CustomerWgProfile, CustomerWgProfile.customer_id == Customer.id
        )
        if status_filter == "member":
            query = query.filter(or_(
                CustomerWgProfile.status.is_(None),
                CustomerWgProfile.status == "member",
            ))
        else:
            query = query.filter(CustomerWgProfile.status == status_filter)
    # Funktions-Filter: Sentinels __any__/__none__ fuer "hat irgendeine Funktion"
    # bzw. "hat keine Funktion", sonst eine konkrete Funktion. customer_id ist
    # NOT NULL -> NOT IN ist dialekt-portabel ohne NULL-Falle.
    if func_filter == "__any__":
        query = query.filter(Customer.id.in_(
            db.session.query(WgFunction.customer_id)
        ))
    elif func_filter == "__none__":
        query = query.filter(~Customer.id.in_(
            db.session.query(WgFunction.customer_id)
        ))
    elif func_filter in FUNCTION_LABELS:
        query = query.filter(Customer.id.in_(
            db.session.query(WgFunction.customer_id).filter(
                WgFunction.function == func_filter
            )
        ))

    query = _apply_customer_sort(query, sort, direction)

    pagination = paginate_query(query, page_key="customers")
    customers = pagination.items

    # Distinct-Laender fuer den Filter-Dropdown.
    countries = [
        r[0] for r in db.session.query(Customer.land)
        .filter(Customer.active.is_(True), Customer.land.isnot(None), Customer.land != "")
        .distinct().order_by(Customer.land).all()
    ]

    # Property-Map fuer Anzeige aufbauen — nur fuer die aktuell sichtbaren Kunden.
    if customers:
        ownerships = PropertyOwnership.query.filter(
            PropertyOwnership.valid_to.is_(None),
            PropertyOwnership.customer_id.in_([c.id for c in customers])
        ).all()
    else:
        ownerships = []
    property_map = {o.customer_id: o.property for o in ownerships}

    # WG-Profile + Funktionen der sichtbaren Kontakte vorladen (N+1 vermeiden).
    if customers:
        _ids = [c.id for c in customers]
        _profiles = CustomerWgProfile.query.filter(
            CustomerWgProfile.customer_id.in_(_ids)
        ).all()
        _funcs = WgFunction.query.filter(WgFunction.customer_id.in_(_ids)).all()
    else:
        _profiles, _funcs = [], []
    wg_profile_map = {p.customer_id: p for p in _profiles}
    wg_functions_map = {}
    for _f in _funcs:
        wg_functions_map.setdefault(_f.customer_id, []).append(_f.function)

    # Back-URL fuer Edit-/Delete-Links: erhaelt alle aktuellen Filter, Such-
    # und Pagination-Parameter, damit der User nach dem Speichern wieder auf
    # derselben Liste landet.
    back_url = request.full_path
    if back_url.endswith("?"):
        back_url = back_url[:-1]

    ctx = dict(
        customers=customers,
        property_map=property_map,
        pagination=pagination,
        type_filter=type_filter,
        q=q,
        sort=sort,
        dir=direction,
        back_url=back_url,
        country_filter=country_filter,
        countries=countries,
        status_filter=status_filter,
        func_filter=func_filter,
        wg_profile_map=wg_profile_map,
        wg_functions_map=wg_functions_map,
    )
    # HTMX: nur Tabellen-Fragment zurueckgeben
    if request.headers.get("HX-Request"):
        return render_template("customers/_table.html", **ctx)
    return render_template("customers/index.html", **ctx)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        # 1) Pflichtfeld-Validierung
        missing = _missing_required_fields(request.form)
        if missing:
            flash(
                "Bitte folgende Pflichtfelder ausfüllen: " + ", ".join(missing),
                "danger",
            )
            return _render_new_form(request.form)

        # 2) Mindestens ein Kontakttyp
        is_customer = request.form.get("is_customer") == "1"
        is_supplier = request.form.get("is_supplier") == "1"
        if not (is_customer or is_supplier):
            flash("Bitte mindestens einen Kontakttyp wählen (Kunde oder Lieferant).", "danger")
            return _render_new_form(request.form)

        # 3) Dubletten-Gate (außer der Nutzer hat "trotzdem anlegen" bestätigt).
        # Dubletten-Suche ueber alle Kontakttypen (auch Lieferanten), damit
        # gleiche Adressen nicht doppelt angelegt werden.
        force = request.form.get("force") == "1"
        if not force:
            similar = find_similar_customers(
                name=request.form.get("name", ""),
                strasse=request.form.get("strasse", ""),
                plz=request.form.get("plz", ""),
                ort=request.form.get("ort", ""),
            )
            if similar:
                return render_template(
                    "customers/_duplicate_warning.html",
                    similar=similar,
                    form_data=request.form,
                )

        # 4) Anlage
        c = Customer()
        nr_error = _apply_customer_fields(c, request.form, is_new=True)
        if nr_error:
            flash(nr_error, "danger")
            return _render_new_form(request.form)
        db.session.add(c)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                "Die Kundennummer wurde gerade von einem anderen Vorgang vergeben. "
                "Bitte erneut speichern.",
                "danger",
            )
            return _render_new_form(request.form)
        flash(f"Kontakt '{c.name}' angelegt.", "success")
        return redirect(url_for("customers.index"))
    suggested_nr = next_customer_number(peek=True)
    return _render_new_form(form_data=None, suggested_nr=suggested_nr)


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
    # Edit laeuft wahlweise als Vollseite (form.html) oder im Modal: der
    # X-From-Modal-Header steuert GET (Body-Fragment) und POST-Antwort
    # (204 + HX-Trigger statt Redirect).
    is_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        warnings = []
        error = _validate_customer_form(request.form)
        if not error:
            error = _apply_customer_fields(customer, request.form, is_new=False)
        if not error:
            # WG-Funktions-Warnungen aus dem fertig befuellten Objekt ableiten
            # (nur Warnung, kein Block) und nach erfolgreichem Speichern flashen.
            from app.settings_service import is_wassergenossenschaft
            from app.wg import function_warnings
            if is_wassergenossenschaft():
                warnings = function_warnings(customer.wg_status, customer.function_keys())
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                error = ("Die Kundennummer wurde gerade von einem anderen Vorgang "
                         "vergeben. Bitte erneut speichern.")

        if error:
            if is_modal:
                return render_template(
                    "customers/_customer_edit_form_body.html",
                    customer=customer, form_data=request.form, error=error,
                )
            flash(error, "danger")
            return render_template(
                "customers/form.html", customer=customer, form_data=request.form,
            )

        for w in warnings:
            flash(w, "warning")

        if is_modal:
            resp = make_response("", 204)
            resp.headers["HX-Trigger"] = json.dumps({
                "closeCustomerEditModal": True,
                "customerEdited": {"customer_id": customer.id},
            })
            return resp

        flash("Kontakt aktualisiert.", "success")
        # Redirect zurueck zur Uebersicht — bevorzugt zur exakten URL,
        # von der der User kam (mit Filter, Suche, Pagination). Open-Redirect-
        # Schutz: nur Pfade unterhalb /customers erlauben.
        next_url = request.form.get("next", "")
        if next_url.startswith("/customers"):
            return redirect(next_url)
        return redirect(url_for("customers.index"))

    # GET
    if is_modal:
        return render_template(
            "customers/_customer_edit_form_body.html", customer=customer, form_data=None,
        )
    next_url = request.args.get("next", "")
    return render_template(
        "customers/form.html", customer=customer, form_data=None, next_url=next_url,
    )


@bp.route("/<int:customer_id>/row")
@login_required
def row(customer_id):
    """Liefert genau eine Kontakt-Tabellenzeile als HTML-Fragment.

    Wird nach dem Modal-Speichern (HX-Trigger ``customerEdited``) per HTMX
    nachgeladen und an Ort und Stelle in die Tabelle getauscht, statt die
    ganze Seite neu zu laden — so bleiben Filter, Suche und Pagination der
    Liste erhalten. Die aktuellen Listen-Filter kommen als Query-Args mit, damit
    das ``next`` des Loeschen-Formulars in der frisch gerenderten Zeile weiter
    auf die gefilterte Liste zeigt.
    """
    customer = db.get_or_404(Customer, customer_id)

    ownership = PropertyOwnership.query.filter(
        PropertyOwnership.valid_to.is_(None),
        PropertyOwnership.customer_id == customer_id,
    ).first()
    property_map = {customer_id: ownership.property} if ownership else {}

    profile = CustomerWgProfile.query.filter_by(customer_id=customer_id).first()
    wg_profile_map = {customer_id: profile} if profile else {}
    funcs = WgFunction.query.filter_by(customer_id=customer_id).all()
    wg_functions_map = {customer_id: [f.function for f in funcs]} if funcs else {}

    return render_template(
        "customers/_row.html",
        c=customer,
        property_map=property_map,
        wg_profile_map=wg_profile_map,
        wg_functions_map=wg_functions_map,
        type_filter=request.args.get("type", ""),
        q=request.args.get("q", ""),
        sort=request.args.get("sort", _DEFAULT_SORT),
        dir=request.args.get("dir", "asc"),
        country_filter=request.args.get("country", ""),
        status_filter=request.args.get("status", ""),
        func_filter=request.args.get("func", ""),
    )


@bp.route("/<int:customer_id>/deactivate", methods=["POST"])
@login_required
def deactivate(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    customer.active = False
    db.session.commit()
    flash(f"Kontakt '{customer.name}' archiviert.", "info")
    return redirect(url_for("customers.index"))


@bp.route("/<int:customer_id>/delete", methods=["POST"])
@login_required
def delete(customer_id):
    """Hartes Loeschen — nur wenn keine Referenzen bestehen.

    Bei vorhandenen Referenzen schlagen wir das Archivieren als Alternative vor.
    """
    from app.models import Booking, BookingGroup, Invoice, OpenItem

    customer = db.get_or_404(Customer, customer_id)

    blockers = []
    if PropertyOwnership.query.filter_by(customer_id=customer_id).first():
        blockers.append("Eigentümerverhältnisse")
    if Invoice.query.filter_by(customer_id=customer_id).first():
        blockers.append("Rechnungen")
    if Booking.query.filter_by(customer_id=customer_id).first():
        blockers.append("Buchungen")
    if BookingGroup.query.filter_by(customer_id=customer_id).first():
        blockers.append("Sammelbuchungen")
    if OpenItem.query.filter_by(customer_id=customer_id).first():
        blockers.append("Offene Posten")

    if blockers:
        flash(
            f"Kontakt '{customer.name}' kann nicht gelöscht werden, da noch "
            f"verknüpft: {', '.join(blockers)}. Bitte stattdessen archivieren.",
            "danger",
        )
        return redirect(url_for("customers.detail", customer_id=customer_id))

    name = customer.name
    db.session.delete(customer)
    db.session.commit()
    flash(f"Kontakt '{name}' gelöscht.", "info")
    # Filter-Erhalt: Kontaktliste-URL kommt als hidden ``next`` aus dem Form.
    # Open-Redirect-Schutz: nur Pfade unterhalb /customers erlauben.
    next_url = request.form.get("next", "")
    if next_url.startswith("/customers"):
        return redirect(next_url)
    return redirect(url_for("customers.index"))


@bp.route("/fix-housenumbers", methods=["POST"])
@login_required
def fix_housenumbers():
    """Extrahiert Hausnummern aus dem Straßenfeld für alle Kontakte,
    bei denen das Hausnummer-Feld leer ist.

    Alles ab der ersten Ziffer in ``strasse`` wird nach ``hausnummer``
    verschoben; der Rest (ohne nachfolgende Leerzeichen) bleibt in ``strasse``.
    """
    candidates = Customer.query.filter(
        Customer.active.is_(True),
        Customer.strasse.isnot(None),
        Customer.strasse != "",
        (Customer.hausnummer.is_(None)) | (Customer.hausnummer == ""),
    ).all()

    changed = 0
    skipped = []
    for customer in candidates:
        m = re.search(r"\d", customer.strasse)
        if m:
            pos = m.start()
            hausnummer = customer.strasse[pos:].strip()
            if len(hausnummer) > 20:
                skipped.append(f'{customer.name} – „{customer.strasse}"')
                continue
            customer.hausnummer = hausnummer
            customer.strasse = customer.strasse[:pos].strip()
            changed += 1

    if changed:
        db.session.commit()
    if changed:
        flash(f"Hausnummern korrigiert: {changed} Kontakt{'e' if changed != 1 else ''} aktualisiert.", "success")
    if skipped:
        skipped_list = "; ".join(skipped)
        flash(
            f"{len(skipped)} Kontakt{'e' if len(skipped) != 1 else ''} übersprungen "
            f"(Hausnummer-Teil zu lang, bitte manuell korrigieren): {skipped_list}",
            "warning",
        )
    if not changed and not skipped:
        flash("Keine Kontakte gefunden, bei denen eine Hausnummer in der Straße stand.", "info")

    return redirect(url_for("customers.index"))


@bp.route("/check-duplicates")
@login_required
def check_duplicates():
    """HTMX-Endpoint: liefert ein HTML-Fragment mit ähnlichen Kunden
    für die Live-Anzeige im Quick-Create-Modal oder Formular."""
    name = request.args.get("name", "").strip()
    strasse = request.args.get("strasse", "").strip()
    plz = request.args.get("plz", "").strip()
    ort = request.args.get("ort", "").strip()
    exclude_id = request.args.get("exclude_id", type=int)

    if not name:
        return ""

    similar = find_similar_customers(
        name=name,
        strasse=strasse,
        plz=plz,
        ort=ort,
        exclude_id=exclude_id,
    )
    return render_template("customers/_similar_customers.html", similar=similar)


@bp.route("/quick-create", methods=["POST"])
@login_required
def quick_create():
    """Quick-Create-Endpoint für das Kontakt-Anlage-Modal.

    Wird vom Rechnungs- und Buchungs-Anlageform aus aufgerufen. Liefert JSON
    ``{ok, id, name, label, is_customer, is_supplier}`` zurück, damit das
    aufrufende TomSelect den neuen Kontakt sofort übernehmen kann.
    """
    missing = _missing_required_fields(request.form)
    if missing:
        return jsonify({
            "ok": False,
            "error": "missing_fields",
            "missing": missing,
        }), 400

    is_customer = request.form.get("is_customer") == "1"
    is_supplier = request.form.get("is_supplier") == "1"
    if not (is_customer or is_supplier):
        return jsonify({
            "ok": False,
            "error": "type_required",
        }), 400

    force = request.form.get("force") == "1"
    if not force:
        similar = find_similar_customers(
            name=request.form.get("name", ""),
            strasse=request.form.get("strasse", ""),
            plz=request.form.get("plz", ""),
            ort=request.form.get("ort", ""),
        )
        if similar:
            return jsonify({
                "ok": False,
                "error": "duplicates_found",
                "candidates": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "address": c.address_display(),
                        "active": bool(c.active),
                        "score": round(score, 2),
                    }
                    for c, score in similar
                ],
            }), 409

    c = Customer(
        name=request.form.get("name", "").strip(),
        is_customer=is_customer,
        is_supplier=is_supplier,
        strasse=request.form.get("strasse", "").strip(),
        hausnummer=request.form.get("hausnummer", "").strip(),
        plz=request.form.get("plz", "").strip(),
        ort=request.form.get("ort", "").strip(),
        land=request.form.get("land", "Österreich").strip() or "Österreich",
        email=request.form.get("email", "").strip(),
        active=True,
    )
    if is_customer:
        c.customer_number = next_customer_number()
    db.session.add(c)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({
            "ok": False,
            "error": "number_conflict",
        }), 409

    return jsonify({
        "ok": True,
        "id": c.id,
        "name": c.name,
        "label": f"{c.name} – {c.address_display()}" if c.address_display() else c.name,
        "is_customer": bool(c.is_customer),
        "is_supplier": bool(c.is_supplier),
    })


def _apply_customer_sort(query, sort: str, direction: str):
    """Haengt die ORDER-BY-Klausel passend zum gewaehlten Spalten-Sort an.

    Sekundaer immer nach Name, damit gleiche Werte stabil sortiert sind.
    NULL-Werte (z.B. fehlende Kundennummer bei reinen Lieferanten oder leere
    E-Mails) wandern in beiden Richtungen ans Ende — portabel ueber SQLite,
    MySQL/MariaDB und Postgres via "is null"-CASE-Sortier-Praefix (ANSI
    ``NULLS LAST`` wird von MySQL nicht unterstuetzt).
    """
    desc = direction == "desc"

    def order(col):
        return [
            sa_case((col.is_(None), 1), else_=0).asc(),
            col.desc() if desc else col.asc(),
        ]

    if sort == "nr":
        return query.order_by(*order(Customer.customer_number), Customer.name.asc())
    if sort == "type":
        # asc: Kunden zuerst (inkl. Doppelrolle), dann reine Lieferanten.
        # desc: reine Lieferanten zuerst.
        if desc:
            return query.order_by(
                Customer.is_supplier.desc(),
                Customer.is_customer.desc(),
                Customer.name.asc(),
            )
        return query.order_by(
            Customer.is_customer.desc(),
            Customer.is_supplier.desc(),
            Customer.name.asc(),
        )
    if sort == "address":
        return query.order_by(
            *order(Customer.ort),
            *order(Customer.strasse),
            *order(Customer.hausnummer),
            Customer.name.asc(),
        )
    if sort == "object":
        # Pro Kunde nur eine Repraesentations-Objektnummer fuer den Sort
        # (kleinste object_number aller aktiven Eigentuemer-Verhaeltnisse).
        # LEFT JOIN, damit Kunden ohne Objekt (z.B. Lieferanten) nicht
        # rausfallen — sie landen via NULLS-LAST-CASE am Ende.
        sub = (
            db.session.query(
                PropertyOwnership.customer_id.label("cid"),
                sa_func.min(Property.object_number).label("obj_nr"),
            )
            .join(Property, Property.id == PropertyOwnership.property_id)
            .filter(PropertyOwnership.valid_to.is_(None))
            .group_by(PropertyOwnership.customer_id)
            .subquery()
        )
        return (
            query.outerjoin(sub, sub.c.cid == Customer.id)
            .order_by(*order(sub.c.obj_nr), Customer.name.asc())
        )
    if sort == "email":
        return query.order_by(*order(Customer.email), Customer.name.asc())
    # Default und sort == "name"
    return query.order_by(Customer.name.desc() if desc else Customer.name.asc())


def _missing_required_fields(form) -> list[str]:
    """Gibt die Labels der leeren Pflichtfelder zurück."""
    missing = []
    for field, label in REQUIRED_ADDRESS_FIELDS:
        if not form.get(field, "").strip():
            missing.append(label)
    return missing


def _validate_customer_form(form) -> str | None:
    """Pflichtfeld- + Kontakttyp-Validierung. Gibt eine deutsche Fehlermeldung
    zurueck oder None, wenn alles passt."""
    missing = _missing_required_fields(form)
    if missing:
        return "Bitte folgende Pflichtfelder ausfüllen: " + ", ".join(missing)
    if not (form.get("is_customer") == "1" or form.get("is_supplier") == "1"):
        return "Bitte mindestens einen Kontakttyp wählen (Kunde oder Lieferant)."
    return None


def _render_new_form(form_data, suggested_nr=None):
    return render_template(
        "customers/form.html",
        customer=None,
        form_data=form_data,
        suggested_nr=suggested_nr,
    )


def _apply_customer_fields(customer, form, *, is_new: bool) -> str | None:
    """Setzt alle Felder am Customer aus dem Formular.

    Gibt eine Fehlermeldung als String zurueck, wenn die manuell vergebene
    Kundennummer bereits anderweitig vergeben ist; sonst None.
    """
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    customer.name = form.get("name", "").strip()
    customer.is_customer = form.get("is_customer") == "1"
    customer.is_supplier = form.get("is_supplier") == "1"
    customer.strasse = form.get("strasse", "").strip()
    customer.hausnummer = form.get("hausnummer", "").strip()
    customer.plz = form.get("plz", "").strip()
    customer.ort = form.get("ort", "").strip()
    customer.land = form.get("land", "Österreich").strip() or "Österreich"
    customer.email = form.get("email", "").strip()
    customer.rechnung_per_email = form.get("rechnung_per_email") == "1"
    customer.phone = form.get("phone", "").strip()
    customer.notes = form.get("notes", "").strip()
    if is_new:
        customer.active = True
    # member_since nur anfassen, wenn das Feld gesendet wurde — im Versorger-
    # Modus ist der Mitgliedschafts-Block (inkl. member_since) ausgeblendet,
    # ein blindes Ueberschreiben wuerde den Wert loeschen (Kundenauswertung
    # nutzt member_since).
    if "member_since" in form:
        ms = form.get("member_since", "").strip()
        customer.member_since = datetime.strptime(ms, "%Y-%m-%d").date() if ms else None
    customer.externe_kennung = form.get("externe_kennung", "").strip() or None

    raw_base = form.get("base_fee_override", "").strip().replace(",", ".")
    try:
        customer.base_fee_override = Decimal(raw_base) if raw_base else None
    except InvalidOperation:
        return f"Ungültiger Wert für Grundgebühr: {raw_base}"
    raw_add = form.get("additional_fee_override", "").strip().replace(",", ".")
    try:
        customer.additional_fee_override = Decimal(raw_add) if raw_add else None
    except InvalidOperation:
        return f"Ungültiger Wert für Zusatzgebühr: {raw_add}"

    # Kundennummer ist optional und unique. Auch reine Lieferanten duerfen
    # eine Nummer haben — nur die Auto-Vergabe bei leerem Feld bleibt Kunden
    # vorbehalten, damit Lieferanten nicht ungewollt Counter-Werte ziehen.
    raw_nr = form.get("customer_number", "").strip()
    if raw_nr:
        try:
            requested = int(raw_nr)
        except ValueError:
            return f"Kundennummer muss eine Zahl sein: {raw_nr}"
        if requested < 1:
            return "Kundennummer muss positiv sein."
        # Konflikt-Check (eigene id ausschliessen)
        existing_q = Customer.query.filter(Customer.customer_number == requested)
        if customer.id is not None:
            existing_q = existing_q.filter(Customer.id != customer.id)
        if db.session.query(existing_q.exists()).scalar():
            return f"Kundennummer {requested} ist bereits vergeben."
        customer.customer_number = requested
        bump_customer_counter_to(requested)
    elif is_new:
        # Feld leer bei Neuanlage: nur Kunden ziehen einen Counter-Wert,
        # reine Lieferanten bleiben ohne Nummer (None).
        if customer.is_customer:
            customer.customer_number = next_customer_number()
    else:
        # Feld leer beim Bearbeiten: Nummer explizit entfernen. Gebraucht,
        # wenn ein faelschlich als Kunde angelegter Datensatz auf reinen
        # Lieferanten umgestellt wird und die Kundennummer wegfallen soll.
        customer.customer_number = None

    # WG-Felder nur im Genossenschafts-Modus anwenden — im Versorger-Modus
    # fehlen sie im Formular, bestehende WG-Daten bleiben unangetastet.
    from app.settings_service import is_wassergenossenschaft
    if is_wassergenossenschaft():
        _apply_wg_fields(customer, form)
    return None


def _apply_wg_fields(customer, form):
    """Setzt Status, Mitglied-bis und Funktionen am Kontakt (WG-Modus).

    Funktions-Regeln sind bewusst nur Warnungen (siehe ``app.wg`` / Frontend) —
    hier wird nichts blockiert, nur synchronisiert.
    """
    from datetime import datetime
    from app.wg import STATUS_LABELS, FUNCTION_LABELS, STATUS_MEMBER
    from app.models import WgFunction

    profile = customer.ensure_wg_profile()
    status = form.get("wg_status", STATUS_MEMBER)
    profile.status = status if status in STATUS_LABELS else STATUS_MEMBER

    mu = form.get("member_until", "").strip()
    profile.member_until = datetime.strptime(mu, "%Y-%m-%d").date() if mu else None

    # Funktionen gegen die Checkbox-Auswahl synchronisieren (nur gueltige Keys);
    # delete-orphan-Cascade raeumt entfernte Eintraege beim Flush ab.
    selected = {f for f in form.getlist("wg_functions") if f in FUNCTION_LABELS}
    existing = {f.function: f for f in customer.wg_functions}
    for key in selected - set(existing):
        customer.wg_functions.append(WgFunction(function=key))
    for key, obj in existing.items():
        if key not in selected:
            customer.wg_functions.remove(obj)


# ---------------------------------------------------------------------------
# CSV / Excel Import — 3-stufiger Wizard
# ---------------------------------------------------------------------------
#
# Schritt 1: /customers/import          (GET = Formular, POST = Datei hochladen)
# Schritt 2: /customers/import/preview  (GET = Vorschau; POST action=refresh od. confirm)
# Schritt 3: /customers/import/result   (GET = Ergebnis)
#
# Persistenz: DataFrame als Pickle in instance/; Session haelt nur den Pfad
# und die Config.  Die Vorschau-Zeilen werden pro Request neu aufgebaut.

_CI_FILE_KEY = "customer_import_file"
_CI_CFG_KEY = "customer_import_cfg"
_CI_RESULT_KEY = "customer_import_result"


def _abort_to_import_upload(reason: str = "Sitzung abgelaufen — bitte erneut hochladen.",
                            category: str = "warning"):
    flash(reason, category)
    path = session.pop(_CI_FILE_KEY, None)
    if path:
        import_common.delete_dataframe(path)
    session.pop(_CI_CFG_KEY, None)
    return redirect(url_for("customers.import_upload"))


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_upload():
    """Schritt 1: Datei hochladen + Duplikat-Modus wählen."""
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Bitte eine Datei auswählen.", "warning")
            return redirect(url_for("customers.import_upload"))

        try:
            df = import_common.read_table(f)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("customers.import_upload"))

        # Alten Pickle bereinigen
        old_path = session.pop(_CI_FILE_KEY, None)
        if old_path:
            import_common.delete_dataframe(old_path)

        path = import_common.save_dataframe(df, prefix="customer_import_")
        session[_CI_FILE_KEY] = path

        cfg = import_service.CustomerImportConfig.from_form(request.form)
        session[_CI_CFG_KEY] = cfg.to_dict()

        return redirect(url_for("customers.import_preview"))

    return render_template("customers/import.html")


@bp.route("/import/preview", methods=["GET", "POST"])
@login_required
def import_preview():
    """Schritt 2: Spalten zuordnen, Vorschau prüfen, Import ausführen."""
    path = session.get(_CI_FILE_KEY)
    df = import_common.load_dataframe(path) if path else None
    if df is None:
        return _abort_to_import_upload()

    columns = list(df.columns)
    from app.settings_service import is_wassergenossenschaft
    is_wg = is_wassergenossenschaft()

    if request.method == "POST":
        # Config immer aus dem Form übernehmen (beide Aktionen: refresh + confirm)
        cfg = import_service.CustomerImportConfig.from_form(request.form)
        session[_CI_CFG_KEY] = cfg.to_dict()

        if request.form.get("action") == "confirm":
            baseline = import_service.build_preview_rows(df, cfg, is_wg=is_wg)
            merged = import_service.apply_edits(request.form, baseline)
            stats = import_service.commit(merged, cfg, is_wg=is_wg)

            # Aufräumen
            path_to_delete = session.pop(_CI_FILE_KEY, None)
            if path_to_delete:
                import_common.delete_dataframe(path_to_delete)
            session.pop(_CI_CFG_KEY, None)
            session[_CI_RESULT_KEY] = stats.to_dict()

            total = stats.created + stats.updated
            category = "success" if total > 0 and not stats.errors else "warning"
            flash(
                f"Import abgeschlossen: {stats.created} angelegt, "
                f"{stats.updated} aktualisiert, {stats.skipped} übersprungen.",
                category,
            )
            return redirect(url_for("customers.import_result"))

        # action=refresh: Vorschau neu rendern (durch Fall-Through zu GET-Pfad)
    else:
        cfg = import_service.CustomerImportConfig.from_dict(session.get(_CI_CFG_KEY))
        # Auto-suggest leere Felder beim ersten Aufruf
        suggested = import_service.suggest_config(columns)
        if not cfg.col_customer_number:
            cfg.col_customer_number = suggested.col_customer_number
        if not cfg.col_externe_kennung:
            cfg.col_externe_kennung = suggested.col_externe_kennung
        if not cfg.col_name:
            cfg.col_name = suggested.col_name
        if not cfg.col_name_last:
            cfg.col_name_last = suggested.col_name_last
        if not cfg.col_name_first:
            cfg.col_name_first = suggested.col_name_first
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
        if not cfg.col_email:
            cfg.col_email = suggested.col_email
        if not cfg.col_phone:
            cfg.col_phone = suggested.col_phone
        if not cfg.col_notes:
            cfg.col_notes = suggested.col_notes
        if is_wg:
            if not cfg.col_wg_status:
                cfg.col_wg_status = suggested.col_wg_status
            if not cfg.col_member_since:
                cfg.col_member_since = suggested.col_member_since
            if not cfg.col_member_until:
                cfg.col_member_until = suggested.col_member_until

    rows = import_service.build_preview_rows(df, cfg, is_wg=is_wg)

    counts = {
        "count_new": sum(1 for r in rows if r.status == import_common.ROW_NEW),
        "count_update": sum(1 for r in rows if r.status == import_common.ROW_UPDATE),
        "count_exists": sum(1 for r in rows if r.status == import_common.ROW_EXISTS),
        "count_error": sum(1 for r in rows if r.status == import_common.ROW_ERROR),
    }

    return render_template(
        "customers/import_preview.html",
        cfg=cfg,
        columns=columns,
        rows=rows,
        counts=counts,
        status_labels=STATUS_LABELS,
    )


@bp.route("/import/result")
@login_required
def import_result():
    """Schritt 3: Ergebnis anzeigen."""
    stats = session.pop(_CI_RESULT_KEY, None)
    if stats is None:
        return redirect(url_for("customers.index"))
    return render_template("customers/import_result.html", stats=stats)
