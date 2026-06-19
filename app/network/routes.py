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

import json
import os
import unicodedata
import uuid
from datetime import date

from flask import (
    render_template, request, jsonify, redirect, url_for, flash,
    make_response, send_from_directory, session, current_app,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.network import bp
from app.network import services as svc
from app.network import vocab
from app.network import wlk_import as wlk
from app.extensions import db
from app.pagination import paginate_list
from app.models import (
    NetworkPlan, NetworkFeature, MaintenanceLog, FeaturePhoto,
    Property, PropertyOwnership, Customer,
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
        f=f, vocab=vocab, today_iso=date.today().isoformat(), **_link_options()
    )


def _render_maintenance_modal_body(f):
    """Body des „Wartung & Prüfung"-Modals der Elementliste (Liste + Felder, ohne <form>)."""
    return render_template(
        "network/_maintenance_modal_body.html",
        f=f, vocab=vocab, today_iso=date.today().isoformat(),
    )


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


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
    erscheinen grell."""
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
    ftype = request.args.get("type")
    if ftype:
        query = query.filter(NetworkFeature.feature_type == ftype)
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
