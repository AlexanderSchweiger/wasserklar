from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_TECHNIK

bp = Blueprint("technik", __name__, url_prefix="/technik")
bp.before_request(require_blueprint_permission(PERM_TECHNIK))

from app.technik import routes  # noqa: E402, F401
