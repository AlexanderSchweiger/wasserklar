from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required
from sqlalchemy.exc import IntegrityError

from app.customers import bp
from app.customers.duplicate_check import find_similar_customers
from app.extensions import db
from app.models import Customer, PropertyOwnership
from app.pagination import paginate_query
from app.utils import bump_customer_counter_to, next_customer_number


# Pflichtfelder auf Formular-Ebene. Adressfelder sind seit dem
# Lieferanten-Rollout bewusst alle optional — der Name reicht (z.B. Behoerden,
# Online-Dienste, ad-hoc-Lieferanten ohne vollstaendige Adresse).
REQUIRED_ADDRESS_FIELDS = [
    ("name", "Name"),
]

# Erlaubte Werte des type-Filters in der Liste.
_TYPE_FILTERS = {"customer", "supplier", "all"}


@bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "all")
    if type_filter not in _TYPE_FILTERS:
        type_filter = "all"

    query = Customer.query.filter_by(active=True).order_by(Customer.name)
    if type_filter == "customer":
        query = query.filter(Customer.is_customer.is_(True))
    elif type_filter == "supplier":
        query = query.filter(Customer.is_supplier.is_(True))
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%"))

    pagination = paginate_query(query, page_key="customers")
    customers = pagination.items

    # Property-Map fuer Anzeige aufbauen — nur fuer die aktuell sichtbaren Kunden.
    if customers:
        ownerships = PropertyOwnership.query.filter(
            PropertyOwnership.valid_to.is_(None),
            PropertyOwnership.customer_id.in_([c.id for c in customers])
        ).all()
    else:
        ownerships = []
    property_map = {o.customer_id: o.property for o in ownerships}

    ctx = dict(
        customers=customers,
        property_map=property_map,
        pagination=pagination,
        type_filter=type_filter,
        q=q,
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
    if request.method == "POST":
        missing = _missing_required_fields(request.form)
        if missing:
            flash(
                "Bitte folgende Pflichtfelder ausfüllen: " + ", ".join(missing),
                "danger",
            )
            return render_template(
                "customers/form.html",
                customer=customer,
                form_data=request.form,
            )
        is_customer = request.form.get("is_customer") == "1"
        is_supplier = request.form.get("is_supplier") == "1"
        if not (is_customer or is_supplier):
            flash("Bitte mindestens einen Kontakttyp wählen (Kunde oder Lieferant).", "danger")
            return render_template(
                "customers/form.html",
                customer=customer,
                form_data=request.form,
            )
        nr_error = _apply_customer_fields(customer, request.form, is_new=False)
        if nr_error:
            flash(nr_error, "danger")
            return render_template(
                "customers/form.html",
                customer=customer,
                form_data=request.form,
            )
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                "Die Kundennummer wurde gerade von einem anderen Vorgang vergeben. "
                "Bitte erneut speichern.",
                "danger",
            )
            return render_template(
                "customers/form.html",
                customer=customer,
                form_data=request.form,
            )
        flash("Kontakt aktualisiert.", "success")
        return redirect(url_for("customers.detail", customer_id=customer.id))
    return render_template("customers/form.html", customer=customer, form_data=None)


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


def _missing_required_fields(form) -> list[str]:
    """Gibt die Labels der leeren Pflichtfelder zurück."""
    missing = []
    for field, label in REQUIRED_ADDRESS_FIELDS:
        if not form.get(field, "").strip():
            missing.append(label)
    return missing


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
    ms = form.get("member_since", "")
    if ms:
        customer.member_since = datetime.strptime(ms, "%Y-%m-%d").date()
    else:
        customer.member_since = None
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

    # Kundennummer: nur fuer Kunden vergeben.
    if not customer.is_customer:
        customer.customer_number = None
        return None

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
    else:
        # Feld leer: bei Neuanlage Counter ziehen, beim Bearbeiten bestehende behalten.
        if is_new or customer.customer_number is None:
            customer.customer_number = next_customer_number()
    return None
