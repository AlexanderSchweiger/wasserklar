"""Service-Funktionen fuer das Technik-/Leitungsplan-Modul.

- GeoJSON-(De)Serialisierung (NetworkFeature <-> GeoJSON-Feature)
- Haversine-Laengenberechnung fuer Linien (``length_m``)
- ``inspections_due()`` fuer die Dashboard-Erinnerung „Faellige Pruefungen"
- ``technik_upload_dir()`` — tenant-sicherer Foto-Ordner (reitet auf ``PDF_DIR``)
"""

import json
import math
import os
from datetime import date

from flask import current_app

from app.extensions import db
from app.models import NetworkFeature, MaintenanceLog, NetworkPlan
from app.technik import vocab


# ---------------------------------------------------------------------------
# Datei-Ablage
# ---------------------------------------------------------------------------

def technik_upload_dir():
    """Tenant-sicherer Ordner fuer Feature-Fotos.

    Reitet auf dem bereits per-Request umgebogenen ``PDF_DIR``
    (SaaS: ``instance/tenants/<slug>/pdfs`` -> ``.../technik``; OSS standalone:
    ``instance/pdfs`` -> ``instance/technik``). Damit ist die Tenant-Trennung
    geschenkt, ohne dass die SaaS-Schicht angefasst werden muss.
    """
    base = os.path.dirname(current_app.config["PDF_DIR"])
    path = os.path.join(base, "technik")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _to_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def add_months(d, months):
    """Addiert ``months`` Monate auf ein Datum (Tagesueberlauf wird gekappt)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(d.day, days_in_month[month - 1])
    return date(year, month, day)


def haversine_m(lat1, lng1, lat2, lng2):
    """Grosskreis-Distanz zweier WGS84-Punkte in Metern."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def linestring_length_m(coords):
    """Laenge einer GeoJSON-LineString-Koordinatenliste ``[[lng,lat], ...]`` in Metern."""
    total = 0.0
    for (lng1, lat1), (lng2, lat2) in zip(coords, coords[1:]):
        total += haversine_m(lat1, lng1, lat2, lng2)
    return total


# ---------------------------------------------------------------------------
# GeoJSON <-> Model
# ---------------------------------------------------------------------------

def apply_geometry(feature, geometry):
    """Setzt ``geometry``/``geometry_kind``/``lat``/``lng``/``length_m`` aus einem
    GeoJSON-Geometry-Dict. Wirft ``ValueError`` bei ungueltiger Geometrie."""
    if not isinstance(geometry, dict):
        raise ValueError("geometry fehlt oder ist kein Objekt")
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point":
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            raise ValueError("Point-Koordinaten ungültig")
        lng, lat = float(coords[0]), float(coords[1])
        feature.geometry_kind = NetworkFeature.GEOMETRY_POINT
        feature.lat, feature.lng, feature.length_m = lat, lng, None
        feature.geometry = json.dumps({"type": "Point", "coordinates": [lng, lat]})
    elif gtype == "LineString":
        if not (isinstance(coords, list) and len(coords) >= 2):
            raise ValueError("LineString braucht mindestens 2 Punkte")
        clean = [[float(c[0]), float(c[1])] for c in coords]
        feature.geometry_kind = NetworkFeature.GEOMETRY_LINE
        feature.lat = feature.lng = None
        feature.length_m = linestring_length_m(clean)
        feature.geometry = json.dumps({"type": "LineString", "coordinates": clean})
    else:
        raise ValueError(f"Geometrietyp nicht unterstützt: {gtype}")


def apply_attributes(feature, data):
    """Setzt die Sachattribute aus einem dict (JSON-Payload oder Formular).
    Geometrie wird hier NICHT angefasst (siehe ``apply_geometry``)."""
    ft = (data.get("feature_type") or "").strip()
    if ft and vocab.is_valid_type(ft, feature.geometry_kind):
        feature.feature_type = ft

    feature.name = (data.get("name") or "").strip() or None

    acc = (data.get("accuracy") or "").strip()
    if acc in vocab.ACCURACIES:
        feature.accuracy = acc

    feature.material = (data.get("material") or "").strip() or None
    feature.dimension_dn = _to_int(data.get("dimension_dn"))
    feature.year_built = _to_int(data.get("year_built"))
    feature.notes = (data.get("notes") or "").strip() or None
    feature.property_id = _to_int(data.get("property_id"))
    feature.meter_id = _to_int(data.get("meter_id"))


