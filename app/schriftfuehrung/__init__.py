"""Schriftführung — Sitzungen, Einladungen, Protokolle, Beschlüsse, Schriftverkehr.

Nur im Mandant-Typ Wassergenossenschaft verfügbar (``is_wassergenossenschaft``)
und unter dem Recht ``schriftfuehrung`` (PERM_SCHRIFTFUEHRUNG). Beides wird im
``before_request``-Guard erzwungen; die einzelnen Routen tragen zusätzlich
``@login_required`` (der Guard lässt unauthentifizierte Requests durch, damit
der Login-Redirect mit ``?next=`` greift).
"""
from flask import Blueprint, flash, redirect, url_for
from flask_login import current_user

from app.auth.permissions import PERM_SCHRIFTFUEHRUNG
from app.settings_service import is_wassergenossenschaft
from app.schriftfuehrung import constants

bp = Blueprint("schriftfuehrung", __name__, url_prefix="/schriftfuehrung")


@bp.before_request
def _guard():
    if not current_user.is_authenticated:
        return None  # @login_required der Route übernimmt den Redirect
    if not is_wassergenossenschaft():
        flash("Die Schriftführung ist nur für Wassergenossenschaften verfügbar.", "warning")
        return redirect(url_for("main.dashboard"))
    if not current_user.has_permission(PERM_SCHRIFTFUEHRUNG):
        flash("Kein Zugriff für diesen Bereich.", "danger")
        return redirect(url_for("main.dashboard"))
    return None


@bp.context_processor
def _inject_labels():
    """Label-/Badge-Dicts + Dokumentformat für die Schriftführungs-Templates
    (nur in diesem Blueprint aktiv)."""
    try:
        from app.models import AppSetting
        doc_format = AppSetting.get("invoice.document_format", "pdf")
        if doc_format not in ("pdf", "docx", "both"):
            doc_format = "pdf"
    except Exception:
        doc_format = "pdf"
    return {
        "sf_doc_format": doc_format,
        "sf_meeting_type_labels": constants.MEETING_TYPE_LABELS,
        "sf_meeting_type_labels_plural": constants.MEETING_TYPE_LABELS_PLURAL,
        "sf_meeting_status_labels": constants.MEETING_STATUS_LABELS,
        "sf_meeting_status_badge": constants.MEETING_STATUS_BADGE,
        "sf_attendance_status_labels": constants.ATTENDANCE_STATUS_LABELS,
        "sf_attendance_status_badge": constants.ATTENDANCE_STATUS_BADGE,
        "sf_resolution_status_labels": constants.RESOLUTION_STATUS_LABELS,
        "sf_resolution_status_badge": constants.RESOLUTION_STATUS_BADGE,
        "sf_protocol_status_labels": constants.PROTOCOL_STATUS_LABELS,
        "sf_protocol_status_badge": constants.PROTOCOL_STATUS_BADGE,
        "sf_delivery_method_labels": constants.DELIVERY_METHOD_LABELS,
        "sf_delivery_action_labels": constants.DELIVERY_ACTION_LABELS,
        "sf_doc_type_labels": constants.DOC_TYPE_LABELS,
        "sf_upload_hint": constants.ALLOWED_UPLOAD_HINT,
    }


from app.schriftfuehrung import routes  # noqa: E402,F401
