"""Speicherbelegung des Mandanten — Grundlage fuer Kontingent und Speicheruebersicht.

Gezaehlt wird alles, was der Mandant dauerhaft ablegt: jedes ``Document`` (Belege,
Ausgangsrechnungen, Mahnungen, Protokolle, Schriftverkehr) plus die Fotos aus Leitungsnetz und
Stoerungsjournal (``size_bytes``; Bestand ohne Groesse zaehlt 0, bis ``documents-register-files``
nachmisst). Nicht gezaehlt: Datensicherungen und Temporaeres.
"""
from sqlalchemy import func

from app.extensions import db
from app.models import Document, FeaturePhoto, IncidentPhoto

PHOTO_SOURCES = {
    "network_photos": ("Fotos Leitungsnetz", FeaturePhoto),
    "incident_photos": ("Fotos Störungsjournal", IncidentPhoto),
}


def usage_by_area():
    """``{schluessel: {"label", "count", "bytes"}}`` — Bereiche des Registers, dann die Fotos."""
    result = {area: {"label": label, "count": 0, "bytes": 0} for area, label in Document.AREA_LABELS.items()}
    rows = (db.session.query(Document.area, func.count(Document.id), func.coalesce(func.sum(Document.size_bytes), 0))
            .group_by(Document.area).all())
    for area, count, size in rows:
        entry = result.setdefault(area, {"label": Document.AREA_LABELS.get(area, area), "count": 0, "bytes": 0})
        entry["count"], entry["bytes"] = int(count or 0), int(size or 0)
    for key, (label, model) in PHOTO_SOURCES.items():
        count, size = db.session.query(func.count(model.id), func.coalesce(func.sum(model.size_bytes), 0)).one()
        result[key] = {"label": label, "count": int(count or 0), "bytes": int(size or 0)}
    return result


def total_used():
    """Belegte Bytes (Register + Fotos) — das, was gegen das Kontingent zaehlt."""
    documents = db.session.query(func.coalesce(func.sum(Document.size_bytes), 0)).scalar() or 0
    photos = sum(db.session.query(func.coalesce(func.sum(model.size_bytes), 0)).scalar() or 0
                 for _label, model in PHOTO_SOURCES.values())
    return int(documents) + int(photos)
