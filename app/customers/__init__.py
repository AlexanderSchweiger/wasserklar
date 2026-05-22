from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_STAMMDATEN

bp = Blueprint("customers", __name__, url_prefix="/customers")
bp.before_request(require_blueprint_permission(PERM_STAMMDATEN))

from app.customers import routes  # noqa
