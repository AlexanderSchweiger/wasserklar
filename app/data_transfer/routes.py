"""Routes fuer Daten-Export/Import (Admin only).

Wizard-Flow Import: Upload -> Preview -> Confirm. Zwischenstand wird in
``instance/tmp/imports/<uuid>/`` gehalten und nach 24h automatisch
aufgeraeumt.
"""

from __future__ import annotations

import io
import json
import shutil
from datetime import datetime
from pathlib import Path

from flask import (
    abort, current_app, flash, redirect, render_template, request,
    send_file, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func

from app.data_transfer import bp
from app.data_transfer.services import (
    ImportError_, export_to_zip, extract_to_temp, import_from_zip,
    validate_manifest,
)
from app.extensions import db
from app.models import Booking, Invoice, MeterReading, Transfer


def _require_admin():
    if not current_user.is_authenticated:
        abort(401)
    if not current_user.is_admin:
        flash("Kein Zugriff.", "danger")
        return redirect(url_for("main.dashboard"))
    return None


def _available_years() -> list[int]:
    """Sammelt alle Jahre, die in Buchungs-Tabellen vorkommen, fuer den Filter."""
    years: set[int] = set()
    # MeterReading.year
    for (y,) in db.session.query(MeterReading.year).distinct().all():
        if y is not None:
            years.add(int(y))
    # Invoice.period_year
    for (y,) in db.session.query(Invoice.period_year).distinct().all():
        if y is not None:
            years.add(int(y))
    # Booking.date -> Jahr
    for (y,) in db.session.query(func.extract("year", Booking.date)).distinct().all():
        if y is not None:
            years.add(int(y))
    # Transfer.date -> Jahr
    for (y,) in db.session.query(func.extract("year", Transfer.date)).distinct().all():
        if y is not None:
            years.add(int(y))
    return sorted(years, reverse=True)


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp
    return render_template("data_transfer/index.html")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@bp.route("/export", methods=["GET"])
@login_required
def export_form():
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp
    return render_template(
        "data_transfer/export.html",
        years=_available_years(),
    )


@bp.route("/export", methods=["POST"])
@login_required
def export_run():
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp

    selection = {
        "stammdaten": bool(request.form.get("stammdaten")),
        "buchungen": bool(request.form.get("buchungen")),
        "mahnwesen": bool(request.form.get("mahnwesen")),
        "einstellungen": bool(request.form.get("einstellungen")),
        "include_pdfs": bool(request.form.get("include_pdfs")),
        "years": [int(y) for y in request.form.getlist("years") if y.isdigit()],
    }
    if not any([selection["stammdaten"], selection["buchungen"],
                selection["mahnwesen"], selection["einstellungen"]]):
        flash("Bitte mindestens eine Kategorie auswaehlen.", "warning")
        return redirect(url_for("data_transfer.export_form"))

    buf = io.BytesIO()
    try:
        export_to_zip(selection, buf, exported_by=current_user.username)
    except Exception as exc:  # pragma: no cover — defensiv
        flash(f"Export fehlgeschlagen: {exc}", "danger")
        return redirect(url_for("data_transfer.export_form"))

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"wasserklar-export-{ts}.zip"
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@bp.route("/import", methods=["GET"])
@login_required
def import_form():
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp
    return render_template("data_transfer/import_upload.html")


@bp.route("/import", methods=["POST"])
@login_required
def import_upload():
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Bitte eine Export-Datei (.zip) hochladen.", "warning")
        return redirect(url_for("data_transfer.import_form"))
    if not upload.filename.lower().endswith(".zip"):
        flash("Nur .zip-Dateien werden unterstuetzt.", "warning")
        return redirect(url_for("data_transfer.import_form"))

    mode = request.form.get("mode", "replace")
    update_existing = bool(request.form.get("update_existing"))
    if mode not in ("replace", "merge"):
        flash("Ungueltiger Import-Modus.", "danger")
        return redirect(url_for("data_transfer.import_form"))

    try:
        extract_dir, manifest = extract_to_temp(upload, current_app.instance_path)
    except Exception as exc:
        flash(f"Datei konnte nicht entpackt werden: {exc}", "danger")
        return redirect(url_for("data_transfer.import_form"))

    validation = validate_manifest(manifest, extract_dir)

    return render_template(
        "data_transfer/import_preview.html",
        manifest=manifest,
        validation=validation,
        token=extract_dir.name,
        mode=mode,
        update_existing=update_existing,
    )


@bp.route("/import/<token>/confirm", methods=["POST"])
@login_required
def import_confirm(token: str):
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp

    extract_dir = _resolve_token(token)
    if extract_dir is None:
        flash("Import-Sitzung abgelaufen oder ungueltig. Bitte erneut hochladen.", "warning")
        return redirect(url_for("data_transfer.import_form"))

    manifest_path = extract_dir / "manifest.json"
    if not manifest_path.exists():
        flash("Manifest fehlt — Import kann nicht angewendet werden.", "danger")
        return redirect(url_for("data_transfer.import_form"))
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    mode = request.form.get("mode", "replace")
    update_existing = bool(request.form.get("update_existing"))

    try:
        stats = import_from_zip(
            extract_dir, manifest,
            mode=mode,
            update_existing=update_existing,
            instance_path=current_app.instance_path,
        )
    except ImportError_ as exc:
        flash(str(exc), "danger")
        return redirect(url_for("data_transfer.import_form"))

    # Aufraeumen
    shutil.rmtree(extract_dir, ignore_errors=True)

    total_inserted = sum(s["inserted"] for s in stats.values())
    total_updated = sum(s["updated"] for s in stats.values())
    total_skipped = sum(s["skipped"] for s in stats.values())
    flash(
        f"Import abgeschlossen: {total_inserted} neu, {total_updated} aktualisiert, "
        f"{total_skipped} uebersprungen.",
        "success",
    )
    return redirect(url_for("data_transfer.index"))


@bp.route("/import/<token>/cancel", methods=["POST"])
@login_required
def import_cancel(token: str):
    redirect_resp = _require_admin()
    if redirect_resp is not None:
        return redirect_resp
    extract_dir = _resolve_token(token)
    if extract_dir is not None:
        shutil.rmtree(extract_dir, ignore_errors=True)
    flash("Import abgebrochen.", "info")
    return redirect(url_for("data_transfer.import_form"))


def _resolve_token(token: str) -> Path | None:
    """Validiert das Token (nur Hex-Zeichen) und liefert das Extract-Verzeichnis."""
    if not token or not all(c in "0123456789abcdef" for c in token):
        return None
    base = Path(current_app.instance_path) / "tmp" / "imports" / token
    if not base.exists():
        return None
    return base
