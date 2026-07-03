from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (
    render_template, redirect, url_for, flash, request, jsonify, abort,
)
from flask_login import login_required, current_user

from app.meter_tours import bp
from app.meter_tours import services as svc
from app.auth.permissions import permission_required, PERM_RECHNUNGEN
from app.extensions import db
from app.models import (
    AppSetting, Customer, MeterTour, MeterTourStop, PropertyOwnership, TaxRate,
)


def _parse_float(raw):
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _get_tour(tour_id):
    return db.get_or_404(MeterTour, tour_id)


def _get_stop(tour, stop_id):
    stop = db.session.get(MeterTourStop, stop_id)
    if stop is None or stop.tour_id != tour.id:
        abort(404)
    return stop


def _owner_payload(customers):
    return [{
        "name": c.letter_name,
        "phone": c.phone,
        "email": c.email,
        "wants_email": c.wants_email,
    } for c in customers]


def _stop_payload(tour, stop, owners_map):
    """Serialisierung eines Stops fuer stops.json / das eingebettete
    tour-data-JSON der Kartenseite. Koordinaten IMMER live vom Objekt."""
    prop = stop.property
    lat = prop.lat if prop else None
    lng = prop.lng if prop else None
    gmaps_url = (f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"
                 if lat is not None and lng is not None else None)
    return {
        "id": stop.id,
        "position": stop.position,
        "status": stop.status,
        "meter_id": stop.meter_id,
        "meter_number": stop.meter.meter_number if stop.meter else None,
        "property_label": prop.label() if prop else "?",
        "address": prop.address_display() if prop else "",
        "lat": lat,
        "lng": lng,
        "owners": _owner_payload(owners_map.get(stop.property_id, [])),
        "skip_reason": stop.skip_reason,
        "notified": stop.notified_at is not None,
        "has_invoice": stop.invoice_id is not None,
        "replace_url": url_for("meters.meter_replace", meter_id=stop.meter_id),
        "complete_url": url_for("meter_tours.stop_complete",
                                tour_id=tour.id, stop_id=stop.id),
        "status_url": url_for("meter_tours.stop_status",
                              tour_id=tour.id, stop_id=stop.id),
        "move_url": url_for("meter_tours.stop_move",
                            tour_id=tour.id, stop_id=stop.id),
        "invoice_url": url_for("meter_tours.stop_invoice",
                               tour_id=tour.id, stop_id=stop.id),
        "gmaps_url": gmaps_url,
    }


def _tour_payload(tour):
    owners_map = svc.owners_by_property({s.property_id for s in tour.stops})
    return {
        "id": tour.id,
        "name": tour.name,
        "status": tour.status,
        "start_lat": tour.start_lat,
        "start_lng": tour.start_lng,
        "start_address": tour.start_address,
        "reorder_url": url_for("meter_tours.reorder", tour_id=tour.id),
        "stops_url": url_for("meter_tours.stops_json", tour_id=tour.id),
        "can_invoice": current_user.has_permission(PERM_RECHNUNGEN),
        "stops": [_stop_payload(tour, s, owners_map)
                  for s in sorted(tour.stops, key=lambda s: s.position)],
    }


# ---------------------------------------------------------------------------
# Faellige Zaehler + Tour-Anlage
# ---------------------------------------------------------------------------

@bp.route("/due")
@login_required
def due():
    q = request.args.get("q", "").strip()
    current_year = date.today().year
    try:
        year = int(request.args.get("year", current_year))
    except ValueError:
        year = current_year
    show_toured = request.args.get("show_toured") == "1"

    rows = svc.due_meters(due_until_year=year, q=q, include_toured=show_toured)
    ctx = {
        "rows": rows,
        "q": q,
        "year": year,
        "current_year": current_year,
        "show_toured": show_toured,
        "interval": svc.calibration_interval_years(),
        "open_tours": MeterTour.query.filter(
            MeterTour.status.in_([MeterTour.STATUS_PLANNED,
                                  MeterTour.STATUS_ACTIVE]))
            .order_by(MeterTour.created_at.desc()).all(),
    }
    if request.headers.get("HX-Request"):
        return render_template("meter_tours/_due_table.html", **ctx)
    return render_template("meter_tours/due.html", **ctx)


@bp.route("/settings/interval", methods=["POST"])
@login_required
def set_interval():
    """Nacheichfrist-Intervall (Jahre) inline auf der Faelligen-Seite pflegen."""
    try:
        value = int(request.form.get("interval", ""))
    except ValueError:
        value = 0
    if value <= 0 or value > 30:
        flash("Ungültiges Eich-Intervall.", "danger")
    else:
        AppSetting.set(svc.SETTING_INTERVAL, str(value))
        db.session.commit()
        flash(f"Nacheichfrist auf {value} Jahre gesetzt.", "success")
    return redirect(url_for("meter_tours.due"))


