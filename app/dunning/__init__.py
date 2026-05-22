from flask import Blueprint
from app.auth.permissions import require_blueprint_permission, PERM_MAHNWESEN

bp = Blueprint("dunning", __name__, url_prefix="/dunning")
bp.before_request(require_blueprint_permission(PERM_MAHNWESEN))

from app.dunning import routes  # noqa
