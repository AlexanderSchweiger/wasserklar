from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_BUCHHALTUNG

bp = Blueprint("bank_import", __name__, url_prefix="/bank-import")
bp.before_request(require_blueprint_permission(PERM_BUCHHALTUNG))

from app.bank_import import routes  # noqa: E402, F401