@bp.route("/")
@login_required
def index():
    tours = MeterTour.query.order_by(MeterTour.created_at.desc()).all()
    return render_template("meter_tours/index.html", tours=tours)


@bp.route("/", methods=["POST"])
@login_required
def create():
    meter_ids = request.form.getlist("meter_ids")
    planned_raw = request.form.get("planned_date") or ""
    planned_date = None
    if planned_raw:
        try:
            planned_date = date.fromisoformat(planned_raw)
        except ValueError:
            flash("Ungültiges Datum.", "danger")
            return redirect(url_for("meter_tours.due"))
    try:
        tour = svc.create_tour(
            name=request.form.get("name", ""),
            planned_date=planned_date,
            time_window=request.form.get("time_window"),
            start_lat=_parse_float(request.form.get("start_lat")),
            start_lng=_parse_float(request.form.get("start_lng")),
            start_address=request.form.get("start_address"),
            meter_ids=meter_ids,
            created_by_id=current_user.id,
            notes=request.form.get("notes"),
        )
    except svc.TourError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("meter_tours.due"))
    db.session.commit()
    flash(f"Tour „{tour.name}“ mit {len(tour.stops)} Stopps angelegt.", "success")
    return redirect(url_for("meter_tours.detail", tour_id=tour.id))


# ---------------------------------------------------------------------------
# Tour-Detail (Karte) + Stop-Daten
# ---------------------------------------------------------------------------

def _tour_qr(tour):
    """(inline-SVG-QR, absolute Karten-URL) fuer den Handy-Einstieg.

    QR steht auf dem gedruckten Zettel und im "Am Handy öffnen"-Dialog.
    ``segno`` lazy wie beim 2FA-QR (app/auth/routes.py) — ohne die Lib bleibt
    der Link als Text nutzbar, nichts bricht.
    """
    url = url_for("meter_tours.detail", tour_id=tour.id, _external=True)
    try:
        import segno
        return segno.make(url).svg_inline(scale=3), url
    except Exception:
        return None, url


@bp.route("/<int:tour_id>")
@login_required
def detail(tour_id):
    tour = _get_tour(tour_id)
    if svc.sync_tour_completions(tour):
        db.session.commit()
    qr_svg, tour_url = _tour_qr(tour)
    return render_template(
        "meter_tours/detail.html", tour=tour, tour_data=_tour_payload(tour),
        qr_svg=qr_svg, tour_url=tour_url)


@bp.route("/<int:tour_id>/stops.json")
@login_required
def stops_json(tour_id):
    tour = _get_tour(tour_id)
    if svc.sync_tour_completions(tour):
        db.session.commit()
    return jsonify(_tour_payload(tour))


@bp.route("/<int:tour_id>/reorder", methods=["POST"])
@login_required
def reorder(tour_id):
    tour = _get_tour(tour_id)
    if tour.status not in (MeterTour.STATUS_PLANNED, MeterTour.STATUS_ACTIVE):
        abort(409)
    svc.reorder_pending_stops(
        tour,
        _parse_float(request.form.get("start_lat")),
        _parse_float(request.form.get("start_lng")),
    )
    db.session.commit()
    return "", 204, {"HX-Trigger": "tourChanged"}


# ---------------------------------------------------------------------------
# Tour-Statuswechsel
# ---------------------------------------------------------------------------

def _tour_transition(tour, new_status, allowed_from):
    if tour.status not in allowed_from:
        flash("Dieser Statuswechsel ist nicht möglich.", "danger")
        return False
    tour.status = new_status
    return True


@bp.route("/<int:tour_id>/start", methods=["POST"])
@login_required
def start(tour_id):
    tour = _get_tour(tour_id)
    if _tour_transition(tour, MeterTour.STATUS_ACTIVE,
                        (MeterTour.STATUS_PLANNED,)):
        db.session.commit()
        flash("Tour gestartet.", "success")
    return redirect(url_for("meter_tours.detail", tour_id=tour.id))


