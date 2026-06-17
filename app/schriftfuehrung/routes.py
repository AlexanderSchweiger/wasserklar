"""Routen der Schriftführung: Sitzungen (Vorstand + Hauptversammlung), Agenda,
Einladungs-Versand (Mail + Druck + iCal, mit Tracking), Protokoll (Anwesenheit/
Quorum/Beschlüsse + Rich-Text oder Upload), Beschluss-Register und Schriftverkehr-
Archiv.
"""
import io
import os
from datetime import datetime, date

from flask import (
    render_template, redirect, url_for, flash, request,
    current_app, send_file, abort,
)
from flask_login import login_required, current_user
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from app.schriftfuehrung import bp, constants, services, storage, documents, ical
from app.extensions import db
from app.models import (
    AppSetting, Customer,
    Meeting, MeetingAgendaItem, MeetingInvitation, MeetingDeliveryLog,
    MeetingAttendance, MeetingResolution, MeetingProtocol,
    SchriftverkehrDocument,
)
from app.email_tracking import record_email_sent
from app.schriftfuehrung.send_email_hooks import run_before_send
from app.settings_service import send_mail, sanitize_rich_text, wg_settings

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ── kleine Helfer ────────────────────────────────────────────────────────────

def _get_meeting(meeting_id):
    return db.get_or_404(Meeting, meeting_id)


def _list_endpoint(meeting_type):
    return ("schriftfuehrung.assemblies" if meeting_type == Meeting.TYPE_ASSEMBLY
            else "schriftfuehrung.board_meetings")


def _type_label(meeting_type):
    return constants.MEETING_TYPE_LABELS.get(meeting_type, "Sitzung")


def _parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _meeting_when(meeting):
    """Datum + Uhrzeit von–bis als deutscher Text (für Mail/Dateinamen)."""
    parts = []
    if meeting.meeting_date:
        parts.append(meeting.meeting_date.strftime("%d.%m.%Y"))
    if meeting.start_time:
        t = meeting.start_time.strftime("%H:%M")
        if meeting.end_time:
            t += "–" + meeting.end_time.strftime("%H:%M")
        parts.append(t + " Uhr")
    return ", ".join(parts)


def _invitation_basename(meeting, customer=None):
    when = (meeting.meeting_date or date.today()).strftime("%Y-%m-%d")
    base = f"Einladung_{storage.slugify_filename(meeting.title)}_{when}"
    if customer is not None:
        base += "_" + storage.slugify_filename(customer.name)
    return base


def _weasyprint():
    """WeasyPrint-HTML-Klasse oder None (GTK/WeasyPrint lokal nicht installiert)."""
    try:
        from weasyprint import HTML
        return HTML
    except (ImportError, OSError):
        return None


def _doc_format():
    """Konfiguriertes Dokumentformat (pdf/docx/both) — teilt die Einstellung mit
    den Rechnungen (AppSetting ``invoice.document_format``)."""
    fmt = AppSetting.get("invoice.document_format", "pdf")
    return fmt if fmt in ("pdf", "docx", "both") else "pdf"


def _upsert_invitation(meeting, customer, method):
    """Legt die Einladung für einen Empfänger an oder aktualisiert sie."""
    inv = MeetingInvitation.query.filter_by(
        meeting_id=meeting.id, customer_id=customer.id).first()
    if inv is None:
        inv = MeetingInvitation(meeting_id=meeting.id, customer_id=customer.id)
        db.session.add(inv)
    inv.delivery_method = method
    db.session.flush()
    return inv


def _attach_invitation_docs(msg, meeting, customer, fmt):
    """Hängt die formelle Einladung gemäß Format-Einstellung an die Mail: PDF via
    WeasyPrint (falls verfügbar), Word via python-docx (Fallback bzw. bei
    ``docx``). Gibt die Liste der angehängten Formate zurück."""
    attached = []
    base = _invitation_basename(meeting, customer)
    pdf_ok = False
    HTML = _weasyprint()
    if fmt in ("pdf", "both") and HTML is not None:
        try:
            pdf = HTML(string=documents.render_invitation_html(
                meeting, customer, meeting.agenda_items)).write_pdf()
            msg.attach(f"{base}.pdf", "application/pdf", pdf)
            pdf_ok = True
            attached.append("pdf")
        except Exception:
            current_app.logger.exception("Einladungs-PDF fehlgeschlagen")
    if fmt == "docx" or (fmt == "both" and not pdf_ok):
        data = documents.build_invitation_docx(
            meeting, customer, meeting.agenda_items, _type_label(meeting.meeting_type))
        msg.attach(f"{base}.docx", _DOCX_MIME, data)
        attached.append("docx")
    return attached


# ── Sitzungslisten ───────────────────────────────────────────────────────────

@bp.route("/board-meetings")
@login_required
def board_meetings():
    return _meetings_index(Meeting.TYPE_BOARD)


@bp.route("/assemblies")
@login_required
def assemblies():
    return _meetings_index(Meeting.TYPE_ASSEMBLY)


