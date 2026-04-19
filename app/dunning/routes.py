import io
import os
from datetime import date, datetime

from flask import (
    current_app, jsonify, render_template, request, redirect,
    send_file, url_for, flash,
)
from flask_login import login_required, current_user
from sqlalchemy import or_

from app.dunning import bp
from app.extensions import db
from app.models import (
    AppSetting, Customer, DunningNotice, DunningPolicy, DunningStage, Invoice,
)
from app.dunning.services import (
    cancel_dunnings_for_invoice, compute_fee, create_dunning_notice,
    current_dunning_level, defer_dunning_notice, dunning_summary,
    eligible_invoices_for_stage, reset_dunning_notice,
)
from app.invoices.design import get_design

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _current_design():
    return get_design(AppSetting.get("invoice.design", "classic"))


def _get_dunning_doc_dir(notice):
    """Gibt den Unterordner für Mahn-Dokumente zurück: <PDF_DIR>/<Jahr>/dunning/"""
    year = notice.issued_date.year if notice.issued_date else "misc"
    doc_dir = os.path.join(current_app.config["PDF_DIR"], str(year), "dunning")
    os.makedirs(doc_dir, exist_ok=True)
    return doc_dir


def _dunning_filename(notice, ext):
    """Dateiname: <Rechnungsnr>_M<level>.<ext>"""
    return f"{notice.invoice.invoice_number}_M{notice.level_snapshot}.{ext}"


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    """Mahnwesen-Dashboard: Zusammenfassung und fällige Mahnungen."""
    policy = DunningPolicy.query.filter_by(is_default=True, active=True).first()
    eligible = eligible_invoices_for_stage(policy) if policy else []

    # Aktive Mahnungen nach Status
    active_count = DunningNotice.query.filter_by(status=DunningNotice.STATUS_AKTIV).count()
    reset_count = DunningNotice.query.filter_by(status=DunningNotice.STATUS_ZURUECKGESETZT).count()
    total_count = DunningNotice.query.count()

    return render_template(
        "dunning/index.html",
        eligible=eligible,
        active_count=active_count,
        reset_count=reset_count,
        total_count=total_count,
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Mahnlauf
# ---------------------------------------------------------------------------

@bp.route("/run")
@login_required
def run():
    """Mahnlauf: zeigt fällige Vorschläge, Operator wählt aus."""
    policy = DunningPolicy.query.filter_by(is_default=True, active=True).first()
    if not policy:
        flash("Keine aktive Standard-Mahnvorlage konfiguriert.", "danger")
        return redirect(url_for("dunning.index"))

    eligible = eligible_invoices_for_stage(policy)
    return render_template("dunning/run.html", eligible=eligible, policy=policy)


@bp.route("/run/execute", methods=["POST"])
@login_required
def run_execute():
    """Ausgewählte Mahnungen erzeugen (Bulk)."""
    invoice_ids = request.form.getlist("invoice_ids")
    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "danger")
        return redirect(url_for("dunning.run"))

    policy = DunningPolicy.query.filter_by(is_default=True, active=True).first()
    if not policy:
        flash("Keine aktive Standard-Mahnvorlage konfiguriert.", "danger")
        return redirect(url_for("dunning.run"))

    eligible = eligible_invoices_for_stage(policy)
    eligible_map = {str(inv.id): stage for inv, stage in eligible}

    created = 0
    for inv_id in invoice_ids:
        stage = eligible_map.get(inv_id)
        if not stage:
            continue
        invoice = db.session.get(Invoice, int(inv_id))
        if not invoice:
            continue
        create_dunning_notice(invoice, stage, current_user.id)
        created += 1

    db.session.commit()
    flash(f"{created} Mahnung(en) erzeugt.", "success")
    return redirect(url_for("dunning.notices"))


# ---------------------------------------------------------------------------
# Notices (Liste)
# ---------------------------------------------------------------------------

@bp.route("/notices")
@login_required
def notices():
    """Alle Mahnungen auflisten mit Filtern."""
    status_filter = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    query = (
        DunningNotice.query
        .join(Invoice, DunningNotice.invoice_id == Invoice.id)
        .join(Customer, Invoice.customer_id == Customer.id)
        .order_by(DunningNotice.issued_date.desc(), DunningNotice.id.desc())
    )

    if status_filter:
        query = query.filter(DunningNotice.status == status_filter)
    if q:
        query = query.filter(or_(
            Customer.name.ilike(f"%{q}%"),
            Invoice.invoice_number.ilike(f"%{q}%"),
        ))

    notices_list = query.all()

    if request.headers.get("HX-Request"):
        return render_template("dunning/_notices_table.html", notices=notices_list)

    return render_template(
        "dunning/notices.html",
        notices=notices_list,
        statuses=DunningNotice.ALL_STATUSES,
        status_filter=status_filter,
        q=q,
    )


