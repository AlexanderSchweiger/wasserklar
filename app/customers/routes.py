from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required
from sqlalchemy import func

from app.customers import bp
from app.customers.duplicate_check import find_similar_customers
from app.extensions import db
from app.models import Customer, PropertyOwnership


# Pflichtfelder auf Formular-Ebene (Schema bleibt NULLable, Bestandsdaten mit
# Leerstring bleiben erhalten — siehe ADR-001).
REQUIRED_ADDRESS_FIELDS = [
    ("name", "Name"),
    ("strasse", "Straße"),
    ("hausnummer", "Hausnummer"),
    ("plz", "PLZ"),
    ("ort", "Ort"),
]


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
        # 1) Pflichtfeld-Validierung
        missing = _missing_required_fields(request.form)
        if missing:
            flash(
                "Bitte folgende Pflichtfelder ausfüllen: " + ", ".join(missing),
                "danger",
            )
            return render_template(
                "customers/form.html",
                customer=None,
                form_data=request.form,
            )

        # 2) Dubletten-Gate (außer der Nutzer hat "trotzdem anlegen" bestätigt)
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

        # 3) Anlage
        c = _customer_from_form(Customer())
        max_nr = db.session.query(func.max(Customer.customer_number)).scalar() or 0
        c.customer_number = max_nr + 1
        db.session.add(c)
        db.session.commit()
        flash(f"Kunde '{c.name}' angelegt.", "success")
        return redirect(url_for("customers.index"))
    return render_template("customers/form.html", customer=None, form_data=None)


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
        _customer_from_form(customer)
        db.session.commit()
        flash("Kunde aktualisiert.", "success")
        return redirect(url_for("customers.detail", customer_id=customer.id))
    return render_template("customers/form.html", customer=customer, form_data=None)


@bp.route("/<int:customer_id>/deactivate", methods=["POST"])
@login_required
def deactivate(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    customer.active = False
    db.session.commit()
    flash(f"Kunde '{customer.name}' archiviert.", "info")
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
    """Quick-Create-Endpoint für das Rechnungs-Anlage-Modal.

    Legt einen Kunden mit den Pflichtfeldern an und liefert JSON
    ``{id, name, label}`` zurück, damit das TomSelect im Rechnungsformular
    den neuen Kunden sofort übernehmen kann. Dubletten-Prüfung wird
    durchgeführt, sofern ``force=1`` nicht gesetzt ist.
    """
    missing = _missing_required_fields(request.form)
    if missing:
        return jsonify({
            "ok": False,
            "error": "missing_fields",
            "missing": missing,
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
        strasse=request.form.get("strasse", "").strip(),
        hausnummer=request.form.get("hausnummer", "").strip(),
        plz=request.form.get("plz", "").strip(),
        ort=request.form.get("ort", "").strip(),
        land=request.form.get("land", "Österreich").strip() or "Österreich",
        email=request.form.get("email", "").strip(),
        active=True,
    )
    max_nr = db.session.query(func.max(Customer.customer_number)).scalar() or 0
    c.customer_number = max_nr + 1
    db.session.add(c)
    db.session.commit()

    return jsonify({
        "ok": True,
        "id": c.id,
        "name": c.name,
        "label": f"{c.name} – {c.address_display()}",
    })


def _missing_required_fields(form) -> list[str]:
    """Gibt die Labels der leeren Pflichtfelder zurück."""
    missing = []
    for field, label in REQUIRED_ADDRESS_FIELDS:
        if not form.get(field, "").strip():
            missing.append(label)
    return missing


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