def _meetings_index(meeting_type):
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    year = request.args.get("year") or ""

    query = Meeting.query.filter(Meeting.meeting_type == meeting_type)
    if q:
        query = query.filter(Meeting.title.ilike(f"%{q}%"))
    if status in Meeting.STATUSES:
        query = query.filter(Meeting.status == status)
    if year.isdigit():
        y = int(year)
        query = query.filter(Meeting.meeting_date >= date(y, 1, 1),
                             Meeting.meeting_date <= date(y, 12, 31))
    meetings = query.order_by(
        Meeting.meeting_date.is_(None),       # Termine ohne Datum ans Ende
        Meeting.meeting_date.desc(), Meeting.id.desc(),
    ).all()

    all_dates = (db.session.query(Meeting.meeting_date)
                 .filter(Meeting.meeting_type == meeting_type,
                         Meeting.meeting_date.isnot(None)).all())
    years = sorted({d[0].year for d in all_dates}, reverse=True)

    ctx = dict(meetings=meetings, meeting_type=meeting_type,
               q=q, status_filter=status, year_filter=year, years=years)
    if request.headers.get("HX-Request"):
        return render_template("schriftfuehrung/_table.html", **ctx)
    return render_template("schriftfuehrung/index.html", **ctx)


# ── Sitzung CRUD ─────────────────────────────────────────────────────────────

@bp.route("/meetings/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):
    meeting = _get_meeting(meeting_id)
    return render_template("schriftfuehrung/meeting_detail.html", meeting=meeting)


def _apply_meeting_form(meeting, form):
    meeting.title = (form.get("title") or "").strip()
    meeting.meeting_date = _parse_date(form.get("meeting_date"))
    meeting.start_time = _parse_time(form.get("start_time"))
    meeting.end_time = _parse_time(form.get("end_time"))
    meeting.location = (form.get("location") or "").strip() or None
    meeting.invitation_heading = (form.get("invitation_heading") or "").strip() or None
    meeting.intro_text = sanitize_rich_text(form.get("intro_text") or "")
    meeting.closing_text = sanitize_rich_text(form.get("closing_text") or "")


@bp.route("/meetings/new", methods=["POST"])
@login_required
def meeting_new():
    meeting_type = request.form.get("meeting_type")
    if meeting_type not in Meeting.TYPES:
        abort(400)
    if not (request.form.get("title") or "").strip():
        flash("Bitte einen Titel angeben.", "danger")
        return redirect(url_for(_list_endpoint(meeting_type)))
    meeting = Meeting(meeting_type=meeting_type, status=Meeting.STATUS_PLANNING,
                      created_by_id=current_user.id)
    _apply_meeting_form(meeting, request.form)
    db.session.add(meeting)
    db.session.commit()
    flash(f"{_type_label(meeting_type)} „{meeting.title}“ angelegt.", "success")
    return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=meeting.id))


@bp.route("/meetings/<int:meeting_id>/edit", methods=["POST"])
@login_required
def meeting_edit(meeting_id):
    meeting = _get_meeting(meeting_id)
    if not (request.form.get("title") or "").strip():
        flash("Bitte einen Titel angeben.", "danger")
        return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=meeting.id))
    _apply_meeting_form(meeting, request.form)
    db.session.commit()
    flash("Sitzung gespeichert.", "success")
    return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=meeting.id))


@bp.route("/meetings/<int:meeting_id>/delete", methods=["POST"])
@login_required
def meeting_delete(meeting_id):
    meeting = _get_meeting(meeting_id)
    if not meeting.can_delete:
        flash("Diese Sitzung wurde bereits versendet und kann nicht gelöscht werden.", "danger")
        return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=meeting.id))
    meeting_type = meeting.meeting_type
    db.session.delete(meeting)
    db.session.commit()
    flash("Sitzung gelöscht.", "success")
    return redirect(url_for(_list_endpoint(meeting_type)))


@bp.route("/meetings/<int:meeting_id>/copy", methods=["POST"])
@login_required
def meeting_copy(meeting_id):
    src = _get_meeting(meeting_id)
    copy = Meeting(
        meeting_type=src.meeting_type,
        title=f"{src.title} (Kopie)",
        location=src.location,
        invitation_heading=src.invitation_heading,
        intro_text=src.intro_text,
        closing_text=src.closing_text,
        status=Meeting.STATUS_PLANNING,
        created_by_id=current_user.id,
    )
    db.session.add(copy)
    db.session.flush()
    for item in src.agenda_items:
        db.session.add(MeetingAgendaItem(
            meeting_id=copy.id, position=item.position, title=item.title,
            description=item.description, requires_vote=item.requires_vote,
        ))
    db.session.commit()
    flash("Sitzung inklusive Tagesordnung kopiert.", "success")
    return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=copy.id))


@bp.route("/meetings/<int:meeting_id>/set-held", methods=["POST"])
@login_required
def meeting_set_held(meeting_id):
    meeting = _get_meeting(meeting_id)
    meeting.status = Meeting.STATUS_HELD
    db.session.commit()
    flash("Sitzung als abgehalten markiert.", "success")
    return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=meeting.id))


@bp.route("/meetings/<int:meeting_id>/agenda", methods=["POST"])
@login_required
def agenda_save(meeting_id):
    meeting = _get_meeting(meeting_id)
    rows = services.parse_agenda_rows(request.form)
    # Bestehende Resolutions-Verknüpfungen lösen, dann TOPs neu aufbauen.
    for res in meeting.resolutions:
        res.agenda_item_id = None
    for item in list(meeting.agenda_items):
        db.session.delete(item)
    db.session.flush()
    for i, row in enumerate(rows):
        db.session.add(MeetingAgendaItem(
            meeting_id=meeting.id, position=i, title=row["title"][:300],
            description=row["description"], requires_vote=row["requires_vote"],
        ))
    db.session.commit()
    flash("Tagesordnung gespeichert.", "success")
    return redirect(url_for("schriftfuehrung.meeting_detail", meeting_id=meeting.id))


