from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_ZAEHLER

bp = Blueprint("meters", __name__, url_prefix="/meters")
bp.before_request(require_blueprint_permission(PERM_ZAEHLER))

from app.meters import routes  # noqa
