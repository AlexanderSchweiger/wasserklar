"""Routen der Rundschreiben & Notfall-Kommunikation.

Liste + Neu/Bearbeiten (Modal), Empfänger-Auswahl (Kontaktliste + Karten-
Auswahl über den Netzbereich), Versand per E-Mail (serieller AJAX-Loop wie das
Rechnungs-Massenmailing) und per Post (Sammel-PDF/DOCX). CTAs aus dem
Wasserproben-Alarm, dem Störungsjournal und der Entwarnung befüllen das
Neu-Modal per Deep-Link vor.
"""
import io
from datetime import datetime

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_file, abort, jsonify,
)
from flask_login import login_required, current_user

from app.circulars import bp, constants, services, documents
from app.circulars.send_email_hooks import run_before_send, read_message_id
from app.extensions import db
from app.models import (
    AppSetting, Customer, NetworkFeature,
    Circular, CircularRecipient, CircularDeliveryLog,
    WaterSample, Incident,
)
from app.email_tracking import record_email_sent
from app.settings_service import send_mail

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ── Helfer ───────────────────────────────────────────────────────────────────

def _get_circular(circular_id):
    return db.get_or_404(Circular, circular_id)


def _doc_format():
    fmt = AppSetting.get("invoice.document_format", "pdf")
    return fmt if fmt in ("pdf", "docx", "both") else "pdf"


def _bulk_print_limit():
    return current_app.config.get("BULK_PRINT_MAX", 100)


def _mark_sent(circular):
    if circular.status == Circular.STATUS_DRAFT:
        circular.status = Circular.STATUS_SENT
        circular.sent_at = datetime.utcnow()


def _read_selection(form):
    """(set selected_ids, dict customer_id -> method) aus dem Empfänger-Formular."""
    selected = set(form.getlist("recipient_ids", type=int))
    methods = {}
    for cid in selected:
        m = form.get(f"method_{cid}")
        methods[cid] = m if m in (CircularRecipient.METHOD_EMAIL,
                                  CircularRecipient.METHOD_POST) else CircularRecipient.METHOD_NONE
    return selected, methods


def _transient_recipient(circular, customer):
    """Nicht-persistierte Empfänger-Zeile für den Testversand (Hooks brauchen
    ein Recipient-Objekt; ``id`` bleibt None -> SaaS-Tracking überspringt)."""
    rec = CircularRecipient(circular_id=circular.id, customer_id=customer.id)
    rec.customer = customer
    rec.circular = circular
    return rec


def _boil_water_anlass(sample):
    """Baut den Anlass-Text einer Abkochempfehlung aus den überschrittenen
    Laborwerten einer Wasserprobe."""
    from app.network import water_quality as wq
    feat = db.session.get(NetworkFeature, sample.feature_id) if sample.feature_id else None
    stelle = feat.label() if feat else "unserer Probenahmestelle"
    datum = sample.sample_date.strftime("%d.%m.%Y") if sample.sample_date else ""
    lines = []
    for r in sample.results:
        if r.status != "alarm":
            continue
        label = wq.parameter_label(r.parameter_key)
        unit = (r.unit or wq.parameter_unit(r.parameter_key) or "").strip()
        val = r.display_value()
        limit = r.limit_text or wq.limit_display(r.parameter_key)
        val_txt = f"{val} {unit}".strip()
        lines.append(f"- {label}: {val_txt} (Grenzwert: {limit})")
    head = (f"Bei der Wasserprobe vom {datum} an der Probenahmestelle "
            f"„{stelle}“ wurde eine Grenzwertüberschreitung festgestellt:")
    return head + ("\n" + "\n".join(lines) if lines else "")


