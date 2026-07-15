"""Eigentuemerwechsel-Workflow (Blueprint ``owner_change``).

Gefuehrter Ablauf beim Liegenschaftsverkauf: Stichtags-Ablesung ->
Schlussrechnung an den Altbesitzer (unterjaehrig) -> Beenden/Neu-Anlegen der
``PropertyOwnership`` -> WG-Status. Die Schlussrechnung reduziert den spaeteren
Jahresverbrauch des Nachbesitzers im Massen-Rechnungslauf.

Das ganze Blueprint steht unter ``stammdaten``; die schlussrechnungserzeugenden
Schritte gaten zusaetzlich per Route auf ``rechnungen_op``.
"""
from flask import Blueprint

from app.auth.permissions import PERM_STAMMDATEN, require_blueprint_permission

bp = Blueprint(
    "owner_change", __name__,
    url_prefix="/owner-change",
    template_folder="templates",
)
bp.before_request(require_blueprint_permission(PERM_STAMMDATEN))

from app.owner_change import routes  # noqa: E402,F401
