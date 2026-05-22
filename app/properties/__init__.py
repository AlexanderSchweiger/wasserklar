from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_STAMMDATEN

bp = Blueprint("properties", __name__, url_prefix="/properties")
bp.before_request(require_blueprint_permission(PERM_STAMMDATEN))

from app.properties import routes  # noqa: E402, F401
