"""Notizzettel / Pin-Notizen — generisches, polymorphes Notiz-Modul.

Anders als die meisten Blueprints steht ``notes`` unter **keinem** Bereichsrecht:
Notizen sind eine Komfortfunktion, die jeder eingeloggte User sehen und anlegen
darf. Daher kein ``require_blueprint_permission``-Hook — jede Route traegt nur
``@login_required`` (wie ``main``/Dashboard).
"""
from flask import Blueprint

bp = Blueprint("notes", __name__, url_prefix="/notes")

from app.notes import routes  # noqa: E402, F401