# ── Einladungs-Versand ───────────────────────────────────────────────────────

@bp.route("/meetings/<int:meeting_id>/send")
@login_required
def send(meeting_id):
    meeting = _get_meeting(meeting_id)
    contacts = services.all_contacts()
    existing = {inv.customer_id: inv for inv in meeting.invitations}
    if existing:
        preselected = set(existing.keys())
    else:
        preselected = services.preselect_recipient_ids(meeting.meeting_type)
    rows = []
    for c in contacts:
        inv = existing.get(c.id)
        method = (inv.delivery_method if inv else
                  (MeetingInvitation.METHOD_EMAIL if c.wants_email else MeetingInvitation.METHOD_POST))
        rows.append({
            "customer": c,
            "selected": c.id in preselected,
            "functions": services.customer_function_labels(c),
            "wants_email": c.wants_email,
            "invitation": inv,
            "method": method,
        })
    return render_template("schriftfuehrung/send.html", meeting=meeting, rows=rows)


def _read_selection(form):
    """(set selected_ids, dict customer_id -> method) aus dem Versand-Formular."""
    selected = set(form.getlist("recipient_ids", type=int))
    methods = {}
    for cid in selected:
        m = form.get(f"method_{cid}")
        methods[cid] = m if m in (MeetingInvitation.METHOD_EMAIL,
                                  MeetingInvitation.METHOD_POST) else MeetingInvitation.METHOD_NONE
    return selected, methods


def _sync_invitations(meeting, selected_ids, methods):
    """Legt/aktualisiert Invitations für die Auswahl an; entfernt abgewählte
    ohne Versand-History. Gibt die Invitations der Auswahl zurück."""
    existing = {inv.customer_id: inv for inv in meeting.invitations}
    for cid, inv in list(existing.items()):
        if cid not in selected_ids and not inv.email_sent_at and not inv.post_sent_at:
            db.session.delete(inv)
    result = []
    for cid in selected_ids:
        inv = existing.get(cid)
        if inv is None:
            inv = MeetingInvitation(meeting_id=meeting.id, customer_id=cid)
            db.session.add(inv)
        inv.delivery_method = methods.get(cid)
        result.append(inv)
    db.session.flush()
    return result


def _agenda_description(meeting):
    items = meeting.agenda_items
    if not items:
        return ""
    return "Tagesordnung:\n" + "\n".join(
        f"{i}. {it.title}" for i, it in enumerate(items, start=1))


def _invitation_email_body(meeting, customer, type_label):
    wg = wg_settings()
    lines = ["Sehr geehrte Damen und Herren,", ""]
    if customer:
        lines[0] = f"{customer.salutation_line},"
    lines.append(f"wir laden Sie herzlich zur {type_label} ein.")
    lines.append("")
    when = _meeting_when(meeting)
    if when:
        lines.append(f"Termin: {when}")
    if meeting.location:
        lines.append(f"Ort: {meeting.location}")
    agenda = _agenda_description(meeting)
    if agenda:
        lines += ["", agenda]
    lines += ["", "Die formelle Einladung finden Sie im Anhang.", "",
              "Mit freundlichen Grüßen", wg.get("name") or ""]
    return "\n".join(lines)


@bp.route("/meetings/<int:meeting_id>/invitations/email", methods=["POST"])
@login_required
def invitations_email(meeting_id):
    from flask_mail import Message

    meeting = _get_meeting(meeting_id)
    selected_ids, methods = _read_selection(request.form)
    invitations = _sync_invitations(meeting, selected_ids, methods)

    type_label = _type_label(meeting.meeting_type)
    fmt = _doc_format()
    ics_bytes = ical.build_meeting_ics(meeting, description=_agenda_description(meeting))

    sent, skipped, failed = 0, 0, 0
    for inv in invitations:
        if inv.delivery_method != MeetingInvitation.METHOD_EMAIL:
            continue
        customer = inv.customer
        if not customer or not customer.wants_email:
            skipped += 1
            continue
        was_sent = inv.email_sent_at is not None
        msg = Message(
            subject=f"Einladung zur {type_label}"
                    + (f" am {meeting.meeting_date.strftime('%d.%m.%Y')}" if meeting.meeting_date else ""),
            recipients=[customer.email],
            body=_invitation_email_body(meeting, customer, type_label),
        )
        _attach_invitation_docs(msg, meeting, customer, fmt)
        if ics_bytes:
            msg.attach("Termin.ics", "text/calendar", ics_bytes)
        run_before_send(customer, msg)
        try:
            send_mail(msg)
        except Exception as exc:
            failed += 1
            current_app.logger.warning("Einladungs-Mail fehlgeschlagen: %s", exc)
            continue
        record_email_sent(inv, customer.email, None)
        db.session.add(MeetingDeliveryLog(
            meeting_id=meeting.id, customer_id=customer.id,
            recipient_name=customer.name, recipient_email=customer.email,
            method=MeetingDeliveryLog.METHOD_EMAIL,
            action=MeetingDeliveryLog.ACTION_RESENT if was_sent else MeetingDeliveryLog.ACTION_SENT,
            user_id=current_user.id,
        ))
        sent += 1

    if meeting.status == Meeting.STATUS_PLANNING and (sent or failed):
        meeting.status = Meeting.STATUS_INVITED
    db.session.commit()

    if sent:
        msg_txt = f"{sent} Einladung(en) per E-Mail versendet."
        if skipped:
            msg_txt += f" {skipped} ohne E-Mail-Freigabe übersprungen (bitte per Post)."
        if failed:
            msg_txt += f" {failed} fehlgeschlagen."
        flash(msg_txt, "success")
    else:
        flash("Keine E-Mail versendet — keine ausgewählten Empfänger mit E-Mail-Freigabe.", "warning")
    return redirect(url_for("schriftfuehrung.history", meeting_id=meeting.id))


