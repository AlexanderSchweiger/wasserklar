"""Trinkwasser-Parameterkatalog + Grenzwert-Bewertung fuer Wasserproben.

Quelle der Wahrheit fuer die beprobten Trinkwasser-Parameter (Label, Einheit,
Grenzwert) und die Ampel-Logik. Der Katalog gilt je Land des Mandanten
(``app/country.py``):

* **Oesterreich** — Trinkwasserverordnung (TWV), BGBl. II Nr. 304/2001 idgF:
  der Basiskatalog ``PARAMETERS``.
* **Deutschland** — Trinkwasserverordnung (TrinkwV 2023), BGBl. 2023 I Nr. 159:
  Basiskatalog plus ``_DE_OVERLAY`` (abweichende Grenzwerte, zusaetzliche
  Parameter wie Uran/PFAS). Grenzwerte mit Uebergangsfrist (Blei, Arsen, PFAS)
  tragen ihre Stufen als ``limit_steps`` und gelten zum **Probenahmedatum**.

Die Schluessel sind in beiden Laendern gleich, damit erfasste Befunde und
Mandanten-Overrides ein spaeteres Umstellen des Landes ueberleben; Parameter
des jeweils anderen Landes bleiben fuer Altbefunde beschriftbar.

Grenzwerte sind Code-Konstanten, pro Tenant aber ueber das ``AppSetting``-KV
(Key ``water_quality.<param>.limit``) ueberschreibbar — das ist das SaaS-/
Selbsthoster-taugliche Override-Muster (kein Plan-Gate). Die Katalogwerte
sind aus dem Verordnungstext uebernommen, ersetzen aber keine fachliche
Pruefung durch das Untersuchungslabor bzw. das Gesundheitsamt.

Badge-Klassen folgen der Tabler-Konvention (dezente ``bg-*-lt``-Soft-Variante
bzw. solides ``bg-red text-white`` fuer prominente Ueberschreitung — nie
``text-white-lt``; siehe Projekt-CLAUDE.md).

Bewertungslogik (bewusst compliance-orientiert): JEDE Grenzwert-Ueberschreitung
ist ein Verstoss → ``alarm`` (rot). ``warning`` (gelb) markiert nur das Annaehern
an den Grenzwert (>= 90 % bei Max-Parametern); ``ok`` (gruen) sonst. Bei einem
Null-Grenzwert (mikrobiologisch) gibt es keine Warn-Zone — jeder Nachweis ist
sofort ``alarm``.
"""
from datetime import date

from app.country import COUNTRY_AT, COUNTRY_DE, current_code
from app.models import AppSetting

# Parameter-Gruppen (Anzeige-Reihenfolge im Befund-Formular und Bericht).
GROUPS = {
    "mikrobiologisch": "Mikrobiologische Parameter",
    "chemisch": "Chemische / toxikologische Parameter",
    "indikator": "Indikatorparameter",
}

# Rechtsgrundlage je Land (Kurzname fuer Beschriftungen, Langname fuer Hinweise).
REGULATIONS = {
    COUNTRY_AT: {
        "short": "TWV",
        "name": "österreichischen Trinkwasserverordnung (TWV, BGBl. II Nr. 304/2001 idgF)",
    },
    COUNTRY_DE: {
        "short": "TrinkwV",
        "name": "deutschen Trinkwasserverordnung (TrinkwV 2023, BGBl. 2023 I Nr. 159)",
    },
}

# key -> {label, unit, group, kind, ...}
#   kind == "max"   -> Grenzwert "limit" (Ueberschreitung = alarm); ohne "limit"
#                      (und ohne aktive Stufe) gilt der Parameter als "info"
#   kind == "range" -> Bereich "limit_min".."limit_max" (ausserhalb = alarm)
#   kind == "info"  -> kein Grenzwert (nur Dokumentation; numerisch => ok)
#   limit_steps     -> ((ab_datum, neuer_grenzwert), ...) aufsteigend; der Wert
#                      der letzten Stufe mit ab_datum <= Probenahmedatum gilt
#   note            -> Zusatzhinweis, erscheint auf der Grenzwert-Seite
# Der Basiskatalog ist der oesterreichische (TWV).
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

