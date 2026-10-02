"""Eingangsrechnungen (empfangene E-Rechnungen): Upload, Ansicht, Buchungsvorschlag.

Routen des ``accounting``-Blueprints (Recht ``buchhaltung``). Die Logik steckt in
``incoming_service.py``, der Parser in ``app/einvoice/incoming.py``.
"""
import io
from datetime import date
from decimal import Decimal

from flask import (
    abort, flash, make_response, redirect, render_template, request, send_file, url_for,
)
from flask_login import current_user, login_required

from app.accounting import bp
from app.accounting import incoming_service as svc
from app.einvoice import incoming
from app.extensions import db
from app.file_safety import safe_tenant_path
from app.models import Account, Customer, IncomingInvoice, Project, RealAccount
from app.pagination import paginate_query

_STATUS_FILTERS = {
    "open": IncomingInvoice.STATUS_NEW,
    "booked": IncomingInvoice.STATUS_BOOKED,
    "discarded": IncomingInvoice.STATUS_DISCARDED,
}


@bp.route("/incoming")
@login_required
def incoming_list():
    status = request.args.get("status", "open")
    if status not in _STATUS_FILTERS and status != "all":
        status = "open"
    query = IncomingInvoice.query
    if status in _STATUS_FILTERS:
        query = query.filter(IncomingInvoice.status == _STATUS_FILTERS[status])
    query = query.order_by(IncomingInvoice.created_at.desc(), IncomingInvoice.id.desc())
    pagination = paginate_query(query, page_key="incoming_invoices")
    counts = {key: IncomingInvoice.query.filter_by(status=value).count()
              for key, value in _STATUS_FILTERS.items()}
    return render_template("accounting/incoming_list.html", docs=pagination.items,
                           pagination=pagination, status=status, counts=counts)


@bp.route("/incoming/upload", methods=["POST"])
@login_required
def incoming_upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Bitte mindestens eine Datei wählen (XML oder ZUGFeRD-PDF).", "warning")
        return redirect(url_for("accounting.incoming_list"))
    created = []
    for upload in files:
        data = upload.read(svc.MAX_UPLOAD_BYTES + 1)
        try:
            doc = svc.store_upload(upload.filename, data, current_user.id)
        except svc.DuplicateUpload as dup:
            flash(f"„{upload.filename}“ ist schon erfasst (Beleg {dup.existing.number or dup.existing.id}).",
                  "warning")
            continue
        except incoming.IncomingError as exc:
            flash(f"„{upload.filename}“ konnte nicht gelesen werden: {exc}", "danger")
            continue
        db.session.commit()
        created.append(doc)
        for other in svc.possible_duplicates(doc):
            flash(f"Achtung: Rechnung {doc.number} von {doc.seller_name} gibt es schon "
                  f"(Beleg vom {other.created_at.strftime('%d.%m.%Y')}) — möglicherweise doppelt.", "warning")
    if created:
        flash(f"{len(created)} E-Rechnung(en) erfasst.", "success")
    if len(created) == 1:
        return redirect(url_for("accounting.incoming_detail", doc_id=created[0].id))
    return redirect(url_for("accounting.incoming_list"))


@bp.route("/incoming/<int:doc_id>")
@login_required
def incoming_detail(doc_id):
    doc = db.get_or_404(IncomingInvoice, doc_id)
    parsed = doc.parsed
    suggested = svc.suggest_supplier(parsed) if doc.status == IncomingInvoice.STATUS_NEW else doc.supplier
    rows, rows_error = [], None
    try:
        rows = svc.booking_rows(parsed)
    except svc.BookingError as exc:
        rows_error = str(exc)
    return render_template(
        "accounting/incoming_detail.html", doc=doc, inv=parsed, rows=rows, rows_error=rows_error,
        suggested=suggested,
        suppliers=Customer.query.filter(Customer.is_supplier.is_(True), Customer.active.is_(True))
        .order_by(Customer.name).all(),
        accounts=Account.query.filter_by(active=True).order_by(Account.name).all(),
        projects=Project.query.filter_by(closed=False).order_by(Project.name).all(),
        real_accounts=RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all(),
        default_real_account=RealAccount.query.filter_by(is_default=True, active=True).first(),
        booking_date=svc.default_booking_date(parsed), duplicates=svc.possible_duplicates(doc),
        file_exists=safe_tenant_path(doc.file_path) is not None,
        form=None)


