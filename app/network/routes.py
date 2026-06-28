"""Routen des Technik-/Leitungsplan-Moduls.

Aufteilung:
- Geometrie-Mutationen (Zeichnen/Verschieben) laufen als JSON ueber ``fetch``
  (Leaflet-Geoman-Events) — Antwort ist das aktualisierte GeoJSON-Feature.
- Panel-Aktionen (Attribute, Wartung, Fotos, Loeschen) laufen als HTMX-Forms
  und liefern das neu gerenderte ``_feature_panel.html``-Fragment. Aenderungen,
  die die Kartendarstellung betreffen (Typ/Genauigkeit), schicken zusaetzlich
  einen ``HX-Trigger``, damit das Karten-JS den Layer ohne Vollreload restyled.

Das Blueprint-``before_request`` (require_blueprint_permission) blockt nicht-
authentifizierte Requests NICHT — daher traegt jede Route ``@login_required``.
"""

import csv
import io
import json
import os
import secrets
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from flask import (
    render_template, request, jsonify, redirect, url_for, flash,
    make_response, send_from_directory, session, current_app, abort,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.network import bp
from app.network import services as svc
from app.network import vocab
from app.network import water_quality as wq
from app.network import wlk_import as wlk
from app.extensions import db
from app.pagination import paginate_list
from app.models import (
    NetworkPlan, NetworkFeature, MaintenanceLog, SpringYield, FeaturePhoto,
    WaterSample, LabResult, AppSetting,
    HydrantShareLink, Property, PropertyOwnership, Customer,
)

_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Elementliste: erlaubte Sort-Keys (URL-Param -> Sortier-Logik in ``_sort_features``).
_ELEM_SORT_KEYS = {"plan", "name", "feature_type", "accuracy", "length_m",
                   "year_built", "wartung", "objekt", "besitzer"}
_ELEM_DEFAULT_SORT = "name"
_ELEM_PAGE_KEY = "network_elemente"
# Gueltige Feature-Typ-Keys (Punkt + Linie) fuer den Typ-Filter der Elementliste.
_ALL_TYPE_KEYS = set(vocab.POINT_TYPES) | set(vocab.LINE_TYPES)
# Sortier-Rang der Lagegenauigkeit aus der Vokabular-Reihenfolge (geschaetzt < gut < exakt).
_ACCURACY_RANK = {key: i for i, key in enumerate(vocab.ACCURACIES)}


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------

def _map_config(plan):
    """Konfig fuer das Karten-JS (window.TECHNIK). CSRF-Token haengt das
    Template an, damit der Token nicht durch den Server-Cache wandert.

    ``featuresUrl`` ist auf den aktuellen Plan gefiltert; ``planId`` schickt das
    JS beim Anlegen neuer Features mit."""
    pid = plan.id if plan else None
    return {
        "base": url_for("network.index"),
        "featuresUrl": url_for("network.features_geojson", plan=pid),
        "createUrl": url_for("network.feature_create"),
        # Basis-URL fuer den Liegenschafts-Link im Popup (JS haengt die ID an).
        "propertyUrl": url_for("properties.detail", property_id=0).rsplit("/", 1)[0] + "/",
        "planId": pid,
        "vocab": vocab.as_client_dict(),
    }


def _hydrant_map_config(plan):
    """Schlanke Karten-Konfig fuer den Feuerwehr-/Hydranten-Druck (read-only):
    nur Hydranten + Versorgungs-/Hauptleitungen, kein Anlegen/Liegenschafts-Link.
    ``type`` ist wiederholbar (siehe ``features_geojson``)."""
    pid = plan.id if plan else None
    return {
        "base": url_for("network.index"),
        "featuresUrl": url_for(
            "network.features_geojson", plan=pid, type=list(svc.FEUERWEHR_TYPES),
        ),
        "planId": pid,
        "vocab": vocab.as_client_dict(),
    }


def current_plan():
    """Aktuell gewaehlter Leitungsplan fuer Karte/Import/Druck.

    Aufloesung: ``?plan=<id>`` (URL) -> Session-Merker -> erster aktiver Plan ->
    irgendein Plan -> ``None`` (es existiert noch kein Plan). Die Wahl wird in der
    Session gemerkt, damit sie ueber Seitenwechsel haelt.
    """
    plan = None
    pid = request.args.get("plan", type=int)
    if pid:
        plan = NetworkPlan.query.get(pid)
    if plan is None:
        sid = session.get("network_plan_id")
        if sid:
            plan = NetworkPlan.query.get(sid)
    if plan is None:
        plan = (
            NetworkPlan.query.filter_by(status=NetworkPlan.STATUS_ACTIVE)
            .order_by(NetworkPlan.id.asc()).first()
        )
    if plan is None:
        plan = NetworkPlan.query.order_by(NetworkPlan.id.asc()).first()
    if plan is not None:
        session["network_plan_id"] = plan.id
    return plan


def _owner_names_map(property_ids):
    """{property_id: [Besitzer-Name, ...]} der aktuell gueltigen Eigentuemer
    (``valid_to IS NULL``). Mehrere parallele Besitzer sind erlaubt (z.B.
    Ehepaare/Erbengemeinschaften), daher eine Namensliste je Objekt."""
    ids = list(property_ids)
    if not ids:
        return {}
    rows = (
        db.session.query(PropertyOwnership.property_id, Customer.name)
        .join(Customer, PropertyOwnership.customer_id == Customer.id)
        .filter(PropertyOwnership.property_id.in_(ids),
                PropertyOwnership.valid_to.is_(None))
        .all()
    )
    out = {}
    for pid, name in rows:
        out.setdefault(pid, []).append(name)
    return out


def _link_options():
    """Objekte (Liegenschaften) inkl. Besitzer fuer das Verknuepfungs-Dropdown im
    Element-Panel. Eine Wasserzaehler-Zuordnung gibt es bewusst nicht mehr — das
    zugeordnete Objekt genuegt."""
    properties = (
        Property.query.filter_by(active=True)
        .order_by(Property.object_number.asc())
        .all()
    )
    return {
        "properties": properties,
        "owner_map": _owner_names_map([p.id for p in properties]),
    }


def _render_panel(f):
    return render_template(
        "network/_feature_panel.html",
        f=f, vocab=vocab, wq=wq, today_iso=date.today().isoformat(), **_link_options()
    )


def _render_maintenance_modal_body(f):
    """Body des „Wartung & Prüfung"-Modals der Elementliste (Liste + Felder, ohne <form>)."""
    return render_template(
        "network/_maintenance_modal_body.html",
        f=f, vocab=vocab, today_iso=date.today().isoformat(),
    )


def _render_yield_modal_body(f):
    """Body des „Quellschüttung"-Modals der Elementliste (Liste + Felder, ohne <form>)."""
    return render_template(
        "network/_yield_modal_body.html",
        f=f, today_iso=date.today().isoformat(),
    )


def _render_sample_modal_body(f):
    """Body des „Wasserprobe"-Modals der Elementliste (Liste + Parameter-Felder,
    ohne <form>)."""
    return render_template(
        "network/_sample_modal_body.html",
        f=f, wq=wq, catalog=wq.catalog_for_form(),
        today_iso=date.today().isoformat(),
    )


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_decimal(value):
    """'1,5' / '1.5' -> Decimal; None bei leer/ungueltig (z. B. 'n.n.', '< 0,01').
    Dezimaltrenner Komma oder Punkt."""
    raw = (value or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        d = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _de_decimal(value):
    """Decimal/Float -> deutscher String mit Dezimalkomma, leer bei None (CSV)."""
    if value is None:
        return ""
    return str(value).replace(".", ",")


def _add_months(d, months):
    """Addiert ``months`` Monate auf ein Datum (Tagesueberlauf wird gekappt)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(d.day, days_in_month[month - 1])
    return date(year, month, day)


def _delete_photo_files(photos):
    svc.delete_photo_files(photos)


# ---------------------------------------------------------------------------
# Karte / Listen
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    plan = current_plan()
    plans = NetworkPlan.query.order_by(NetworkPlan.name.asc()).all()
    feature_count = (
        NetworkFeature.query.filter_by(plan_id=plan.id).count() if plan else 0
    )
    return render_template(
        "network/index.html",
        cfg=_map_config(plan),
        plan=plan,
        plans=plans,
        feature_count=feature_count,
        unassigned_count=svc.count_unassigned_hausanschluss(plan.id if plan else None),
        vocab=vocab,
    )


@bp.route("/assign-hausanschluss", methods=["POST"])
@login_required
def assign_hausanschluss():
    """Ordnet die Hausanschluss-Punkte des aktuellen Plans automatisch der
    jeweils naechstgelegenen (geocodeten) Liegenschaft zu. Drei-Punkte-Menue
    der Karte. Danach Redirect auf die Karte — unzugeordnete Hausanschluesse
    erscheinen grell.

    SaaS-only-Komfortfeature: im OSS-Standalone ist FEATURE_HAUSANSCHLUSS_AUTOASSIGN
    aus -> 404. Die manuelle Zuordnung (Liegenschaft im Feature-Formular) bleibt
    fuer alle verfuegbar."""
    if not current_app.config.get("FEATURE_HAUSANSCHLUSS_AUTOASSIGN"):
        abort(404)

    plan = current_plan()
    if plan is None:
        flash("Kein Plan gewählt — bitte zuerst einen Plan anlegen.", "warning")
        return redirect(url_for("network.index"))

    raw = (request.form.get("max_distance_m") or "").strip().replace(",", ".")
    try:
        max_dist = float(raw) if raw else svc.DEFAULT_ASSIGN_DISTANCE_M
    except ValueError:
        max_dist = svc.DEFAULT_ASSIGN_DISTANCE_M
    if max_dist <= 0:
        max_dist = svc.DEFAULT_ASSIGN_DISTANCE_M
    only_missing = request.form.get("mode") != "all"

    res = svc.assign_hausanschluss_to_properties(
        plan.id, max_distance_m=max_dist, only_missing=only_missing,
    )

    if res["geocoded_total"] == 0:
        flash("Keine geocodeten Liegenschaften vorhanden — bitte zuerst unter "
              "Liegenschaften „BEV-Adressen abgleichen“ ausführen.", "warning")
    elif res["considered"] == 0:
        flash("Keine passenden Hausanschlüsse gefunden (alle bereits zugeordnet "
              "oder keiner mit Koordinate). Für eine Neu-Zuordnung „Alle neu“ wählen.",
              "info")
    else:
        category = "success" if res["assigned"] else "warning"
        msg = (f"Hausanschluss-Zuordnung: {res['assigned']} von {res['considered']} "
               f"zugeordnet (Radius {max_dist:g} m, je Liegenschaft höchstens ein Anschluss).")
        if res["unmatched"]:
            msg += (f" {res['unmatched']} ohne freie Liegenschaft im Radius — "
                    f"grell markiert.")
        flash(msg, category)
    return redirect(url_for("network.index", plan=plan.id))


@bp.route("/features.geojson")
@login_required
def features_geojson():
    pid = request.args.get("plan", type=int)
    if not pid:
        plan = current_plan()
        pid = plan.id if plan else None
    query = NetworkFeature.query.filter(NetworkFeature.plan_id == pid)
    # ``?type=`` ist wiederholbar (z. B. der Feuerwehr-Plan filtert auf Hydrant +
    # Versorgungs-/Hauptleitung); ein einzelnes ``?type=hydrant`` bleibt gueltig.
    ftypes = [t for t in request.args.getlist("type") if t]
    if ftypes:
        query = query.filter(NetworkFeature.feature_type.in_(ftypes))
    return jsonify(svc.collection_geojson(query.all()))


# ---------------------------------------------------------------------------
# Elementliste (plan-uebergreifend, sortier- + durchsuchbar)
# ---------------------------------------------------------------------------

def _norm(s):
    """Akzent-/Case-insensitiv normalisieren (z. B. „Überlauf" -> „uberlauf")."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def _feature_property_owner_maps(features):
    """({property_id: Property}, {property_id: [Besitzer-Name, ...]}) fuer die
    verknuepften Objekte der uebergebenen Features — je eine Query, kein N+1."""
    pids = {f.property_id for f in features if f.property_id}
    if not pids:
        return {}, {}
    property_map = {p.id: p for p in Property.query.filter(Property.id.in_(pids)).all()}
    return property_map, _owner_names_map(pids)


def _matches_query(f, q, property_map, owner_map):
    """Freitext-Filter (Mehrwort-UND, akzentfrei) ueber Bezeichnung, Notiz,
    Material sowie das zugeordnete Objekt (Nr./Adresse) und dessen Besitzer.
    Der Typ wird bewusst NICHT durchsucht — dafuer gibt es den eigenen Typ-Filter."""
    prop = property_map.get(f.property_id)
    owners = owner_map.get(f.property_id, [])
    hay = _norm(" ".join(filter(None, [
        f.name,
        f.notes,
        f.material,
        f.manufacturer,
        prop.label() if prop else None,
        prop.object_number if prop else None,
        " ".join(owners),
    ])))
    return all(tok in hay for tok in _norm(q).split())


def _sort_features(features, sort, direction, status_map, property_map, owner_map):
    """Sortiert die Feature-Liste in Python — ``wartung`` (``next_due``), ``objekt``
    und ``besitzer`` sind abgeleitet und nicht direkt SQL-sortierbar, daher der
    einheitliche Weg. NULL-/Leerwerte wandern in beiden Richtungen ans Ende
    (Partition: vorhandene zuerst, fehlende anhaengen). Sekundaer stabil nach id.
    Volumen klein (max. einige hundert Features ueber alle Plaene)."""
    desc = direction == "desc"

    def key_of(f):
        if sort == "plan":
            return (f.plan.name or "").casefold()
        if sort == "feature_type":
            return vocab.feature_type_label(f.feature_type).casefold()
        if sort == "accuracy":
            return _ACCURACY_RANK.get(f.accuracy)
        if sort == "length_m":
            return f.length_m
        if sort == "year_built":
            return f.year_built
        if sort == "wartung":
            st = status_map.get(f.id)
            return st["next_due"] if st else None
        if sort == "objekt":
            prop = property_map.get(f.property_id)
            return prop.label().casefold() if prop else None
        if sort == "besitzer":
            owners = owner_map.get(f.property_id)
            return ", ".join(owners).casefold() if owners else None
        return (f.label() or "").casefold()  # default: name

    def is_missing(v):
        return v is None or v == ""

    feats = sorted(features, key=lambda f: f.id)  # stabiler Sekundaer-Sort
    present = [f for f in feats if not is_missing(key_of(f))]
    missing = [f for f in feats if is_missing(key_of(f))]
    present.sort(key=key_of, reverse=desc)
    return present + missing


@bp.route("/elements")
@login_required
def elements():
    """Plan-uebergreifende, sortier- + durchsuchbare Elementliste mit Wartungs-
    Status, zugeordnetem Objekt (Liegenschaft) und dessen Besitzer. Optionale
    Filter: ``?plan=<id>`` und ``?type=<feature_type>`` (kein Session-Merker wie
    ``current_plan`` — die Liste ist bewusst plan-uebergreifend).

    URL bewusst englisch (``/technik/elements``); UI-Texte bleiben deutsch."""
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", _ELEM_DEFAULT_SORT)
    if sort not in _ELEM_SORT_KEYS:
        sort = _ELEM_DEFAULT_SORT
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"
    plan_filter = request.args.get("plan", type=int)
    type_filter = request.args.get("type", "").strip() or None
    if type_filter not in _ALL_TYPE_KEYS:
        type_filter = None

    plans = NetworkPlan.query.order_by(NetworkPlan.name.asc()).all()

    query = NetworkFeature.query
    if plan_filter:
        query = query.filter(NetworkFeature.plan_id == plan_filter)
    if type_filter:
        query = query.filter(NetworkFeature.feature_type == type_filter)
    features = query.all()

    # Objekt (Liegenschaft) + Besitzer je Element vorladen — fuer Anzeige, Filter
    # und Sortierung. Die Suche laeuft in Python, damit Objekt/Besitzer gleichwertig
    # zu Bezeichnung/Notiz durchsuchbar sind (der Typ hat seinen eigenen Filter).
    property_map, owner_map = _feature_property_owner_maps(features)
    if q:
        features = [f for f in features if _matches_query(f, q, property_map, owner_map)]

    status_map = svc.feature_maintenance_status(features)
    features = _sort_features(features, sort, direction, status_map, property_map, owner_map)
    # In Python paginieren — Wartungs-/Objekt-/Besitzer-Sort sind abgeleitet,
    # daher nicht SQL-LIMIT/OFFSET-faehig (paginate_list statt paginate_query).
    pagination = paginate_list(features, _ELEM_PAGE_KEY)

    ctx = dict(
        plans=plans, pagination=pagination, status_map=status_map,
        property_map=property_map, owner_map=owner_map,
        q=q, sort=sort, dir=direction, plan_filter=plan_filter, type_filter=type_filter,
        vocab=vocab, today=date.today(),
    )
    if request.headers.get("HX-Request"):
        return render_template("network/_elemente_table.html", **ctx)
    return render_template("network/elemente.html", **ctx)


# ---------------------------------------------------------------------------
# Feature-CRUD
# ---------------------------------------------------------------------------

@bp.route("/features", methods=["POST"])
@login_required
def feature_create():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="invalid_json"), 400

    pid = data.get("plan_id")
    plan = NetworkPlan.query.get(pid) if pid else None
    if plan is None:
        plan = current_plan()
    if plan is None:
        return jsonify(error="no_plan"), 400

    nf = NetworkFeature()
    nf.plan_id = plan.id
    try:
        svc.apply_geometry(nf, data.get("geometry"))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

    ft = (data.get("feature_type") or "").strip()
    if not vocab.is_valid_type(ft, nf.geometry_kind):
        ft = "sonstige_leitung" if nf.is_line() else "sonstiges"
    nf.feature_type = ft

    svc.apply_attributes(nf, data)
    nf.created_by_id = current_user.id
    db.session.add(nf)
    db.session.commit()
    return jsonify(svc.feature_to_geojson(nf)), 201


@bp.route("/features/<int:feature_id>/geometry", methods=["POST"])
@login_required
def feature_geometry(feature_id):
    f = NetworkFeature.query.get_or_404(feature_id)
    data = request.get_json(silent=True) or {}
    try:
        svc.apply_geometry(f, data.get("geometry"))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    db.session.commit()
    return jsonify(svc.feature_to_geojson(f))


@bp.route("/features/<int:feature_id>")
@login_required
def feature_panel(feature_id):
    f = NetworkFeature.query.get_or_404(feature_id)
    return _render_panel(f)


@bp.route("/features/<int:feature_id>", methods=["POST"])
@login_required
def feature_update(feature_id):
    """Sachattribute aus dem Panel-Formular (HTMX)."""
    f = NetworkFeature.query.get_or_404(feature_id)
    svc.apply_attributes(f, request.form)
    db.session.commit()
    resp = make_response(_render_panel(f))
    # Karte synchron halten (Typ/Genauigkeit koennen Farbe/Label aendern).
    resp.headers["HX-Trigger"] = json.dumps(
        {"technik:featureSaved": svc.feature_to_geojson(f)}
    )
    return resp


@bp.route("/features/<int:feature_id>/edit", methods=["GET", "POST"])
@login_required
def feature_edit(feature_id):
    """Stammdaten-Bearbeiten als eigenstaendiges Modal (Elementliste). GET liefert
    das Felder-Fragment, POST speichert und schliesst das Modal (204 + HX-Trigger
    ``closeFeatureEditModal``). Geometrie/Fotos/Wartung sind hier bewusst NICHT
    dabei — Wartung hat ein eigenes Modal, Geometrie/Fotos das Karten-Panel."""
    f = NetworkFeature.query.get_or_404(feature_id)
    if request.method == "POST":
        svc.apply_attributes(f, request.form)
        db.session.commit()
        resp = make_response("", 204)
        resp.headers["HX-Trigger"] = json.dumps({"closeFeatureEditModal": True})
        return resp
    return render_template(
        "network/_feature_form_fields.html", f=f, vocab=vocab, **_link_options()
    )


@bp.route("/features/<int:feature_id>/delete", methods=["POST"])
@login_required
def feature_delete(feature_id):
    f = NetworkFeature.query.get_or_404(feature_id)
    _delete_photo_files(list(f.photos))
    db.session.delete(f)
    db.session.commit()
    resp = make_response("")  # Panel leeren
    resp.headers["HX-Trigger"] = json.dumps({"technik:featureDeleted": {"id": feature_id}})
    return resp


# ---------------------------------------------------------------------------
# Wartung / Pruefung
# ---------------------------------------------------------------------------

@bp.route("/features/<int:feature_id>/maintenance", methods=["GET", "POST"])
@login_required
def maintenance_add(feature_id):
    """GET: Wartungs-Modal-Body (Elementliste) laden. POST: Wartungs-/Pruefeintrag
    anlegen. Antwort kontextabhaengig — aus dem Modal (X-From-Modal) das Modal-Body-
    Fragment (Modal bleibt offen), sonst das volle Karten-Panel."""
    f = NetworkFeature.query.get_or_404(feature_id)
    from_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        if not f.plan.maintenance_enabled:
            if from_modal:
                return _render_maintenance_modal_body(f)
            flash("Wartung ist für diesen Plan deaktiviert.", "warning")
            return _render_panel(f)
        when = _parse_date(request.form.get("date")) or date.today()
        interval = svc._to_int(request.form.get("interval_months"))
        next_due = _parse_date(request.form.get("next_due"))
        if next_due is None and interval:
            next_due = _add_months(when, interval)

        kind = request.form.get("kind") or MaintenanceLog.KIND_INSPECTION
        if kind not in vocab.MAINTENANCE_KINDS:
            kind = MaintenanceLog.KIND_INSPECTION
        result = request.form.get("result") or None
        if result not in vocab.MAINTENANCE_RESULTS:
            result = None

        log = MaintenanceLog(
            feature_id=f.id,
            date=when,
            kind=kind,
            result=result,
            interval_months=interval,
            next_due=next_due,
            performed_by=(request.form.get("performed_by") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
            created_by_id=current_user.id,
        )
        db.session.add(log)
        db.session.commit()

    if from_modal:
        return _render_maintenance_modal_body(f)
    return _render_panel(f)


@bp.route("/maintenance/<int:log_id>/delete", methods=["POST"])
@login_required
def maintenance_delete(log_id):
    log = MaintenanceLog.query.get_or_404(log_id)
    f = log.feature
    db.session.delete(log)
    db.session.commit()
    if request.headers.get("X-From-Modal"):
        return _render_maintenance_modal_body(f)
    return _render_panel(f)


# ---------------------------------------------------------------------------
# Quellschuettung (Schuettungs-Messreihe je Quelle)
# ---------------------------------------------------------------------------

@bp.route("/features/<int:feature_id>/yields", methods=["GET", "POST"])
@login_required
def yield_add(feature_id):
    """GET: Quellschüttungs-Modal-Body (Elementliste) laden. POST: Schüttungs-
    messung (l/s) anlegen. Nur fuer Features vom Typ 'quelle' (sonst 404 — der
    Button erscheint ohnehin nur bei Quellen). Antwort kontextabhaengig: aus dem
    Modal (X-From-Modal) das Body-Fragment (Modal bleibt offen), sonst das Panel.
    Bewusst KEIN ``maintenance_enabled``-Gate — Monitoring ist davon unabhaengig."""
    f = NetworkFeature.query.get_or_404(feature_id)
    if f.feature_type != "quelle":
        abort(404)
    from_modal = bool(request.headers.get("X-From-Modal"))
    saved = False

    if request.method == "POST":
        when = _parse_date(request.form.get("measurement_date")) or date.today()
        raw = (request.form.get("flow_rate_lps") or "").strip().replace(",", ".")
        try:
            flow = Decimal(raw)
        except (InvalidOperation, ValueError):
            flow = None
        if flow is None or not flow.is_finite() or flow < 0:
            flash("Bitte eine gültige Schüttung in l/s (≥ 0) angeben.", "warning")
        else:
            db.session.add(SpringYield(
                feature_id=f.id,
                measurement_date=when,
                flow_rate_lps=flow,
                notes=(request.form.get("notes") or "").strip() or None,
                created_by_id=current_user.id,
            ))
            db.session.commit()
            saved = True

    if from_modal:
        # HX-Trigger 'yieldSaved' nur bei echter Änderung → das Modal kann beim
        # Schliessen die Host-Seite (Monitoring-Diagramm) zum Reload anstossen.
        resp = make_response(_render_yield_modal_body(f))
        if saved:
            resp.headers["HX-Trigger"] = "yieldSaved"
        return resp
    return _render_panel(f)


@bp.route("/yields/<int:yield_id>/delete", methods=["POST"])
@login_required
def yield_delete(yield_id):
    reading = SpringYield.query.get_or_404(yield_id)
    f = reading.feature
    db.session.delete(reading)
    db.session.commit()
    if request.headers.get("X-From-Modal"):
        resp = make_response(_render_yield_modal_body(f))
        resp.headers["HX-Trigger"] = "yieldSaved"
        return resp
    return _render_panel(f)


@bp.route("/monitoring")
@login_required
def monitoring():
    """Quellschüttung-Monitoring: Schüttungs-Zeitreihen aller Quellen des aktiven
    Plans als Liniendiagramm (Trockenheits-Monitoring) plus Kennzahlen je Quelle.

    Scope = aktiver Plan via ``current_plan()`` (mit ``?plan=``/Session-Override +
    Plan-Switcher). Messungen haengen an EINER ``feature_id`` — bei mehreren
    Plan-Kopien einer Quelle ist die Reihe pro Plan-Feature getrennt; der aktive
    Plan ist der operative Wahrheitsstand."""
    plan = current_plan()
    plans = NetworkPlan.query.order_by(NetworkPlan.name.asc()).all()
    if plan is None:
        return render_template(
            "network/monitoring.html", plan=None, plans=plans, vocab=vocab,
            quellen=[], series=[], stats={}, empty_reason="no_plan",
        )

    quellen = (
        NetworkFeature.query
        .filter_by(plan_id=plan.id, feature_type="quelle")
        .order_by(NetworkFeature.name.asc())
        .all()
    )
    if not quellen:
        return render_template(
            "network/monitoring.html", plan=plan, plans=plans, vocab=vocab,
            quellen=[], series=[], stats={}, empty_reason="no_quelle",
        )

    cutoff_12m = date.today() - timedelta(days=365)
    series, stats = [], {}
    for f in quellen:
        readings = list(f.spring_yields)  # bereits aufsteigend nach Datum (Relationship)
        series.append({
            "id": f.id,
            "label": f.label(),
            "points": [
                {"date": r.measurement_date.isoformat(), "value": float(r.flow_rate_lps)}
                for r in readings
            ],
        })
        if readings:
            values = [float(r.flow_rate_lps) for r in readings]
            vals_12m = [float(r.flow_rate_lps) for r in readings
                        if r.measurement_date >= cutoff_12m]
            med = median(values)
            last = readings[-1]
            stats[f.id] = {
                "count": len(readings),
                "latest_value": values[-1],
                "latest_date": last.measurement_date,
                "min_12m": min(vals_12m) if vals_12m else None,
                "median_all": med,
                "pct_of_median": (values[-1] / med * 100) if med else None,
            }
        else:
            stats[f.id] = {"count": 0, "latest_value": None, "latest_date": None,
                           "min_12m": None, "median_all": None, "pct_of_median": None}

    return render_template(
        "network/monitoring.html", plan=plan, plans=plans, vocab=vocab,
        quellen=quellen, series=series, stats=stats, empty_reason=None,
    )


# ---------------------------------------------------------------------------
# Wasserproben / Laborwerte (TWV-Beprobung) — Geschwister des Quellschuettungs-
# Musters: haengen an einer Probenahmestelle (feature_type='probenahme').
# ---------------------------------------------------------------------------

@bp.route("/features/<int:feature_id>/samples", methods=["GET", "POST"])
@login_required
def sample_add(feature_id):
    """GET: Wasserprobe-Modal-Body (Elementliste) laden. POST: einen Laborbefund
    (WaterSample) mit n Laborwerten (LabResult) anlegen. Nur fuer Features vom
    Typ 'probenahme' (sonst 404 — der Button erscheint ohnehin nur dort).

    Pro Katalog-Parameter wird das Formularfeld ``value__<key>`` gelesen; leere
    Felder werden uebersprungen. unit/limit_text/status werden zur Erfassungszeit
    eingefroren (Beleg-Stabilitaet trotz spaeterer Grenzwert-Aenderung). Antwort
    kontextabhaengig: aus dem Modal (X-From-Modal) das Body-Fragment, sonst Panel."""
    f = NetworkFeature.query.get_or_404(feature_id)
    if f.feature_type != "probenahme":
        abort(404)
    from_modal = bool(request.headers.get("X-From-Modal"))
    saved = False

    if request.method == "POST":
        when = _parse_date(request.form.get("sample_date")) or date.today()
        results = []
        for key in wq.PARAMETERS:
            raw = (request.form.get(f"value__{key}") or "").strip()
            if not raw:
                continue
            num = _parse_decimal(raw)
            results.append(LabResult(
                parameter_key=key,
                value_num=num,
                value_text=raw if num is None else None,
                unit=wq.parameter_unit(key) or None,
                limit_text=wq.limit_display(key) or None,
                status=wq.assess(key, num),
            ))
        if not results:
            flash("Bitte mindestens einen Laborwert erfassen.", "warning")
        else:
            sample = WaterSample(
                feature_id=f.id,
                sample_date=when,
                lab_name=(request.form.get("lab_name") or "").strip() or None,
                sample_no=(request.form.get("sample_no") or "").strip() or None,
                sample_type=(request.form.get("sample_type") or "").strip() or None,
                notes=(request.form.get("notes") or "").strip() or None,
                created_by_id=current_user.id,
            )
            sample.results = results
            db.session.add(sample)
            db.session.commit()
            saved = True

    if from_modal:
        resp = make_response(_render_sample_modal_body(f))
        if saved:
            resp.headers["HX-Trigger"] = "sampleSaved"
        return resp
    return _render_panel(f)


@bp.route("/samples/<int:sample_id>/delete", methods=["POST"])
@login_required
def sample_delete(sample_id):
    sample = WaterSample.query.get_or_404(sample_id)
    f = sample.feature
    db.session.delete(sample)   # ORM-Cascade loescht die lab_results mit
    db.session.commit()
    if request.headers.get("X-From-Modal"):
        resp = make_response(_render_sample_modal_body(f))
        resp.headers["HX-Trigger"] = "sampleSaved"
        return resp
    return _render_panel(f)


@bp.route("/features/<int:feature_id>/water-samples")
@login_required
def samples_overview(feature_id):
    """Befund-Historie EINER Probenahmestelle: Tabelle aller Befunde (neueste
    zuerst, je Zeile zum Einzelbefund) plus Trend-Diagramm fuer einen waehlbaren
    Parameter (``?param=``). Nur fuer Features vom Typ 'probenahme' (sonst 404)."""
    f = NetworkFeature.query.get_or_404(feature_id)
    if f.feature_type != "probenahme":
        abort(404)
    param = request.args.get("param") or "nitrat"
    if param not in wq.PARAMETERS:
        param = "nitrat"
    samples_asc = list(f.water_samples)  # aufsteigend nach Datum (Relationship)
    points = [
        {"date": s.sample_date.isoformat(), "value": float(r.value_num)}
        for s in samples_asc for r in s.results
        if r.parameter_key == param and r.value_num is not None
    ]
    series = [{"id": f.id, "label": f.label(), "points": points}]
    return render_template(
        "network/samples_overview.html",
        f=f, samples=list(reversed(samples_asc)), wq=wq, vocab=vocab,
        param=param, series=series, limit_value=wq.limit_value(param),
    )


@bp.route("/samples/<int:sample_id>")
@login_required
def sample_detail(sample_id):
    """Einzelbefund: alle Laborwerte mit Ampel + Grenzwert (Snapshot)."""
    sample = WaterSample.query.get_or_404(sample_id)
    return render_template(
        "network/sample_detail.html",
        sample=sample, f=sample.feature, wq=wq, vocab=vocab,
    )


def _probenahmestellen(plan):
    return (
        NetworkFeature.query
        .filter_by(plan_id=plan.id, feature_type="probenahme")
        .order_by(NetworkFeature.name.asc())
        .all()
    )


@bp.route("/water-quality")
@login_required
def water_quality():
    """Wasserqualitaets-Uebersicht: je Probenahmestelle des aktiven Plans der
    letzte Befund samt Gesamt-Ampel, plus ein Trend-Diagramm fuer EINEN waehlbaren
    Parameter (``?param=``, Default Nitrat) als Linie je Stelle. Scope = aktiver
    Plan via ``current_plan()`` (analog Quellschuettungs-Monitoring)."""
    plan = current_plan()
    plans = NetworkPlan.query.order_by(NetworkPlan.name.asc()).all()
    param = request.args.get("param") or "nitrat"
    if param not in wq.PARAMETERS:
        param = "nitrat"

    if plan is None:
        return render_template(
            "network/water_quality.html", plan=None, plans=plans, vocab=vocab, wq=wq,
            stellen=[], series=[], stats={}, param=param, limit_value=None,
            empty_reason="no_plan",
        )

    stellen = _probenahmestellen(plan)
    if not stellen:
        return render_template(
            "network/water_quality.html", plan=plan, plans=plans, vocab=vocab, wq=wq,
            stellen=[], series=[], stats={}, param=param, limit_value=None,
            empty_reason="no_probenahme",
        )

    series, stats = [], {}
    for st in stellen:
        samples = list(st.water_samples)  # aufsteigend nach Datum (Relationship)
        points = []
        for s in samples:
            for r in s.results:
                if r.parameter_key == param and r.value_num is not None:
                    points.append({"date": s.sample_date.isoformat(),
                                   "value": float(r.value_num)})
        series.append({"id": st.id, "label": st.label(), "points": points})
        latest = samples[-1] if samples else None
        stats[st.id] = {
            "count": len(samples),
            "latest": latest,
            "latest_status": latest.overall_status() if latest else None,
            "alarm_count": latest.alarm_count() if latest else 0,
        }

    return render_template(
        "network/water_quality.html", plan=plan, plans=plans, vocab=vocab, wq=wq,
        stellen=stellen, series=series, stats=stats, param=param,
        limit_value=wq.limit_value(param), empty_reason=None,
    )


@bp.route("/water-quality/export.csv")
@login_required
def water_quality_export_csv():
    """Alle Laborwerte (je Zeile ein Parameter) des aktiven Plans als CSV
    (UTF-8-BOM, Semikolon, dt. Dezimalkomma)."""
    plan = current_plan()
    query = (
        WaterSample.query
        .join(NetworkFeature, WaterSample.feature_id == NetworkFeature.id)
        .filter(NetworkFeature.feature_type == "probenahme")
    )
    if plan:
        query = query.filter(NetworkFeature.plan_id == plan.id)
    samples = query.order_by(
        WaterSample.sample_date.desc(), WaterSample.id.desc()
    ).all()

    output = io.StringIO()
    output.write("﻿")  # UTF-8-BOM fuer Excel
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Probenahmestelle", "Entnahmedatum", "Labor", "Probennummer",
        "Parameter", "Wert", "Einheit", "Grenzwert", "Bewertung",
    ])
    for s in samples:
        for r in s.results:
            value = (_de_decimal(r.value_num) if r.value_num is not None
                     else (r.value_text or ""))
            writer.writerow([
                s.feature.label(),
                s.sample_date.strftime("%d.%m.%Y"),
                s.lab_name or "",
                s.sample_no or "",
                wq.parameter_label(r.parameter_key),
                value,
                r.unit or "",
                r.limit_text or "",
                wq.status_label(r.status),
            ])
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = 'attachment; filename="wasserproben.csv"'
    return resp