@bp.route("/meetings/<int:meeting_id>/invitations/email-ajax", methods=["POST"])
@login_required
def invitations_email_ajax(meeting_id):
    """JSON-Variante für den seriellen Einladungs-Mailversand (ein Empfänger pro
    Request, mit Fortschritt im Bestätigungs-Modal — analog zum Rechnungs-
    Massenmailing). ``test_mode=1`` schickt an die eigene Adresse und ändert
    nichts an der DB."""
    from flask import jsonify
    from flask_mail import Message

    meeting = _get_meeting(meeting_id)
    test_mode = request.form.get("test_mode") == "1"
    customer = db.session.get(Customer, request.form.get("customer_id", type=int))
    if customer is None:
        return jsonify({"ok": False, "error": "Empfänger nicht gefunden"}), 400

    if test_mode:
        recipient = current_user.email
        if not recipient:
            return jsonify({"ok": False, "error": "Keine eigene E-Mail-Adresse für den Testmodus"}), 400
    else:
        if not customer.wants_email:
            return jsonify({"ok": False, "error": "E-Mail-Versand nicht freigegeben"}), 400
        recipient = customer.email

    type_label = _type_label(meeting.meeting_type)
    subject = f"Einladung zur {type_label}" + (
        f" am {meeting.meeting_date.strftime('%d.%m.%Y')}" if meeting.meeting_date else "")
    body = _invitation_email_body(meeting, customer, type_label)
    if test_mode:
        subject = f"[TEST – an: {customer.email or '—'}] {subject}"
        body = f"[TESTMODUS – eigentlicher Empfänger: {customer.email or '—'}]\n\n{body}"

    try:
        msg = Message(subject=subject, recipients=[recipient], body=body)
        _attach_invitation_docs(msg, meeting, customer, _doc_format())
        ics = ical.build_meeting_ics(meeting, description=_agenda_description(meeting))
        if ics:
            msg.attach("Termin.ics", "text/calendar", ics)
        run_before_send(customer, msg)
        send_mail(msg)

        if not test_mode:
            inv = _upsert_invitation(meeting, customer, MeetingInvitation.METHOD_EMAIL)
            was_sent = inv.email_sent_at is not None
            record_email_sent(inv, recipient, None)
            db.session.add(MeetingDeliveryLog(
                meeting_id=meeting.id, customer_id=customer.id,
                recipient_name=customer.name, recipient_email=recipient,
                method=MeetingDeliveryLog.METHOD_EMAIL,
                action=MeetingDeliveryLog.ACTION_RESENT if was_sent else MeetingDeliveryLog.ACTION_SENT,
                user_id=current_user.id,
            ))
            if meeting.status == Meeting.STATUS_PLANNING:
                meeting.status = Meeting.STATUS_INVITED
            db.session.commit()

        return jsonify({"ok": True, "name": customer.name, "email": recipient, "test_mode": test_mode})
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning("Einladungs-Mail (ajax) fehlgeschlagen: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


def _merged_invitation_pdf(meeting, customers):
    """Gemergtes Einladungs-PDF (eine Seite je Empfänger) als bytes, oder None,
    wenn WeasyPrint fehlt."""
    HTML = _weasyprint()
    if HTML is None:
        return None
    from pypdf import PdfWriter
    writer = PdfWriter()
    agenda_items = meeting.agenda_items
    for c in customers:
        pdf = HTML(string=documents.render_invitation_html(meeting, c, agenda_items)).write_pdf()
        writer.append(io.BytesIO(pdf))
    writer.compress_identical_objects()
    buf = io.BytesIO()
    writer.write(buf)
    writer.close()
    buf.seek(0)
    return buf.read()


def _merged_invitation_docx(meeting, customers):
    from app.invoices.document_service import merge_docx_files
    type_label = _type_label(meeting.meeting_type)
    agenda_items = meeting.agenda_items
    sources = [documents.build_invitation_docx(meeting, c, agenda_items, type_label)
               for c in customers]
    if not sources:
        return None
    return merge_docx_files(sources)


@bp.route("/meetings/<int:meeting_id>/invitations/print", methods=["POST"])
@login_required
def invitations_print(meeting_id):
    meeting = _get_meeting(meeting_id)
    selected_ids, methods = _read_selection(request.form)
    invitations = _sync_invitations(meeting, selected_ids, methods)
    # „Ausgewählte per Post senden" druckt nur die Post-Empfänger; der
    # „Alle Einladungen drucken"-Button (ohne Flag) druckt die ganze Auswahl.
    post_only = request.form.get("post_only") == "1"
    if post_only:
        invitations = [inv for inv in invitations
                       if inv.delivery_method == MeetingInvitation.METHOD_POST]
    customers = [inv.customer for inv in invitations if inv.customer]
    if not customers:
        flash("Keine Empfänger mit Versandart „Post“ ausgewählt." if post_only
              else "Keine Empfänger ausgewählt.", "warning")
        return redirect(url_for("schriftfuehrung.send", meeting_id=meeting.id))

    pdf_bytes = _merged_invitation_pdf(meeting, customers)
    if pdf_bytes is None:
        flash("WeasyPrint ist nicht installiert — Druck-PDF nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("schriftfuehrung.send", meeting_id=meeting.id))

    now = datetime.utcnow()
    for inv in invitations:
        if inv.delivery_method == MeetingInvitation.METHOD_POST:
            inv.post_sent_at = now
        db.session.add(MeetingDeliveryLog(
            meeting_id=meeting.id, customer_id=inv.customer_id,
            recipient_name=inv.customer.name if inv.customer else None,
            recipient_email=inv.customer.email if inv.customer else None,
            method=MeetingDeliveryLog.METHOD_POST,
            action=MeetingDeliveryLog.ACTION_PRINTED,
            user_id=current_user.id,
        ))
    if meeting.status == Meeting.STATUS_PLANNING:
        meeting.status = Meeting.STATUS_INVITED
    db.session.commit()

    return send_file(io.BytesIO(pdf_bytes), as_attachment=True,
                     download_name=f"{_invitation_basename(meeting)}.pdf",
                     mimetype="application/pdf")


@bp.route("/meetings/<int:meeting_id>/invitations/download")
@login_required
def invitations_download(meeting_id):
    """Massen-Download aller aktuellen Einladungen (auch nachträglich), PDF oder
    Word."""
    meeting = _get_meeting(meeting_id)
    fmt = request.args.get("fmt", "pdf")
    customers = [inv.customer for inv in meeting.invitations if inv.customer]
    if not customers:
        flash("Es wurden noch keine Empfänger ausgewählt.", "warning")
        return redirect(url_for("schriftfuehrung.send", meeting_id=meeting.id))

    if fmt == "docx":
        data = _merged_invitation_docx(meeting, customers)
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name=f"{_invitation_basename(meeting)}.docx",
                         mimetype=_DOCX_MIME)

    pdf_bytes = _merged_invitation_pdf(meeting, customers)
    if pdf_bytes is None:
        flash("WeasyPrint ist nicht installiert — PDF nur im Docker-Container verfügbar.", "danger")
        return redirect(url_for("schriftfuehrung.history", meeting_id=meeting.id))
    return send_file(io.BytesIO(pdf_bytes), as_attachment=True,
                     download_name=f"{_invitation_basename(meeting)}.pdf",
                     mimetype="application/pdf")


@bp.route("/meetings/<int:meeting_id>/invitation-preview")
@login_required
def invitation_preview(meeting_id):
    """Einzelne Einladung als PDF oder Word (Vorschau/Druck). ``customer_id``
    optional (sonst Platzhalter-Empfänger), ``fmt`` = pdf|docx. PDF ohne
    WeasyPrint -> HTML-Fallback."""
    meeting = _get_meeting(meeting_id)
    fmt = request.args.get("fmt", "pdf")
    customer_id = request.args.get("customer_id", type=int)
    customer = db.session.get(Customer, customer_id) if customer_id else None
    if customer is None:
        # Vorschau mit Platzhalter-Empfänger
        customer = Customer(name="Mustermitglied", strasse="Musterstraße", hausnummer="1",
                            plz="0000", ort="Musterort")

    if fmt == "docx":
        data = documents.build_invitation_docx(
            meeting, customer, meeting.agenda_items, _type_label(meeting.meeting_type))
        return send_file(io.BytesIO(data), as_attachment=True,
                         download_name=f"{_invitation_basename(meeting, customer)}.docx",
                         mimetype=_DOCX_MIME)

    html = documents.render_invitation_html(meeting, customer, meeting.agenda_items)
    HTML = _weasyprint()
    if HTML is None:
        return html  # HTML-Vorschau (lokal ohne WeasyPrint)
    pdf = HTML(string=html).write_pdf()
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     download_name=f"{_invitation_basename(meeting, customer)}.pdf")


