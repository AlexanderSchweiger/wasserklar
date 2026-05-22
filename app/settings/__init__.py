from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_VERWALTUNG

bp = Blueprint('settings', __name__, url_prefix='/einstellungen')
bp.before_request(require_blueprint_permission(PERM_VERWALTUNG))

from app.settings import routes  # noqa: E402, F401
