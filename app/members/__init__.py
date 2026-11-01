"""Mitglieder — Mitgliederliste (beide Anteils-Bindungen) und Anteilsbuch
(nur bei Anteilen am Mitglied, ``org.share_binding`` = ``person``).

Nur im Mandant-Typ Wassergenossenschaft und unter dem Recht ``stammdaten``;
beides erzwingt der ``before_request``-Guard. Die Routen tragen zusaetzlich
``@login_required`` (der Guard laesst unauthentifizierte Requests durch,
damit der Login-Redirect mit ``?next=`` greift).
"""
from flask import Blueprint, flash, redirect, url_for
from flask_login import current_user

from app.auth.permissions import PERM_STAMMDATEN
from app.settings_service import is_wassergenossenschaft

bp = Blueprint("members", __name__, url_prefix="/members")


@bp.before_request
def _guard():
    if not current_user.is_authenticated:
        return None
    if not is_wassergenossenschaft():
        flash("Die Mitgliederverwaltung ist nur für Wassergenossenschaften verfügbar.",
              "warning")
        return redirect(url_for("main.dashboard"))
    if not current_user.has_permission(PERM_STAMMDATEN):
        flash("Kein Zugriff für diesen Bereich.", "danger")
        return redirect(url_for("main.dashboard"))
    return None


from app.members import routes  # noqa: E402,F401