@bp.route("/<int:tour_id>/close", methods=["POST"])
@login_required
def close(tour_id):
    tour = _get_tour(tour_id)
    if _tour_transition(tour, MeterTour.STATUS_DONE,
                        (MeterTour.STATUS_PLANNED, MeterTour.STATUS_ACTIVE)):
        tour.closed_at = datetime.utcnow()
        db.session.commit()
        _done, pending, _skipped = tour.stop_counts()
        if pending:
            flash(f"Tour abgeschlossen. {pending} offene Stopps erscheinen "
                  "wieder in der Liste der fälligen Zähler.", "info")
        else:
            flash("Tour abgeschlossen.", "success")
    return redirect(url_for("meter_tours.index"))


@bp.route("/<int:tour_id>/cancel", methods=["POST"])
@login_required
def cancel(tour_id):
    tour = _get_tour(tour_id)
    if _tour_transition(tour, MeterTour.STATUS_CANCELLED,
                        (MeterTour.STATUS_PLANNED, MeterTour.STATUS_ACTIVE)):
        tour.closed_at = datetime.utcnow()
        db.session.commit()
        flash("Tour abgebrochen.", "info")
    return redirect(url_for("meter_tours.index"))


@bp.route("/<int:tour_id>/delete", methods=["POST"])
@login_required
def delete(tour_id):
    tour = _get_tour(tour_id)
    if tour.status not in (MeterTour.STATUS_PLANNED, MeterTour.STATUS_CANCELLED):
        flash("Nur geplante oder abgebrochene Touren können gelöscht werden "
              "(abgeschlossene bleiben als Nachweis).", "danger")
        return redirect(url_for("meter_tours.index"))
    db.session.delete(tour)
    db.session.commit()
    flash("Tour gelöscht.", "success")
    return redirect(url_for("meter_tours.index"))


# ---------------------------------------------------------------------------
# Stop-Status + Abschluss
# ---------------------------------------------------------------------------

_ALLOWED_STOP_STATUSES = {
    MeterTourStop.STATUS_PENDING,
    MeterTourStop.STATUS_SKIPPED,
    MeterTourStop.STATUS_NOT_HOME,
}


@bp.route("/<int:tour_id>/stops/<int:stop_id>/status", methods=["POST"])
@login_required
def stop_status(tour_id, stop_id):
    tour = _get_tour(tour_id)
    stop = _get_stop(tour, stop_id)
    new_status = request.form.get("status", "")
    if new_status not in _ALLOWED_STOP_STATUSES:
        abort(400)
    if stop.status == MeterTourStop.STATUS_DONE:
        # Erledigt bleibt erledigt — der Tausch ist dokumentiert.
        abort(409)
    stop.status = new_status
    stop.skip_reason = (request.form.get("skip_reason") or "").strip() or None
    stop.completed_at = (datetime.utcnow()
                         if new_status != MeterTourStop.STATUS_PENDING else None)
    db.session.commit()
    if request.headers.get("HX-Request"):
        owners_map = svc.owners_by_property({stop.property_id})
        return render_template(
            "meter_tours/_stop_card.html", tour=tour, stop=stop,
            stop_data=_stop_payload(tour, stop, owners_map),
            can_invoice=current_user.has_permission(PERM_RECHNUNGEN))
    return jsonify({"ok": True, "status": stop.status})


@bp.route("/<int:tour_id>/stops/<int:stop_id>/move", methods=["POST"])
@login_required
def stop_move(tour_id, stop_id):
    """Manuelles Umsortieren (Pfeil hoch/runter in der Stoppliste)."""
    tour = _get_tour(tour_id)
    stop = _get_stop(tour, stop_id)
    direction = request.form.get("direction", "")
    if direction not in ("up", "down"):
        abort(400)
    moved = svc.move_stop(tour, stop, direction)
    if moved:
        db.session.commit()
    return jsonify({"ok": moved})


@bp.route("/<int:tour_id>/stops/<int:stop_id>/complete", methods=["POST"])
@login_required
def stop_complete(tour_id, stop_id):
    """Nach einem Zaehlertausch (meterReplaced-Event) vom Seiten-JS gerufen.

    Der Server leitet alles aus dem unique ``MeterReplacement.old_meter_id``
    ab — ein manipuliertes Event kann keinen fremden Tausch verknuepfen."""
    tour = _get_tour(tour_id)
    stop = _get_stop(tour, stop_id)
    done = svc.complete_stop_from_replacement(stop)
    if done:
        db.session.commit()
    return jsonify({
        "ok": done,
        "status": stop.status,
        "invoice_offer": done and stop.invoice_id is None
        and current_user.has_permission(PERM_RECHNUNGEN),
    })


# ---------------------------------------------------------------------------
# Zettel-Workflow (Nacherfassung in Routen-Reihenfolge)
# ---------------------------------------------------------------------------