# ---------------------------------------------------------------------------
# Notice Detail
# ---------------------------------------------------------------------------

@bp.route("/notices/<int:notice_id>")
@login_required
def notice_detail(notice_id):
    """Detailseite einer Mahnung."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    summary = dunning_summary(notice.invoice)
    return render_template("dunning/notice_detail.html", notice=notice, summary=summary)


@bp.route("/notices/<int:notice_id>/reset", methods=["POST"])
@login_required
def notice_reset(notice_id):
    """Mahnung zurücksetzen (eine Stufe)."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    if notice.status != DunningNotice.STATUS_AKTIV:
        flash("Nur aktive Mahnungen können zurückgesetzt werden.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    reason = request.form.get("reason", "").strip()
    reset_dunning_notice(notice, current_user, reason)
    db.session.commit()
    flash(f"Mahnung Stufe {notice.level_snapshot} zurückgesetzt.", "success")
    return redirect(url_for("dunning.notice_detail", notice_id=notice_id))


@bp.route("/notices/<int:notice_id>/defer", methods=["POST"])
@login_required
def notice_defer(notice_id):
    """Nachfrist einer Mahnung verlängern."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    if notice.status != DunningNotice.STATUS_AKTIV:
        flash("Nur aktive Mahnungen können verlängert werden.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    new_due = request.form.get("new_due_date", "").strip()
    if not new_due:
        flash("Bitte ein neues Fälligkeitsdatum angeben.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    try:
        new_due_date = date.fromisoformat(new_due)
    except ValueError:
        flash("Ungültiges Datum.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    defer_dunning_notice(notice, new_due_date, current_user)
    db.session.commit()
    flash(f"Nachfrist auf {new_due_date.strftime('%d.%m.%Y')} verlängert.", "success")
    return redirect(url_for("dunning.notice_detail", notice_id=notice_id))


# ---------------------------------------------------------------------------
# Dokument-Download (PDF / DOCX)
# ---------------------------------------------------------------------------

@bp.route("/notices/<int:notice_id>/pdf")
@login_required
def notice_pdf(notice_id):
    """Mahn-Dokument als PDF oder DOCX herunterladen."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    from app.settings_service import wg_settings
    wg = wg_settings()
    summary = dunning_summary(notice.invoice)
    fmt = request.args.get("fmt", AppSetting.get("invoice.document_format", "pdf"))

    filename_base = _dunning_filename(notice, "")

    if fmt == "docx":
        # Cached?
        if notice.doc_path and os.path.exists(notice.doc_path):
            return send_file(notice.doc_path, as_attachment=True,
                             download_name=_dunning_filename(notice, "docx"),
                             mimetype=_DOCX_MIME)
        from app.dunning.document_service import generate_dunning_docx
        doc_data = generate_dunning_docx(notice, wg, design=_current_design())
        # Cache
        doc_dir = _get_dunning_doc_dir(notice)
        doc_path = os.path.join(doc_dir, _dunning_filename(notice, "docx"))
        with open(doc_path, "wb") as f:
            f.write(doc_data)
        notice.doc_path = doc_path
        db.session.commit()
        return send_file(io.BytesIO(doc_data), as_attachment=True,
                         download_name=_dunning_filename(notice, "docx"),
                         mimetype=_DOCX_MIME)

    # PDF (WeasyPrint)
    if notice.pdf_path and os.path.exists(notice.pdf_path):
        return send_file(notice.pdf_path, as_attachment=True,
                         download_name=_dunning_filename(notice, "pdf"))

    try:
        import weasyprint
    except ImportError:
        flash("PDF-Erzeugung erfordert WeasyPrint (nur im Docker-Container verfügbar). "
              "Verwenden Sie ?fmt=docx für Word-Download.", "warning")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    html_str = render_template(
        "dunning/pdf_template.html",
        notice=notice, invoice=notice.invoice,
        summary=summary, wg=wg, design=_current_design(),
    )
    doc_dir = _get_dunning_doc_dir(notice)
    pdf_path = os.path.join(doc_dir, _dunning_filename(notice, "pdf"))
    weasyprint.HTML(string=html_str).write_pdf(pdf_path)
    notice.pdf_path = pdf_path
    db.session.commit()
    return send_file(pdf_path, as_attachment=True,
                     download_name=_dunning_filename(notice, "pdf"))


# ---------------------------------------------------------------------------
# E-Mail-Versand
# ---------------------------------------------------------------------------

@bp.route("/notices/<int:notice_id>/send-email", methods=["POST"])
@login_required
def notice_send_email(notice_id):
    """Mahnung per E-Mail versenden (JSON-Antwort für AJAX)."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        return jsonify(ok=False, error="Mahnung nicht gefunden."), 404

    customer = notice.invoice.customer
    if not customer.email:
        return jsonify(ok=False, error="Kunde hat keine E-Mail-Adresse."), 400

    from app.settings_service import wg_settings
    from flask_mail import Message
    from app.extensions import mail

    wg = wg_settings()
    summary = dunning_summary(notice.invoice)
    fmt = AppSetting.get("invoice.document_format", "pdf")
    title = notice.print_title_snapshot or notice.name_snapshot

    subject = f"{title} – {notice.invoice.invoice_number}"
    body = (
        f"Sehr geehrte Damen und Herren,\n\n"
        f"anbei erhalten Sie eine {title} zu unserer Rechnung "
        f"{notice.invoice.invoice_number}.\n\n"
        f"Bitte überweisen Sie den ausstehenden Betrag bis zum "
        f"{notice.new_due_date.strftime('%d.%m.%Y') if notice.new_due_date else '—'}.\n\n"
        f"Mit freundlichen Grüßen\n{wg.get('name', '')}"
    )

    msg = Message(subject=subject, recipients=[customer.email], body=body)

    # Dokument anhängen
    if fmt in ("docx", "both"):
        from app.dunning.document_service import generate_dunning_docx
        doc_data = generate_dunning_docx(notice, wg, design=_current_design())
        msg.attach(_dunning_filename(notice, "docx"), _DOCX_MIME, doc_data)
        # Cache
        doc_dir = _get_dunning_doc_dir(notice)
        doc_path = os.path.join(doc_dir, _dunning_filename(notice, "docx"))
        with open(doc_path, "wb") as f:
            f.write(doc_data)
        notice.doc_path = doc_path

    if fmt in ("pdf", "both"):
        try:
            import weasyprint
            html_str = render_template(
                "dunning/pdf_template.html",
                notice=notice, invoice=notice.invoice,
                summary=summary, wg=wg, design=_current_design(),
            )
            pdf_data = weasyprint.HTML(string=html_str).write_pdf()
            msg.attach(_dunning_filename(notice, "pdf"), "application/pdf", pdf_data)
            doc_dir = _get_dunning_doc_dir(notice)
            pdf_path = os.path.join(doc_dir, _dunning_filename(notice, "pdf"))
            with open(pdf_path, "wb") as f:
                f.write(pdf_data)
            notice.pdf_path = pdf_path
        except ImportError:
            if fmt == "pdf":
                return jsonify(ok=False, error="PDF-Erzeugung erfordert WeasyPrint."), 500

    try:
        mail.send(msg)
    except Exception as e:
        return jsonify(ok=False, error=f"E-Mail-Versand fehlgeschlagen: {e}"), 500

    notice.sent_via = "email"
    notice.sent_at = datetime.utcnow()
    notice.sent_to = customer.email
    db.session.commit()

    return jsonify(ok=True, notice_id=notice.id, email=customer.email)


# ---------------------------------------------------------------------------
# Bulk-Dokumente
# ---------------------------------------------------------------------------

@bp.route("/bulk-docx-merged", methods=["POST"])
@login_required
def bulk_docx_merged():
    """Alle markierten Mahnungen als ein zusammengeführtes .docx."""
    notice_ids = request.form.getlist("notice_ids", type=int)
    if not notice_ids:
        flash("Keine Mahnungen ausgewählt.", "warning")
        return redirect(url_for("dunning.notices"))

    from app.dunning.document_service import generate_dunning_docx
    from app.invoices.document_service import merge_docx_files
    from app.settings_service import wg_settings

    wg = wg_settings()
    design = _current_design()
    notices = DunningNotice.query.filter(DunningNotice.id.in_(notice_ids)).all()
    sources = []

    for notice in notices:
        if notice.doc_path and os.path.exists(notice.doc_path):
            sources.append(notice.doc_path)
        else:
            doc_data = generate_dunning_docx(notice, wg, design=design)
            doc_dir = _get_dunning_doc_dir(notice)
            doc_path = os.path.join(doc_dir, _dunning_filename(notice, "docx"))
            with open(doc_path, "wb") as f:
                f.write(doc_data)
            notice.doc_path = doc_path
            sources.append(doc_data)

    db.session.commit()
    merged = merge_docx_files(sources)
    return send_file(
        io.BytesIO(merged),
        as_attachment=True,
        download_name="Mahnungen_gesamt.docx",
        mimetype=_DOCX_MIME,
    )


@bp.route("/bulk-pdf-merged", methods=["POST"])
@login_required
def bulk_pdf_merged():
    """Alle markierten Mahnungen als zusammengeführtes PDF."""
    notice_ids = request.form.getlist("notice_ids", type=int)
    if not notice_ids:
        flash("Keine Mahnungen ausgewählt.", "warning")
        return redirect(url_for("dunning.notices"))

    try:
        import weasyprint
    except ImportError:
        flash("PDF-Erzeugung erfordert WeasyPrint (nur im Docker-Container). "
              "Verwenden Sie den DOCX-Export.", "warning")
        return redirect(url_for("dunning.notices"))

    from app.settings_service import wg_settings
    wg = wg_settings()
    notices = DunningNotice.query.filter(DunningNotice.id.in_(notice_ids)).all()
    rendered_docs = []

    design = _current_design()
    for notice in notices:
        summary = dunning_summary(notice.invoice)
        html_str = render_template(
            "dunning/pdf_template.html",
            notice=notice, invoice=notice.invoice,
            summary=summary, wg=wg, design=design,
        )
        rendered_docs.append(weasyprint.HTML(string=html_str).render())

    if not rendered_docs:
        flash("Keine Dokumente erzeugt.", "warning")
        return redirect(url_for("dunning.notices"))

    all_pages = []
    for doc in rendered_docs:
        all_pages.extend(doc.pages)

    doc_dir = os.path.join(current_app.config["PDF_DIR"], "_bulk")
    os.makedirs(doc_dir, exist_ok=True)
    merged_path = os.path.join(doc_dir, "Mahnungen_gesamt.pdf")
    rendered_docs[0].copy(all_pages).write_pdf(merged_path)

    return send_file(merged_path, as_attachment=True,
                     download_name="Mahnungen_gesamt.pdf")


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------

@bp.route("/policies")
@login_required
def policies():
    """Alle Mahnvorlagen auflisten."""
    all_policies = DunningPolicy.query.order_by(DunningPolicy.name).all()
    return render_template("dunning/policies.html", policies=all_policies)


@bp.route("/policies/neu", methods=["GET", "POST"])
@login_required
def policy_new():
    """Neue Mahnvorlage anlegen."""
    if not current_user.is_admin:
        flash("Nur Administratoren können Mahnvorlagen anlegen.", "danger")
        return redirect(url_for("dunning.policies"))

    if request.method == "POST":
        return _save_policy(None)

    return render_template("dunning/policy_form.html", policy=None)


@bp.route("/policies/<int:policy_id>/bearbeiten", methods=["GET", "POST"])
@login_required
def policy_edit(policy_id):
    """Mahnvorlage bearbeiten (inkl. Stufen)."""
    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    if not current_user.is_admin:
        flash("Nur Administratoren können Mahnvorlagen bearbeiten.", "danger")
        return redirect(url_for("dunning.policies"))

    if request.method == "POST":
        return _save_policy(policy)

    return render_template("dunning/policy_form.html", policy=policy)


@bp.route("/policies/<int:policy_id>/loeschen", methods=["POST"])
@login_required
def policy_delete(policy_id):
    """Mahnvorlage löschen (nur wenn keine Notices darauf verweisen)."""
    if not current_user.is_admin:
        flash("Nur Administratoren können Mahnvorlagen löschen.", "danger")
        return redirect(url_for("dunning.policies"))

    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    stage_ids = [s.id for s in policy.stages]
    if stage_ids:
        notice_count = DunningNotice.query.filter(
            DunningNotice.stage_id.in_(stage_ids)
        ).count()
        if notice_count > 0:
            flash(f"Vorlage kann nicht gelöscht werden – {notice_count} Mahnung(en) verknüpft.", "danger")
            return redirect(url_for("dunning.policies"))

    db.session.delete(policy)
    db.session.commit()
    flash(f'Mahnvorlage "{policy.name}" gelöscht.', "success")
    return redirect(url_for("dunning.policies"))


@bp.route("/policies/<int:policy_id>/default", methods=["POST"])
@login_required
def policy_set_default(policy_id):
    """Mahnvorlage als Standard setzen."""
    if not current_user.is_admin:
        flash("Nur Administratoren können den Standard ändern.", "danger")
        return redirect(url_for("dunning.policies"))

    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    DunningPolicy.query.filter(DunningPolicy.id != policy_id).update({"is_default": False})
    policy.is_default = True
    db.session.commit()
    flash(f'"{policy.name}" als Standard-Mahnvorlage gesetzt.', "success")
    return redirect(url_for("dunning.policies"))


# ---------------------------------------------------------------------------
# Hilfsfunktion: Policy + Stages speichern
# ---------------------------------------------------------------------------

def _save_policy(policy):
    """Speichert eine Policy (neu oder bestehend) inkl. ihrer Stages aus dem Formular."""
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()

    if not name:
        flash("Name ist ein Pflichtfeld.", "danger")
        if policy:
            return redirect(url_for("dunning.policy_edit", policy_id=policy.id))
        return redirect(url_for("dunning.policy_new"))

    is_new = policy is None
    if is_new:
        policy = DunningPolicy(name=name, description=description)
        db.session.add(policy)
        db.session.flush()
    else:
        policy.name = name
        policy.description = description

    levels = request.form.getlist("stage_level")
    names = request.form.getlist("stage_name")
    days_list = request.form.getlist("stage_days_after_due")
    fees_fixed = request.form.getlist("stage_fee_fixed")
    fees_percent = request.form.getlist("stage_fee_percent")
    fees_min = request.form.getlist("stage_fee_min")
    fees_max = request.form.getlist("stage_fee_max")
    new_due_days_list = request.form.getlist("stage_new_due_days")
    print_titles = request.form.getlist("stage_print_title")
    colors = request.form.getlist("stage_color")
    icons = request.form.getlist("stage_icon")
    stage_ids = request.form.getlist("stage_id")

    existing = {s.id: s for s in policy.stages}
    seen_ids = set()

    from decimal import Decimal, InvalidOperation

    for i in range(len(levels)):
        s_name = names[i].strip() if i < len(names) else ""
        if not s_name:
            continue

        try:
            level = int(levels[i]) if i < len(levels) else i + 1
            days = int(days_list[i]) if i < len(days_list) else 14
            fee_fixed = Decimal(fees_fixed[i].replace(",", ".")) if i < len(fees_fixed) and fees_fixed[i].strip() else Decimal("0")
            fee_pct = Decimal(fees_percent[i].replace(",", ".")) if i < len(fees_percent) and fees_percent[i].strip() else Decimal("0")
            f_min = Decimal(fees_min[i].replace(",", ".")) if i < len(fees_min) and fees_min[i].strip() else None
            f_max = Decimal(fees_max[i].replace(",", ".")) if i < len(fees_max) and fees_max[i].strip() else None
            ndd = int(new_due_days_list[i]) if i < len(new_due_days_list) and new_due_days_list[i].strip() else 14
        except (ValueError, InvalidOperation):
            flash(f"Ungültige Eingabe in Stufe {i + 1}.", "danger")
            return redirect(url_for("dunning.policy_edit", policy_id=policy.id))

        pt = print_titles[i].strip() if i < len(print_titles) else s_name
        color = colors[i].strip() if i < len(colors) else ""
        icon = icons[i].strip() if i < len(icons) else ""

        sid_raw = stage_ids[i] if i < len(stage_ids) else ""
        sid = int(sid_raw) if sid_raw.strip() else None

        if sid and sid in existing:
            stage = existing[sid]
            seen_ids.add(sid)
        else:
            stage = DunningStage(policy_id=policy.id)
            db.session.add(stage)

        stage.level = level
        stage.name = s_name
        stage.days_after_due = days
        stage.fee_fixed = fee_fixed
        stage.fee_percent = fee_pct
        stage.fee_min = f_min
        stage.fee_max = f_max
        stage.new_due_days = ndd
        stage.print_title = pt or s_name
        stage.color = color or None
        stage.icon = icon or None

    for sid, stage in existing.items():
        if sid not in seen_ids:
            notice_count = DunningNotice.query.filter_by(stage_id=sid).count()
            if notice_count == 0:
                db.session.delete(stage)
            else:
                stage.active = False

    db.session.commit()

    if is_new:
        flash(f'Mahnvorlage "{policy.name}" angelegt.', "success")
    else:
        flash(f'Mahnvorlage "{policy.name}" gespeichert.', "success")
    return redirect(url_for("dunning.policies"))
