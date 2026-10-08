"""Rundung der Rechnungsbeträge — die eine Stelle für die USt je Position.

``Invoice.recalculate_total``, die Tarif-Engine (Rechner, Schlussrechnungs-
Vorschau) und der Schätz-Korrektur-Abgleich rechnen Brutto alle hierüber;
sonst liefen Vorschau und Rechnung um Rundungscents auseinander.

Bewusst ohne Model-Import (``models.py`` importiert dieses Modul).
"""
from decimal import Decimal

CENT = Decimal("0.01")
_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


def line_tax(net, rate):
    """USt-Betrag einer Position: netto × Satz, je Position auf Cent gerundet
    (Default-Rundung des Decimal-Kontexts — so rechnete ``recalculate_total``
    schon immer). Ohne Satz bzw. Satz ≤ 0 → 0."""
    if not rate:
        return _ZERO
    rate = Decimal(str(rate))
    if rate <= 0:
        return _ZERO
    return (Decimal(str(net or 0)) * rate / _HUNDRED).quantize(CENT)


def line_gross(net, rate):
    """Brutto einer Position (netto + ``line_tax``)."""
    return Decimal(str(net or 0)) + line_tax(net, rate)