def _prefill(args):
    """Vorbefüllung des Neu-Modals aus CTA-Deep-Links (Wasserprobe / Störung /
    Entwarnung). Gibt ein Dict mit kind/subject/body + Bezugs-IDs zurück, oder
    None, wenn nichts vorzubefüllen ist."""
    sample_id = args.get("water_sample_id", type=int)
    incident_id = args.get("incident_id", type=int)
    predecessor_id = args.get("predecessor_id", type=int)

    if sample_id:
        sample = db.session.get(WaterSample, sample_id)
        if sample is not None:
            tpl = constants.TEMPLATES_BY_KEY["boil_water"]
            body = tpl["body"].replace("[Anlass der Abkochempfehlung]",
                                       _boil_water_anlass(sample))
            return {"kind": tpl["kind"], "subject": tpl["subject"], "body": body,
                    "water_sample_id": sample_id}

    if incident_id:
        incident = db.session.get(Incident, incident_id)
        if incident is not None:
            emergency = incident.incident_type in (Incident.TYPE_ROHRBRUCH,
                                                   Incident.TYPE_AUSFALL,
                                                   Incident.TYPE_UNDICHTHEIT,
                                                   Incident.TYPE_DRUCKVERLUST)
            key = "outage" if emergency else "general"
            tpl = constants.TEMPLATES_BY_KEY[key]
            body = tpl["body"]
            if incident.location_description:
                body = body.replace("[Straße / Ortsteil]", incident.location_description)
            return {"kind": tpl["kind"], "subject": tpl["subject"], "body": body,
                    "incident_id": incident_id}

    if predecessor_id:
        pred = db.session.get(Circular, predecessor_id)
        if pred is not None:
            tpl = constants.TEMPLATES_BY_KEY["all_clear"]
            body = tpl["body"]
            if pred.sent_at:
                body = body.replace("[Datum]", pred.sent_at.strftime("%d.%m.%Y"))
            return {"kind": tpl["kind"], "subject": tpl["subject"], "body": body,
                    "predecessor_id": predecessor_id}
    return None


# ── Liste + CRUD ─────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def index():
    status = request.args.get("status") or ""
    kind = request.args.get("kind") or ""

    query = Circular.query
    if status in Circular.STATUSES:
        query = query.filter(Circular.status == status)
    if kind in Circular.KINDS:
        query = query.filter(Circular.kind == kind)
    circulars = query.order_by(Circular.created_at.is_(None),
                               Circular.created_at.desc(), Circular.id.desc()).all()

    ctx = dict(circulars=circulars, status_filter=status, kind_filter=kind)
    if request.headers.get("HX-Request"):
        return render_template("circulars/_table.html", **ctx)

    prefill = _prefill(request.args)
    open_modal = bool(prefill) or request.args.get("new") == "1"
    return render_template("circulars/index.html",
                           templates=constants.BUILTIN_TEMPLATES,
                           kinds=constants.KIND_LABELS,
                           prefill=prefill, open_modal=open_modal, **ctx)


def _apply_form(circular, form):
    kind = form.get("kind")
    circular.kind = kind if kind in Circular.KINDS else Circular.KIND_GENERAL
    circular.subject = (form.get("subject") or "").strip()[:200]
    circular.body = (form.get("body") or "").strip()


@bp.route("/new", methods=["POST"])
@login_required
def new():
    if not (request.form.get("subject") or "").strip():
        flash("Bitte einen Betreff angeben.", "danger")
        return redirect(url_for("circulars.index"))
    if not (request.form.get("body") or "").strip():
        flash("Bitte einen Text angeben.", "danger")
        return redirect(url_for("circulars.index"))

    circular = Circular(status=Circular.STATUS_DRAFT, created_by_id=current_user.id)
    _apply_form(circular, request.form)
    # Bezugs-Datensätze (aus den versteckten CTA-Feldern).
    circular.water_sample_id = request.form.get("water_sample_id", type=int)
    circular.incident_id = request.form.get("incident_id", type=int)
    predecessor_id = request.form.get("predecessor_id", type=int)
    circular.predecessor_id = predecessor_id
    db.session.add(circular)
    db.session.flush()

    # Entwarnung: Empfängerkreis der Abkochempfehlung übernehmen.
    if predecessor_id:
        pred = db.session.get(Circular, predecessor_id)
        if pred is not None:
            for r in pred.recipients:
                db.session.add(CircularRecipient(
                    circular_id=circular.id, customer_id=r.customer_id,
                    delivery_method=r.delivery_method))
    db.session.commit()
    flash("Rundschreiben angelegt — jetzt Empfänger auswählen.", "success")
    return redirect(url_for("circulars.recipients", circular_id=circular.id))


