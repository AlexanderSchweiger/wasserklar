import io
import os
from datetime import date, datetime

from flask import (
    abort, current_app, jsonify, render_template, request, redirect,
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
    rendered_email, rendered_letter_texts, TEXT_PLACEHOLDERS,
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
# Dokument-Erzeugung & -Archivierung (Helfer)
# ---------------------------------------------------------------------------

def _dunning_pdf_context(notice, wg=None, summary=None):
    """Vollständiger Template-Kontext fürs Mahn-PDF-Template.

    Zentralisiert die gerenderten Stufentexte (Einleitung/Schluss) + Design,
    damit Download, Vorschau, Mail und Bulk denselben Output erzeugen. Das
    Design kann ein eigenes Template über den Schlüssel ``dunning_template``
    vorgeben (z.B. das SaaS-„wasserklar"-Design, analog zu den Rechnungen);
    sonst die OSS-Standardvorlage — siehe ``_dunning_template_name``.
    """
    from app.settings_service import (
        wg_settings, get_contact_info, get_contact_info_font_size,
        get_invoice_sender_address,
    )
    if wg is None:
        wg = wg_settings()
    if summary is None:
        summary = dunning_summary(notice.invoice)
    intro, closing = rendered_letter_texts(notice, summary, wg)
    return dict(
        notice=notice, invoice=notice.invoice, summary=summary,
        wg=wg, design=_current_design(),
        letter_intro=intro, letter_closing=closing,
        contact_info=get_contact_info(),
        contact_info_font_size=get_contact_info_font_size(),
        invoice_sender_address=get_invoice_sender_address(),
    )


def _dunning_template_name(design):
    """Vorlagen-Datei für das Mahn-PDF: eigenes Design-Template, sonst Default."""
    return design.get("dunning_template", "dunning/pdf_template.html")


def _dunning_versioned_path(notice, ext):
    """Eindeutiger Pfad <Rechnungsnr>_M<level>[_Vn].<ext> — friert den
    Versand-Stand ein, ältere Versionen bleiben erhalten (analog Rechnung)."""
    doc_dir = _get_dunning_doc_dir(notice)
    base = os.path.join(doc_dir, _dunning_filename(notice, ext))
    if not os.path.exists(base):
        return base
    inv_no = notice.invoice.invoice_number
    v = 2
    while True:
        cand = os.path.join(doc_dir, f"{inv_no}_M{notice.level_snapshot}_V{v}.{ext}")
        if not os.path.exists(cand):
            return cand
        v += 1


def _render_dunning_pdf_bytes(notice):
    """PDF-Bytes (WeasyPrint). Wirft ImportError/OSError ohne WeasyPrint."""
    import weasyprint
    ctx = _dunning_pdf_context(notice)
    html_str = render_template(_dunning_template_name(ctx["design"]), **ctx)
    return weasyprint.HTML(string=html_str).write_pdf()


def _render_dunning_docx_bytes(notice):
    """DOCX-Bytes der Mahnung mit aktuellem Design + gerenderten Texten."""
    from app.dunning.document_service import generate_dunning_docx
    from app.settings_service import wg_settings
    return generate_dunning_docx(notice, wg_settings(), design=_current_design())


def _freeze_dunning_document(notice, ext, data):
    """Schreibt das exakt versendete Dokument persistent und merkt sich den Pfad.

    Ab dann liefert der Download diese eingefrorene Datei aus (Audit-/Beleg-
    Spur), statt live neu zu rendern.
    """
    path = _dunning_versioned_path(notice, ext)
    with open(path, "wb") as fh:
        fh.write(data)
    if ext == "pdf":
        notice.pdf_path = path
    else:
        notice.doc_path = path
    return path


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

def is_sendable(notice):
    """Versandbereit = aktive Mahnung, die noch nicht hinausgegangen ist.

    Gegenstueck zu ``Invoice.status == 'Entwurf'`` im Rechnungslauf: nur solche
    Mahnungen landen im „Mailing & Druck"-Dialog. Bereits versendete koennen
    weiterhin ueber die Sammel-Buttons (DOCX/PDF) neu gedruckt werden.
    """
    return (notice.status == DunningNotice.STATUS_AKTIV
            and notice.sent_at is None)


@bp.route("/notices")
@login_required
def notices():
    """Alle Mahnungen auflisten mit Filtern."""
    from sqlalchemy import and_
    from sqlalchemy.orm import joinedload

    status_filter = request.args.get("status", "")
    versand_filter = request.args.get("versand", "")
    q = request.args.get("q", "").strip()

    query = (
        DunningNotice.query
        .join(Invoice, DunningNotice.invoice_id == Invoice.id)
        .join(Customer, Invoice.customer_id == Customer.id)
        # Jede Zeile zeigt Rechnung, Kunde (inkl. Versandart) und Stufe —
        # ohne Eager-Load waeren das drei Zusatz-Queries pro Mahnung.
        .options(
            joinedload(DunningNotice.invoice).joinedload(Invoice.customer),
            joinedload(DunningNotice.stage),
        )
        .order_by(DunningNotice.issued_date.desc(), DunningNotice.id.desc())
    )

    if status_filter:
        query = query.filter(DunningNotice.status == status_filter)
    if q:
        query = query.filter(or_(
            Customer.name.ilike(f"%{q}%"),
            Invoice.invoice_number.ilike(f"%{q}%"),
        ))
    # Versandart-Filter, analog zum `mail_filter` der Rechnungsliste.
    # `Customer.wants_email` ist Einwilligung UND Adresse — hier dialekt-portabel
    # als SQL nachgebaut (kein Python-Property in der WHERE-Klausel moeglich).
    if versand_filter in ("mail", "post"):
        has_email = and_(Customer.email.isnot(None), Customer.email != "")
        if versand_filter == "mail":
            query = query.filter(Customer.rechnung_per_email == True, has_email)  # noqa: E712
        else:
            query = query.filter(or_(Customer.rechnung_per_email != True, ~has_email))  # noqa: E712

    notices_list = query.all()

    if request.headers.get("HX-Request"):
        return render_template("dunning/_notices_table.html", notices=notices_list)

    return render_template(
        "dunning/notices.html",
        notices=notices_list,
        statuses=DunningNotice.ALL_STATUSES,
        status_filter=status_filter,
        versand_filter=versand_filter,
        q=q,
        doc_format=AppSetting.get("invoice.document_format", "pdf"),
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
    """Mahn-Dokument als PDF oder DOCX herunterladen.

    Bereits versendete (eingefrorene) Mahnungen liefern die archivierte Datei
    aus; noch nicht versendete werden live gerendert (kein Cache beim Download —
    eingefroren wird erst beim Versand).
    """
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    fmt = request.args.get("fmt", AppSetting.get("invoice.document_format", "pdf"))
    if fmt == "both":
        fmt = "pdf"  # Download liefert genau ein Dokument

    if fmt == "docx":
        if notice.doc_path and os.path.exists(notice.doc_path):
            return send_file(notice.doc_path, as_attachment=True,
                             download_name=_dunning_filename(notice, "docx"),
                             mimetype=_DOCX_MIME)
        doc_data = _render_dunning_docx_bytes(notice)
        return send_file(io.BytesIO(doc_data), as_attachment=True,
                         download_name=_dunning_filename(notice, "docx"),
                         mimetype=_DOCX_MIME)

    # PDF (WeasyPrint)
    if notice.pdf_path and os.path.exists(notice.pdf_path):
        return send_file(notice.pdf_path, as_attachment=True,
                         download_name=_dunning_filename(notice, "pdf"))
    try:
        pdf_data = _render_dunning_pdf_bytes(notice)
    except (ImportError, OSError):
        if current_app.debug:
            flash("WeasyPrint nicht verfügbar – HTML-Vorschau geöffnet (Strg+P → Als PDF speichern).", "info")
            return redirect(url_for("dunning.notice_pdf_preview", notice_id=notice_id))
        flash("PDF-Erzeugung erfordert WeasyPrint (nur im Docker-Container verfügbar). "
              "Verwenden Sie ?fmt=docx für Word-Download.", "warning")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))
    return send_file(io.BytesIO(pdf_data), as_attachment=True,
                     download_name=_dunning_filename(notice, "pdf"))


