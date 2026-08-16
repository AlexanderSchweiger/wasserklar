"""Plankostenrechnung — Tarifplanung für Rücklagen und Kredittilgung.

Beantwortet die Frage, die jede Genossenschaft vor einer größeren Investition
stellt: *„Welchen Tarif müssen wir setzen, damit in fünf Jahren die halbe
Million für die Netzerneuerung da ist?"* — bzw. dasselbe für einen laufenden
Kredit, der bis zu einem Stichtag getilgt sein soll.

Der Nutzer erfasst ein Finanzierungsziel; die App leitet daraus den jährlichen
Mehrbedarf ab (Sparrate bzw. Annuität) und schlägt **drei Tarifpakete** vor, die
sich im Verteilungsschlüssel unterscheiden: über die Grundgebühr (planbar),
über den m³-Preis (Sparanreiz) oder hälftig. Gerechnet wird nur der
**Zusatzbedarf** — der bestehende Tarif bleibt die Basis.

Zweiter Baustein: die Jahres-Verbrauchssummen (``app/consumption.py``). Sie
sind Rechenbasis für die m³-Pakete, dienen gleichzeitig als Cache für Dashboard
und Auswertungen und lassen sich für Jahre vor der App-Einführung manuell
nachtragen.

Recht: ``auswertungen`` für das ganze Blueprint (Planung ist eine Auswertung).
Einzige Ausnahme ist ``goal_apply_tariff`` — wer aus einem Paket einen echten
Tarif macht, braucht zusätzlich ``rechnungen_op``.
"""
from flask import Blueprint

from app.auth.permissions import PERM_AUSWERTUNGEN, require_blueprint_permission
from app.cost_planning import services

bp = Blueprint("cost_planning", __name__, url_prefix="/cost-planning")

bp.before_request(require_blueprint_permission(PERM_AUSWERTUNGEN))


@bp.context_processor
def _inject_labels():
    """Label-Dicts für die Plankosten-Templates."""
    return {
        "cp_scenario_labels": services.SCENARIO_LABELS,
        "cp_rounding_choices": services.ROUNDING_CHOICES,
    }


from app.cost_planning import routes  # noqa: E402,F401