@bp.route("/<int:circular_id>/edit", methods=["POST"])
@login_required
def edit(circular_id):
    circular = _get_circular(circular_id)
    if not circular.can_edit:
        flash("Das Rundschreiben wurde bereits versendet und kann nicht mehr geändert werden.", "danger")
        return redirect(url_for("circulars.detail", circular_id=circular.id))
    if not (request.form.get("subject") or "").strip() or not (request.form.get("body") or "").strip():
        flash("Betreff und Text dürfen nicht leer sein.", "danger")
        return redirect(url_for("circulars.detail", circular_id=circular.id))
    _apply_form(circular, request.form)
    db.session.commit()
    flash("Rundschreiben gespeichert.", "success")
    return redirect(url_for("circulars.detail", circular_id=circular.id))


@bp.route("/<int:circular_id>/delete", methods=["POST"])
@login_required
def delete(circular_id):
    circular = _get_circular(circular_id)
    if not circular.can_edit:
        flash("Versendete Rundschreiben bleiben als Beleg erhalten und können nicht gelöscht werden.", "danger")
        return redirect(url_for("circulars.detail", circular_id=circular.id))
    db.session.delete(circular)
    db.session.commit()
    flash("Rundschreiben gelöscht.", "success")
    return redirect(url_for("circulars.index"))


@bp.route("/<int:circular_id>")
@login_required
def detail(circular_id):
    circular = _get_circular(circular_id)
    # Zustell-Kennzahlen.
    recipients = circular.recipients
    n_email = sum(1 for r in recipients if r.email_sent_at)
    n_post = sum(1 for r in recipients if r.post_sent_at)
    logs = (CircularDeliveryLog.query
            .filter(CircularDeliveryLog.circular_id == circular.id)
            .order_by(CircularDeliveryLog.occurred_at.desc(),
                      CircularDeliveryLog.id.desc()).all())
    # Entwarnung-CTA nur bei versendeter Abkochempfehlung ohne bereits erzeugte Entwarnung.
    has_all_clear = any(s.kind == Circular.KIND_ALL_CLEAR for s in circular.successors)
    return render_template("circulars/detail.html", circular=circular,
                           recipients=recipients, n_email=n_email, n_post=n_post,
                           logs=logs, has_all_clear=has_all_clear)


# ── Empfänger-Auswahl ────────────────────────────────────────────────────────

@bp.route("/<int:circular_id>/recipients", methods=["GET", "POST"])
@login_required
def recipients(circular_id):
    circular = _get_circular(circular_id)
    if request.method == "POST":
        if not circular.can_edit:
            flash("Empfänger können nach dem Versand nicht mehr geändert werden.", "warning")
            return redirect(url_for("circulars.send", circular_id=circular.id))
        selected, methods = _read_selection(request.form)
        services.sync_recipients(circular, selected, methods)
        db.session.commit()
        flash(f"{len(selected)} Empfänger gespeichert.", "success")
        return redirect(url_for("circulars.send", circular_id=circular.id))

    contacts = services.active_contacts()
    existing = {r.customer_id: r for r in circular.recipients}
    if existing:
        preselected = set(existing.keys())
    else:
        preselected = {c.id for c in contacts if c.is_customer}
    rows = []
    for c in contacts:
        rec = existing.get(c.id)
        elig = services.email_eligibility(circular, c)
        method = (rec.delivery_method if rec else services.default_method(circular, c))
        rows.append({
            "customer": c,
            "selected": c.id in preselected,
            "method": method,
            "eligibility": elig,
            "has_address": bool(c.address_display()),
        })
    return render_template("circulars/recipients.html", circular=circular, rows=rows)


@bp.route("/<int:circular_id>/map-select")
@login_required
def map_select(circular_id):
    circular = _get_circular(circular_id)
    center = None
    if circular.incident_id:
        incident = db.session.get(Incident, circular.incident_id)
        if incident and incident.lat is not None and incident.lng is not None:
            center = [incident.lat, incident.lng]
    plans = services.all_plans()
    default_plan = services.active_plan()
    return render_template("circulars/map_select.html", circular=circular, center=center,
                           plans=plans, default_plan=default_plan)


@bp.route("/<int:circular_id>/map-data.json")
@login_required
def map_data(circular_id):
    circular = _get_circular(circular_id)
    plan = services.active_plan()
    targets = services.map_targets(plan)
    center = None
    if circular.incident_id:
        incident = db.session.get(Incident, circular.incident_id)
        if incident and incident.lat is not None and incident.lng is not None:
            center = [incident.lat, incident.lng]
    if center is None and targets:
        center = [sum(t["lat"] for t in targets) / len(targets),
                  sum(t["lng"] for t in targets) / len(targets)]
    # Bereits ausgewählte Liegenschaften sind nicht am Rundschreiben gespeichert
    # (nur Kunden) — daher keine Vorauswahl auf der Karte.
    return jsonify({"center": center, "targets": targets})