# ---------------------------------------------------------------------------
# HTML-Vorschau (Entwicklung)
# ---------------------------------------------------------------------------

@bp.route("/notices/<int:notice_id>/pdf-preview")
@login_required
def notice_pdf_preview(notice_id):
    """HTML-Vorschau des Mahn-PDF-Templates im Browser (nur Entwicklung)."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        abort(404)
    ctx = _dunning_pdf_context(notice)
    return render_template(_dunning_template_name(ctx["design"]), **ctx)


# ---------------------------------------------------------------------------
# E-Mail-Versand
# ---------------------------------------------------------------------------

@bp.route("/notices/<int:notice_id>/send-email", methods=["POST"])
@login_required
def notice_send_email(notice_id):
    """Mahnung per E-Mail versenden (JSON-Antwort; Einzel- und Massenversand).

    Verhält sich wie der Rechnungsversand: Testmodus (Mail an die eigene
    Admin-Adresse), konfigurierbare Stufentexte, Postmark-Tracking via
    ``EmailEvent`` und Einfrieren des versendeten Dokuments.
    """
    from flask_mail import Message
    from app.settings_service import wg_settings, send_mail
    from app.dunning.send_email_hooks import run_before_send, read_message_id
    from app.email_tracking import record_email_sent

    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        return jsonify(ok=False, error="Mahnung nicht gefunden."), 404

    test_mode = request.form.get("test_mode") == "1"
    customer = notice.invoice.customer
    if not customer.email:
        return jsonify(ok=False, error="Kunde hat keine E-Mail-Adresse."), 400
    # Einwilligung: ohne aktivierten Schriftverkehr-per-E-Mail darf nur der
    # Test an die eigene Admin-Adresse gehen.
    if not test_mode and not customer.wants_email:
        return jsonify(
            ok=False,
            error="Der Kunde hat den Schriftverkehr per E-Mail nicht aktiviert.",
        ), 400
    if test_mode:
        recipient = current_user.email
        if not recipient:
            return jsonify(ok=False, error="Kein eigener E-Mail-Account für Testmodus hinterlegt."), 400
    else:
        recipient = customer.email

    # Sperrliste: an gesperrte Adressen nicht versenden (Test an Admin bleibt ok).
    # Ergebnis NICHT nach `notice` schreiben — das ueberschriebe die Mahnung und
    # der Versand liefe danach auf None (AttributeError statt Mail).
    if not test_mode:
        from app.email_suppression import suppression_notice
        blocked = suppression_notice(recipient)
        if blocked:
            return jsonify(ok=False, error=blocked), 400

    wg = wg_settings()
    summary = dunning_summary(notice.invoice)
    fmt = AppSetting.get("invoice.document_format", "pdf")
    subject, body = rendered_email(notice, summary, wg)
    if test_mode:
        subject = f"[TEST – an: {customer.email}] {subject}"
        body = f"[TESTMODUS – eigentlicher Empfänger: {customer.email}]\n\n{body}"

    msg = Message(subject=subject, recipients=[recipient], body=body)

    pdf_data = None
    docx_data = None
    if fmt in ("docx", "both"):
        docx_data = _render_dunning_docx_bytes(notice)
        msg.attach(_dunning_filename(notice, "docx"), _DOCX_MIME, docx_data)
    if fmt in ("pdf", "both"):
        try:
            pdf_data = _render_dunning_pdf_bytes(notice)
            msg.attach(_dunning_filename(notice, "pdf"), "application/pdf", pdf_data)
        except (ImportError, OSError):
            if fmt == "pdf":
                preview_url = url_for("dunning.notice_pdf_preview", notice_id=notice_id)
                return jsonify(ok=False, error="PDF-Erzeugung erfordert WeasyPrint.",
                               preview_url=preview_url if current_app.debug else None), 503
            # bei 'both' reicht das DOCX als Anhang

    if not msg.attachments:
        return jsonify(ok=False, error="Kein Dokument konnte erzeugt werden."), 500

    run_before_send(notice, msg)
    try:
        send_mail(msg)
    except Exception as e:
        return jsonify(ok=False, error=f"E-Mail-Versand fehlgeschlagen: {e}"), 500

    if test_mode:
        return jsonify(ok=True, notice_id=notice.id, email=recipient, test_mode=True)

    # Versand protokollieren (EmailEvent + Tracking-Felder) und Dokument einfrieren.
    record_email_sent(notice, recipient, read_message_id(msg))
    notice.sent_via = "email"
    notice.sent_at = datetime.utcnow()
    notice.sent_to = recipient
    if docx_data is not None:
        _freeze_dunning_document(notice, "docx", docx_data)
    if pdf_data is not None:
        _freeze_dunning_document(notice, "pdf", pdf_data)
    db.session.commit()

    return jsonify(ok=True, notice_id=notice.id, email=recipient, test_mode=False)


@bp.route("/notices/<int:notice_id>/mark-sent", methods=["POST"])
@login_required
def notice_mark_sent(notice_id):
    """Mahnung als per Post versendet markieren und das Dokument archivieren."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    fmt = AppSetting.get("invoice.document_format", "pdf")
    frozen = False
    if fmt in ("docx", "both"):
        try:
            _freeze_dunning_document(notice, "docx", _render_dunning_docx_bytes(notice))
            frozen = True
        except Exception:  # noqa: BLE001
            current_app.logger.exception("DOCX-Archivierung fehlgeschlagen")
    if fmt in ("pdf", "both"):
        try:
            _freeze_dunning_document(notice, "pdf", _render_dunning_pdf_bytes(notice))
            frozen = True
        except (ImportError, OSError):
            pass

    notice.sent_via = "post"
    notice.sent_at = datetime.utcnow()
    notice.sent_to = "Post"
    db.session.commit()
    if frozen:
        flash("Mahnung als versendet (Post) markiert und archiviert.", "success")
    else:
        flash("Mahnung als versendet (Post) markiert. Dokument konnte nicht "
              "archiviert werden (WeasyPrint nur im Docker-Container).", "warning")
    return redirect(url_for("dunning.notice_detail", notice_id=notice_id))