# Deutschland (TrinkwV 2023, Anlagen 1-3): ueberschreibt einzelne Werte des
# Basiskatalogs und ergaenzt Parameter (neue Schluessel muessen label/unit/
# group/kind vollstaendig mitbringen). Werte aus dem Verordnungstext:
# https://www.gesetze-im-internet.de/trinkwv_2023/ (Anlage 2 in mg/l dort,
# hier je Parameter in der gebraeuchlichen Laborenheit).
_DE_OVERLAY = {
    "koloniezahl_37": {"label": "Koloniezahl 36 °C"},
    "nitrit": {"note": "Am Ausgang des Wasserwerks gilt zusätzlich ein Grenzwert von 0,10 mg/l. "
                       "Außerdem muss Nitrat/50 + Nitrit/3 ≤ 1 sein."},
    # Blei 10 µg/l bis 11.01.2028, danach 5 µg/l; Arsen 10 µg/l bis 11.01.2036,
    # danach 4 µg/l (fuer ab 12.01.2028 neu in Betrieb genommene Anlagen schon
    # ab 12.01.2028 — als Stufe nicht abbildbar, daher der Hinweis).
    "blei": {"limit": 10, "limit_steps": ((date(2028, 1, 12), 5),),
             "note": "Ab 12.01.2028 gilt 5 µg/l (bis dahin 10 µg/l)."},
    "arsen": {"limit": 10, "limit_steps": ((date(2036, 1, 12), 4),),
              "note": "Ab 12.01.2036 gilt 4 µg/l; für ab 12.01.2028 neu in Betrieb "
                      "genommene Anlagen bereits ab 12.01.2028."},
    "leitfaehigkeit": {"label": "Elektr. Leitfähigkeit (25 °C)", "limit": 2790},
    "chlorid": {"limit": 250},
    "truebung": {"note": "Der Grenzwert gilt als eingehalten, wenn er am Ausgang des "
                         "Wasserwerks nicht überschritten wird."},
    # --- zusaetzliche chemische Parameter ---
    "uran":     {"label": "Uran",     "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 10},
    "cadmium":  {"label": "Cadmium",  "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 3},
    "fluorid":  {"label": "Fluorid",  "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 1.5},
    "bor":      {"label": "Bor",      "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 1.0},
    "pestizide_einzeln": {
        "label": "Pestizide (je Wirkstoff)", "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 0.1,
        "note": "Für Aldrin, Dieldrin, Heptachlor und Heptachlorepoxid gilt 0,030 µg/l."},
    "pestizide_summe": {
        "label": "Pestizide (gesamt)", "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 0.5},
    "pfas_20": {
        "label": "Summe PFAS-20", "unit": "µg/l", "group": "chemisch", "kind": "max",
        "limit_steps": ((date(2026, 1, 12), 0.1),),
        "note": "Grenzwert gilt ab 12.01.2026."},
    "pfas_4": {
        "label": "Summe PFAS-4", "unit": "µg/l", "group": "chemisch", "kind": "max",
        "limit_steps": ((date(2028, 1, 12), 0.02),),
        "note": "Grenzwert gilt ab 12.01.2028 (PFOA, PFNA, PFHxS, PFOS)."},
    "chlorat": {
        "label": "Chlorat", "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 0.07,
        "note": "Nur bei Desinfektion mit chloratbildenden Mitteln zu untersuchen."},
    "chlorit": {
        "label": "Chlorit", "unit": "mg/l", "group": "chemisch", "kind": "max", "limit": 0.2,
        "note": "Nur bei Desinfektion mit Chlordioxid zu untersuchen."},
    "thm": {
        "label": "Trihalogenmethane (THM)", "unit": "µg/l", "group": "chemisch", "kind": "max", "limit": 50,
        "note": "Nur bei Desinfektion mit THM-bildenden Mitteln zu untersuchen."},
    # --- zusaetzlicher Indikatorparameter ---
    "aluminium": {"label": "Aluminium", "unit": "mg/l", "group": "indikator", "kind": "max", "limit": 0.2},
}


def _build_catalog(overlay):
    """Basiskatalog + Laender-Overlay (Kopie — ``PARAMETERS`` bleibt unberuehrt)."""
    merged = {key: dict(meta) for key, meta in PARAMETERS.items()}
    for key, patch in overlay.items():
        merged.setdefault(key, {}).update(patch)
    return merged


_CATALOGS = {
    COUNTRY_AT: PARAMETERS,
    COUNTRY_DE: _build_catalog(_DE_OVERLAY),
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


def parameters(code=None):
    """Parameterkatalog des Mandanten-Landes (oder von ``code``)."""
    return _CATALOGS.get(code or current_code(), PARAMETERS)


def regulation(code=None):
    """Rechtsgrundlage des Landes: ``{"short": ..., "name": ...}``."""
    return REGULATIONS.get(code or current_code(), REGULATIONS[COUNTRY_AT])


def regulation_short(code=None):
    """Kurzname der Trinkwasserverordnung („TWV" / „TrinkwV")."""
    return regulation(code)["short"]


def regulation_name(code=None):
    """Langname samt Fundstelle (Genitiv-Form: „… der österreichischen …")."""
    return regulation(code)["name"]


def _meta(key):
    """Katalogeintrag: erst das Mandanten-Land, dann jedes andere — so bleiben
    Altbefunde mit Parametern eines frueheren Landes beschriftbar."""
    meta = parameters().get(key)
    if meta is None:
        for catalog in _CATALOGS.values():
            if key in catalog:
                return catalog[key]
    return meta


def parameter_label(key):
    meta = _meta(key)
    return meta["label"] if meta else key


def parameter_unit(key):
    meta = _meta(key)
    return meta["unit"] if meta else ""


def parameter_note(key):
    """Zusatzhinweis zum Grenzwert (Uebergangsfristen, Sonderfaelle) oder ``""``."""
    meta = _meta(key)
    return (meta.get("note") or "") if meta else ""


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


def _max_on(meta, on):
    """Max-Grenzwert am Stichtag ``on``: der Basiswert ``limit``, abgeloest von
    der letzten Stufe aus ``limit_steps`` mit Datum <= ``on``. ``None``, wenn
    (noch) kein Grenzwert gilt."""
    value = meta.get("limit")
    for start, step_value in meta.get("limit_steps", ()):
        if on >= start:
            value = step_value
    return value


def effective_limit(key, on=None):
    """Geltender Grenzwert als Tuple:
    ``("max", value)`` | ``("range", lo, hi)`` | ``("info", None)``.

    ``on`` ist der Stichtag (Probenahmedatum, Default heute) fuer Grenzwerte
    mit Uebergangsfrist. Ein AppSetting-Override schlaegt den Default (und gilt
    zeitlos). Override-Format: ``"50"`` (max) oder ``"6,5-9,5"`` (range; dt.
    Komma erlaubt). Ungueltige Overrides werden ignoriert (Fallback auf Default)."""
    meta = _meta(key)
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
        value = _max_on(meta, on or date.today())
        return ("max", float(value)) if value is not None else ("info", None)
    if kind == "range":
        return ("range", float(meta["limit_min"]), float(meta["limit_max"]))
    return ("info", None)


def _fmt(v):
    """Float -> kompakter dt. String (ganze Zahlen ohne Nachkomma)."""
    if v == int(v):
        return str(int(v))
    return ("%g" % v).replace(".", ",")


def limit_display(key, on=None):
    """Menschliche Grenzwert-Anzeige inkl. Einheit (z. B. ``50 mg/l``,
    ``6,5–9,5``). Leerer String fuer Info-Parameter ohne Grenzwert."""
    lim = effective_limit(key, on)
    unit = parameter_unit(key)
    suffix = (" " + unit) if unit else ""
    if lim[0] == "max":
        return (_fmt(lim[1]) + suffix).strip()
    if lim[0] == "range":
        return (_fmt(lim[1]) + "–" + _fmt(lim[2]) + suffix).strip()
    return ""


def limit_value(key, on=None):
    """Numerischer Max-Grenzwert (fuer die Diagramm-Referenzlinie) oder ``None``
    (bei range/info)."""
    lim = effective_limit(key, on)
    return lim[1] if lim[0] == "max" else None


def assess(key, value_num, on=None):
    """Ampel-Status fuer einen Messwert. ``value_num``: float|Decimal|None;
    ``on``: Probenahmedatum (Default heute).

    None -> ``unknown``. Info-Parameter -> ``ok``. max: ``> limit`` -> alarm,
    ``>= 90 % limit`` -> warning, sonst ok. range: ausserhalb -> alarm."""
    if value_num is None:
        return STATUS_UNKNOWN
    try:
        v = float(value_num)
    except (TypeError, ValueError):
        return STATUS_UNKNOWN
    lim = effective_limit(key, on)
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
    for key, meta in parameters().items():
        rows_by_group.setdefault(meta["group"], []).append(
            (key, meta["label"], meta["unit"], limit_display(key))
        )
    out = []
    for gkey, glabel in GROUPS.items():
        if rows_by_group.get(gkey):
            out.append((glabel, rows_by_group[gkey]))
    return out
