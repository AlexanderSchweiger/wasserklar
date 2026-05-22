from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_STAMMDATEN

bp = Blueprint("periods", __name__, url_prefix="/perioden")
bp.before_request(require_blueprint_permission(PERM_STAMMDATEN))

from app.periods import routes  # noqa