def _wq_available_years(plan):
    if not plan:
        return []
    rows = (
        db.session.query(WaterSample.sample_date)
        .join(NetworkFeature, WaterSample.feature_id == NetworkFeature.id)
        .filter(NetworkFeature.plan_id == plan.id,
                NetworkFeature.feature_type == "probenahme")
        .all()
    )
    return sorted({d[0].year for d in rows if d[0]}, reverse=True)


def _wq_report_ctx():
    """Kontext fuer Behoerdenbericht (Print + PDF): je Stelle die Befunde des
    aktiven Plans (optional Jahr-Filter), plus Summen-Kennzahlen."""
    plan = current_plan()
    year = request.args.get("year", type=int)
    stellen_data, total_samples, total_breaches = [], 0, 0
    if plan:
        for st in _probenahmestellen(plan):
            samples = sorted(st.water_samples,
                             key=lambda x: x.sample_date, reverse=True)
            if year:
                samples = [s for s in samples if s.sample_date.year == year]
            if not samples:
                continue
            total_samples += len(samples)
            for s in samples:
                total_breaches += s.alarm_count()
            stellen_data.append({"feature": st, "samples": samples})
    return {
        "plan": plan, "year": year, "vocab": vocab, "wq": wq,
        "stellen_data": stellen_data,
        "total_samples": total_samples,
        "total_breaches": total_breaches,
        "stellen_count": len(stellen_data),
        "years": _wq_available_years(plan),
        "today_de": date.today().strftime("%d.%m.%Y"),
    }


