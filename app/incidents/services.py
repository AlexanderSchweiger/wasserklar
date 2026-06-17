"""Service-Funktionen fuer das Stoerungs-/Rohrbruch-Journal.

- ``incident_upload_dir()`` — tenant-sicherer Foto-Ordner (reitet auf ``PDF_DIR``,
  identisches Muster wie ``network.services.technik_upload_dir``)
- Formular-/JSON-Mapping inkl. ``status``<->``resolved_at``-Kopplung
- GeoJSON-(De)Serialisierung (Point-only) fuer die Karte
- ``report_aggregates()`` — Kennzahlen fuer PDF-Jahresbericht + CSV (Decimal-genau)
"""

import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import current_app, url_for

from app.extensions import db
from app.models import Incident
from app.incidents import vocab


# ---------------------------------------------------------------------------
# Datei-Ablage
# ---------------------------------------------------------------------------

def incident_upload_dir():
    """Tenant-sicherer Ordner fuer Stoerungs-Fotos.

    Reitet — wie ``network.services.technik_upload_dir`` — auf dem bereits
    per-Request umgebogenen ``PDF_DIR`` (SaaS: ``instance/tenants/<slug>/pdfs``
    -> ``.../incidents``; OSS standalone: ``instance/pdfs`` -> ``instance/incidents``).
    Damit ist die Tenant-Trennung geschenkt, ohne die SaaS-Schicht anzufassen.
    """
    base = os.path.dirname(current_app.config["PDF_DIR"])
    path = os.path.join(base, "incidents")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Parsing-Helfer
# ---------------------------------------------------------------------------

