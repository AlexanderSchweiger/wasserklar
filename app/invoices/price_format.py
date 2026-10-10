"""Nachkommastellen von Preisen — die eine Stelle für Anzeige und Text.

Die Gebührenart legt fest, mit wie vielen Nachkommastellen ihr Preis je m³
erscheint (``ChargeType.price_decimals``: 2, 3 oder 4): im Rechnungstext
(„120 m³ × 0,10 €/m³“), in den Formularfeldern, in der Tarifübersicht und auf
der Rechnung. Gerechnet wird immer mit dem gespeicherten Preis (4 Stellen) —
die Einstellung betrifft nur die Darstellung.

Regel: **nie weniger Stellen, als der Preis hat.** Steht die Einstellung auf 2,
der Preis aber auf 0,0815 €/m³, erscheinen alle vier Stellen — sonst ergäbe
„Menge × angezeigter Preis“ nicht mehr den Betrag der Zeile.

Voreingestellt sind 2 Stellen. Altbelege und Tarif-Snapshots aus der Zeit
vor der Einstellung zeigen weiter 4 Stellen je m³ (``LEGACY_PLACES``).

Bewusst flask- und DB-frei: Model, Tarif-Engine, Formulare und Word-Export
nutzen dieselben Regeln.
"""
from decimal import Decimal, InvalidOperation

MIN_PLACES = 2
MAX_PLACES = 4            # TariffComponent.amount / InvoiceItem.unit_price: Numeric(10, 4)
DEFAULT_PLACES = 2        # Voreinstellung der Gebührenart
LEGACY_PLACES = 4         # Altbelege/-laeufe ohne Einstellung: bisher immer 4 je m³
PLACES_CHOICES = (2, 3, 4)


def _dec(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def clamp_places(decimals, default=DEFAULT_PLACES):
    """Eingestellte Stellen auf 2–4 begrenzen (ungültig → ``default``)."""
    try:
        n = int(decimals)
    except (TypeError, ValueError):
        n = default
    return min(max(n, MIN_PLACES), MAX_PLACES)


def needed_places(value):
    """Stellen, die der Wert wirklich nutzt: ``0.1000`` → 1, ``0.0815`` → 4
    (höchstens 4 — mehr kann die DB nicht speichern)."""
    d = _dec(value)
    if d is None:
        return 0
    exponent = d.normalize().as_tuple().exponent
    return min(max(-exponent, 0), MAX_PLACES)


def price_places(value, decimals=DEFAULT_PLACES):
    """Stellen, mit denen ``value`` erscheint: die eingestellten, aber nie
    weniger, als der Preis hat."""
    return max(clamp_places(decimals), needed_places(value))


def typed_places(value):
    """Stellen wie eingetippt (``"1.40"`` → 2, ``"0.1000"`` → 4, ``"12"`` → 2) —
    für Preise, die jemand im Positions-Editor von Hand eingibt. ``Decimal``
    behält die Nullen am Ende, deshalb zählt der Exponent, nicht der Wert."""
    d = _dec(value)
    if d is None:
        return MIN_PLACES
    return clamp_places(-d.as_tuple().exponent)


def format_price(value, decimals=DEFAULT_PLACES, *, sep=","):
    """``Decimal("0.1")`` → ``"0,10"`` (bei 2 Stellen), ohne Tausenderpunkt —
    für Rechnungstexte und Formularfelder (``sep="."`` für ``type="number"``).
    ``None`` → ``""``."""
    d = _dec(value)
    if d is None:
        return ""
    return f"{d:.{price_places(d, decimals)}f}".replace(".", sep)