@bp.route("/water-quality/print")
@login_required
def water_quality_print():
    return render_template("network/water_quality_print.html", **_wq_report_ctx())


@bp.route("/water-quality/report.pdf")
@login_required
def water_quality_report_pdf():
    try:
        from weasyprint import HTML
    except (ImportError, OSError):
        flash("PDF-Export ist nur im Docker-Container verfügbar. "
              "Die Druckansicht steht als Alternative bereit.", "warning")
        return redirect(url_for("network.water_quality_print",
                                year=request.args.get("year")))
    ctx = _wq_report_ctx()
    html = render_template("network/water_quality_pdf.html", **ctx)
    pdf = HTML(string=html, base_url=request.url_root).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    suffix = ctx["year"] or "alle"
    resp.headers["Content-Disposition"] = f'inline; filename="wasserbefund_{suffix}.pdf"'
    return resp


@bp.route("/water-quality/limits", methods=["GET", "POST"])
@login_required
def water_quality_limits():
    """Pro-Tenant-Override der TWV-Grenzwerte (AppSetting ``water_quality.<key>.limit``).
    Leeres Feld = Standard-Grenzwert verwenden (Override wird geloescht)."""
    if request.method == "POST":
        for key in wq.PARAMETERS:
            skey = f"water_quality.{key}.limit"
            raw = (request.form.get(f"limit__{key}") or "").strip()
            if raw:
                AppSetting.set(skey, raw)
            else:
                AppSetting.query.filter_by(key=skey).delete()  # zurueck auf Standard
        db.session.commit()
        flash("Grenzwerte gespeichert.", "success")
        return redirect(url_for("network.water_quality"))

    rows = []
    for key, meta in wq.PARAMETERS.items():
        if meta["kind"] == "info":
            continue  # kein Grenzwert anpassbar
        rows.append({
            "key": key,
            "label": meta["label"],
            "unit": meta["unit"],
            "kind": meta["kind"],
            "override": (AppSetting.get(f"water_quality.{key}.limit") or ""),
            "default_display": wq.limit_display(key),
        })
    return render_template("network/water_quality_limits.html", rows=rows, wq=wq)