def _to_int(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_decimal(value):
    """Decimal-Parse, das deutsches Komma als Dezimaltrenner toleriert.
    Geld-/Mengenwerte bleiben strikt ``Decimal`` (nie Float)."""
    if value is None:
        return None
    s = str(value).strip().replace(",", ".")
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Attribut-Mapping
# ---------------------------------------------------------------------------

def apply_attributes(inc, data):
    """Setzt die Sachattribute aus einem dict (Formular oder JSON).

    Validiert Typ/Schweregrad/Status/Ursache gegen das Vokabular (ungueltig ->
    Default bzw. None), koppelt ``resolved_at`` an ``status`` und parst Geld-/
    Mengenwerte als ``Decimal``. Geometrie wird hier NICHT angefasst (siehe
    ``apply_location``).
    """
    inc.title = (data.get("title") or "").strip() or None

    itype = (data.get("incident_type") or "").strip()
    if vocab.is_valid_type(itype):
        inc.incident_type = itype
    elif inc.incident_type is None:
        inc.incident_type = Incident.TYPE_ROHRBRUCH

    severity = (data.get("severity") or "").strip()
    if vocab.is_valid_severity(severity):
        inc.severity = severity
    elif inc.severity is None:
        inc.severity = Incident.SEVERITY_MEDIUM

    cause = (data.get("cause") or "").strip()
    inc.cause = cause if vocab.is_valid_cause(cause) else None

    detected = parse_date(data.get("detected_at"))
    inc.detected_at = detected or inc.detected_at or date.today()

    inc.location_description = (data.get("location_description") or "").strip() or None
    inc.water_loss_m3 = _to_decimal(data.get("water_loss_m3"))
    inc.affected_count = _to_int(data.get("affected_count"))
    inc.cost = _to_decimal(data.get("cost"))
    inc.performed_by = (data.get("performed_by") or "").strip() or None
    inc.description = (data.get("description") or "").strip() or None
    inc.repair_notes = (data.get("repair_notes") or "").strip() or None

    inc.customer_id = _to_int(data.get("customer_id"))
    inc.property_id = _to_int(data.get("property_id"))
    inc.feature_id = _to_int(data.get("feature_id"))

    # Status zuletzt setzen, dann resolved_at koppeln (auch ein manuell im
    # Formular gesetztes resolved_at wird respektiert, solange Status != offen).
    status = (data.get("status") or "").strip()
    if vocab.is_valid_status(status):
        inc.status = status
    elif inc.status is None:
        inc.status = Incident.STATUS_OPEN

    manual_resolved = parse_date(data.get("resolved_at"))
    if inc.status == Incident.STATUS_RESOLVED:
        inc.resolved_at = manual_resolved or inc.resolved_at or date.today()
    else:
        # Solange nicht behoben gibt es kein Behebungsdatum (sonst waere
        # duration_days() inkonsistent).
        inc.resolved_at = None


# ---------------------------------------------------------------------------
# GeoJSON <-> Model (Point-only)
# ---------------------------------------------------------------------------

def apply_location(inc, geometry):
    """Setzt ``location_geojson``/``lat``/``lng`` aus einem GeoJSON-Point-Dict.
    Wirft ``ValueError`` bei ungueltiger Geometrie. ``geometry=None`` loescht die
    Lage (Pin entfernen)."""
    if geometry is None:
        inc.location_geojson = inc.lat = inc.lng = None
        return
    if not isinstance(geometry, dict):
        raise ValueError("geometry ist kein Objekt")
    if geometry.get("type") != "Point":
        raise ValueError("Nur Punkt-Geometrie wird unterstützt")
    coords = geometry.get("coordinates")
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        raise ValueError("Point-Koordinaten ungültig")
    lng, lat = float(coords[0]), float(coords[1])
    inc.lat, inc.lng = lat, lng
    inc.location_geojson = json.dumps({"type": "Point", "coordinates": [lng, lat]})


def incident_to_geojson(inc):
    """Incident -> GeoJSON-Feature-Dict (Point). Ohne gesetzte Lage: ``None``."""
    if inc.lat is None or inc.lng is None:
        return None
    return {
        "type": "Feature",
        "id": inc.id,
        "geometry": {"type": "Point", "coordinates": [inc.lng, inc.lat]},
        "properties": {
            "id": inc.id,
            "title": inc.title,
            "incident_type": inc.incident_type,
            "type_label": vocab.type_label(inc.incident_type),
            "type_icon": vocab.type_icon(inc.incident_type),
            "type_color": vocab.type_color(inc.incident_type),
            "status": inc.status,
            "status_label": vocab.status_label(inc.status),
            "severity": inc.severity,
            "severity_label": vocab.severity_label(inc.severity),
            "severity_color": vocab.severity_color(inc.severity),
            "detected_at": inc.detected_at.isoformat() if inc.detected_at else None,
            "detail_url": url_for("incidents.detail", incident_id=inc.id),
        },
    }


def collection_geojson(status=None, incident_type=None, year=None):
    """Gefilterte FeatureCollection aller Stoerungen MIT gesetzter Lage.

    ``year`` filtert auf ``detected_at`` (nicht resolved_at/created_at).
    Filterwerte werden gegen das Vokabular validiert; positionslose Stoerungen
    werden still ausgelassen (sie erscheinen nur in der Liste)."""
    query = Incident.query.filter(Incident.lat.isnot(None), Incident.lng.isnot(None))
    if status and vocab.is_valid_status(status):
        query = query.filter(Incident.status == status)
    if incident_type and vocab.is_valid_type(incident_type):
        query = query.filter(Incident.incident_type == incident_type)
    if year:
        query = query.filter(db.extract("year", Incident.detected_at) == year)
    feats = [incident_to_geojson(inc) for inc in query.all()]
    return {"type": "FeatureCollection", "features": [f for f in feats if f]}


# ---------------------------------------------------------------------------
# Fotos
# ---------------------------------------------------------------------------

def delete_photo_files(photos):
    """Entfernt die Bilddateien der angegebenen IncidentPhotos vom Datentraeger
    (DB-Records loescht der ORM-Cascade)."""
    folder = incident_upload_dir()
    for p in photos:
        try:
            os.remove(os.path.join(folder, p.filename))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Auswertung (PDF/CSV)
# ---------------------------------------------------------------------------

def filtered_query(year=None, status=None, incident_type=None, severity=None):
    """Basis-Query fuer Liste/CSV/PDF mit den gleichen Filtern, jung-zuerst."""
    query = Incident.query
    if year:
        query = query.filter(db.extract("year", Incident.detected_at) == year)
    if status and vocab.is_valid_status(status):
        query = query.filter(Incident.status == status)
    if incident_type and vocab.is_valid_type(incident_type):
        query = query.filter(Incident.incident_type == incident_type)
    if severity and vocab.is_valid_severity(severity):
        query = query.filter(Incident.severity == severity)
    return query.order_by(Incident.detected_at.desc(), Incident.id.desc())


def report_aggregates(year=None):
    """Kennzahlen fuer PDF/CSV. Geld-/Mengen-Summen strikt mit ``Decimal``."""
    incidents = filtered_query(year=year).all()
    by_type, by_status = {}, {}
    cost_sum = Decimal("0")
    loss_sum = Decimal("0")
    affected_sum = 0
    affected_max = 0
    durations = []
    for inc in incidents:
        by_type[inc.incident_type] = by_type.get(inc.incident_type, 0) + 1
        by_status[inc.status] = by_status.get(inc.status, 0) + 1
        if inc.cost is not None:
            cost_sum += Decimal(str(inc.cost))
        if inc.water_loss_m3 is not None:
            loss_sum += Decimal(str(inc.water_loss_m3))
        if inc.affected_count:
            affected_sum += inc.affected_count
            affected_max = max(affected_max, inc.affected_count)
        d = inc.duration_days()
        if d is not None:
            durations.append(d)
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None
    return {
        "incidents": incidents,
        "total": len(incidents),
        "by_type": by_type,
        "by_status": by_status,
        "cost_sum": cost_sum,
        "loss_sum": loss_sum,
        "affected_sum": affected_sum,
        "affected_max": affected_max,
        "avg_duration_days": avg_duration,
        "resolved_count": len(durations),
    }


def available_years():
    """Distinct Jahre aus ``detected_at`` (absteigend) fuer den Jahr-Filter."""
    rows = (
        db.session.query(db.extract("year", Incident.detected_at))
        .filter(Incident.detected_at.isnot(None))
        .distinct()
        .all()
    )
    years = sorted({int(r[0]) for r in rows if r[0] is not None}, reverse=True)
    return years
