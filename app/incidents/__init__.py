from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_INCIDENTS

bp = Blueprint("incidents", __name__, url_prefix="/incidents")
bp.before_request(require_blueprint_permission(PERM_INCIDENTS))

from app.incidents import routes  # noqa: E402, F401
