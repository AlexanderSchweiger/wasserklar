"""Belegablage: Belegeingang, Belegseite, Dateiauslieferung, Zuordnung zu Buchungen.

Routen des ``accounting``-Blueprints (Recht ``buchhaltung`` — ohne Eintrag in
``_ENDPOINT_PERMS`` gilt der Fallback). Die Logik steckt in ``app/documents/service.py``,
die Ablage in ``app/documents/storage.py``, der E-Rechnungs-Parser in
``app/einvoice/incoming.py``, der Buchungsvorschlag in ``incoming_service.py``.

Zwei Wege zum Beleg: die Seite „Belege" (Eingang → buchen oder zuordnen) und die
Bueroklammer in der Buchungsliste (Panel im Modal, wie die Notizen).
"""
import io
import json
import tempfile
from datetime import date

from flask import (
    abort, current_app, flash, jsonify, make_response, redirect, render_template, request, send_file, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import undefer

from app import country
from app.accounting import bp
from app.accounting import incoming_service as einvoice_svc
from app.documents import archive, extract
from app.documents import service as svc
from app.documents import storage
from app.einvoice import incoming
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Document, DocumentLink, Project, RealAccount,
)
from app.pagination import paginate_query

_TABS = (("inbox", "Eingang"), ("booked", "Verbucht"), ("filed", "Abgelegt"),
         ("discarded", "Verworfen"), ("all", "Alle"))