@bp.route("/notices/<int:notice_id>/email-events")
@login_required
def notice_email_events(notice_id):
    """Read-only Audit-Trail aller E-Mail-Versand-/Webhook-Events zur Mahnung."""
    from app.models import EmailEvent
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))
    events = (EmailEvent.query
              .filter_by(subject_type="dunning", subject_id=notice.id)
              .order_by(EmailEvent.occurred_at.desc(), EmailEvent.id.desc())
              .all())
    return render_template("dunning/email_events.html", notice=notice, events=events)


# ---------------------------------------------------------------------------
# Bulk-Dokumente
# ---------------------------------------------------------------------------

def _bulk_print_limit_exceeded(ids):
    """Sicherheitsnetz fuer Massendruck/-export der Mahnungen: gibt eine
    Redirect-Response zurueck, wenn die Auswahl das konfigurierte Limit
    (BULK_PRINT_MAX) ueberschreitet, sonst None. Die UI batcht bereits in
    Gruppen, der Cap faengt direkte/veraltete Clients ab und schuetzt vor
    RAM-/Timeout-Last."""
    limit = current_app.config.get("BULK_PRINT_MAX", 100)
    if len(ids) > limit:
        flash(f"Es können maximal {limit} Dokumente pro Durchgang erstellt werden. "
              f"Bitte die Auswahl in kleinere Gruppen aufteilen.", "warning")
        return redirect(url_for("dunning.notices"))
    return None


