"""Staffel und Bedingungen einer Tarifposition — Datenform + Prüfung.

Beides steht als JSON-Text an ``TariffComponent`` (``tiers``, ``conditions``)
und reist so unverändert im Tarif-Snapshot des Rechnungslaufs und im
data_transfer-Export mit. Lesen/Schreiben nur über die Funktionen hier (bzw.
die Model-Properties, die sie nutzen) — ein kaputter Wert wirft ``ValueError``,
statt still falsch abzurechnen.

Bewusst flask- und DB-frei: Model, Tarifformular und Engine nutzen dieselben
Regeln, und die Unit-Tests brauchen keine App.

**Staffel** (nur bei m³-Positionen): Der Betrag der Position ist der Preis der
1. Stufe; ``tiers`` sind die weiteren Stufen „über ``above`` m³ → ``price``
€/m³“, streng aufsteigend. Die Grenze gehört zur unteren Stufe („bis 200 m³“
schließt 200 ein). Zwei Modi:

* ``graduated`` (anteilig): jede Stufe rechnet ihre eigene Teilmenge.
* ``whole``: die ganze Menge kostet den Preis der erreichten Stufe.

**Bedingungen**: ``{"contact_status": ["member", …]}`` — die Position entsteht
nur, wenn der Status des Rechnungsempfängers einer der genannten ist
(Schlüssel aus ``app.wg.STATUS_LABELS``).
"""
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.invoices.price_format import format_price
from app.wg import STATUS_LABELS

TIER_GRADUATED = "graduated"
TIER_WHOLE = "whole"
TIER_MODES = (TIER_GRADUATED, TIER_WHOLE)
TIER_MODE_LABELS = {
    TIER_GRADUATED: "anteilig — jede Stufe mit ihrem Preis",
    TIER_WHOLE: "Gesamtmenge zum Preis der erreichten Stufe",
}
# Weitere Stufen ueber der ersten (die 1. Stufe ist der Betrag der Position).
MAX_TIERS = 10

COND_CONTACT_STATUS = "contact_status"
CONDITION_KEYS = (COND_CONTACT_STATUS,)

_PRICE_STEP = Decimal("0.0001")      # TariffComponent.amount: Numeric(10, 4)
_QTY_STEP = Decimal("0.001")         # InvoiceItem.quantity: Numeric(12, 3)


@dataclass(frozen=True)
class TierStep:
    """Eine Stufe über der ersten: gilt für die Menge über ``above`` m³."""
    above: Decimal
    price: Decimal


def _num(value):
    """``Decimal`` → kurzer, verlustfreier Text fürs JSON (``"200"``, ``"1.5"``)."""
    text = format(Decimal(str(value)).normalize(), "f")
    return "0" if text in ("-0", "") else text


def _dec(value, what):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Ungültige Zahl in der Staffel ({what}).")
    if not d.is_finite():
        raise ValueError(f"Ungültige Zahl in der Staffel ({what}).")
    return d


def check_tiers(steps):
    """Prüft eine Stufenliste; wirft ``ValueError`` mit deutscher Meldung."""
    if len(steps) > MAX_TIERS:
        raise ValueError(f"Höchstens {MAX_TIERS} weitere Stufen.")
    prev = Decimal("0")
    for step in steps:
        if step.above <= prev:
            raise ValueError("Die Grenzen der Staffel müssen größer als 0 sein "
                             "und von Stufe zu Stufe steigen.")
        if step.above != step.above.quantize(_QTY_STEP):
            raise ValueError("Grenzen der Staffel höchstens mit drei Nachkommastellen.")
        if step.price < 0:
            raise ValueError("Preise der Staffel dürfen nicht negativ sein.")
        if step.price != step.price.quantize(_PRICE_STEP):
            raise ValueError("Preise der Staffel höchstens mit vier Nachkommastellen.")
        prev = step.above


def parse_tiers(raw):
    """JSON-Text (oder schon geladene Liste) → Tupel von ``TierStep``.
    Leer/``None`` → ``()``. Ungültig → ``ValueError``."""
    if not raw:
        return ()
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise ValueError("Staffel ist kein gültiges JSON.")
    if isinstance(data, tuple):
        data = list(data)
    if not isinstance(data, list):
        raise ValueError("Staffel muss eine Liste sein.")
    steps = []
    for item in data:
        if isinstance(item, TierStep):
            steps.append(item)
            continue
        if not isinstance(item, dict) or "above" not in item or "price" not in item:
            raise ValueError("Staffel-Eintrag braucht „above“ und „price“.")
        steps.append(TierStep(_dec(item["above"], "Grenze"), _dec(item["price"], "Preis")))
    steps = tuple(steps)
    check_tiers(steps)
    return steps