# ---------------------------------------------------------------------------
# Fotos
# ---------------------------------------------------------------------------

@bp.route("/features/<int:feature_id>/photos", methods=["POST"])
@login_required
def photo_upload(feature_id):
    f = NetworkFeature.query.get_or_404(feature_id)
    file = request.files.get("photo")
    if not file or not file.filename:
        flash("Keine Bilddatei gewählt.", "warning")
        return _render_panel(f)

    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in _ALLOWED_IMG_EXT or not (file.mimetype or "").startswith("image/"):
        flash("Nur Bilddateien (JPG, PNG, WebP, GIF) erlaubt.", "warning")
        return _render_panel(f)

    fname = f"{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(svc.technik_upload_dir(), fname))
    photo = FeaturePhoto(
        feature_id=f.id,
        filename=fname,
        original_name=secure_filename(file.filename),
        content_type=file.mimetype,
        caption=(request.form.get("caption") or "").strip() or None,
        uploaded_by_id=current_user.id,
    )
    db.session.add(photo)
    db.session.commit()
    return _render_panel(f)


@bp.route("/photos/<int:photo_id>")
@login_required
def photo_serve(photo_id):
    photo = FeaturePhoto.query.get_or_404(photo_id)
    return send_from_directory(svc.technik_upload_dir(), photo.filename)