@bp.route("/bulk-docx-merged", methods=["POST"])
@login_required
def bulk_docx_merged():
    """Alle markierten Mahnungen als ein zusammengeführtes .docx."""
    notice_ids = request.form.getlist("notice_ids", type=int)
    if not notice_ids:
        flash("Keine Mahnungen ausgewählt.", "warning")
        return redirect(url_for("dunning.notices"))
    if (resp := _bulk_print_limit_exceeded(notice_ids)):
        return resp

    from app.invoices.document_service import merge_docx_files

    notices = DunningNotice.query.filter(DunningNotice.id.in_(notice_ids)).all()
    sources = []
    for notice in notices:
        # Versendete Mahnungen: archivierte Datei; sonst live rendern.
        if notice.doc_path and os.path.exists(notice.doc_path):
            sources.append(notice.doc_path)
        else:
            sources.append(_render_dunning_docx_bytes(notice))

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
    if (resp := _bulk_print_limit_exceeded(notice_ids)):
        return resp

    try:
        import weasyprint
    except (ImportError, OSError):
        if current_app.debug:
            flash("WeasyPrint nicht verfügbar. Einzelne PDF-Vorschau unter /dunning/notices/<id>/pdf-preview.", "info")
        else:
            flash("PDF-Erzeugung erfordert WeasyPrint (nur im Docker-Container). "
                  "Verwenden Sie den DOCX-Export.", "warning")
        return redirect(url_for("dunning.notices"))

    from pypdf import PdfWriter
    notices = DunningNotice.query.filter(DunningNotice.id.in_(notice_ids)).all()
    if not notices:
        flash("Keine Dokumente erzeugt.", "warning")
        return redirect(url_for("dunning.notices"))

    # Pro Mahnung einzeln rendern und als fertiges PDF an den Merger haengen.
    # Bewusst KEIN WeasyPrint-copy() ueber mehrere Dokumente — kippt auf dem
    # Server mit PIL.UnidentifiedImageError und korrumpiert dabei die Bild-Buffer
    # (Details siehe invoices.bulk_pdf_merged). Einzel-Render + pypdf laeuft stabil.
    # Versendete Mahnungen liefern ihr archiviertes PDF, sonst Live-Render.
    writer = PdfWriter()
    for notice in notices:
        if notice.pdf_path and os.path.exists(notice.pdf_path):
            writer.append(notice.pdf_path)
        else:
            ctx = _dunning_pdf_context(notice)
            html_str = render_template(_dunning_template_name(ctx["design"]), **ctx)
            pdf_bytes = weasyprint.HTML(string=html_str).render().write_pdf()
            writer.append(io.BytesIO(pdf_bytes))
    writer.compress_identical_objects()
    doc_dir = os.path.join(current_app.config["PDF_DIR"], "_bulk")
    os.makedirs(doc_dir, exist_ok=True)
    merged_path = os.path.join(doc_dir, "Mahnungen_gesamt.pdf")
    with open(merged_path, "wb") as fh:
        writer.write(fh)
    writer.close()

    return send_file(merged_path, as_attachment=True,
                     download_name="Mahnungen_gesamt.pdf")


