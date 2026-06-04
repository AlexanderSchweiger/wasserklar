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
import uuid
from datetime import date

from flask import (
    render_template, request, jsonify, redirect, url_for, flash,
    make_response, send_from_directory, session, current_app,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.technik import bp
from app.technik import services as svc
from app.technik import vocab
from app.technik import wlk_import as wlk
from app.extensions import db
from app.models import (
    NetworkPlan, NetworkFeature, MaintenanceLog, FeaturePhoto,
    Property, WaterMeter,
)

_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


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
        "base": url_for("technik.index"),
        "featuresUrl": url_for("technik.features_geojson", plan=pid),
        "createUrl": url_for("technik.feature_create"),
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
        sid = session.get("technik_plan_id")
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
        session["technik_plan_id"] = plan.id
    return plan


def _link_options():
    """Objekte/Zaehler fuer die Verknuepfungs-Dropdowns im Feature-Panel."""
    properties = (
        Property.query.filter_by(active=True)
        .order_by(Property.object_number.asc())
        .all()
    )
    meters = (
        WaterMeter.query.join(Property)
        .filter(WaterMeter.active.is_(True))
        .order_by(WaterMeter.meter_number.asc())
        .all()
    )
    return {"properties": properties, "meters": meters}


def _render_panel(f):
    return render_template(
        "technik/_feature_panel.html",
        f=f, vocab=vocab, today_iso=date.today().isoformat(), **_link_options()
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
        "technik/index.html",
        cfg=_map_config(plan),
        plan=plan,
        plans=plans,
        feature_count=feature_count,
        vocab=vocab,
    )


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

@bp.route("/features/<int:feature_id>/maintenance", methods=["POST"])
@login_required
def maintenance_add(feature_id):
    f = NetworkFeature.query.get_or_404(feature_id)
    if not f.plan.maintenance_enabled:
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
    return _render_panel(f)


@bp.route("/maintenance/<int:log_id>/delete", methods=["POST"])
@login_required
def maintenance_delete(log_id):
    log = MaintenanceLog.query.get_or_404(log_id)
    f = log.feature
    db.session.delete(log)
    db.session.commit()
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
    return render_template("technik/import.html", **kwargs)


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_view():
    plan = current_plan()

    # Schritt 2: bestaetigter Import (commit) in den aktuellen Plan.
    if request.method == "POST" and request.form.get("confirm"):
        if plan is None:
            flash("Kein Ziel-Plan vorhanden. Bitte zuerst einen Plan anlegen.", "warning")
            return redirect(url_for("technik.plans_index"))
        raw = request.form.get("geojson", "")
        try:
            feats = svc.iter_geojson_features(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            flash(f"Ungültiges GeoJSON: {exc}", "danger")
            return redirect(url_for("technik.import_view"))

        created, skipped = 0, 0
        for feat in feats:
            try:
                nf = svc.build_feature_from_geojson(feat, current_user.id, plan.id)
            except (ValueError, TypeError):
                skipped += 1
                continue
            db.session.add(nf)
            created += 1
        db.session.commit()

        msg = f"{created} Objekt(e) importiert (Ziel: {plan.name})"
        if skipped:
            msg += f", {skipped} übersprungen (nicht unterstützte Geometrie)"
        flash(msg + ".", "success")
        return redirect(url_for("technik.index"))

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
        return redirect(url_for("technik.import_view"))

    try:
        result = wlk.convert_zip(file)
    except wlk.WlkImportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("technik.import_view"))

    stats = result["stats"]
    token = uuid.uuid4().hex
    os.makedirs(current_app.instance_path, exist_ok=True)
    path = os.path.join(current_app.instance_path, f"technik_import_{token}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": result["features"]}, f)
    session["technik_import_file"] = path

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
        return redirect(url_for("technik.plans_index"))

    path = session.get("technik_import_file")
    if not path or not os.path.exists(path):
        flash("Die Import-Sitzung ist abgelaufen. Bitte die ZIP-Datei erneut hochladen.", "warning")
        return redirect(url_for("technik.import_view"))

    try:
        with open(path, encoding="utf-8") as f:
            feats = svc.iter_geojson_features(f.read())
    except (OSError, ValueError, json.JSONDecodeError):
        flash("Die Import-Daten konnten nicht gelesen werden.", "danger")
        return redirect(url_for("technik.import_view"))

    created, created_logs, skipped = 0, 0, 0
    for feat in feats:
        try:
            nf = svc.build_feature_from_geojson(feat, current_user.id, plan.id)
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
    session.pop("technik_import_file", None)

    msg = f"{created} Objekt(e) importiert (Ziel: {plan.name})"
    if created_logs:
        msg += f", {created_logs} Wartungseintrag/-einträge angelegt"
    if skipped:
        msg += f", {skipped} übersprungen"
    flash(msg + ".", "success")
    return redirect(url_for("technik.index"))


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
        "technik/print.html",
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
    return render_template("technik/plans/index.html", plans=plans, vocab=vocab)