@bp.route("/meetings/<int:meeting_id>/history")
@login_required
def history(meeting_id):
    meeting = _get_meeting(meeting_id)
    logs = (MeetingDeliveryLog.query
            .filter(MeetingDeliveryLog.meeting_id == meeting.id)
            .order_by(MeetingDeliveryLog.occurred_at.desc(), MeetingDeliveryLog.id.desc())
            .all())
    return render_template("schriftfuehrung/history.html", meeting=meeting, logs=logs)


# ── Protokoll ────────────────────────────────────────────────────────────────

def _get_or_create_protocol(meeting):
    protocol = meeting.protocol
    if protocol is None:
        protocol = MeetingProtocol(meeting_id=meeting.id,
                                   source_type=MeetingProtocol.SOURCE_RICHTEXT,
                                   status=MeetingProtocol.STATUS_DRAFT,
                                   created_by_id=current_user.id)
        db.session.add(protocol)
        services.prefill_protocol(meeting)
        if meeting.status != Meeting.STATUS_HELD:
            meeting.status = Meeting.STATUS_HELD
        db.session.commit()
    return protocol


@bp.route("/meetings/<int:meeting_id>/protocol")
@login_required
def protocol(meeting_id):
    meeting = _get_meeting(meeting_id)
    protocol = _get_or_create_protocol(meeting)
    present, total, is_quorate = services.compute_quorum(meeting)
    attendances = sorted(meeting.attendances,
                         key=lambda a: (a.customer.name if a.customer else ""))
    resolutions = sorted(meeting.resolutions, key=lambda r: r.id)
    # Vorbelegung für Freitext/Kopfzahl: Anwesende laut Personenliste.
    default_present = services.list_present_count(meeting)
    return render_template(
        "schriftfuehrung/protocol.html", meeting=meeting, protocol=protocol,
        attendances=attendances, resolutions=resolutions,
        quorum=dict(present=present, total=total, is_quorate=is_quorate),
        default_present=default_present,
        default_freetext=f"{default_present} Personen anwesend.",
    )


