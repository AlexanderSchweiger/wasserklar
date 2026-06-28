"""TWV-Parameter-Katalog + Grenzwert-Bewertung fuer Wasserproben.

Quelle der Wahrheit fuer die beprobten Trinkwasser-Parameter (Label, Einheit,
Grenzwert nach oesterr. Trinkwasserverordnung, BGBl. II Nr. 304/2001 idgF) und
die Ampel-Logik. Grenzwerte sind Code-Konstanten, pro Tenant aber ueber das
``AppSetting``-KV (Key ``water_quality.<param>.limit``) ueberschreibbar — das
ist das SaaS-/Selbsthoster-taugliche Override-Muster (kein Plan-Gate).

Badge-Klassen folgen der Tabler-Konvention (dezente ``bg-*-lt``-Soft-Variante
bzw. solides ``bg-red text-white`` fuer prominente Ueberschreitung — nie
``text-white-lt``; siehe Projekt-CLAUDE.md).

Bewertungslogik (bewusst compliance-orientiert): JEDE Grenzwert-Ueberschreitung
ist ein Verstoss → ``alarm`` (rot). ``warning`` (gelb) markiert nur das Annaehern
an den Grenzwert (>= 90 % bei Max-Parametern); ``ok`` (gruen) sonst. Bei einem
Null-Grenzwert (mikrobiologisch) gibt es keine Warn-Zone — jeder Nachweis ist
sofort ``alarm``.
"""
from app.models import AppSetting

# Parameter-Gruppen (Anzeige-Reihenfolge im Befund-Formular und Bericht).
GROUPS = {
    "mikrobiologisch": "Mikrobiologische Parameter",
    "chemisch": "Chemische / toxikologische Parameter",
    "indikator": "Indikatorparameter",
}

