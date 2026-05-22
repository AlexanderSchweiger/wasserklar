from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_BUCHHALTUNG

bp = Blueprint("projects", __name__, url_prefix="/projekte")
bp.before_request(require_blueprint_permission(PERM_BUCHHALTUNG))

from app.projects import routes  # noqa
