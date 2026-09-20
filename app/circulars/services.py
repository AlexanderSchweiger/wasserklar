"""Geschäftslogik der Rundschreiben: Platzhalter-Ersetzung, E-Mail-Eignung
(inkl. Notfall-Bypass), Empfänger-Sync und Karten-Zielauflösung.
"""
from collections import namedtuple

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    Customer, Incident, NetworkFeature, NetworkPlan, Property,
    Circular, CircularRecipient,
)
from app.email_suppression import is_suppressed, suppression_notice
from app.meter_tours.services import owners_by_property
from app.incidents import services as incident_services
from app.network import services as network_services


# ── Text-Platzhalter ─────────────────────────────────────────────────────────

def render_circular_text(text, customer):
    """Ersetzt die Auto-Platzhalter ``{anrede}``/``{name}`` durch die
    Empfänger-Werte (``salutation_line``/``letter_name``). Eckige Lückentexte
    ``[…]`` bleiben unangetastet — die füllt der Verfasser."""
    if not text:
        return ""
    anrede = customer.salutation_line if customer else "Sehr geehrte Damen und Herren"
    name = customer.letter_name if customer else ""
    return text.replace("{anrede}", anrede).replace("{name}", name)


# ── Empfänger ────────────────────────────────────────────────────────────────

def active_contacts():
    """Alle aktiven Kontakte (Empfängerauswahl), alphabetisch, mit vorgeladenem
    WG-Profil. Gilt für beide Mandant-Typen (kein WG-Gate)."""
    return (Customer.query
            .options(joinedload(Customer.wg_profile))
            .filter(Customer.active.is_(True))
            .order_by(Customer.name.asc())
            .all())


# Ergebnis der E-Mail-Eignungsprüfung eines Empfängers.
#   can_email  – darf per E-Mail versendet werden
#   suppressed – Adresse steht auf der Sperrliste (blockt IMMER, auch Notfall)
#   bypass     – Versand erfolgt ohne Einwilligung (nur bei Notfall-Arten)
#   notice     – deutscher Sperr-Hinweis (Flash/JSON) oder None
EmailEligibility = namedtuple("EmailEligibility",
                              ["can_email", "suppressed", "bypass", "notice"])


def email_eligibility(circular, customer):
    """Ob und wie an ``customer`` per E-Mail versendet werden darf.

    Reihenfolge (die Sperrliste hat IMMER Vorrang, auch im Notfall):
      1. Keine Adresse -> kein E-Mail-Versand.
      2. Adresse gesperrt -> blockiert.
      3. Notfall-Art -> Versand auch ohne Einwilligung (``bypass`` wenn keine
         Einwilligung vorliegt); Rechtsgrundlage Art. 6 Abs. 1 lit. d DSGVO.
      4. Sonst -> nur mit Einwilligung (``customer.wants_email``).
    """
    if not customer or not customer.email:
        return EmailEligibility(False, False, False, None)
    notice = suppression_notice(customer.email)
    if notice or is_suppressed(customer.email):
        return EmailEligibility(False, True, False, notice)
    if circular.is_emergency:
        return EmailEligibility(True, False, not customer.wants_email, None)
    return EmailEligibility(bool(customer.wants_email), False, False, None)


def default_method(circular, customer):
    """Vorgeschlagene Versandart je Empfänger: E-Mail wenn zulässig, sonst Post
    (falls Anschrift vorhanden), sonst „kein Versand"."""
    if email_eligibility(circular, customer).can_email:
        return CircularRecipient.METHOD_EMAIL
    if customer.address_display():
        return CircularRecipient.METHOD_POST
    return CircularRecipient.METHOD_NONE


def upsert_recipient(circular, customer, method=None):
    """Legt die Empfänger-Zeile an oder aktualisiert die Versandart."""
    rec = CircularRecipient.query.filter_by(
        circular_id=circular.id, customer_id=customer.id).first()
    if rec is None:
        rec = CircularRecipient(circular_id=circular.id, customer_id=customer.id)
        db.session.add(rec)
    if method is not None:
        rec.delivery_method = method
    db.session.flush()
    return rec


