"""Routen des Stoerungs-/Rohrbruch-Journals.

- Listenseite (table-fill) mit Filtern + Modal-CRUD + Row-Swap (Neu & Edit teilen
  einen Modal-Form-Body; nach dem Speichern wird nur die betroffene Zeile getauscht).
- Detailseite mit Mini-Karte, Fotos und Kennzahlen.
- Kartenansicht (Leaflet) mit Pins aller Stoerungen; GeoJSON-Endpoint als
  Datenquelle. Pin setzen/verschieben laeuft als JSON ueber ``fetch`` (Geoman).
- Foto-Upload/-Delete 1:1 wie das Netz-Modul.
- Jahresbericht als PDF (WeasyPrint, ImportError-sicher) und CSV-Export.

Das Blueprint-``before_request`` (require_blueprint_permission) blockt nicht-
authentifizierte Requests NICHT — daher traegt jede Route ``@login_required``.
"""

import csv
import io
import json
import os
import uuid
from datetime import date

from flask import (
    render_template, request, jsonify, redirect, url_for, flash,
    make_response, send_from_directory, current_app,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.incidents import bp
from app.incidents import services as svc
from app.incidents import vocab
from app.extensions import db
from app.pagination import paginate_query
from app.models import Incident, IncidentPhoto, Customer, Property, NetworkPlan

_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_PAGE_KEY = "incidents"


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------

def _filters_from_request():
    """Aktuelle Listen-Filter aus der Query (validiert)."""
    status = request.args.get("status", "").strip()
    itype = request.args.get("type", "").strip()
    severity = request.args.get("severity", "").strip()
    year = request.args.get("year", type=int)
    return {
        "q": request.args.get("q", "").strip(),
        "status": status if vocab.is_valid_status(status) else "",
        "type": itype if vocab.is_valid_type(itype) else "",
        "severity": severity if vocab.is_valid_severity(severity) else "",
        "year": year,
    }


def _apply_list_filters(query, f):
    if f["q"]:
        like = f"%{f['q']}%"
        query = query.filter(db.or_(
            Incident.title.ilike(like),
            Incident.location_description.ilike(like),
            Incident.performed_by.ilike(like),
            Incident.description.ilike(like),
        ))
    if f["status"]:
        query = query.filter(Incident.status == f["status"])
    if f["type"]:
        query = query.filter(Incident.incident_type == f["type"])
    if f["severity"]:
        query = query.filter(Incident.severity == f["severity"])
    if f["year"]:
        query = query.filter(db.extract("year", Incident.detected_at) == f["year"])
    return query


def _link_options():
    """Objekte + Kontakte fuer die optionalen Verknuepfungs-Dropdowns im Modal."""
    return {
        "properties": Property.query.filter_by(active=True)
                      .order_by(Property.object_number.asc()).all(),
        "customers": Customer.query.filter_by(active=True)
                     .order_by(Customer.name.asc()).all(),
    }


def _render_form_body(inc, form_data=None, error=None):
    return render_template(
        "incidents/_form_body.html",
        inc=inc, vocab=vocab, form_data=form_data, error=error,
        today_iso=date.today().isoformat(), **_link_options(),
    )


def _render_row(inc):
    return render_template(
        "incidents/_row.html", inc=inc, vocab=vocab, filters=_filters_from_request(),
    )


def _trigger(resp, incident_id):
    resp.headers["HX-Trigger"] = json.dumps({
        "closeIncidentEditModal": True,
        "incidentEdited": {"id": incident_id},
    })
    return resp


# ---------------------------------------------------------------------------
# Liste
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    f = _filters_from_request()
    query = _apply_list_filters(Incident.query, f).order_by(
        Incident.detected_at.desc(), Incident.id.desc()
    )
    pagination = paginate_query(query, _PAGE_KEY)
    ctx = dict(
        pagination=pagination, vocab=vocab, filters=f,
        years=svc.available_years(),
    )
    if request.headers.get("HX-Request"):
        return render_template("incidents/_table.html", **ctx)
    return render_template("incidents/index.html", **ctx)


# ---------------------------------------------------------------------------
# Modal-CRUD + Row-Swap
# ---------------------------------------------------------------------------

@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        if not (request.form.get("title") or "").strip():
            return _render_form_body(Incident(), form_data=request.form,
                                     error="Bitte einen Titel angeben.")
        inc = Incident()
        svc.apply_attributes(inc, request.form)
        inc.created_by_id = current_user.id
        db.session.add(inc)
        db.session.commit()
        # Neu angelegte Störung sofort öffnen: HX-Redirect navigiert die ganze
        # Seite (Vollload) auf die Detailseite — dort kann direkt die Position
        # auf der Karte gesetzt werden. (Edit bleibt beim Row-Swap, s. u.)
        resp = make_response("")
        resp.headers["HX-Redirect"] = url_for("incidents.detail", incident_id=inc.id)
        return resp
    return _render_form_body(Incident())


@bp.route("/<int:incident_id>/edit", methods=["GET", "POST"])
@login_required
def edit(incident_id):
    inc = db.get_or_404(Incident, incident_id)
    if request.method == "POST":
        if not (request.form.get("title") or "").strip():
            return _render_form_body(inc, form_data=request.form,
                                     error="Bitte einen Titel angeben.")
        svc.apply_attributes(inc, request.form)
        db.session.commit()
        return _trigger(make_response("", 204), inc.id)
    return _render_form_body(inc)


@bp.route("/<int:incident_id>/row")
@login_required
def row(incident_id):
    """Eine Tabellenzeile als Fragment — nach dem Modal-Speichern in-place
    getauscht, damit Filter/Suche/Pagination der Liste erhalten bleiben."""
    inc = db.get_or_404(Incident, incident_id)
    return _render_row(inc)


@bp.route("/<int:incident_id>/delete", methods=["POST"])
@login_required
def delete(incident_id):
    inc = db.get_or_404(Incident, incident_id)
    # ORM-Cascade loescht nur die DB-Records — Fotodateien explizit entfernen.
    svc.delete_photo_files(list(inc.photos))
    db.session.delete(inc)
    db.session.commit()
    flash("Störung gelöscht.", "success")
    next_url = request.form.get("next", "")
    if next_url.startswith("/incidents"):
        return redirect(next_url)
    return redirect(url_for("incidents.index"))


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

@bp.route("/<int:incident_id>")
@login_required
def detail(incident_id):
    inc = db.get_or_404(Incident, incident_id)
    return render_template(
        "incidents/detail.html", inc=inc, vocab=vocab,
        cfg=_map_config_single(inc),
    )


# ---------------------------------------------------------------------------
# Karte
# ---------------------------------------------------------------------------

def _with_plan_context(cfg):
    """Ergänzt die Karten-Config um die Leitungsplan-Auswahl (Default = erster
    aktiver Plan), sofern der User das Netz-Modul darf — sonst liefert
    /network/features.geojson einen HTML-Redirect statt JSON. Genutzt von der
    Detail- (single) UND der Sammelkarte (collection)."""
    if current_user.has_permission("network"):
        plans = NetworkPlan.query.order_by(NetworkPlan.name.asc()).all()
        if plans:
            active = next((p for p in plans if p.status == NetworkPlan.STATUS_ACTIVE), None)
            cfg["plans"] = [{"id": p.id, "name": p.name} for p in plans]
            cfg["defaultPlanId"] = (active or plans[0]).id
            cfg["networkBase"] = url_for("network.features_geojson")
    return cfg


def _map_config_collection():
    return _with_plan_context({
        "mode": "collection",
        "dataUrl": url_for("incidents.map_geojson"),
        "vocab": vocab.as_client_dict(),
    })


def _map_config_single(inc):
    return _with_plan_context({
        "mode": "single",
        "geometryUrl": url_for("incidents.geometry", incident_id=inc.id),
        "feature": svc.incident_to_geojson(inc),
        "vocab": vocab.as_client_dict(),
    })


@bp.route("/map")
@login_required
def map_view():
    return render_template(
        "incidents/map.html", cfg=_map_config_collection(), vocab=vocab,
    )


@bp.route("/map.geojson")
@login_required
def map_geojson():
    f = _filters_from_request()
    return jsonify(svc.collection_geojson(
        status=f["status"] or None,
        incident_type=f["type"] or None,
        year=f["year"],
    ))


@bp.route("/<int:incident_id>/geometry", methods=["POST"])
@login_required
def geometry(incident_id):
    inc = db.get_or_404(Incident, incident_id)
    data = request.get_json(silent=True) or {}
    try:
        svc.apply_location(inc, data.get("geometry"))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    db.session.commit()
    return jsonify(svc.incident_to_geojson(inc) or {"cleared": True})


# ---------------------------------------------------------------------------
# Fotos
# ---------------------------------------------------------------------------

@bp.route("/<int:incident_id>/photos", methods=["POST"])
@login_required
def photo_upload(incident_id):
    inc = db.get_or_404(Incident, incident_id)
    file = request.files.get("photo")
    if not file or not file.filename:
        flash("Keine Bilddatei gewählt.", "warning")
        return render_template("incidents/_photos.html", inc=inc)

    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in _ALLOWED_IMG_EXT or not (file.mimetype or "").startswith("image/"):
        flash("Nur Bilddateien (JPG, PNG, WebP, GIF) erlaubt.", "warning")
        return render_template("incidents/_photos.html", inc=inc)

    fname = f"{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(svc.incident_upload_dir(), fname))
    photo = IncidentPhoto(
        incident_id=inc.id,
        filename=fname,
        original_name=secure_filename(file.filename),
        content_type=file.mimetype,
        caption=(request.form.get("caption") or "").strip() or None,
        uploaded_by_id=current_user.id,
    )
    db.session.add(photo)
    db.session.commit()
    return render_template("incidents/_photos.html", inc=inc)


@bp.route("/photos/<int:photo_id>")
@login_required
def photo_serve(photo_id):
    photo = db.get_or_404(IncidentPhoto, photo_id)
    return send_from_directory(svc.incident_upload_dir(), photo.filename)


@bp.route("/photos/<int:photo_id>/delete", methods=["POST"])
@login_required
def photo_delete(photo_id):
    photo = db.get_or_404(IncidentPhoto, photo_id)
    inc = photo.incident
    svc.delete_photo_files([photo])
    db.session.delete(photo)
    db.session.commit()
    return render_template("incidents/_photos.html", inc=inc)


# ---------------------------------------------------------------------------
# Export (CSV / PDF)
# ---------------------------------------------------------------------------

def _de_decimal(value):
    """Decimal/Float -> deutscher String mit Dezimalkomma, leer bei None."""
    if value is None:
        return ""
    return str(value).replace(".", ",")


@bp.route("/export.csv")
@login_required
def export_csv():
    f = _filters_from_request()
    query = _apply_list_filters(Incident.query, f).order_by(
        Incident.detected_at.desc(), Incident.id.desc()
    )
    output = io.StringIO()
    output.write("﻿")  # UTF-8-BOM fuer Excel
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Nr", "Titel", "Typ", "Schweregrad", "Status", "Ursache",
        "Erkannt am", "Behoben am", "Dauer (Tage)", "Wasserverlust (m³)",
        "Kosten (EUR)", "Ausführende Firma", "Betroffene Anschlüsse", "Lage",
    ])
    for inc in query.all():
        writer.writerow([
            inc.id,
            inc.title or "",
            vocab.type_label(inc.incident_type),
            vocab.severity_label(inc.severity),
            vocab.status_label(inc.status),
            vocab.cause_label(inc.cause),
            inc.detected_at.strftime("%d.%m.%Y") if inc.detected_at else "",
            inc.resolved_at.strftime("%d.%m.%Y") if inc.resolved_at else "",
            inc.duration_days() if inc.duration_days() is not None else "",
            _de_decimal(inc.water_loss_m3),
            _de_decimal(inc.cost),
            inc.performed_by or "",
            inc.affected_count if inc.affected_count is not None else "",
            inc.location_description or "",
        ])
    suffix = f["year"] or "alle"
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="stoerungsjournal_{suffix}.csv"'
    return resp


@bp.route("/print")
@login_required
def print_view():
    year = request.args.get("year", type=int)
    agg = svc.report_aggregates(year=year)
    return render_template(
        "incidents/print.html", vocab=vocab, agg=agg, year=year,
        today_de=date.today().strftime("%d.%m.%Y"),
    )


@bp.route("/report.pdf")
@login_required
def report_pdf():
    year = request.args.get("year", type=int)
    try:
        from weasyprint import HTML
    except (ImportError, OSError):
        flash("PDF-Export ist nur im Docker-Container verfügbar. "
              "Die Druckansicht steht als Alternative bereit.", "warning")
        return redirect(url_for("incidents.print_view", year=year))

    agg = svc.report_aggregates(year=year)
    html = render_template(
        "incidents/pdf_report.html", vocab=vocab, agg=agg, year=year,
        today_de=date.today().strftime("%d.%m.%Y"),
    )
    pdf = HTML(string=html, base_url=request.url_root).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    suffix = year or "alle"
    resp.headers["Content-Disposition"] = f'inline; filename="stoerungsbericht_{suffix}.pdf"'
    return resp
