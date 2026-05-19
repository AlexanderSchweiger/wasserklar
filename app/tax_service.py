"""Zentrale Steuersatz-Definition.

Single Source of Truth fuer die im System verfuegbaren MwSt-Saetze. Aktuell
fest hinterlegt; sobald die Saetze in den Einstellungen aenderbar werden,
aendert sich nur die Implementierung hier — die Aufrufer (Routen, Templates,
Seed in cli.py) bleiben unveraendert.
"""
from decimal import Decimal


class TaxRateOption:
    """Schlanker Steuersatz-Datensatz mit ``.rate`` (Decimal) und ``.label``
    (str) — feldkompatibel zum ``TaxRate``-Model, damit Templates unveraendert
    darueber iterieren koennen."""

    __slots__ = ("rate", "label")

    def __init__(self, rate, label):
        self.rate = rate
        self.label = label


_DEFAULT_TAX_RATES = (
    (Decimal("0"), "0 % – keine MwSt"),
    (Decimal("10"), "10 %"),
    (Decimal("13"), "13 %"),
    (Decimal("20"), "20 %"),
)


def tax_rates():
    """Alle Steuersaetze, aufsteigend sortiert, als Liste von
    :class:`TaxRateOption` (Attribute ``rate`` und ``label``)."""
    return [TaxRateOption(rate, label) for rate, label in _DEFAULT_TAX_RATES]


def tax_rate_values():
    """Nur die Satz-Werte als Liste von Decimals, aufsteigend sortiert."""
    return [rate for rate, _ in _DEFAULT_TAX_RATES]