@bp.route("/bulk-post-pdf", methods=["POST"])
@login_required
def bulk_post_pdf():
    """Post-Versand: markierte Mahnungen als ein zusammengeführtes PDF — und
    markiert sie dabei als per Post versendet.

    Gegenstück zum Mailversand (:func:`notice_send_email`) und Spiegel von
    ``invoices.billing_run_post_bulk``: das heruntergeladene PDF **ist** der
    Post-Beleg, wird daher pro Mahnung eingefroren (``_freeze_dunning_document``)
    und die Mahnung auf ``sent_via='post'`` gesetzt. Bewusst **kein**
    Archiv-Reuse wie in :func:`bulk_pdf_merged` — gedruckt wird genau das
    Dokument, das jetzt hinausgeht.

    Bereits versendete Mahnungen in der Auswahl bleiben unverändert, werden aber
    mit ausgeliefert (Nachdruck).
    """
    notice_ids = request.form.getlist("notice_ids", type=int)
    if not notice_ids:
        flash("Keine Mahnungen ausgewählt.", "warning")
        return redirect(url_for("dunning.notices"))
    if (resp := _bulk_print_limit_exceeded(notice_ids)):
        return resp

    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        flash("PDF-Erzeugung erfordert WeasyPrint (nur im Docker-Container). "
              "Verwenden Sie den DOCX-Export.", "warning")
        return redirect(url_for("dunning.notices"))

    from pypdf import PdfWriter

    notices_list = (
        DunningNotice.query
        .filter(DunningNotice.id.in_(notice_ids))
        .join(Invoice, DunningNotice.invoice_id == Invoice.id)
        .order_by(Invoice.invoice_number)
        .all()
    )
    if not notices_list:
        flash("Keine Mahnungen gefunden.", "warning")
        return redirect(url_for("dunning.notices"))

    # Einzel-Render + pypdf statt WeasyPrint-copy() ueber mehrere Dokumente —
    # Begruendung siehe bulk_pdf_merged.
    writer = PdfWriter()
    for notice in notices_list:
        pdf_bytes = _render_dunning_pdf_bytes(notice)
        _freeze_dunning_document(notice, "pdf", pdf_bytes)
        if is_sendable(notice):
            notice.sent_via = "post"
            notice.sent_at = datetime.utcnow()
            notice.sent_to = "Post"
        writer.append(io.BytesIO(pdf_bytes))
    db.session.commit()

    writer.compress_identical_objects()
    doc_dir = os.path.join(current_app.config["PDF_DIR"], "_bulk")
    os.makedirs(doc_dir, exist_ok=True)
    merged_path = os.path.join(doc_dir, "Mahnungen_Post.pdf")
    with open(merged_path, "wb") as fh:
        writer.write(fh)
    writer.close()

    return send_file(merged_path, as_attachment=True,
                     download_name="Mahnungen_Post.pdf")


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
    if request.method == "POST":
        return _save_policy(None)

    return render_template("dunning/policy_form.html", policy=None,
                           placeholders=TEXT_PLACEHOLDERS)