@bp.route("/photos/<int:photo_id>/delete", methods=["POST"])
@login_required
def photo_delete(photo_id):
    photo = FeaturePhoto.query.get_or_404(photo_id)
    f = photo.feature
    _delete_photo_files([photo])
    db.session.delete(photo)
    db.session.commit()
    return _render_panel(f)


# ---------------------------------------------------------------------------
# Export / Import / Druck
# ---------------------------------------------------------------------------

@bp.route("/export.geojson")
@login_required
def export_geojson():
    plan = current_plan()
    features = (
        NetworkFeature.query.filter_by(plan_id=plan.id).all() if plan else []
    )
    payload = json.dumps(
        svc.collection_geojson(features), ensure_ascii=False, indent=2
    )
    fname = secure_filename(f"leitungsplan_{plan.name}.geojson") if plan else "wasserleitungsplan.geojson"
    resp = make_response(payload)
    resp.headers["Content-Type"] = "application/geo+json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


def _render_import(**kwargs):
    """``technik/import.html`` rendern und dabei den aktuellen Ziel-Plan +
    die Plan-Auswahl (fuer den Umschalter oben) immer mitgeben."""
    kwargs.setdefault("plan", current_plan())
    kwargs.setdefault("plans", NetworkPlan.query.order_by(NetworkPlan.name.asc()).all())
    kwargs.setdefault("shapefile_available", wlk.dependencies_available())
    kwargs.setdefault("vocab", vocab)
    return render_template("network/import.html", **kwargs)


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_view():
    plan = current_plan()

    # Schritt 2: bestaetigter Import (commit) in den aktuellen Plan.
    if request.method == "POST" and request.form.get("confirm"):
        if plan is None:
            flash("Kein Ziel-Plan vorhanden. Bitte zuerst einen Plan anlegen.", "warning")
            return redirect(url_for("network.plans_index"))
        raw = request.form.get("geojson", "")
        try:
            feats = svc.iter_geojson_features(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            flash(f"Ungültiges GeoJSON: {exc}", "danger")
            return redirect(url_for("network.import_view"))

        keep_unknown = bool(request.form.get("keep_unknown_notes"))
        created, skipped = 0, 0
        for feat in feats:
            try:
                nf = svc.build_feature_from_geojson(
                    feat, current_user.id, plan.id,
                    extract_note_fields=True, keep_unknown_notes=keep_unknown,
                )
            except (ValueError, TypeError):
                skipped += 1
                continue
            db.session.add(nf)
            created += 1
        db.session.commit()

        msg = f"{created} Element(e) importiert (Ziel: {plan.name})"
        if skipped:
            msg += f", {skipped} übersprungen (nicht unterstützte Geometrie)"
        flash(msg + ".", "success")
        return redirect(url_for("network.index"))

    # Schritt 1: Datei hochladen -> Vorschau.
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Keine Datei gewählt.", "warning")
            return _render_import(preview=False)
        try:
            raw = file.read().decode("utf-8")
        except UnicodeDecodeError:
            flash("Datei ist kein UTF-8-Text (erwarte .geojson / .json).", "danger")
            return _render_import(preview=False)
        try:
            counts, total, skipped = svc.summarize_geojson(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            flash(f"Ungültiges GeoJSON: {exc}", "danger")
            return _render_import(preview=False)

        return _render_import(
            preview=True, counts=counts, total=total, skipped=skipped, geojson=raw,
        )

    return _render_import(preview=False)


@bp.route("/import/shapefile", methods=["POST"])
@login_required
def import_shapefile():
    """Schritt 1 fuer den WLK-Shapefile-Import: ZIP hochladen -> konvertieren
    (reprojizieren + WLK-Mapping) -> angereichertes GeoJSON in ein instance-
    Tempfile schreiben, Token in die Session, Vorschau rendern.

    Anders als der GeoJSON-Pfad (kleine Dateien, Hidden-Field) wird hier die
    potenziell grosse FeatureCollection serverseitig zwischengelagert — analog
    zum Ablesungen-Import-Wizard."""
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Keine ZIP-Datei gewählt.", "warning")
        return redirect(url_for("network.import_view"))

    try:
        result = wlk.convert_zip(file)
    except wlk.WlkImportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("network.import_view"))

    stats = result["stats"]
    token = uuid.uuid4().hex
    os.makedirs(current_app.instance_path, exist_ok=True)
    path = os.path.join(current_app.instance_path, f"network_import_{token}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": result["features"]}, f)
    session["network_import_file"] = path

    return _render_import(
        preview=False,
        mode="shapefile",
        stats=stats,
        counts=stats["type_counts"],
        total=stats["total"],
        skipped=stats["skipped"],
    )


