"""Rundschreiben & Notfall-Kommunikation.

Rundschreiben an die Mitglieder — normale Mitteilungen und Notfälle
(Abkochempfehlung bei Wasserverunreinigung, Wasserabschaltungs-Infos bei
Rohrbruch/geplanter Reparatur). Versand per E-Mail oder Post (Sammel-PDF),
analog zu den Massenrechnungen.

Verfügbar für **beide** Mandant-Typen (kein ``is_wassergenossenschaft``-Gate,
anders als die Schriftführung) unter dem eigenen Recht ``circulars``
(PERM_CIRCULARS). Der ``before_request``-Guard erzwingt das Recht; die Routen
tragen zusätzlich ``@login_required`` (der Guard lässt unauthentifizierte
Requests durch, damit der Login-Redirect mit ``?next=`` greift).
"""
from flask import Blueprint

from app.auth.permissions import PERM_CIRCULARS, require_blueprint_permission
from app.circulars import constants

bp = Blueprint("circulars", __name__, url_prefix="/circulars")

bp.before_request(require_blueprint_permission(PERM_CIRCULARS))


@bp.context_processor
def _inject_labels():
    """Label-/Badge-Dicts + Vorlagen für die Rundschreiben-Templates."""
    return {
        "circ_kind_labels": constants.KIND_LABELS,
        "circ_kind_badges": constants.KIND_BADGES,
        "circ_kind_icons": constants.KIND_ICONS,
        "circ_status_labels": constants.STATUS_LABELS,
        "circ_status_badges": constants.STATUS_BADGES,
        "circ_method_labels": constants.DELIVERY_METHOD_LABELS,
        "circ_action_labels": constants.DELIVERY_ACTION_LABELS,
    }


from app.circulars import routes  # noqa: E402,F401
