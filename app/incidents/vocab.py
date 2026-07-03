"""Vokabular fuer das Stoerungs-/Rohrbruch-Journal (incidents).

Eine Quelle der Wahrheit fuer Ereignisart, Ursachenkategorie, Status und
Schweregrad — genutzt von Backend, Templates und dem Karten-JS (per
``window.INCIDENT.vocab`` injiziert, siehe ``as_client_dict``).

Badge-Klassen folgen der Tabler-Konvention: prominente Status solid +
``text-white``, dezente Hilfsangaben ``bg-*-lt`` (Soft-Variante); NIE
``text-white-lt`` auf Badges (siehe Projekt-CLAUDE.md, Badge-Lesbarkeit).
"""

# Ereignisart: key -> {label, icon (Font Awesome 5), color (Hex Marker), desc}
INCIDENT_TYPES = {
    "rohrbruch":     {"label": "Rohrbruch",          "icon": "fa-bolt",                 "color": "#c92a2a", "desc": "Bruch einer Leitung mit Wasseraustritt."},
    "undichtheit":   {"label": "Undichtheit / Leck", "icon": "fa-tint",                 "color": "#e8590c", "desc": "Schleichende Undichtheit, Muffen-/Verbindungsleck."},
    "druckverlust":  {"label": "Druckverlust",       "icon": "fa-tachometer-alt",           "color": "#f59f00", "desc": "Druckabfall im Netz ohne sichtbaren Austritt."},
    "verschmutzung": {"label": "Verschmutzung",      "icon": "fa-biohazard",            "color": "#7048e8", "desc": "Trinkwasser-Verunreinigung / Trübung / Befund."},
    "ausfall":       {"label": "Versorgungsausfall", "icon": "fa-power-off",            "color": "#343a40", "desc": "Ausfall der Versorgung (Pumpe, Stromausfall, Speicher leer)."},
    "sonstiges":     {"label": "Sonstiges",          "icon": "fa-exclamation-triangle", "color": "#868e96", "desc": "Sonstige Störung ohne eigene Kategorie."},
}

# Ursachenkategorie: key -> {label, icon}
CAUSES = {
    "frostschaden":      {"label": "Frostschaden",                "icon": "fa-snowflake"},
    "materialermuedung": {"label": "Materialermüdung / Alterung", "icon": "fa-hourglass-end"},
    "korrosion":         {"label": "Korrosion",                   "icon": "fa-bacterium"},
    "erddruck":          {"label": "Erddruck / Setzung",          "icon": "fa-mountain"},
    "fremdeinwirkung":   {"label": "Fremdeinwirkung (Bagger)",    "icon": "fa-truck-monster"},
    "ueberdruck":        {"label": "Überdruck / Druckstoß",       "icon": "fa-tachometer-alt"},
    "montagefehler":     {"label": "Montage-/Einbaufehler",       "icon": "fa-tools"},
    "unbekannt":         {"label": "Unbekannt",                   "icon": "fa-question"},
}

# Status: key -> {label, badge}. Prominent (Status entscheidend): solid + text-white.
STATUSES = {
    "offen":          {"label": "Offen",          "badge": "bg-danger text-white"},
    "in_bearbeitung": {"label": "In Bearbeitung", "badge": "bg-warning text-white"},
    "behoben":        {"label": "Behoben",        "badge": "bg-success text-white"},
}

# Schweregrad: key -> {label, badge (dezent/Soft), color (Marker-Ring)}
SEVERITIES = {
    "niedrig":  {"label": "Niedrig",  "badge": "bg-azure-lt",  "color": "#0ca678"},
    "mittel":   {"label": "Mittel",   "badge": "bg-yellow-lt", "color": "#f59f00"},
    "hoch":     {"label": "Hoch",     "badge": "bg-orange-lt", "color": "#e8590c"},
    "kritisch": {"label": "Kritisch", "badge": "bg-red-lt",    "color": "#c92a2a"},
}


def type_label(key):     return INCIDENT_TYPES.get(key, {}).get("label", key)
def type_icon(key):      return INCIDENT_TYPES.get(key, {}).get("icon", "fa-exclamation-triangle")
def type_color(key):     return INCIDENT_TYPES.get(key, {}).get("color", "#868e96")
def cause_label(key):    return CAUSES.get(key, {}).get("label", key) if key else ""
def status_label(key):   return STATUSES.get(key, {}).get("label", key)
def status_badge(key):   return STATUSES.get(key, {}).get("badge", "bg-secondary-lt")
def severity_label(key): return SEVERITIES.get(key, {}).get("label", key)
def severity_badge(key): return SEVERITIES.get(key, {}).get("badge", "bg-secondary-lt")
def severity_color(key): return SEVERITIES.get(key, {}).get("color", "#868e96")


def is_valid_type(key):     return key in INCIDENT_TYPES
def is_valid_status(key):   return key in STATUSES
def is_valid_severity(key): return key in SEVERITIES
def is_valid_cause(key):    return key in CAUSES


def as_client_dict():
    """Serialisierbares Dict fuer das Karten-JS (``window.INCIDENT.vocab``)."""
    return {
        "incidentTypes": INCIDENT_TYPES,
        "causes": CAUSES,
        "statuses": STATUSES,
        "severities": SEVERITIES,
    }