def _int_or_none(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _wants_json():
    best = request.accept_mimetypes
    return best.accept_json and not best.accept_html


def _doc_redirect(doc):
    return redirect(url_for("accounting.document_detail", doc_id=doc.id))


def _doc_or_404(doc_id):
    """Ein Beleg — nur aus dem Bereich ``accounting``. Ausgangsrechnungen, Mahnungen und Protokolle
    liegen im selben Register, gehoeren aber anderen Rechten (``app/documents/access.py``)."""
    doc = db.get_or_404(Document, doc_id)
    if doc.area != Document.AREA_ACCOUNTING:
        abort(404)
    return doc


def _entity_or_404(entity_type, entity_id):
    model = svc.ENTITY_TYPES.get(entity_type)
    if model is None:
        abort(404)
    return db.get_or_404(model, entity_id)


# ---------------------------------------------------------------------------
# Alte Adressen (Eingangsrechnungen) → Belege
# ---------------------------------------------------------------------------

@bp.route("/incoming")
@login_required
def incoming_list():
    return redirect(url_for("accounting.documents"), 301)


@bp.route("/incoming/<int:doc_id>")
@login_required
def incoming_detail(doc_id):
    return redirect(url_for("accounting.document_detail", doc_id=doc_id), 301)


# ---------------------------------------------------------------------------
# Liste + Upload
# ---------------------------------------------------------------------------

@bp.route("/documents")
@login_required
def documents():
    tab = request.args.get("tab", "inbox")
    if tab not in dict(_TABS):
        tab = "inbox"
    term = (request.args.get("q") or "").strip()
    query = svc.accounting_documents()
    tab_filter = svc.tab_filter(tab)
    if tab_filter is not None:
        query = query.filter(tab_filter)
    if term:
        # der Text wird nur fuer die Fundstellen-Auszuege der angezeigten Seite mitgeladen
        query = query.filter(svc.search_filter(term)).options(undefer(Document.text_content))
    query = query.order_by(Document.created_at.desc(), Document.id.desc())
    pagination = paginate_query(query, page_key="documents")
    ids = [d.id for d in pagination.items]
    booked_ids = ({row[0] for row in db.session.query(Document.id)
                   .filter(Document.id.in_(ids), svc.booked_clause()).all()} if ids else set())
    snippets = {}
    if term:
        for d in pagination.items:
            if not svc.matches_meta(d, term):          # nur Treffer im Text bekommen einen Auszug
                hit = extract.snippet(d.text_content, term)
                if hit:
                    snippets[d.id] = hit
    return render_template(
        "accounting/documents_list.html", docs=pagination.items, pagination=pagination, tab=tab,
        tabs=_TABS, counts=svc.tab_counts(), search=term, booked_ids=booked_ids, snippets=snippets,
        quota=svc.quota_status(), max_upload_mb=svc.max_upload_mb(),
        expired_count=svc.expired_count())


@bp.route("/documents/archive")
@login_required
def documents_archive():
    """Belegarchiv je Jahr zum Herunterladen (Steuerberater, Betriebspruefung, Jahresabschluss)."""
    return render_template("accounting/documents_archive.html", years=archive.year_summary(),
                           retention_years=country.current_profile().document_retention_years)


@bp.route("/documents/archive/<int:year>/download")
@login_required
def documents_archive_download(year):
    """ZIP mit den Belegdateien des Jahres, ``index.csv`` und ``sha256sums.txt``.

    Gebaut wird in eine Temp-Datei im Dateibaum des Mandanten (nicht im Arbeitsspeicher — ein
    Jahresarchiv kann Gigabyte gross sein); sie verschwindet, sobald die Antwort ausgeliefert ist.
    """
    if not 2000 <= year <= date.today().year + 1:
        abort(404)
    needed = archive.year_size(year)
    min_free = int(current_app.config.get("DOCUMENT_MIN_FREE_DISK_MB", 0)) * svc.MB
    try:
        free = storage.free_bytes()
    except OSError:
        free = None
    if free is not None and free < needed + min_free:
        flash("Auf dem Server ist gerade zu wenig Speicherplatz frei, um das Archiv zu erstellen. "
              "Bitte später erneut versuchen.", "danger")
        return redirect(url_for("accounting.documents_archive"))
    root = storage.tenant_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryFile(dir=root)
    try:
        result = archive.build(year, tmp, created_by=current_user.username)
    except Exception:  # noqa: BLE001 — Fehler beim Zusammenstellen: melden statt halbes ZIP ausliefern
        tmp.close()
        current_app.logger.exception("Belegarchiv %s konnte nicht erstellt werden", year)
        flash("Das Belegarchiv konnte nicht erstellt werden.", "danger")
        return redirect(url_for("accounting.documents_archive"))
    if result.missing or result.mismatched:
        flash(f"Hinweis zum Archiv {year}: bei {len(result.missing)} Beleg(en) fehlte die Datei, bei "
              f"{len(result.mismatched)} passte sie nicht mehr zur Prüfsumme — Einzelheiten stehen "
              "in der Spalte „Prüfung“ der index.csv.", "warning")
    tmp.seek(0)
    resp = send_file(tmp, mimetype="application/zip", as_attachment=True, download_name=f"belegarchiv-{year}.zip")
    resp.call_on_close(tmp.close)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/documents/expired")
@login_required
def documents_expired():
    """Belege, deren Aufbewahrungsfrist abgelaufen ist. Loeschen duerfen nur Administratoren."""
    rows = svc.expired_documents()
    return render_template(
        "accounting/documents_expired.html", rows=rows, is_admin=current_user.is_admin,
        total_size=sum(doc.size_bytes or 0 for doc, _end in rows),
        retention_years=country.current_profile().document_retention_years,
        retention_hint=svc.retention_hint())


@bp.route("/documents/expired/delete", methods=["POST"])
@login_required
def documents_expired_delete():
    """Loescht die gewaehlten Belege nach Fristablauf (nur Administratoren). Die Frist rechnet der
    Server je Beleg neu — ein manipuliertes Formular loescht nie einen Beleg mit laufender Frist."""
    if not current_user.is_admin:
        flash("Belege nach Ablauf der Aufbewahrungsfrist löschen dürfen nur Administratoren.", "danger")
        return redirect(url_for("accounting.documents_expired"))
    ids = [int(raw) for raw in request.form.getlist("ids") if raw.isascii() and raw.isdigit()]
    if not ids:
        flash("Bitte mindestens einen Beleg wählen.", "warning")
        return redirect(url_for("accounting.documents_expired"))
    deleted, freed, problems = 0, 0, []
    for doc in svc.by_ids(ids):
        size = doc.size_bytes or 0
        try:
            svc.delete_expired(doc, current_user.id)
            deleted, freed = deleted + 1, freed + size
        except svc.DocumentError as exc:
            db.session.rollback()
            problems.append(f"„{doc.display_title}“: {exc}")
    if deleted:
        flash(f"{deleted} Beleg(e) gelöscht, {svc.format_size(freed)} Speicher frei.", "success")
    for problem in problems:
        flash(problem, "danger")
    return redirect(url_for("accounting.documents_expired" if svc.expired_documents() else "accounting.documents"))


@bp.route("/documents/upload", methods=["POST"])
@login_required
def document_upload():
    """Nimmt Belege entgegen. ``fetch`` (Accept: application/json) bekommt je Datei ein
    JSON-Ergebnis, ein normales Formular Hinweise + Weiterleitung. Optional verknuepft der
    Upload sofort (``link_type`` = booking|booking_group, ``link_id``)."""
    uploads = [f for f in request.files.getlist("file") + request.files.getlist("files") if f and f.filename]
    if not uploads:
        message = "Bitte mindestens eine Datei wählen."
        if _wants_json():
            return jsonify(ok=False, error=message), 400
        flash(message, "warning")
        return redirect(url_for("accounting.documents"))

    target = None
    link_type = request.form.get("link_type")
    if link_type:
        target = _entity_or_404(link_type, _int_or_none(request.form.get("link_id")) or 0)
    detail = None
    if request.form.get("shrunk") == "1":
        detail = {"verkleinert": True, "original_name": (request.form.get("original_name") or "")[:255],
                  "original_groesse": _int_or_none(request.form.get("original_size"))}

    results = []
    for upload in uploads:
        data = upload.read(svc.max_upload_bytes() + 1)
        result = {"name": upload.filename}
        doc = None
        try:
            doc = svc.store_upload(upload.filename, data, current_user.id,
                                   kind=request.form.get("kind") or None, upload_detail=detail)
            result.update(ok=True, duplicate=False)
        except svc.DuplicateUpload as dup:
            doc = dup.existing
            result.update(ok=True, duplicate=True,
                          message=f"„{upload.filename}“ ist schon abgelegt ({doc.display_title}).")
        except svc.DocumentError as exc:
            result.update(ok=False, error=str(exc))
        if doc is not None:
            result.update(id=doc.id, title=doc.display_title, size=doc.size_bytes,
                          url=url_for("accounting.document_detail", doc_id=doc.id),
                          warning=getattr(doc, "parse_warning", None))
            if target is not None:
                try:
                    svc.link(doc, booking=target if isinstance(target, Booking) else None,
                             group=target if isinstance(target, BookingGroup) else None,
                             user_id=current_user.id)
                    db.session.commit()
                    result["linked"] = True
                except svc.DocumentError as exc:
                    db.session.rollback()
                    result.update(linked=False, warning=str(exc))
        results.append(result)

    if _wants_json():
        first = results[0]
        status = 200 if first.get("ok") else 400
        return jsonify(first if len(results) == 1 else {"ok": all(r.get("ok") for r in results),
                                                         "results": results}), status
    stored = [r for r in results if r.get("ok") and not r.get("duplicate")]
    for r in results:
        if not r.get("ok"):
            flash(f"„{r['name']}“ konnte nicht abgelegt werden: {r['error']}", "danger")
        elif r.get("duplicate"):
            flash(r["message"], "warning")
        elif r.get("warning"):
            flash(f"„{r['name']}“: {r['warning']}", "warning")
    if stored:
        flash(f"{len(stored)} Beleg(e) abgelegt.", "success")
    if len(results) == 1 and results[0].get("id"):
        return redirect(url_for("accounting.document_detail", doc_id=results[0]["id"]))
    return redirect(url_for("accounting.documents"))


# ---------------------------------------------------------------------------
# Belegseite
# ---------------------------------------------------------------------------

@bp.route("/documents/<int:doc_id>")
@login_required
def document_detail(doc_id):
    doc = _doc_or_404(doc_id)
    booked = svc.is_booked(doc)
    detected_vat_ids, detected_ibans = svc.detected_identifiers(doc)
    ctx = dict(
        doc=doc, booked=booked, file_ok=storage.exists(doc.storage_key),
        detected_vat_ids=detected_vat_ids, detected_ibans=detected_ibans,
        links=doc.links, events=list(reversed(doc.events)),
        link_blockers={row.id: svc.unlink_blocker(row) for row in doc.links},
        retention_end=svc.retention_end(doc), retention_hint=svc.retention_hint(),
        retention_over=svc.retention_end(doc) < date.today(), is_admin=current_user.is_admin,
        can_delete=svc.can_delete(doc), form=None,
        suppliers=Customer.query.filter(Customer.is_supplier.is_(True), Customer.active.is_(True))
        .order_by(Customer.name).all(),
        kinds=Document.KIND_LABELS)
    if doc.status != Document.STATUS_DISCARDED:
        ctx["candidates"] = svc.candidates(doc)
        ctx["booking_choices"], ctx["group_choices"] = svc.link_choices()
    if doc.einvoice is not None:
        parsed = doc.einvoice.parsed
        ctx.update(
            inv=parsed, duplicates=einvoice_svc.possible_duplicates(doc),
            suggested=einvoice_svc.suggest_supplier(parsed) if not booked else doc.supplier,
            booking_date=einvoice_svc.default_booking_date(parsed),
            accounts=Account.query.filter_by(active=True).order_by(Account.name).all(),
            projects=Project.query.filter_by(closed=False).order_by(Project.name).all(),
            real_accounts=RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all(),
            default_real_account=RealAccount.query.filter_by(is_default=True, active=True).first(),
            rows=[], rows_error=None)
        try:
            ctx["rows"] = einvoice_svc.booking_rows(parsed)
        except einvoice_svc.BookingError as exc:
            ctx["rows_error"] = str(exc)
    return render_template("accounting/document_detail.html", **ctx)


@bp.route("/documents/<int:doc_id>/file")
@login_required
def document_raw(doc_id):
    """Das unveraenderte Original: PDF und Bilder inline, XML immer als Download."""
    doc = _doc_or_404(doc_id)
    inline = (doc.is_pdf or doc.is_image) and not request.args.get("download")
    resp = storage.send(doc.storage_key, download_name=doc.original_name, mimetype=doc.content_type,
                        as_attachment=not inline)
    if resp is None:
        flash("Die Datei ist nicht mehr vorhanden.", "danger")
        return _doc_redirect(doc)
    return resp


def _einvoice_xml(doc):
    """Das Rechnungs-XML einer E-Rechnung (bei einem ZUGFeRD-PDF der eingebettete Anhang)."""
    data = storage.read(doc.storage_key)
    if data is None:
        return None
    try:
        return incoming.parse(data, doc.original_name).xml
    except incoming.IncomingError:
        return None


@bp.route("/documents/<int:doc_id>/xml")
@login_required
def document_einvoice_xml(doc_id):
    doc = _doc_or_404(doc_id)
    if doc.einvoice is None:
        abort(404)
    xml = _einvoice_xml(doc)
    if xml is None:
        flash("Die Originaldatei ist nicht mehr vorhanden oder nicht lesbar.", "danger")
        return _doc_redirect(doc)
    resp = make_response(xml)
    resp.headers["Content-Type"] = "application/xml; charset=utf-8"
    number = (doc.number or str(doc.id)).replace("/", "-")
    resp.headers["Content-Disposition"] = f'attachment; filename="{number}.xml"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/documents/<int:doc_id>/attachment/<int:index>")
@login_required
def document_einvoice_attachment(doc_id, index):
    """Ein in die Rechnung eingebetteter Anhang (BG-24), z. B. die PDF-Ansicht einer XRechnung."""
    doc = _doc_or_404(doc_id)
    if doc.einvoice is None:
        abort(404)
    xml = _einvoice_xml(doc)
    found = incoming.attachment_bytes(xml, index) if xml is not None else None
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


# ---------------------------------------------------------------------------
# Aktionen an einem Beleg
# ---------------------------------------------------------------------------

@bp.route("/documents/<int:doc_id>/update", methods=["POST"])
@login_required
def document_update(doc_id):
    doc = _doc_or_404(doc_id)
    try:
        document_date = None
        raw_date = (request.form.get("document_date") or "").strip()
        if raw_date:
            try:
                document_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise svc.DocumentError("Ungültiges Belegdatum.") from exc
        try:
            amount = svc.parse_decimal(request.form.get("amount"))
        except ValueError as exc:
            raise svc.DocumentError(str(exc)) from exc
        supplier_id = _int_or_none(request.form.get("supplier_id"))
        if supplier_id is not None and db.session.get(Customer, supplier_id) is None:
            supplier_id = None
        was_auto = doc.meta_auto
        changes = svc.update_meta(
            doc, title=request.form.get("title"), number=request.form.get("number"),
            document_date=document_date, amount=amount, supplier_id=supplier_id,
            kind=request.form.get("kind") or doc.kind, user_id=current_user.id)
        db.session.commit()
        if changes:
            flash("Gespeichert.", "success")
        elif was_auto:
            flash("Angaben bestätigt.", "success")
        else:
            flash("Keine Änderung.", "info")
    except svc.DocumentError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return _doc_redirect(doc)


@bp.route("/documents/<int:doc_id>/book", methods=["POST"])
@login_required
def document_book_einvoice(doc_id):
    """Bucht eine E-Rechnung (Buchungsvorschlag je Steuersatz) und verknuepft den Beleg."""
    doc = _doc_or_404(doc_id)
    if doc.einvoice is None:
        abort(404)
    parsed = doc.einvoice.parsed
    try:
        booking_date = date.fromisoformat(request.form.get("date", ""))
    except ValueError:
        flash("Ungültiges Buchungsdatum.", "danger")
        return _doc_redirect(doc)
    try:
        supplier_raw = request.form.get("supplier_id", "")
        if supplier_raw == "new":
            supplier = einvoice_svc.create_supplier(parsed)
        else:
            supplier = db.session.get(Customer, _int_or_none(supplier_raw)) if _int_or_none(supplier_raw) else None
            if supplier is not None and not supplier.is_supplier:
                supplier = None
        result = einvoice_svc.book(
            doc, supplier=supplier, account_id=_int_or_none(request.form.get("account_id")),
            booking_date=booking_date, user_id=current_user.id,
            project_id=_int_or_none(request.form.get("project_id")),
            real_account_id=_int_or_none(request.form.get("real_account_id")))
        db.session.commit()
    except einvoice_svc.BookingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return _doc_redirect(doc)
    flash(f"Eingangsrechnung {parsed.number or ''} gebucht.", "success")
    if isinstance(result, BookingGroup):
        return redirect(url_for("accounting.booking_group_edit", group_id=result.id))
    return _doc_redirect(doc)


@bp.route("/documents/<int:doc_id>/link", methods=["POST"])
@login_required
def document_link(doc_id):
    """Ordnet den Beleg einer bestehenden Buchung / Sammelbuchung zu (``target`` = booking:ID | group:ID)."""
    doc = _doc_or_404(doc_id)
    kind, _, raw_id = (request.form.get("target") or "").partition(":")
    target_id = _int_or_none(raw_id)
    if kind not in ("booking", "group") or target_id is None:
        flash("Bitte eine Buchung wählen.", "warning")
        return _doc_redirect(doc)
    try:
        if kind == "booking":
            svc.link(doc, booking=db.get_or_404(Booking, target_id), user_id=current_user.id)
        else:
            svc.link(doc, group=db.get_or_404(BookingGroup, target_id), user_id=current_user.id)
        db.session.commit()
        flash("Beleg der Buchung zugeordnet.", "success")
    except svc.DocumentError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return _doc_redirect(doc)


@bp.route("/documents/links/<int:link_id>/unlink", methods=["POST"])
@login_required
def document_unlink(link_id):
    """Loest eine Zuordnung — aus der Belegseite oder aus dem Panel einer Buchung (``ctx=panel``)."""
    link_row = db.get_or_404(DocumentLink, link_id)
    doc = link_row.document
    entity_type = "booking" if link_row.booking_id is not None else "booking_group"
    entity_id = link_row.booking_id or link_row.booking_group_id
    error = None
    try:
        svc.unlink(link_row, user_id=current_user.id, reason=(request.form.get("reason") or "").strip()[:200] or None)
        db.session.commit()
    except svc.DocumentError as exc:
        db.session.rollback()
        error = str(exc)
    if request.form.get("ctx") == "panel":
        return _panel_response(entity_type, entity_id, error=error)
    flash(error or "Zuordnung gelöst — der Beleg ist wieder im Eingang.", "danger" if error else "success")
    return _doc_redirect(doc)


def _simple_action(doc_id, action, success):
    doc = _doc_or_404(doc_id)
    try:
        action(doc, current_user.id)
        db.session.commit()
        flash(success, "success")
    except svc.DocumentError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return _doc_redirect(doc)


@bp.route("/documents/<int:doc_id>/shelve", methods=["POST"])
@login_required
def document_shelve(doc_id):
    return _simple_action(doc_id, svc.shelve, "Beleg abgelegt (ohne Buchung).")


@bp.route("/documents/<int:doc_id>/discard", methods=["POST"])
@login_required
def document_discard(doc_id):
    return _simple_action(doc_id, svc.discard, "Beleg verworfen. Die Datei bleibt aufbewahrt.")


@bp.route("/documents/<int:doc_id>/reopen", methods=["POST"])
@login_required
def document_reopen(doc_id):
    return _simple_action(doc_id, svc.reopen, "Beleg wieder im Eingang.")


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def document_delete(doc_id):
    doc = _doc_or_404(doc_id)
    try:
        svc.delete(doc, current_user.id)
    except svc.DocumentError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return _doc_redirect(doc)
    flash("Beleg gelöscht.", "info")
    return redirect(url_for("accounting.documents"))


# ---------------------------------------------------------------------------
# Panel + Pin an Buchungen (Bueroklammer in der Buchungsliste)
# ---------------------------------------------------------------------------

def _panel_context(entity_type, entity_id, error=None):
    entity = _entity_or_404(entity_type, entity_id)
    links = svc.links_for(entity_type, entity_id)
    if entity_type == "booking":
        attach_blocker = (
            "Diese Buchung gehört zu einer Sammelbuchung — der Beleg wird an der Sammelbuchung abgelegt."
            if entity.group_id is not None else
            None if svc.is_effective_booking(entity) else "Die Buchung ist storniert.")
    else:
        attach_blocker = None if entity.status == BookingGroup.STATUS_AKTIV else "Die Sammelbuchung ist storniert."
    return dict(
        entity_type=entity_type, entity=entity, links=links, error=error,
        link_blockers={row.id: svc.unlink_blocker(row) for row in links},
        attach_blocker=attach_blocker, choices=svc.inbox_choices(),
        quota=svc.quota_status(), max_upload_mb=svc.max_upload_mb())


def _panel_response(entity_type, entity_id, error=None):
    resp = make_response(render_template("documents/_panel.html",
                                         **_panel_context(entity_type, entity_id, error)))
    resp.headers["HX-Trigger"] = json.dumps(
        {"documents:changed": {"entity_type": entity_type, "entity_id": entity_id}})
    return resp


@bp.route("/documents/panel/<entity_type>/<int:entity_id>")
@login_required
def document_panel(entity_type, entity_id):
    return render_template("documents/_panel.html", **_panel_context(entity_type, entity_id))


@bp.route("/documents/panel/<entity_type>/<int:entity_id>/attach", methods=["POST"])
@login_required
def document_panel_attach(entity_type, entity_id):
    """Haengt einen Beleg aus dem Eingang an die Buchung / Sammelbuchung des Panels."""
    entity = _entity_or_404(entity_type, entity_id)
    error = None
    doc = db.session.get(Document, _int_or_none(request.form.get("document_id")) or 0)
    if doc is not None and doc.area != Document.AREA_ACCOUNTING:
        doc = None
    if doc is None:
        error = "Bitte einen Beleg wählen."
    else:
        try:
            svc.link(doc, booking=entity if entity_type == "booking" else None,
                     group=entity if entity_type == "booking_group" else None, user_id=current_user.id)
            db.session.commit()
        except svc.DocumentError as exc:
            db.session.rollback()
            error = str(exc)
    return _panel_response(entity_type, entity_id, error=error)


@bp.route("/documents/pin/<entity_type>/<int:entity_id>")
@login_required
def document_pin(entity_type, entity_id):
    _entity_or_404(entity_type, entity_id)
    count = svc.counts_by_entity(entity_type, [entity_id]).get(entity_id, 0)
    return render_template("documents/_pin.html", entity_type=entity_type, entity_id=entity_id, count=count)
