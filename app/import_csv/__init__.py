from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_STAMMDATEN

bp = Blueprint("import_csv", __name__, url_prefix="/import")
bp.before_request(require_blueprint_permission(PERM_STAMMDATEN))

from app.import_csv import routes  # noqa: E402, F401
