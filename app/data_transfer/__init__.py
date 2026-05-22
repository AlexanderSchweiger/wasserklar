from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_VERWALTUNG

bp = Blueprint(
    "data_transfer",
    __name__,
    url_prefix="/data-transfer",
    template_folder="../templates",
)
bp.before_request(require_blueprint_permission(PERM_VERWALTUNG))

from app.data_transfer import routes  # noqa: E402,F401