@bp.route("/import/shapefile/commit", methods=["POST"])
@login_required
def import_shapefile_commit():
    """Schritt 2: das zwischengelagerte GeoJSON einlesen und als
    ``NetworkFeature`` (+ ``MaintenanceLog`` aus den Wartungs-Props) anlegen."""
    plan = current_plan()
    if plan is None:
        flash("Kein Ziel-Plan vorhanden. Bitte zuerst einen Plan anlegen.", "warning")
        return redirect(url_for("network.plans_index"))

    path = session.get("network_import_file")
    if not path or not os.path.exists(path):
        flash("Die Import-Sitzung ist abgelaufen. Bitte die ZIP-Datei erneut hochladen.", "warning")
        return redirect(url_for("network.import_view"))

    try:
        with open(path, encoding="utf-8") as f:
            feats = svc.iter_geojson_features(f.read())
    except (OSError, ValueError, json.JSONDecodeError):
        flash("Die Import-Daten konnten nicht gelesen werden.", "danger")
        return redirect(url_for("network.import_view"))

    keep_unknown = bool(request.form.get("keep_unknown_notes"))
    created, created_logs, skipped = 0, 0, 0
    for feat in feats:
        try:
            nf = svc.build_feature_from_geojson(
                feat, current_user.id, plan.id,
                extract_note_fields=True, keep_unknown_notes=keep_unknown,
            )
        except (ValueError, TypeError):
            skipped += 1
            continue
        db.session.add(nf)
        created += 1
        created_logs += len(nf.maintenance_logs)
    db.session.commit()

    try:
        os.remove(path)
    except OSError:
        pass
    session.pop("network_import_file", None)

    msg = f"{created} Element(e) importiert (Ziel: {plan.name})"
    if created_logs:
        msg += f", {created_logs} Wartungseintrag/-einträge angelegt"
    if skipped:
        msg += f", {skipped} übersprungen"
    flash(msg + ".", "success")
    return redirect(url_for("network.index"))