@bp.route("/<int:tour_id>/batch")
@login_required
def batch(tour_id):
    tour = _get_tour(tour_id)
    if svc.sync_tour_completions(tour):
        db.session.commit()
    owners_map = svc.owners_by_property({s.property_id for s in tour.stops})
    stops = sorted(tour.stops, key=lambda s: s.position)
    qr_svg, tour_url = _tour_qr(tour)
    return render_template(
        "meter_tours/batch.html", tour=tour, stops=stops,
        qr_svg=qr_svg, tour_url=tour_url,
        stop_payloads={s.id: _stop_payload(tour, s, owners_map) for s in stops},
        owners_map=owners_map,
        # meter_id -> complete-URL fuers onMeterReplaced-JS (Zettel-Workflow).
        complete_urls={str(s.meter_id): {
            "url": url_for("meter_tours.stop_complete",
                           tour_id=tour.id, stop_id=s.id),
            "stop_id": s.id,
        } for s in stops},
        can_invoice=current_user.has_permission(PERM_RECHNUNGEN))


# ---------------------------------------------------------------------------
# Vorab-Info (Ankuendigungsmail)
# ---------------------------------------------------------------------------

def _notify_recipients(tour):
    """Stops je Kunde gruppieren (ein Kunde kann mehrere Objekte in der Tour
    haben; ein Objekt kann mehrere aktuelle Eigentuemer haben)."""
    owners_map = svc.owners_by_property({s.property_id for s in tour.stops})
    grouped = {}
    for stop in sorted(tour.stops, key=lambda s: s.position):
        if stop.status != MeterTourStop.STATUS_PENDING:
            continue
        for customer in owners_map.get(stop.property_id, []):
            entry = grouped.setdefault(customer.id, {
                "customer": customer, "stops": [],
            })
            entry["stops"].append(stop)
    return list(grouped.values())


@bp.route("/<int:tour_id>/notify")
@login_required
def notify(tour_id):
    tour = _get_tour(tour_id)
    defaults = svc.notify_defaults()
    return render_template(
        "meter_tours/notify.html", tour=tour,
        recipients=_notify_recipients(tour),
        subject_template=defaults["subject"],
        body_template=defaults["body"])


@bp.route("/<int:tour_id>/notify/save-template", methods=["POST"])
@login_required
def notify_save_template(tour_id):
    _get_tour(tour_id)
    AppSetting.set(svc.SETTING_NOTIFY_SUBJECT,
                   request.form.get("subject", svc.NOTIFY_SUBJECT_DEFAULT))
    AppSetting.set(svc.SETTING_NOTIFY_BODY,
                   request.form.get("body", svc.NOTIFY_BODY_DEFAULT))
    db.session.commit()
    flash("Vorlage gespeichert.", "success")
    return redirect(url_for("meter_tours.notify", tour_id=tour_id))


@bp.route("/<int:tour_id>/notify/send", methods=["POST"])
@login_required
def notify_send(tour_id):
    """JSON-Endpoint fuer den frontend-getriebenen Serienversand (eine
    Anfrage pro Kunde, Pause = bulk_mail_delay_ms — wie der Rechnungs-
    Massenversand)."""
    tour = _get_tour(tour_id)
    customer = db.session.get(Customer, request.form.get("customer_id", type=int))
    if customer is None:
        return jsonify({"ok": False, "error": "Kunde nicht gefunden"}), 404

    # Nur Stops dieser Tour, deren Objekt dem Kunden aktuell gehoert.
    owned_property_ids = {
        o.property_id for o in PropertyOwnership.query.filter(
            PropertyOwnership.customer_id == customer.id,
            PropertyOwnership.valid_to.is_(None)).all()
    }
    stops = [s for s in tour.stops
             if s.property_id in owned_property_ids
             and s.status == MeterTourStop.STATUS_PENDING]
    if not stops:
        return jsonify({"ok": False, "error": "Keine offenen Stopps für diesen Kunden"}), 400

    # Pflicht-Gate fuer JEDEN Kunden-Mailversand: E-Mail + Einwilligung.
    if not customer.wants_email:
        return jsonify({"ok": False, "error": "E-Mail-Versand nicht aktiviert"}), 400
    from app.email_suppression import suppression_notice
    notice = suppression_notice(customer.email)
    if notice:
        return jsonify({"ok": False, "error": notice}), 400

    subject = svc.render_notify_text(
        request.form.get("subject") or svc.NOTIFY_SUBJECT_DEFAULT,
        customer=customer, stops=stops, tour=tour)
    body = svc.render_notify_text(
        request.form.get("body") or svc.NOTIFY_BODY_DEFAULT,
        customer=customer, stops=stops, tour=tour)
    try:
        svc.send_stop_notification(customer, subject, body, channel="email")
    except Exception as exc:  # pro Empfaenger melden, Serie laeuft weiter
        return jsonify({"ok": False, "error": str(exc)}), 500

    now = datetime.utcnow()
    for stop in stops:
        stop.notified_at = now
    db.session.commit()
    return jsonify({"ok": True, "email": customer.email})