def dump_tiers(steps):
    """Tupel/Liste von ``TierStep`` → JSON-Text (``None`` ohne Stufen)."""
    steps = tuple(steps or ())
    if not steps:
        return None
    check_tiers(steps)
    return json.dumps([{"above": _num(s.above), "price": _num(s.price)} for s in steps])


def tiers_as_list(steps):
    """JSON-taugliche Liste (für den Snapshot)."""
    return [{"above": _num(s.above), "price": _num(s.price)} for s in (steps or ())]


def check_tier_mode(mode):
    if mode not in TIER_MODES:
        raise ValueError("Unbekannter Staffel-Modus.")
    return mode


def parse_conditions(raw):
    """JSON-Text (oder Dict) → normiertes Dict ``{"contact_status": (…)}``.
    Leer/``None`` → ``{}``. Unbekannte Bedingung oder Status → ``ValueError``."""
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise ValueError("Bedingung ist kein gültiges JSON.")
    if not isinstance(data, dict):
        raise ValueError("Bedingung muss ein Objekt sein.")
    out = {}
    for key, value in data.items():
        if key not in CONDITION_KEYS:
            raise ValueError(f"Unbekannte Bedingung „{key}“.")
        if key == COND_CONTACT_STATUS:
            if not isinstance(value, (list, tuple)) or not value:
                raise ValueError("Bitte mindestens einen Status für die Bedingung wählen.")
            unknown = [v for v in value if v not in STATUS_LABELS]
            if unknown:
                raise ValueError(f"Unbekannter Status „{unknown[0]}“ in der Bedingung.")
            # Kanonische Reihenfolge = Reihenfolge der Status-Labels.
            out[key] = tuple(k for k in STATUS_LABELS if k in set(value))
    return out


def dump_conditions(conditions):
    """Dict → JSON-Text (``None`` ohne Bedingung)."""
    norm = parse_conditions(conditions or {})
    if not norm:
        return None
    return json.dumps({k: list(v) for k, v in norm.items()})


def conditions_as_dict(conditions):
    """JSON-taugliches Dict (für den Snapshot)."""
    return {k: list(v) for k, v in parse_conditions(conditions or {}).items()}


def contact_statuses(conditions):
    """Status-Menge der Bedingung (``frozenset``) oder ``None`` = gilt für alle."""
    statuses = parse_conditions(conditions or {}).get(COND_CONTACT_STATUS)
    return frozenset(statuses) if statuses else None


def status_condition_label(statuses):
    """„Mitglied“ bzw. „Mitglied, Interessent“ — für Badges und Texte."""
    return ", ".join(STATUS_LABELS[k] for k in STATUS_LABELS if k in (statuses or ()))


def condition_text(conditions):
    """Lesbare Bedingung (``"nur Mitglied"``) oder ``""`` — nimmt JSON-Text,
    Dict oder Snapshot-Dict. Jinja-Global ``tariff_condition_text``."""
    statuses = contact_statuses(conditions)
    return f"nur {status_condition_label(statuses)}" if statuses else ""


def _de(value, decimals):
    text = f"{Decimal(str(value)):,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def _de_qty(value):
    d = Decimal(str(value))
    if d == d.to_integral_value():
        return _de(d, 0)
    return format(d.normalize(), "f").replace(".", ",")


def levels_text(first_price, tiers, mode=TIER_GRADUATED, decimals=2):
    """Stufen als Text: ``bis 200 m³: 1,20 · über 200 m³: 1,50 €/m³
    (anteilig)`` — ``""`` ohne Staffel. ``tiers`` als JSON-Text, Liste von
    Dicts (Snapshot) oder ``TierStep``-Tupel; ``decimals`` = Nachkommastellen
    der Gebührenart. Jinja-Global ``tariff_levels_text``."""
    steps = parse_tiers(tiers)
    if not steps or first_price is None:
        return ""
    bounds = [s.above for s in steps]
    prices = [Decimal(str(first_price))] + [s.price for s in steps]
    parts = []
    for i, price in enumerate(prices):
        lower = bounds[i - 1] if i > 0 else None
        upper = bounds[i] if i < len(bounds) else None
        if lower is None:
            rng = f"bis {_de_qty(upper)} m³"
        elif upper is None:
            rng = f"über {_de_qty(lower)} m³"
        else:
            rng = f"über {_de_qty(lower)} bis {_de_qty(upper)} m³"
        parts.append(f"{rng}: {format_price(price, decimals)}")
    how = "Gesamtmenge zum Stufenpreis" if mode == TIER_WHOLE else "anteilig"
    return " · ".join(parts) + f" €/m³ ({how})"