@bp.route("/print")
@login_required
def print_view():
    plan = current_plan()
    features = (
        NetworkFeature.query.filter_by(plan_id=plan.id)
        .order_by(NetworkFeature.geometry_kind, NetworkFeature.feature_type)
        .all() if plan else []
    )
    return render_template(
        "network/print.html",
        cfg=_map_config(plan),
        plan=plan,
        features=features,
        vocab=vocab,
        today_iso=date.today().strftime("%d.%m.%Y"),
    )


# ---------------------------------------------------------------------------
# Hydrantenplan fuer die Feuerwehr (Druck A4/A3 + oeffentliche Freigabe-Links)
# ---------------------------------------------------------------------------

def _share_public_url(token):
    """Vollstaendige Public-URL eines Freigabe-Links. Bewusst aus ``host_url``
    zusammengebaut statt via ``url_for`` — die einloesende Route ``/feuerwehr/...``
    lebt im SaaS-Blueprint und existiert im OSS-Prozess nicht."""
    return request.host_url.rstrip("/") + "/feuerwehr/" + token


@bp.route("/hydrants/print")
@login_required
def hydrants_print():
    """Druckoptimierter Hydrantenplan fuer die Feuerwehr: nur Hydranten +
    Versorgungs-/Hauptleitungen + basemap.at-Strassen, A4/A3. Zusaetzlich (sofern
    FEATURE_HYDRANT_PUBLIC_SHARE an) die Verwaltung oeffentlicher Freigabe-Links."""
    plan = current_plan()
    hydrants = (
        NetworkFeature.query
        .filter_by(plan_id=plan.id, feature_type=svc.HYDRANT_TYPE)
        .order_by(NetworkFeature.name.asc())
        .all() if plan else []
    )
    status_map = svc.feature_maintenance_status(hydrants)
    share_enabled = bool(current_app.config.get("FEATURE_HYDRANT_PUBLIC_SHARE"))
    share_links = (
        HydrantShareLink.query.filter_by(plan_id=plan.id)
        .order_by(HydrantShareLink.created_at.desc()).all()
        if (plan and share_enabled) else []
    )
    return render_template(
        "network/hydrants_print.html",
        cfg=_hydrant_map_config(plan),
        plan=plan,
        hydrants=hydrants,
        status_map=status_map,
        share_enabled=share_enabled,
        share_links=share_links,
        share_urls={l.id: _share_public_url(l.token) for l in share_links},
        vocab=vocab,
        today=date.today(),
        today_iso=date.today().strftime("%d.%m.%Y"),
    )