def sync_recipients(circular, selected_ids, methods):
    """Legt/aktualisiert die Empfänger der Auswahl; entfernt abgewählte OHNE
    Versand-History. Gibt die Empfänger der Auswahl zurück (Spiegel
    ``schriftfuehrung._sync_invitations``)."""
    existing = {r.customer_id: r for r in circular.recipients}
    for cid, rec in list(existing.items()):
        if cid not in selected_ids and not rec.email_sent_at and not rec.post_sent_at:
            db.session.delete(rec)
    result = []
    for cid in selected_ids:
        rec = existing.get(cid)
        if rec is None:
            rec = CircularRecipient(circular_id=circular.id, customer_id=cid)
            db.session.add(rec)
        rec.delivery_method = methods.get(cid)
        result.append(rec)
    db.session.flush()
    return result


def add_recipients(circular, customers):
    """Fügt Kontakte zur Empfängerliste hinzu (idempotent, ohne Methode zu
    überschreiben) — genutzt vom Karten-Handoff. Neue Zeilen bekommen die
    Vorschlags-Versandart. Gibt die Anzahl neu hinzugefügter zurück.

    Bestehende Empfänger werden frisch aus der DB gelesen (nicht über die
    ggf. gecachte ``recipients``-Relationship), damit mehrfaches Hinzufügen im
    selben Request nicht in den Unique-Constraint läuft."""
    existing = {
        cid for (cid,) in db.session.query(CircularRecipient.customer_id)
        .filter(CircularRecipient.circular_id == circular.id).all()
    }
    added = 0
    for c in customers:
        if c.id in existing:
            continue
        db.session.add(CircularRecipient(
            circular_id=circular.id, customer_id=c.id,
            delivery_method=default_method(circular, c)))
        existing.add(c.id)
        added += 1
    db.session.flush()
    return added


# ── Karten-Zielauflösung (Netzbereich-Targeting) ─────────────────────────────

def active_plan():
    """Aktiver Leitungsplan (Standardplan für die Karten-Auswahl); request-
    unabhängig. Erster ``aktiv``-Plan, sonst irgendein Plan, sonst None."""
    return (NetworkPlan.query.filter(NetworkPlan.status == "aktiv")
            .order_by(NetworkPlan.id.asc()).first()
            or NetworkPlan.query.order_by(NetworkPlan.id.asc()).first())


def all_plans():
    """Alle Leitungspläne für die Karten-Auswahl (Standardplan zuerst, dann
    nach Name) — Grundlage für die „weitere Pläne einblenden“-Auswahl."""
    return (NetworkPlan.query
            .order_by(NetworkPlan.status != NetworkPlan.STATUS_ACTIVE,
                      NetworkPlan.name.asc())
            .all())


def plan_lines_geojson(plan_ids):
    """Leitungs-Linien (kein Anlagen-Punkt) der gegebenen Pläne als GeoJSON —
    Hintergrund-Kontext auf der Rundschreiben-Kartenauswahl."""
    if not plan_ids:
        return {"type": "FeatureCollection", "features": []}
    features = (NetworkFeature.query
                .filter(NetworkFeature.plan_id.in_(plan_ids),
                        NetworkFeature.geometry_kind == NetworkFeature.GEOMETRY_LINE)
                .all())
    return network_services.collection_geojson(features)


def _property_label(prop):
    addr = prop.address_display()
    num = prop.object_number or f"Objekt #{prop.id}"
    return f"{num} — {addr}" if addr else num