def _save_attendances(meeting, form):
    for att in meeting.attendances:
        status = form.get(f"attendance_{att.customer_id}_status")
        if status in MeetingAttendance.STATUSES:
            att.status = status
        att.is_member = form.get(f"attendance_{att.customer_id}_member") in ("1", "on", "true")


def _int_or_none(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _save_protocol_attendance(meeting, protocol, form):
    """Liest die Anwesenheits-Metadaten (Freitext-Modus, Kopfzahl, Wartefrist)
    aus dem Protokoll-Formular und legt sie auf das Protokoll."""
    freetext_mode = form.get("attendance_freetext_mode") in ("1", "on", "true")
    protocol.attendance_mode = (MeetingProtocol.ATTENDANCE_FREETEXT if freetext_mode
                                else MeetingProtocol.ATTENDANCE_LIST)

    # Wartefrist/Wiedereröffnung nur bei der Hauptversammlung.
    if meeting.is_assembly:
        protocol.reconvened = form.get("reconvened") in ("1", "on", "true")
        protocol.reconvene_wait_minutes = _int_or_none(form.get("reconvene_wait_minutes"))
    else:
        protocol.reconvened = False
        protocol.reconvene_wait_minutes = None

    # Kopfzahl: im Wartefrist-Fall aus dem dortigen Feld, sonst aus dem Freitext-Feld.
    headcount = _int_or_none(form.get("present_headcount"))
    if protocol.reconvened:
        rc = _int_or_none(form.get("reconvene_headcount"))
        if rc is not None:
            headcount = rc
    protocol.present_headcount = headcount

    freetext = (form.get("attendance_freetext") or "").strip()
    if freetext_mode and not freetext:
        freetext = f"{headcount if headcount is not None else 0} Personen anwesend."
    protocol.attendance_freetext = freetext or None


def _save_resolutions(meeting, form):
    """Aktualisiert bestehende Beschlüsse + legt manuell ergänzte (id leer) an."""
    existing = {r.id: r for r in meeting.resolutions}
    seen = set()
    import re
    idx = set()
    pat = re.compile(r"^resolution\[([^\]]+)\]\[")
    for key in form.keys():
        m = pat.match(key)
        if m:
            idx.add(m.group(1))

    def _int(v):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    for key in idx:
        title = (form.get(f"resolution[{key}][title]") or "").strip()
        if not title:
            continue
        status = form.get(f"resolution[{key}][status]")
        status = status if status in MeetingResolution.STATUSES else MeetingResolution.STATUS_ACCEPTED
        votes_for = _int(form.get(f"resolution[{key}][votes_for]"))
        votes_against = _int(form.get(f"resolution[{key}][votes_against]"))
        votes_abstain = _int(form.get(f"resolution[{key}][votes_abstain]"))
        notes = (form.get(f"resolution[{key}][notes]") or "").strip() or None

        res = existing.get(int(key)) if key.isdigit() else None
        if res is None:
            res = MeetingResolution(meeting_id=meeting.id,
                                    decided_on=meeting.meeting_date or date.today(),
                                    created_by_id=current_user.id)
            db.session.add(res)
        else:
            seen.add(res.id)
        res.title = title[:300]
        res.status = status
        res.votes_for = votes_for
        res.votes_against = votes_against
        res.votes_abstain = votes_abstain
        res.notes = notes

    # Vorbelegte, aber im Formular gelöschte Beschlüsse entfernen.
    for rid, res in existing.items():
        if rid not in seen and str(rid) not in idx:
            db.session.delete(res)


@bp.route("/meetings/<int:meeting_id>/protocol/save", methods=["POST"])
@login_required
def protocol_save(meeting_id):
    meeting = _get_meeting(meeting_id)
    protocol = _get_or_create_protocol(meeting)
    if protocol.is_locked:
        flash("Das Protokoll ist abgeschlossen und kann nicht mehr bearbeitet werden.", "danger")
        return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))

    protocol.content_html = sanitize_rich_text(request.form.get("content_html") or "")
    _save_attendances(meeting, request.form)
    _save_protocol_attendance(meeting, protocol, request.form)
    _save_resolutions(meeting, request.form)
    present, total, is_quorate = services.compute_quorum(meeting)
    protocol.quorum_present, protocol.quorum_total, protocol.is_quorate = present, total, is_quorate
    db.session.commit()
    flash("Protokoll gespeichert (Entwurf).", "success")
    return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))