def _int_or_none(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


@bp.route("/incoming/<int:doc_id>/book", methods=["POST"])
@login_required
def incoming_book(doc_id):
    doc = db.get_or_404(IncomingInvoice, doc_id)
    parsed = doc.parsed
    try:
        booking_date = date.fromisoformat(request.form.get("date", ""))
    except ValueError:
        flash("Ungültiges Buchungsdatum.", "danger")
        return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))
    try:
        supplier_raw = request.form.get("supplier_id", "")
        if supplier_raw == "new":
            supplier = svc.create_supplier(parsed)
        else:
            supplier = db.session.get(Customer, _int_or_none(supplier_raw)) if _int_or_none(supplier_raw) else None
            if supplier is not None and not supplier.is_supplier:
                supplier = None
        result = svc.book(
            doc, supplier=supplier, account_id=_int_or_none(request.form.get("account_id")),
            booking_date=booking_date, user_id=current_user.id,
            project_id=_int_or_none(request.form.get("project_id")),
            real_account_id=_int_or_none(request.form.get("real_account_id")))
        db.session.commit()
    except svc.BookingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))
    flash(f"Eingangsrechnung {parsed.number or ''} gebucht.", "success")
    if doc.booking_group_id:
        return redirect(url_for("accounting.booking_group_edit", group_id=result.id))
    return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))


@bp.route("/incoming/<int:doc_id>/discard", methods=["POST"])
@login_required
def incoming_discard(doc_id):
    """Beleg verwerfen (z. B. Dublette). Die Originaldatei bleibt aufbewahrt."""
    doc = db.get_or_404(IncomingInvoice, doc_id)
    if doc.status != IncomingInvoice.STATUS_NEW:
        flash("Nur ein offener Beleg lässt sich verwerfen.", "warning")
    else:
        doc.status = IncomingInvoice.STATUS_DISCARDED
        db.session.commit()
        flash("Beleg verworfen. Die Originaldatei bleibt aufbewahrt.", "info")
    return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))


@bp.route("/incoming/<int:doc_id>/reopen", methods=["POST"])
@login_required
def incoming_reopen(doc_id):
    doc = db.get_or_404(IncomingInvoice, doc_id)
    if doc.status == IncomingInvoice.STATUS_DISCARDED:
        doc.status = IncomingInvoice.STATUS_NEW
        db.session.commit()
        flash("Beleg wieder geöffnet.", "success")
    return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))


def _original(doc):
    """Pfad des Originals — nur aus dem Dateibaum des Mandanten (app/file_safety.py)."""
    path = safe_tenant_path(doc.file_path)
    if path is None:
        flash("Die Originaldatei ist nicht mehr vorhanden.", "danger")
    return path


@bp.route("/incoming/<int:doc_id>/file")
@login_required
def incoming_file(doc_id):
    """Das unveränderte Original (PDF inline, XML als Download)."""
    doc = db.get_or_404(IncomingInvoice, doc_id)
    path = _original(doc)
    if path is None:
        return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))
    is_pdf = doc.source_kind == incoming.SOURCE_PDF
    return send_file(path, as_attachment=not is_pdf, download_name=doc.original_name,
                     mimetype="application/pdf" if is_pdf else "application/xml")


@bp.route("/incoming/<int:doc_id>/xml")
@login_required
def incoming_xml(doc_id):
    """Das Rechnungs-XML — bei einem ZUGFeRD-PDF der eingebettete Anhang, sonst das Original."""
    doc = db.get_or_404(IncomingInvoice, doc_id)
    path = _original(doc)
    if path is None:
        return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))
    with open(path, "rb") as fh:
        try:
            xml = incoming.parse(fh.read(), doc.original_name).xml
        except incoming.IncomingError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))
    resp = make_response(xml)
    resp.headers["Content-Type"] = "application/xml; charset=utf-8"
    number = (doc.number or str(doc.id)).replace("/", "-")
    resp.headers["Content-Disposition"] = f'attachment; filename="{number}.xml"'
    return resp


@bp.route("/incoming/<int:doc_id>/attachment/<int:index>")
@login_required
def incoming_attachment(doc_id, index):
    """Ein in die Rechnung eingebetteter Anhang (BG-24), z. B. die PDF-Ansicht einer XRechnung."""
    doc = db.get_or_404(IncomingInvoice, doc_id)
    path = _original(doc)
    if path is None:
        return redirect(url_for("accounting.incoming_detail", doc_id=doc.id))
    with open(path, "rb") as fh:
        try:
            xml = incoming.parse(fh.read(), doc.original_name).xml
        except incoming.IncomingError:
            abort(404)
    found = incoming.attachment_bytes(xml, index)
    if found is None:
        abort(404)
    filename, mime, content = found
    # Fremde Anhaenge nur als Download — ausser ein echtes PDF (der Inhalt muss so beginnen,
    # die vom Absender angegebene Art allein genuegt nicht): das zeigt der Browser an.
    inline = mime == "application/pdf" and content.lstrip()[:5] == b"%PDF-"
    resp = send_file(io.BytesIO(content), as_attachment=not inline, download_name=filename,
                     mimetype="application/pdf" if inline else "application/octet-stream")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