def feature_to_geojson(f):
    """NetworkFeature -> GeoJSON-Feature-Dict (inkl. abgeleiteter Display-Props)."""
    return {
        "type": "Feature",
        "id": f.id,
        "geometry": json.loads(f.geometry),
        "properties": {
            "id": f.id,
            "geometry_kind": f.geometry_kind,
            "feature_type": f.feature_type,
            "type_label": vocab.feature_type_label(f.feature_type),
            "color": vocab.feature_type_color(f.feature_type),
            "name": f.name,
            "accuracy": f.accuracy,
            "material": f.material,
            "dimension_dn": f.dimension_dn,
            "year_built": f.year_built,
            "notes": f.notes,
            "property_id": f.property_id,
            "meter_id": f.meter_id,
            "length_m": round(f.length_m, 1) if f.length_m is not None else None,
            "photo_count": len(f.photos),
            "maintenance_count": len(f.maintenance_logs),
        },
    }


def collection_geojson(features):
    return {
        "type": "FeatureCollection",
        "features": [feature_to_geojson(f) for f in features],
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def iter_geojson_features(raw_text):
    """Liefert GeoJSON-Feature-Dicts aus rohem Text (FeatureCollection ODER
    einzelnes Feature). Wirft ``ValueError`` bei kaputtem JSON / falscher Struktur."""
    obj = json.loads(raw_text)
    if not isinstance(obj, dict):
        raise ValueError("GeoJSON-Wurzel ist kein Objekt")
    t = obj.get("type")
    if t == "FeatureCollection":
        feats = obj.get("features") or []
    elif t == "Feature":
        feats = [obj]
    else:
        raise ValueError("Erwarte FeatureCollection oder Feature")
    return [f for f in feats if isinstance(f, dict) and f.get("type") == "Feature"]


def build_feature_from_geojson(feat, user_id=None, plan_id=None):
    """Baut (uncommitted) eine NetworkFeature aus einem GeoJSON-Feature.
    Wirft ``ValueError`` bei nicht unterstuetzter Geometrie.

    ``plan_id`` ordnet das Feature dem Ziel-Plan zu (Pflichtspalte) — beim Import
    der aktuell gewaehlte Plan. Bei der reinen Vorschau (``summarize_geojson``)
    bleibt es ``None``, da dort nichts committed wird."""
    geom = feat.get("geometry") or {}
    props = feat.get("properties") or {}

    nf = NetworkFeature()
    nf.plan_id = plan_id
    apply_geometry(nf, geom)  # kann ValueError werfen -> vom Aufrufer gezaehlt

    ft = (props.get("feature_type") or "").strip()
    if not vocab.is_valid_type(ft, nf.geometry_kind):
        ft = "sonstige_leitung" if nf.is_line() else "sonstiges"
    nf.feature_type = ft

    nf.name = (props.get("name") or "").strip() or None
    acc = (props.get("accuracy") or "").strip()
    nf.accuracy = acc if acc in vocab.ACCURACIES else NetworkFeature.ACCURACY_ESTIMATED
    nf.material = (props.get("material") or "").strip() or None
    nf.dimension_dn = _to_int(props.get("dimension_dn"))
    nf.year_built = _to_int(props.get("year_built"))
    nf.notes = (props.get("notes") or "").strip() or None
    nf.created_by_id = user_id

    # Optionale Wartungsinfo aus dem Import (z.B. WLK-Shapefile: Wartungsintervall
    # + letzte Wartung) als MaintenanceLog mitnehmen, damit das Dashboard-Widget
    # „Faellige Pruefungen" greift. Plain-GeoJSON ohne diese Props bleibt unberuehrt.
    log = build_maintenance_from_props(props, user_id)
    if log is not None:
        nf.maintenance_logs.append(log)
    return nf


def build_maintenance_from_props(props, user_id=None):
    """Erzeugt (uncommitted) einen ``MaintenanceLog`` aus ``maintenance_*``-
    Properties eines GeoJSON-Features, oder ``None`` wenn keine Wartungsinfo
    vorhanden ist. ``next_due`` = Datum + Intervall (treibt das Dashboard)."""
    interval = _to_int(props.get("maintenance_interval_months"))
    last_raw = (props.get("maintenance_last_date") or "").strip()
    log_notes = (props.get("maintenance_notes") or "").strip() or None
    kind = (props.get("maintenance_kind") or "").strip()
    if interval is None and not last_raw and not log_notes:
        return None

    try:
        log_date = date.fromisoformat(last_raw) if last_raw else date.today()
    except ValueError:
        log_date = date.today()
    if kind not in vocab.MAINTENANCE_KINDS:
        kind = MaintenanceLog.KIND_INSPECTION

    return MaintenanceLog(
        date=log_date,
        kind=kind,
        interval_months=interval,
        next_due=add_months(log_date, interval) if interval else None,
        notes=log_notes,
        created_by_id=user_id,
    )


def summarize_geojson(raw_text):
    """Fuer die Import-Vorschau: (counts_by_type_label, total, skipped).
    Zaehlt, ohne in die DB zu schreiben."""
    counts, total, skipped = {}, 0, 0
    for feat in iter_geojson_features(raw_text):
        try:
            nf = build_feature_from_geojson(feat)
        except (ValueError, TypeError):
            skipped += 1
            continue
        label = vocab.feature_type_label(nf.feature_type)
        counts[label] = counts.get(label, 0) + 1
        total += 1
    return counts, total, skipped


# ---------------------------------------------------------------------------
# Plan-Kopie / Merge
# ---------------------------------------------------------------------------

# Geometrie + Sachdaten eines Features (ohne Wartung/Fotos/Abstammung).
FEATURE_COPY_ATTRS = (
    "geometry_kind", "feature_type", "name", "geometry",
    "lat", "lng", "length_m", "accuracy", "material",
    "dimension_dn", "year_built", "notes", "property_id", "meter_id",
)


def apply_feature_data(dst, src):
    """Kopiert Geometrie + Sachdaten von ``src`` auf ``dst`` (Wartungs-Logs und
    Fotos bleiben unberuehrt — wichtig beim Merge in den Hauptplan)."""
    for attr in FEATURE_COPY_ATTRS:
        setattr(dst, attr, getattr(src, attr))


def clone_feature(src, plan_id, source_feature_id, uid=None):
    """Neues (uncommitted) NetworkFeature als Kopie von ``src`` im Ziel-Plan."""
    nf = NetworkFeature(
        plan_id=plan_id, source_feature_id=source_feature_id, created_by_id=uid,
    )
    apply_feature_data(nf, src)
    return nf


def delete_photo_files(photos):
    """Entfernt die Bilddateien der angegebenen FeaturePhotos vom Datentraeger
    (DB-Records loescht der ORM-Cascade)."""
    folder = technik_upload_dir()
    for p in photos:
        try:
            os.remove(os.path.join(folder, p.filename))
        except OSError:
            pass


def copy_plan(src, uid=None):
    """Legt einen Entwurfs-Plan als Kopie von ``src`` an — nur Features
    (Geometrie + Sachdaten), ohne Wartungs-Logs/Fotos, Wartung deaktiviert.
    Jedes kopierte Feature merkt sich via ``source_feature_id`` sein Quell-Feature
    (Basis fuer den spaeteren Merge). Committed; gibt ``(plan, anzahl)`` zurueck."""
    dup = NetworkPlan(
        name=f"{src.name} (Kopie)",
        status=NetworkPlan.STATUS_DRAFT,
        maintenance_enabled=False,
        description=src.description,
        source_plan_id=src.id,
        created_by_id=uid,
        updated_by_id=uid,
    )
    db.session.add(dup)
    db.session.flush()  # dup.id
    count = 0
    for sf in src.features:
        db.session.add(clone_feature(sf, dup.id, sf.id, uid))
        count += 1
    db.session.commit()
    return dup, count


def merge_plan_into_source(copy, uid=None):
    """Spiegelt *alle* Aenderungen der Kopie in ihren Quellplan:

    - in der Kopie geloeschte Quell-Features werden auch im Quellplan geloescht,
    - bestehende (ueber ``source_feature_id`` verknuepfte) werden aktualisiert
      (nur Geometrie + Sachdaten — Wartungs-Logs/Fotos des Quellplans bleiben),
    - neu gezeichnete werden im Quellplan angelegt und in der Kopie
      zurueckverlinkt (ein zweiter Merge aktualisiert dann statt zu duplizieren).

    Committed. Gibt ``{added, updated, deleted, source}`` zurueck, oder ``None``
    wenn die Kopie keinen Quellplan (mehr) hat."""
    src = copy.source_plan
    if src is None:
        return None

    referenced = {f.source_feature_id for f in copy.features if f.source_feature_id is not None}
    src_ids = {af.id for af in src.features}
    added = updated = deleted = 0

    # 1) Spiegel-Loeschungen (vor den Inserts -> Snapshot der Quell-IDs nutzen).
    for af in list(src.features):
        if af.id not in referenced:
            delete_photo_files(list(af.photos))
            db.session.delete(af)
            deleted += 1

    # 2) Updates (bestehende Abstammung) + neue Features (neu gezeichnet).
    for cf in copy.features:
        if cf.source_feature_id is not None and cf.source_feature_id in src_ids:
            af = NetworkFeature.query.get(cf.source_feature_id)
            if af is not None:
                apply_feature_data(af, cf)
                af.updated_by_id = uid
                updated += 1
        else:
            nf = clone_feature(cf, src.id, None, uid)
            db.session.add(nf)
            db.session.flush()             # nf.id
            cf.source_feature_id = nf.id   # Re-Link
            added += 1

    src.updated_by_id = uid
    db.session.commit()
    return {"added": added, "updated": updated, "deleted": deleted, "source": src}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def inspections_due(today=None, limit=None):
    """NetworkFeatures mit faelliger Pruefung.

    Logik: der je Feature *juengste* MaintenanceLog (nach Datum) gilt; ist dort
    ``next_due`` gesetzt und ``<= today``, ist die Pruefung faellig. Ein neuerer
    Eintrag ohne ``next_due`` setzt den Zeitplan also bewusst zurueck.

    Liefert eine nach Faelligkeit sortierte Liste von Dicts
    ``{feature, log, due, overdue_days}``.

    Beruecksichtigt nur Features in Plaenen mit ``status='aktiv'`` UND
    ``maintenance_enabled`` — Planungs-Kopien (Wartung aus) und archivierte
    Plaene erzeugen also bewusst keine Erinnerung.
    """
    today = today or date.today()
    logs = (
        MaintenanceLog.query
        .join(NetworkFeature, MaintenanceLog.feature_id == NetworkFeature.id)
        .join(NetworkPlan, NetworkFeature.plan_id == NetworkPlan.id)
        .filter(
            NetworkPlan.maintenance_enabled.is_(True),
            NetworkPlan.status == NetworkPlan.STATUS_ACTIVE,
        )
        .order_by(
            MaintenanceLog.feature_id,
            MaintenanceLog.date.desc(),
            MaintenanceLog.id.desc(),
        )
        .all()
    )
    latest = {}
    for log in logs:
        latest.setdefault(log.feature_id, log)  # erster je Feature = juengster

    due = []
    for log in latest.values():
        if log.next_due and log.next_due <= today:
            due.append({
                "feature": log.feature,
                "log": log,
                "due": log.next_due,
                "overdue_days": (today - log.next_due).days,
            })
    due.sort(key=lambda r: r["due"])
    return due[:limit] if limit else due