def _write_protocol_pdf(meeting, protocol):
    """Erzeugt das Protokoll-PDF im Schriftverkehr-Archiv (Jahr-Unterordner) und
    setzt file_path/Meta. Gibt True zurück bei Erfolg, False ohne WeasyPrint."""
    HTML = _weasyprint()
    if HTML is None:
        return False
    present, total, is_quorate = services.compute_quorum(meeting)
    html = documents.render_protocol_html(
        meeting, protocol,
        sorted(meeting.attendances, key=lambda a: (a.customer.name if a.customer else "")),
        sorted(meeting.resolutions, key=lambda r: r.id),
        dict(present=present, total=total, is_quorate=is_quorate),
    )
    year = (meeting.meeting_date or date.today()).year
    base = f"Protokoll_{storage.slugify_filename(meeting.title)}_" \
           f"{(meeting.meeting_date or date.today()).strftime('%Y-%m-%d')}"
    path = storage.versioned_path(storage.get_schriftverkehr_dir(year), base, "pdf")
    HTML(string=html).write_pdf(path)
    protocol.file_path = path
    protocol.original_filename = os.path.basename(path)
    protocol.mime_type = "application/pdf"
    try:
        protocol.file_size = os.path.getsize(path)
    except OSError:
        protocol.file_size = None
    return True


@bp.route("/meetings/<int:meeting_id>/protocol/finalize", methods=["POST"])
@login_required
def protocol_finalize(meeting_id):
    meeting = _get_meeting(meeting_id)
    protocol = _get_or_create_protocol(meeting)
    if protocol.is_locked:
        flash("Das Protokoll ist bereits abgeschlossen.", "info")
        return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))

    protocol.content_html = sanitize_rich_text(request.form.get("content_html") or protocol.content_html or "")
    _save_attendances(meeting, request.form)
    _save_protocol_attendance(meeting, protocol, request.form)
    _save_resolutions(meeting, request.form)
    present, total, is_quorate = services.compute_quorum(meeting)
    protocol.quorum_present, protocol.quorum_total, protocol.is_quorate = present, total, is_quorate

    protocol.status = MeetingProtocol.STATUS_FINAL
    protocol.finalized_at = datetime.utcnow()
    db.session.flush()
    pdf_ok = _write_protocol_pdf(meeting, protocol)
    db.session.commit()
    if pdf_ok:
        flash("Protokoll abgeschlossen und im Schriftverkehr abgelegt.", "success")
    else:
        flash("Protokoll abgeschlossen. PDF-Ablage nur im Docker-Container (WeasyPrint).", "warning")
    return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))


@bp.route("/meetings/<int:meeting_id>/protocol/upload", methods=["POST"])
@login_required
def protocol_upload(meeting_id):
    meeting = _get_meeting(meeting_id)
    protocol = _get_or_create_protocol(meeting)
    if protocol.is_locked:
        flash("Es ist bereits ein abgeschlossenes Protokoll vorhanden.", "danger")
        return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))

    file = request.files.get("document")
    error = _validate_upload(file)
    if error:
        flash(error, "danger")
        return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))

    data = file.read()
    if len(data) > constants.MAX_UPLOAD_BYTES:
        flash("Die Datei ist größer als 5 MB.", "danger")
        return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))

    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    year = (meeting.meeting_date or date.today()).year
    base = f"Protokoll_{storage.slugify_filename(meeting.title)}_" \
           f"{(meeting.meeting_date or date.today()).strftime('%Y-%m-%d')}"
    path = storage.versioned_path(storage.get_schriftverkehr_dir(year), base, ext.lstrip("."))
    with open(path, "wb") as fh:
        fh.write(data)

    protocol.source_type = MeetingProtocol.SOURCE_UPLOAD
    protocol.status = MeetingProtocol.STATUS_FINAL
    protocol.finalized_at = datetime.utcnow()
    protocol.file_path = path
    protocol.original_filename = file.filename
    protocol.mime_type = file.mimetype or None
    protocol.file_size = len(data)
    present, total, is_quorate = services.compute_quorum(meeting)
    protocol.quorum_present, protocol.quorum_total, protocol.is_quorate = present, total, is_quorate
    db.session.commit()
    flash("Protokoll hochgeladen und abgeschlossen.", "success")
    return redirect(url_for("schriftfuehrung.protocol", meeting_id=meeting.id))


@bp.route("/meetings/<int:meeting_id>/protocol/download")
@login_required
def protocol_download(meeting_id):
    meeting = _get_meeting(meeting_id)
    protocol = meeting.protocol
    if protocol is None:
        abort(404)
    if protocol.file_path and os.path.exists(protocol.file_path):
        return send_file(protocol.file_path, as_attachment=True,
                         download_name=protocol.original_filename or os.path.basename(protocol.file_path))
    # Kein File (z.B. lokal ohne WeasyPrint, Rich-Text-Protokoll) → HTML-Ansicht.
    if protocol.source_type == MeetingProtocol.SOURCE_RICHTEXT:
        present, total, is_quorate = services.compute_quorum(meeting)
        return documents.render_protocol_html(
            meeting, protocol,
            sorted(meeting.attendances, key=lambda a: (a.customer.name if a.customer else "")),
            sorted(meeting.resolutions, key=lambda r: r.id),
            dict(present=present, total=total, is_quorate=is_quorate),
        )
    abort(404)


# ── Beschluss-Register ───────────────────────────────────────────────────────