@bp.route("/<int:circular_id>/map-lines.json")
@login_required
def map_lines(circular_id):
    """Leitungs-Linien der gewählten Pläne (Hintergrund-Kontext) — standardmäßig
    nur der Standardplan, ``?plan=`` (wiederholbar) blendet weitere Pläne ein."""
    _get_circular(circular_id)
    plan_ids = request.args.getlist("plan", type=int)
    if not plan_ids:
        default_plan = services.active_plan()
        plan_ids = [default_plan.id] if default_plan else []
    return jsonify(services.plan_lines_geojson(plan_ids))


@bp.route("/<int:circular_id>/recipients/from-map", methods=["POST"])
@login_required
def recipients_from_map(circular_id):
    circular = _get_circular(circular_id)
    if not circular.can_edit:
        flash("Empfänger können nach dem Versand nicht mehr geändert werden.", "warning")
        return redirect(url_for("circulars.send", circular_id=circular.id))
    property_ids = request.form.getlist("property_ids", type=int)
    customers = services.resolve_customers_from_properties(property_ids)
    added = services.add_recipients(circular, customers)
    db.session.commit()
    if customers:
        flash(f"{len(customers)} Eigentümer aus {len(property_ids)} Liegenschaft(en) "
              f"übernommen ({added} neu).", "success")
    else:
        flash("Keine Eigentümer im gewählten Bereich gefunden.", "warning")
    return redirect(url_for("circulars.recipients", circular_id=circular.id))


# ── Versand ──────────────────────────────────────────────────────────────────

@bp.route("/<int:circular_id>/send")
@login_required
def send(circular_id):
    circular = _get_circular(circular_id)
    rows = []
    for rec in circular.recipients:
        c = rec.customer
        if c is None:
            continue
        elig = services.email_eligibility(circular, c)
        rows.append({"recipient": rec, "customer": c, "eligibility": elig})
    mail_rows = [r for r in rows
                 if r["recipient"].delivery_method == CircularRecipient.METHOD_EMAIL
                 and r["eligibility"].can_email]
    post_rows = [r for r in rows
                 if r["recipient"].delivery_method == CircularRecipient.METHOD_POST]
    blocked_rows = [r for r in rows
                    if r["recipient"].delivery_method == CircularRecipient.METHOD_EMAIL
                    and not r["eligibility"].can_email]
    return render_template("circulars/send.html", circular=circular,
                           mail_rows=mail_rows, post_rows=post_rows,
                           blocked_rows=blocked_rows)


