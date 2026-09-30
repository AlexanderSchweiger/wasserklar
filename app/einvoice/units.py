"""Einheiten der Rechnungspositionen (Freitext) → UN/ECE-Recommendation-20-Codes.

EN 16931 verlangt je Position einen Einheiten-Code (BT-130). In der App ist die
Einheit Freitext (``m³`` aus dem Tarif, ``Pauschal``, im Positions-Editor frei
waehlbar). Unbekanntes faellt auf ``C62`` („Einheit") zurueck — gueltig, nur
weniger sprechend.
"""

_UNIT_CODES = {
    "m³": "MTQ", "m3": "MTQ", "cbm": "MTQ", "kubikmeter": "MTQ",
    "pauschal": "LS", "pauschale": "LS", "psch": "LS", "psch.": "LS", "pausch.": "LS",
    "stk": "H87", "stk.": "H87", "stück": "H87", "stueck": "H87",
    "monat": "MON", "monate": "MON", "mon": "MON", "mon.": "MON",
    "jahr": "ANN", "jahre": "ANN",
    "tag": "DAY", "tage": "DAY",
    "h": "HUR", "std": "HUR", "std.": "HUR", "stunde": "HUR", "stunden": "HUR",
    "m": "MTR", "lfm": "MTR", "meter": "MTR",
    "m²": "MTK", "m2": "MTK", "qm": "MTK",
    "kwh": "KWH",
    "l": "LTR", "liter": "LTR",
    "kg": "KGM",
    "t": "TNE",
}

DEFAULT_UNIT_CODE = "C62"


def unit_code(unit):
    """Rec-20-Code fuer eine Freitext-Einheit, Fallback ``C62``."""
    return _UNIT_CODES.get((unit or "").strip().lower(), DEFAULT_UNIT_CODE)