@bp.route("/plans", methods=["POST"])
@login_required
def plan_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Bitte einen Namen für den Plan angeben.", "warning")
        return redirect(url_for("technik.plans_index"))
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
    return redirect(url_for("technik.plans_index"))


@bp.route("/plans/<int:plan_id>", methods=["POST"])
@login_required
def plan_update(plan_id):
    plan = NetworkPlan.query.get_or_404(plan_id)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Bitte einen Namen für den Plan angeben.", "warning")
        return redirect(url_for("technik.plans_index"))
    status = request.form.get("status")
    if status in NetworkPlan.STATUSES:
        plan.status = status
    plan.name = name
    plan.maintenance_enabled = bool(request.form.get("maintenance_enabled"))
    plan.description = (request.form.get("description") or "").strip() or None
    plan.updated_by_id = current_user.id
    db.session.commit()
    flash(f"Plan '{name}' gespeichert.", "success")
    return redirect(url_for("technik.plans_index"))


@bp.route("/plans/<int:plan_id>/copy", methods=["POST"])
@login_required
def plan_copy(plan_id):
    src = NetworkPlan.query.get_or_404(plan_id)
    dup, count = svc.copy_plan(src, current_user.id)
    flash(
        f"Plan '{src.name}' kopiert ({count} Objekt(e)). "
        f"Die Kopie '{dup.name}' ist ein Entwurf mit deaktivierter Wartung.",
        "success",
    )
    return redirect(url_for("technik.plans_index"))


@bp.route("/plans/<int:plan_id>/merge", methods=["POST"])
@login_required
def plan_merge(plan_id):
    """Aenderungen der Kopie in ihren Quellplan spiegeln (alle Aenderungen inkl.
    Loeschungen). Wartungs-Logs/Fotos des Quellplans bleiben unberuehrt."""
    cp = NetworkPlan.query.get_or_404(plan_id)
    res = svc.merge_plan_into_source(cp, current_user.id)
    if res is None:
        flash("Dieser Plan ist keine Kopie (kein Quellplan) — Übertragen nicht möglich.", "warning")
        return redirect(url_for("technik.plans_index"))
    flash(
        f"Änderungen in '{res['source'].name}' übertragen: {res['added']} neu, "
        f"{res['updated']} aktualisiert, {res['deleted']} gelöscht.", "success"
    )
    return redirect(url_for("technik.plans_index"))


@bp.route("/plans/<int:plan_id>/delete", methods=["POST"])
@login_required
def plan_delete(plan_id):
    plan = NetworkPlan.query.get_or_404(plan_id)
    # Fotodateien aller Features entfernen — die DB-Records loescht der ORM-Cascade.
    for f in plan.features:
        _delete_photo_files(list(f.photos))
    name = plan.name
    was_current = session.get("technik_plan_id") == plan.id
    db.session.delete(plan)
    db.session.commit()
    if was_current:
        session.pop("technik_plan_id", None)  # Auswahl neu aufloesen lassen
    flash(f"Plan '{name}' und alle zugehörigen Objekte gelöscht.", "success")
    return redirect(url_for("technik.plans_index"))