@bp.route("/<int:circular_id>/send-email-ajax", methods=["POST"])
@login_required
def send_email_ajax(circular_id):
    """Serieller Pro-Empfänger-Mailversand (JSON), analog zum Rechnungs-/
    Sitzungs-Massenmailing. ``test_mode=1`` schickt an die eigene Adresse."""
    from flask_mail import Message

    circular = _get_circular(circular_id)
    test_mode = request.form.get("test_mode") == "1"
    customer = db.session.get(Customer, request.form.get("customer_id", type=int))
    if customer is None:
        return jsonify({"ok": False, "error": "Empfänger nicht gefunden"}), 400

    if test_mode:
        recipient = current_user.email
        if not recipient:
            return jsonify({"ok": False, "error": "Keine eigene E-Mail-Adresse für den Testmodus"}), 400
    else:
        elig = services.email_eligibility(circular, customer)
        if not elig.can_email:
            reason = (elig.notice or ("Keine E-Mail-Adresse hinterlegt" if not customer.email
                                      else "E-Mail-Versand nicht freigegeben"))
            return jsonify({"ok": False, "error": reason}), 400
        recipient = customer.email

    subject = circular.subject
    body = documents.mail_body(circular, customer)
    if test_mode:
        subject = f"[TEST – an: {customer.email or '—'}] {subject}"
        body = f"[TESTMODUS – eigentlicher Empfänger: {customer.email or '—'}]\n\n{body}"

    try:
        msg = Message(subject=subject, recipients=[recipient], body=body)
        if test_mode:
            rec = _transient_recipient(circular, customer)
        else:
            rec = services.upsert_recipient(circular, customer, CircularRecipient.METHOD_EMAIL)
        run_before_send(rec, msg)
        send_mail(msg)

        if test_mode:
            db.session.add(CircularDeliveryLog(
                circular_id=circular.id, customer_id=customer.id,
                recipient_name=customer.name, recipient_email=recipient,
                method=CircularDeliveryLog.METHOD_EMAIL,
                action=CircularDeliveryLog.ACTION_TEST, user_id=current_user.id))
            db.session.commit()
        else:
            was_sent = rec.email_sent_at is not None
            record_email_sent(rec, recipient, read_message_id(msg))
            db.session.add(CircularDeliveryLog(
                circular_id=circular.id, customer_id=customer.id,
                recipient_name=customer.name, recipient_email=recipient,
                method=CircularDeliveryLog.METHOD_EMAIL,
                action=(CircularDeliveryLog.ACTION_RESENT if was_sent
                        else CircularDeliveryLog.ACTION_SENT),
                user_id=current_user.id))
            _mark_sent(circular)
            db.session.commit()

        return jsonify({"ok": True, "name": customer.name, "email": recipient,
                        "test_mode": test_mode})
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("Rundschreiben-Mail (ajax) fehlgeschlagen: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


def _merged_bytes(circular, post_recipients):
    """(bytes, ext, mimetype) des Sammel-Dokuments nach Format-Einstellung, oder
    ``(None, None, None)`` wenn nichts erzeugbar (WeasyPrint fehlt, kein DOCX)."""
    fmt = _doc_format()
    if fmt in ("pdf", "both"):
        pdf = documents.render_merged_pdf(circular, post_recipients)
        if pdf is not None:
            return pdf, "pdf", "application/pdf"
        if fmt == "pdf":
            return None, None, None
    # docx oder both-Fallback (ohne WeasyPrint).
    docx = documents.render_merged_docx(circular, post_recipients)
    if docx is not None:
        return docx, "docx", _DOCX_MIME
    return None, None, None


@bp.route("/<int:circular_id>/print-merged", methods=["POST"])
@login_required
def print_merged(circular_id):
    circular = _get_circular(circular_id)
    post_recipients = [r for r in circular.recipients
                       if r.delivery_method == CircularRecipient.METHOD_POST and r.customer]
    if not post_recipients:
        flash("Keine Empfänger mit Versandart „Post“.", "warning")
        return redirect(url_for("circulars.send", circular_id=circular.id))
    if len(post_recipients) > _bulk_print_limit():
        flash(f"Zu viele Empfänger auf einmal (max. {_bulk_print_limit()}). "
              "Bitte in kleineren Gruppen drucken.", "danger")
        return redirect(url_for("circulars.send", circular_id=circular.id))

    data, ext, mimetype = _merged_bytes(circular, post_recipients)
    if data is None:
        flash("WeasyPrint ist nicht installiert — Druck-PDF nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("circulars.send", circular_id=circular.id))

    now = datetime.utcnow()
    for rec in post_recipients:
        rec.post_sent_at = now
        db.session.add(CircularDeliveryLog(
            circular_id=circular.id, customer_id=rec.customer_id,
            recipient_name=rec.customer.name if rec.customer else None,
            recipient_email=rec.customer.email if rec.customer else None,
            method=CircularDeliveryLog.METHOD_POST,
            action=CircularDeliveryLog.ACTION_PRINTED, user_id=current_user.id))
    _mark_sent(circular)
    db.session.commit()

    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=f"Rundschreiben_{circular.id}.{ext}",
                     mimetype=mimetype)


@bp.route("/<int:circular_id>/preview")
@login_required
def preview(circular_id):
    """Brief-Vorschau als PDF (oder HTML-Fallback ohne WeasyPrint) mit
    Platzhalter-Empfänger."""
    circular = _get_circular(circular_id)
    customer_id = request.args.get("customer_id", type=int)
    customer = db.session.get(Customer, customer_id) if customer_id else None
    if customer is None:
        customer = Customer(name="Muster Maria", first_name="Maria", last_name="Muster",
                            salutation="Frau", strasse="Musterstraße", hausnummer="1",
                            plz="0000", ort="Musterort")
    html = documents.render_letter_html(circular, customer)
    HTML = documents._weasyprint()
    if HTML is None:
        return html
    pdf = HTML(string=html).write_pdf()
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     download_name=f"Rundschreiben_{circular.id}_Vorschau.pdf")