# ---------------------------------------------------------------------------
# Pauschalen-Rechnung zu einem erledigten Stop
# ---------------------------------------------------------------------------

def _fee_defaults():
    return {
        "description": AppSetting.get(svc.SETTING_FEE_DESCRIPTION,
                                      svc.FEE_DESCRIPTION_DEFAULT),
        "amount": AppSetting.get(svc.SETTING_FEE_AMOUNT, ""),
        "tax_rate": AppSetting.get(svc.SETTING_FEE_TAX_RATE,
                                   svc.FEE_TAX_RATE_DEFAULT),
    }


@bp.route("/<int:tour_id>/stops/<int:stop_id>/invoice", methods=["GET", "POST"])
@permission_required(PERM_RECHNUNGEN)
def stop_invoice(tour_id, stop_id):
    tour = _get_tour(tour_id)
    stop = _get_stop(tour, stop_id)
    owners = svc.owners_by_property({stop.property_id}).get(stop.property_id, [])

    if request.method == "GET":
        if stop.invoice_id is not None:
            return render_template(
                "meter_tours/_invoice_modal_body.html", tour=tour, stop=stop,
                owners=owners, defaults=_fee_defaults(), tax_rates=[],
                already_invoiced=True)
        return render_template(
            "meter_tours/_invoice_modal_body.html", tour=tour, stop=stop,
            owners=owners, defaults=_fee_defaults(),
            tax_rates=TaxRate.query.order_by(TaxRate.rate).all(),
            already_invoiced=False)

    # POST
    if stop.invoice_id is not None:
        return jsonify({"ok": False, "error": "Zu diesem Stopp existiert bereits eine Rechnung"}), 409
    if not owners:
        return jsonify({"ok": False, "error": "Kein aktueller Eigentümer für dieses Objekt"}), 400

    customer_id = request.form.get("customer_id", type=int)
    customer = next((c for c in owners if c.id == customer_id), None)
    if customer is None:
        if len(owners) == 1 and not customer_id:
            customer = owners[0]
        else:
            return jsonify({"ok": False, "error": "Ungültiger Rechnungsempfänger"}), 400

    description = (request.form.get("description") or "").strip() \
        or svc.FEE_DESCRIPTION_DEFAULT
    try:
        amount = Decimal((request.form.get("amount") or "").strip()
                         .replace(",", "."))
    except InvalidOperation:
        return jsonify({"ok": False, "error": "Ungültiger Betrag"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "Betrag muss größer 0 sein"}), 400

    tax_rate = None
    tax_raw = (request.form.get("tax_rate") or "").strip()
    if tax_raw:
        try:
            tax_rate = Decimal(tax_raw.replace(",", "."))
        except InvalidOperation:
            return jsonify({"ok": False, "error": "Ungültiger Steuersatz"}), 400

    from app.invoices.services import create_fee_invoice
    meter_note = (f"Zählertausch {stop.meter.meter_number}"
                  if stop.meter else "Zählertausch")
    if stop.replacement is not None and stop.replacement.new_meter is not None:
        meter_note += f" → {stop.replacement.new_meter.meter_number}"
    invoice = create_fee_invoice(
        customer=customer,
        property=stop.property,
        description=description,
        amount=amount,
        tax_rate=tax_rate,
        created_by_id=current_user.id,
        notes=meter_note,
    )
    stop.invoice_id = invoice.id

    if request.form.get("save_default") == "1":
        AppSetting.set(svc.SETTING_FEE_DESCRIPTION, description)
        AppSetting.set(svc.SETTING_FEE_AMOUNT, str(amount))
        AppSetting.set(svc.SETTING_FEE_TAX_RATE,
                       str(tax_rate) if tax_rate is not None else "")

    db.session.commit()
    return jsonify({
        "ok": True,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "wants_email": customer.wants_email,
        "send_ajax_url": url_for("invoices.send_email_ajax",
                                 invoice_id=invoice.id),
        "set_status_url": url_for("invoices.set_status", invoice_id=invoice.id),
        "detail_url": url_for("invoices.detail", invoice_id=invoice.id),
    })