# key -> {label, unit, group, kind, ...}
#   kind == "max"   -> Grenzwert "limit" (Ueberschreitung = alarm)
#   kind == "range" -> Bereich "limit_min".."limit_max" (ausserhalb = alarm)
#   kind == "info"  -> kein Grenzwert (nur Dokumentation; numerisch => ok)
PARAMETERS = {
    # --- Mikrobiologisch (Null-Toleranz) ---
    "e_coli":         {"label": "E. coli",                "unit": "KBE/100 ml", "group": "mikrobiologisch", "kind": "max", "limit": 0},
    "enterokokken":   {"label": "Enterokokken",           "unit": "KBE/100 ml", "group": "mikrobiologisch", "kind": "max", "limit": 0},
    "coliforme":      {"label": "Coliforme Bakterien",    "unit": "KBE/100 ml", "group": "mikrobiologisch", "kind": "max", "limit": 0},
    "koloniezahl_22": {"label": "Koloniezahl 22 °C",      "unit": "KBE/ml",     "group": "mikrobiologisch", "kind": "info"},
    "koloniezahl_37": {"label": "Koloniezahl 37 °C",      "unit": "KBE/ml",     "group": "mikrobiologisch", "kind": "info"},
    # --- Chemisch / toxikologisch ---
    "nitrat":   {"label": "Nitrat",   "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 50},
    "nitrit":   {"label": "Nitrit",   "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 0.5},
    "ammonium": {"label": "Ammonium", "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 0.5},
    "arsen":    {"label": "Arsen",    "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 10},
    "blei":     {"label": "Blei",     "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 10},
    "nickel":   {"label": "Nickel",   "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 20},
    "kupfer":   {"label": "Kupfer",   "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 2},
    # --- Indikatorparameter ---
    "ph":             {"label": "pH-Wert",               "unit": "",      "group": "indikator", "kind": "range", "limit_min": 6.5, "limit_max": 9.5},
    "leitfaehigkeit": {"label": "Elektr. Leitfähigkeit", "unit": "µS/cm", "group": "indikator", "kind": "max", "limit": 2500},
    "truebung":       {"label": "Trübung",               "unit": "NTU",   "group": "indikator", "kind": "max", "limit": 1.0},
    "eisen":          {"label": "Eisen",                 "unit": "mg/l",  "group": "indikator", "kind": "max", "limit": 0.2},
    "mangan":         {"label": "Mangan",                "unit": "mg/l",  "group": "indikator", "kind": "max", "limit": 0.05},
    "chlorid":        {"label": "Chlorid",               "unit": "mg/l",  "group": "indikator", "kind": "max", "limit": 200},
    "sulfat":         {"label": "Sulfat",                "unit": "mg/l",  "group": "indikator", "kind": "max", "limit": 250},
    "natrium":        {"label": "Natrium",               "unit": "mg/l",  "group": "indikator", "kind": "max", "limit": 200},
    "gesamthaerte":   {"label": "Gesamthärte",           "unit": "°dH",   "group": "indikator", "kind": "info"},
}

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ALARM = "alarm"
STATUS_UNKNOWN = "unknown"

STATUS_BADGES = {
    "ok":      "bg-green-lt",
    "warning": "bg-yellow-lt",
    "alarm":   "bg-red text-white",
    "unknown": "bg-secondary-lt",
}
STATUS_LABELS = {
    "ok":      "im Grenzwert",
    "warning": "grenzwertig",
    "alarm":   "überschritten",
    "unknown": "ohne Bewertung",
}

# Anteil des Grenzwerts, ab dem ein Max-Wert als "grenzwertig" (gelb) gilt.
_WARN_FRACTION = 0.9


def parameter_label(key):
    meta = PARAMETERS.get(key)
    return meta["label"] if meta else key


def parameter_unit(key):
    meta = PARAMETERS.get(key)
    return meta["unit"] if meta else ""


def status_badge(status):
    return STATUS_BADGES.get(status, "bg-secondary-lt")


def status_label(status):
    return STATUS_LABELS.get(status, status or "")


def _override(key):
    """AppSetting-Override fuer einen Parameter-Grenzwert (oder ``None``)."""
    raw = AppSetting.get(f"water_quality.{key}.limit")
    return (raw or "").strip() or None


def _to_float(text):
    try:
        return float((text or "").strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def effective_limit(key):
    """Geltender Grenzwert als Tuple:
    ``("max", value)`` | ``("range", lo, hi)`` | ``("info", None)``.

    Ein AppSetting-Override schlaegt den Default. Override-Format: ``"50"`` (max)
    oder ``"6,5-9,5"`` (range; dt. Komma erlaubt). Ungueltige Overrides werden
    ignoriert (Fallback auf Default)."""
    meta = PARAMETERS.get(key)
    if not meta:
        return ("info", None)
    kind = meta["kind"]
    ov = _override(key)
    if ov:
        if kind == "range" and "-" in ov:
            lo_raw, _, hi_raw = ov.partition("-")
            lo, hi = _to_float(lo_raw), _to_float(hi_raw)
            if lo is not None and hi is not None:
                return ("range", lo, hi)
        else:
            val = _to_float(ov)
            if val is not None:
                return ("max", val)
    if kind == "max":
        return ("max", float(meta["limit"]))
    if kind == "range":
        return ("range", float(meta["limit_min"]), float(meta["limit_max"]))
    return ("info", None)


def _fmt(v):
    """Float -> kompakter dt. String (ganze Zahlen ohne Nachkomma)."""
    if v == int(v):
        return str(int(v))
    return ("%g" % v).replace(".", ",")


def limit_display(key):
    """Menschliche Grenzwert-Anzeige inkl. Einheit (z. B. ``50 mg/l``,
    ``6,5–9,5``). Leerer String fuer Info-Parameter ohne Grenzwert."""
    lim = effective_limit(key)
    unit = parameter_unit(key)
    suffix = (" " + unit) if unit else ""
    if lim[0] == "max":
        return (_fmt(lim[1]) + suffix).strip()
    if lim[0] == "range":
        return (_fmt(lim[1]) + "–" + _fmt(lim[2]) + suffix).strip()
    return ""


def limit_value(key):
    """Numerischer Max-Grenzwert (fuer die Diagramm-Referenzlinie) oder ``None``
    (bei range/info)."""
    lim = effective_limit(key)
    return lim[1] if lim[0] == "max" else None


def assess(key, value_num):
    """Ampel-Status fuer einen Messwert. ``value_num``: float|Decimal|None.

    None -> ``unknown``. Info-Parameter -> ``ok``. max: ``> limit`` -> alarm,
    ``>= 90 % limit`` -> warning, sonst ok. range: ausserhalb -> alarm."""
    if value_num is None:
        return STATUS_UNKNOWN
    try:
        v = float(value_num)
    except (TypeError, ValueError):
        return STATUS_UNKNOWN
    lim = effective_limit(key)
    if lim[0] == "info":
        return STATUS_OK
    if lim[0] == "range":
        _, lo, hi = lim
        return STATUS_OK if (lo <= v <= hi) else STATUS_ALARM
    _, limit = lim  # max
    if v > limit:
        return STATUS_ALARM
    if limit > 0 and v >= _WARN_FRACTION * limit:
        return STATUS_WARNING
    return STATUS_OK


def catalog_for_form():
    """Fuer das Befund-Formular gruppiert:
    ``[(group_label, [(key, label, unit, limit_display), ...]), ...]`` in
    Katalog-Reihenfolge; nur Gruppen mit Parametern."""
    rows_by_group = {g: [] for g in GROUPS}
    for key, meta in PARAMETERS.items():
        rows_by_group.setdefault(meta["group"], []).append(
            (key, meta["label"], meta["unit"], limit_display(key))
        )
    out = []
    for gkey, glabel in GROUPS.items():
        if rows_by_group.get(gkey):
            out.append((glabel, rows_by_group[gkey]))
    return out