@bp.route("/policies/<int:policy_id>/bearbeiten", methods=["GET", "POST"])
@login_required
def policy_edit(policy_id):
    """Mahnvorlage bearbeiten (inkl. Stufen)."""
    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    if request.method == "POST":
        return _save_policy(policy)

    return render_template("dunning/policy_form.html", policy=policy,
                           placeholders=TEXT_PLACEHOLDERS)


@bp.route("/policies/<int:policy_id>/loeschen", methods=["POST"])
@login_required
def policy_delete(policy_id):
    """Mahnvorlage löschen (nur wenn keine Notices darauf verweisen)."""
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
    email_subjects = request.form.getlist("stage_email_subject")
    email_bodies = request.form.getlist("stage_email_body")
    letter_intros = request.form.getlist("stage_letter_intro")
    letter_closings = request.form.getlist("stage_letter_closing")
    stage_ids = request.form.getlist("stage_id")

    def _text_at(lst, idx):
        """Trimmt den Wert an Index ``idx`` oder None (leer = Standardtext)."""
        if idx < len(lst) and lst[idx].strip():
            return lst[idx].strip()
        return None

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
        stage.email_subject = _text_at(email_subjects, i)
        stage.email_body = _text_at(email_bodies, i)
        stage.letter_intro = _text_at(letter_intros, i)
        stage.letter_closing = _text_at(letter_closings, i)

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