@bp.route("/hydrants/share-links", methods=["POST"])
@login_required
def share_link_create():
    """Neuen oeffentlichen Freigabe-Link fuer den aktuellen Plan anlegen.
    SaaS-only (FEATURE_HYDRANT_PUBLIC_SHARE) -> 404 im OSS-Standalone."""
    if not current_app.config.get("FEATURE_HYDRANT_PUBLIC_SHARE"):
        abort(404)
    plan = current_plan()
    if plan is None:
        flash("Kein Plan gewählt — bitte zuerst einen Plan anlegen.", "warning")
        return redirect(url_for("network.hydrants_print"))
    link = HydrantShareLink(
        plan_id=plan.id,
        token=secrets.token_urlsafe(32),
        label=(request.form.get("label") or "").strip() or None,
        created_by_id=current_user.id,
    )
    db.session.add(link)
    db.session.commit()
    flash("Feuerwehr-Link erstellt. Bitte den Link an die Feuerwehr weitergeben.", "success")
    return redirect(url_for("network.hydrants_print", plan=plan.id))


@bp.route("/hydrants/share-links/<int:link_id>/revoke", methods=["POST"])
@login_required
def share_link_revoke(link_id):
    if not current_app.config.get("FEATURE_HYDRANT_PUBLIC_SHARE"):
        abort(404)
    link = HydrantShareLink.query.get_or_404(link_id)
    link.is_active = False
    db.session.commit()
    flash("Feuerwehr-Link widerrufen.", "success")
    return redirect(url_for("network.hydrants_print", plan=link.plan_id))


@bp.route("/hydrants/share-links/<int:link_id>/delete", methods=["POST"])
@login_required
def share_link_delete(link_id):
    if not current_app.config.get("FEATURE_HYDRANT_PUBLIC_SHARE"):
        abort(404)
    link = HydrantShareLink.query.get_or_404(link_id)
    pid = link.plan_id
    db.session.delete(link)
    db.session.commit()
    flash("Feuerwehr-Link gelöscht.", "success")
    return redirect(url_for("network.hydrants_print", plan=pid))


# ---------------------------------------------------------------------------
# Plaene (Mehrplan-Verwaltung)
# ---------------------------------------------------------------------------

@bp.route("/plans")
@login_required
def plans_index():
    plans = NetworkPlan.query.order_by(NetworkPlan.name.asc()).all()
    return render_template("network/plans/index.html", plans=plans, vocab=vocab)


@bp.route("/plans", methods=["POST"])
@login_required
def plan_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Bitte einen Namen für den Plan angeben.", "warning")
        return redirect(url_for("network.plans_index"))
    status = request.form.get("status")
    if status not in NetworkPlan.STATUSES:
        status = NetworkPlan.STATUS_DRAFT
    uid = current_user.id
    plan = NetworkPlan(
        name=name,
        status=status,
        maintenance_enabled=bool(request.form.get("maintenance_enabled")),
        description=(request.form.get("description") or "").strip() or None,
        created_by_id=uid,
        updated_by_id=uid,
    )
    db.session.add(plan)
    db.session.commit()
    flash(f"Plan '{name}' angelegt.", "success")
    return redirect(url_for("network.plans_index"))


@bp.route("/plans/<int:plan_id>", methods=["POST"])
@login_required
def plan_update(plan_id):
    plan = NetworkPlan.query.get_or_404(plan_id)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Bitte einen Namen für den Plan angeben.", "warning")
        return redirect(url_for("network.plans_index"))
    status = request.form.get("status")
    if status in NetworkPlan.STATUSES:
        plan.status = status
    plan.name = name
    plan.maintenance_enabled = bool(request.form.get("maintenance_enabled"))
    plan.description = (request.form.get("description") or "").strip() or None
    plan.updated_by_id = current_user.id
    db.session.commit()
    flash(f"Plan '{name}' gespeichert.", "success")
    return redirect(url_for("network.plans_index"))


@bp.route("/plans/<int:plan_id>/copy", methods=["POST"])
@login_required
def plan_copy(plan_id):
    src = NetworkPlan.query.get_or_404(plan_id)
    dup, count = svc.copy_plan(src, current_user.id)
    flash(
        f"Plan '{src.name}' kopiert ({count} Element(e)). "
        f"Die Kopie '{dup.name}' ist ein Entwurf mit deaktivierter Wartung.",
        "success",
    )
    return redirect(url_for("network.plans_index"))


@bp.route("/plans/<int:plan_id>/merge", methods=["POST"])
@login_required
def plan_merge(plan_id):
    """Aenderungen der Kopie in ihren Quellplan spiegeln (alle Aenderungen inkl.
    Loeschungen). Wartungs-Logs/Fotos des Quellplans bleiben unberuehrt."""
    cp = NetworkPlan.query.get_or_404(plan_id)
    res = svc.merge_plan_into_source(cp, current_user.id)
    if res is None:
        flash("Dieser Plan ist keine Kopie (kein Quellplan) — Übertragen nicht möglich.", "warning")
        return redirect(url_for("network.plans_index"))
    flash(
        f"Änderungen in '{res['source'].name}' übertragen: {res['added']} neu, "
        f"{res['updated']} aktualisiert, {res['deleted']} gelöscht.", "success"
    )
    return redirect(url_for("network.plans_index"))


@bp.route("/plans/<int:plan_id>/delete", methods=["POST"])
@login_required
def plan_delete(plan_id):
    plan = NetworkPlan.query.get_or_404(plan_id)
    # Fotodateien aller Features entfernen — die DB-Records loescht der ORM-Cascade.
    for f in plan.features:
        _delete_photo_files(list(f.photos))
    name = plan.name
    was_current = session.get("network_plan_id") == plan.id
    db.session.delete(plan)
    db.session.commit()
    if was_current:
        session.pop("network_plan_id", None)  # Auswahl neu aufloesen lassen
    flash(f"Plan '{name}' und alle zugehörigen Elemente gelöscht.", "success")
    return redirect(url_for("network.plans_index"))