def map_targets(plan):
    """Auswählbare Liegenschaften für die Karten-Auswahl.

    Koordinate bevorzugt aus dem Hausanschluss-Punkt (präzise Anschlussstelle),
    sonst aus dem BEV-Geocode der Liegenschaft. Nur Liegenschaften MIT Koordinate
    UND mindestens einem aktiven Eigentümer. Jedes Ziel trägt seine Eigentümer
    (mehrere möglich — Ehepaare/Erbengemeinschaften).

    ``source`` unterscheidet die Herkunft der Koordinate (``hausanschluss`` vs.
    ``property``) — die Karte zeichnet Hausanschlüsse mit dem Netz-Symbol, rein
    geocodete Liegenschaften mit einem neutralen Pin. ``props`` sind die
    GeoJSON-Properties für das Popup: bei Hausanschlüssen exakt die des
    Leitungsplans (``feature_to_geojson``), damit das Popup identisch aussieht;
    sonst ein schlankes Äquivalent aus den Liegenschaftsdaten."""
    if plan is None:
        return []
    coords = {}          # property_id -> (lat, lng)
    feature_by_pid = {}  # property_id -> NetworkFeature (Hausanschluss)
    hausanschluesse = (
        NetworkFeature.query
        .filter(NetworkFeature.plan_id == plan.id,
                NetworkFeature.feature_type == "hausanschluss",
                NetworkFeature.property_id.isnot(None),
                NetworkFeature.lat.isnot(None),
                NetworkFeature.lng.isnot(None))
        .all()
    )
    for f in hausanschluesse:
        if f.property_id in coords:
            continue
        coords[f.property_id] = (f.lat, f.lng)
        feature_by_pid[f.property_id] = f

    geocoded = (Property.query
                .filter(Property.lat.isnot(None), Property.lng.isnot(None))
                .all())
    prop_by_id = {p.id: p for p in geocoded}
    for p in geocoded:
        coords.setdefault(p.id, (p.lat, p.lng))

    # Liegenschaften, die nur über den Hausanschluss Koordinaten haben, nachladen.
    missing = [pid for pid in coords if pid not in prop_by_id]
    if missing:
        for p in Property.query.filter(Property.id.in_(missing)).all():
            prop_by_id[p.id] = p

    # Popup-Properties der Hausanschlüsse gebündelt über die Netz-Serialisierung
    # (batcht Liegenschaft/Eigentümer/Zählungen — kein N+1 pro Feature).
    props_by_pid = {}
    if feature_by_pid:
        collection = network_services.collection_geojson(list(feature_by_pid.values()))
        by_feature_id = {f["id"]: f["properties"] for f in collection["features"]}
        for pid, f in feature_by_pid.items():
            props = by_feature_id.get(f.id)
            if props is not None:
                props_by_pid[pid] = props

    owners = owners_by_property(list(coords.keys()))
    targets = []
    for pid, (lat, lng) in coords.items():
        prop = prop_by_id.get(pid)
        olist = [o for o in owners.get(pid, []) if o and o.active]
        if not prop or not olist:
            continue
        props = props_by_pid.get(pid)
        if props is None:
            # Kein Hausanschluss-Punkt — Popup aus den Liegenschaftsdaten.
            # ``feature_type: None`` -> neutraler grauer Pin im Karten-JS.
            props = {
                "feature_type": None,
                "type_label": "Liegenschaft (Adress-Geokodierung)",
                "name": prop.label(),
                "property_id": pid,
                "property_label": prop.label(),
                "property_address": prop.address_display(),
                "owner_names": [o.letter_name for o in olist],
            }
        targets.append({
            "property_id": pid,
            "lat": lat,
            "lng": lng,
            "source": "hausanschluss" if pid in props_by_pid else "property",
            "label": _property_label(prop),
            "owners": [{"id": o.id, "name": o.letter_name} for o in olist],
            "props": props,
        })
    return targets


def map_incidents_geojson(circular=None):
    """Störungen für die Karten-Auswahl (eigene, ein-/ausblendbare Ebene).

    Gezeigt wird die aktuelle Lage — offene und in Bearbeitung befindliche
    Störungen MIT Position. Die Störung, aus der das Rundschreiben erzeugt
    wurde, kommt immer mit, auch wenn sie bereits behoben ist (die Entwarnung
    ist selbst ein Rundschreiben)."""
    query = Incident.query.filter(Incident.lat.isnot(None), Incident.lng.isnot(None))
    conditions = [Incident.status != Incident.STATUS_RESOLVED]
    if circular is not None and circular.incident_id:
        conditions.append(Incident.id == circular.incident_id)
    query = query.filter(db.or_(*conditions))
    feats = [incident_services.incident_to_geojson(inc) for inc in query.all()]
    return {"type": "FeatureCollection", "features": [f for f in feats if f]}


def resolve_customers_from_properties(property_ids):
    """Aktive Eigentümer der übergebenen Liegenschaften, dedupliziert (mehrere
    aktive Eigentümer je Objekt möglich — nie ``.scalar()``)."""
    if not property_ids:
        return []
    owners = owners_by_property(list(property_ids))
    seen = {}
    for plist in owners.values():
        for o in plist:
            if o and o.active:
                seen[o.id] = o
    return list(seen.values())
