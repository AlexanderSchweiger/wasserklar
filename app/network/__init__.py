from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_NETWORK

bp = Blueprint("network", __name__, url_prefix="/network")
bp.before_request(require_blueprint_permission(PERM_NETWORK))

from app.network import routes  # noqa: E402, F401