@bp.route("/resolutions")
@login_required
def resolutions():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    mtype = request.args.get("type") or ""
    year = request.args.get("year") or ""

    query = MeetingResolution.query.join(Meeting, MeetingResolution.meeting_id == Meeting.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(MeetingResolution.title.ilike(like),
                                 MeetingResolution.notes.ilike(like)))
    if status in MeetingResolution.STATUSES:
        query = query.filter(MeetingResolution.status == status)
    if mtype in Meeting.TYPES:
        query = query.filter(Meeting.meeting_type == mtype)
    if year.isdigit():
        y = int(year)
        query = query.filter(MeetingResolution.decided_on >= date(y, 1, 1),
                             MeetingResolution.decided_on <= date(y, 12, 31))
    items = query.order_by(MeetingResolution.decided_on.is_(None),
                           MeetingResolution.decided_on.desc(),
                           MeetingResolution.id.desc()).all()

    all_dates = (db.session.query(MeetingResolution.decided_on)
                 .filter(MeetingResolution.decided_on.isnot(None)).all())
    years = sorted({d[0].year for d in all_dates}, reverse=True)

    ctx = dict(items=items, q=q, status_filter=status, type_filter=mtype,
               year_filter=year, years=years)
    if request.headers.get("HX-Request"):
        return render_template("schriftfuehrung/_resolutions_table.html", **ctx)
    return render_template("schriftfuehrung/resolutions.html", **ctx)


# ── Schriftverkehr-Archiv ────────────────────────────────────────────────────

def _validate_upload(file):
    if not file or not file.filename:
        return "Bitte eine Datei auswählen."
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in constants.ALLOWED_UPLOAD_EXTENSIONS:
        return f"Dateityp nicht erlaubt. Erlaubt: {constants.ALLOWED_UPLOAD_HINT}."
    if request.content_length and request.content_length > constants.MAX_UPLOAD_BYTES + 1024 * 1024:
        return "Die Datei ist größer als 5 MB."
    return None


@bp.route("/archive")
@login_required
def archive():
    year = request.args.get("year") or ""
    dtype = request.args.get("type") or ""
    yr = int(year) if year.isdigit() else None

    show_docs = dtype != "protocol"
    show_protocols = dtype in ("", "protocol")

    docs = []
    if show_docs:
        docs_q = SchriftverkehrDocument.query
        if yr:
            docs_q = docs_q.filter(SchriftverkehrDocument.year == yr)
        if dtype in SchriftverkehrDocument.TYPES:
            docs_q = docs_q.filter(SchriftverkehrDocument.doc_type == dtype)
        docs = docs_q.order_by(SchriftverkehrDocument.document_date.is_(None),
                               SchriftverkehrDocument.document_date.desc(),
                               SchriftverkehrDocument.id.desc()).all()

    protocols = []
    if show_protocols:
        protocols = (MeetingProtocol.query
                     .join(Meeting, MeetingProtocol.meeting_id == Meeting.id)
                     .filter(MeetingProtocol.status == MeetingProtocol.STATUS_FINAL)
                     .order_by(MeetingProtocol.finalized_at.desc()).all())
        if yr:
            protocols = [p for p in protocols
                         if p.meeting.meeting_date and p.meeting.meeting_date.year == yr]

    all_years = set(d[0] for d in db.session.query(SchriftverkehrDocument.year).distinct())
    for p in (MeetingProtocol.query.join(Meeting)
              .filter(MeetingProtocol.status == MeetingProtocol.STATUS_FINAL).all()):
        if p.meeting.meeting_date:
            all_years.add(p.meeting.meeting_date.year)
    years = sorted(all_years, reverse=True)

    return render_template("schriftfuehrung/archive.html", docs=docs, protocols=protocols,
                           year_filter=year, type_filter=dtype, years=years)


@bp.route("/archive/upload", methods=["POST"])
@login_required
def archive_upload():
    file = request.files.get("document")
    error = _validate_upload(file)
    if error:
        flash(error, "danger")
        return redirect(url_for("schriftfuehrung.archive"))
    data = file.read()
    if len(data) > constants.MAX_UPLOAD_BYTES:
        flash("Die Datei ist größer als 5 MB.", "danger")
        return redirect(url_for("schriftfuehrung.archive"))

    title = (request.form.get("title") or "").strip() or file.filename
    doc_type = request.form.get("doc_type")
    if doc_type not in SchriftverkehrDocument.TYPES:
        doc_type = SchriftverkehrDocument.TYPE_OUTGOING
    doc_date = _parse_date(request.form.get("document_date"))
    note = (request.form.get("note") or "").strip() or None
    year = (doc_date or date.today()).year

    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    base = storage.slugify_filename(title)
    path = storage.versioned_path(storage.get_schriftverkehr_dir(year), base, ext.lstrip("."))
    with open(path, "wb") as fh:
        fh.write(data)

    db.session.add(SchriftverkehrDocument(
        year=year, title=title[:300], doc_type=doc_type, document_date=doc_date,
        file_path=path, original_filename=file.filename, mime_type=file.mimetype or None,
        file_size=len(data), note=note, created_by_id=current_user.id,
    ))
    db.session.commit()
    flash("Dokument im Schriftverkehr abgelegt.", "success")
    return redirect(url_for("schriftfuehrung.archive", year=year))


@bp.route("/archive/<int:doc_id>/download")
@login_required
def archive_download(doc_id):
    doc = db.get_or_404(SchriftverkehrDocument, doc_id)
    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("Datei nicht gefunden.", "danger")
        return redirect(url_for("schriftfuehrung.archive"))
    return send_file(doc.file_path, as_attachment=True,
                     download_name=doc.original_filename or os.path.basename(doc.file_path))
