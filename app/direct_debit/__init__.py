"""SEPA-Lastschrift: Einzugslaeufe und Bankdatei (pain.008).

Fachlogik in ``services.py``, Rechnungs-Hinweis in ``notice.py``, Dateien in
``pain008.py``/``export_csv.py``. Recht wie das Bezahlen auf der OP-Seite:
``rechnungen_op``.
"""
from flask import Blueprint

from app.auth.permissions import PERM_RECHNUNGEN, require_blueprint_permission

bp = Blueprint("direct_debit", __name__, url_prefix="/direct-debit")
bp.before_request(require_blueprint_permission(PERM_RECHNUNGEN))

from app.direct_debit import routes  # noqa: E402, F401
