"""Vokabular fuer das Technik-/Leitungsplan-Modul.

Eine Quelle der Wahrheit fuer Feature-Typen (Punkt & Linie), Lagegenauigkeit,
Material-Vorschlaege und Wartungsarten — genutzt von Backend, Templates und dem
Karten-JS (per ``window.TECHNIK`` injiziert, siehe ``as_client_dict``).

Badge-Klassen folgen der Tabler-Konvention (dezente ``bg-*-lt``-Soft-Variante,
nie ``text-white-lt`` — siehe Projekt-CLAUDE.md).
"""

# Punkt-Typen: key -> {label, icon (Font Awesome 5), color (Hex), desc (Tooltip)}
POINT_TYPES = {
    "hydrant":         {"label": "Hydrant",                      "icon": "fa-fire-extinguisher", "color": "#d63939", "desc": "Über-/Unterflurhydrant — Löschwasserentnahme und Netzspülung."},
    "schieber":        {"label": "Schieber",                     "icon": "fa-circle-notch",      "color": "#f59f00", "desc": "Absperrarmatur — sperrt einen Leitungsabschnitt ab (z. B. für Reparaturen)."},
    "quelle":          {"label": "Quelle / Quellfassung",        "icon": "fa-tint",              "color": "#0ca678", "desc": "Quelle bzw. Quellfassung — Wasseraufkommen, Eintritt ins Netz."},
    "behaelter":       {"label": "Wasserbehälter / Hochbehälter", "icon": "fa-database",         "color": "#1971c2", "desc": "Hoch- oder Tiefbehälter — Speicherung und Druckhaltung im Netz."},
    "verteiler":       {"label": "Verteiler / Schacht",          "icon": "fa-project-diagram",   "color": "#7048e8", "desc": "Verteilerschacht/Knoten, an dem mehrere Leitungen zusammentreffen."},
    "pumpe":           {"label": "Pumpe / Druckerhöhung",        "icon": "fa-cog",               "color": "#e8590c", "desc": "Pumpe oder Druckerhöhungsanlage zur Förderung/Druckanhebung."},
    "hausanschluss":   {"label": "Hausanschluss",                "icon": "fa-home",              "color": "#2f9e44", "desc": "Anschlusspunkt einer Liegenschaft ans Netz (oft mit Wasserzähler)."},
    "anbohrschelle":   {"label": "Anbohrschelle",               "icon": "fa-code-branch",       "color": "#66a80f", "desc": "Anbohrschelle / Abzweigung auf einer bestehenden Leitung (Sattelbohrsystem)."},
    "entlueftung":     {"label": "Entlüftung",                 "icon": "fa-wind",              "color": "#3bc9db", "desc": "Entlüftungsventil — lässt eingeschlossene Luft aus der Leitung entweichen."},
    "auslauf":         {"label": "Auslauf / Entleerung",       "icon": "fa-arrow-alt-circle-down", "color": "#ff8787", "desc": "Auslauf- oder Entleerungspunkt — kontrollierte Wasserabgabe oder Netzentleerung."},
    "probenahme":      {"label": "Probenahmestelle",             "icon": "fa-vial",              "color": "#c2255c", "desc": "Entnahmestelle für die Trinkwasser-Beprobung (Wasserqualität)."},
    "leitungsende":    {"label": "Leitungsende",                 "icon": "fa-stop",              "color": "#343a40", "desc": "Blindes Leitungsende/Endkappe — Spülpunkt, Stagnationsgefahr."},
    "materialwechsel": {"label": "Material-/Dimensionswechsel",  "icon": "fa-exchange-alt",      "color": "#0c8599", "desc": "Punkt, an dem Material oder Dimension (DN) der Leitung wechselt."},
    "sonstiges":       {"label": "Sonstiges",                    "icon": "fa-map-marker-alt",    "color": "#868e96", "desc": "Sonstige Anlage ohne eigene Kategorie."},
}

# Linien-Typen: key -> {label, color (Hex), desc (Tooltip)}
LINE_TYPES = {
    "versorgungsleitung":   {"label": "Versorgungsleitung",          "color": "#1971c2", "desc": "Verteilleitung in der Straße — hier hängen die Hausanschlüsse direkt dran (meist kleinerer DN)."},
    "hauptleitung":         {"label": "Hauptleitung",                "color": "#0b3d91", "desc": "Netz-Rückgrat — trägt den Hauptdurchfluss zwischen Netzbereichen (größerer DN, selten direkte Hausanschlüsse)."},
    "zubringer":            {"label": "Zubringer-/Transportleitung", "color": "#0ca678", "desc": "Bringt Wasser von Quelle/Behälter ins Versorgungsgebiet — keine Hausanschlüsse entlang der Strecke."},
    "hausanschlussleitung": {"label": "Hausanschlussleitung",        "color": "#2f9e44", "desc": "Stichleitung von der Versorgungsleitung zum einzelnen Gebäude/Zähler."},
    "sonstige_leitung":     {"label": "Sonstige Leitung",            "color": "#868e96", "desc": "Sonstige Leitung ohne eigene Kategorie."},
}

# Lagegenauigkeit: key -> {label, badge (Tabler-Soft), dash (SVG-Strichmuster fuer Linien)}
ACCURACIES = {
    "geschaetzt": {"label": "geschätzt", "badge": "bg-yellow-lt", "dash": "8 8"},
    "gut":        {"label": "gut",       "badge": "bg-azure-lt",  "dash": "1 6"},
    "exakt":      {"label": "exakt",     "badge": "bg-green-lt",  "dash": None},
}

MAINTENANCE_KINDS = {
    "spuelung":          "Spülung",
    "funktionspruefung": "Funktionsprüfung",
    "wartung":           "Wartung",
    "inspektion":        "Inspektion / Sichtprüfung",
    "sonstiges":         "Sonstiges",
}

MAINTENANCE_RESULTS = {
    "ok":     {"label": "in Ordnung", "badge": "bg-green-lt"},
    "mangel": {"label": "Mangel",     "badge": "bg-red-lt"},
}

# Plan-Status: key -> {label, badge}. ``aktiv`` ist der operative Plan und wird
# prominent (solid) gezeigt; Entwurf/Archiv dezent (Tabler-Soft-Variante).
# Badge-Konvention: solid + ``text-white`` bzw. ``-lt``, nie ``text-white-lt``.
PLAN_STATUSES = {
    "entwurf":     {"label": "Entwurf",     "badge": "bg-yellow-lt"},
    "aktiv":       {"label": "Aktiv",       "badge": "bg-success text-white"},
    "archiviert":  {"label": "Archiviert",  "badge": "bg-secondary-lt"},
}

# Gaengige Rohrmaterialien (Vorschlagsliste; Freitext bleibt erlaubt).
MATERIALS = [
    "PE", "PVC", "Guss (GG)", "Duktilguss (GGG)", "Stahl",
    "Eternit / AZ", "Beton", "Kupfer", "Unbekannt",
]


def feature_type_label(key):
    if key in POINT_TYPES:
        return POINT_TYPES[key]["label"]
    if key in LINE_TYPES:
        return LINE_TYPES[key]["label"]
    return key


def feature_type_color(key):
    if key in POINT_TYPES:
        return POINT_TYPES[key]["color"]
    if key in LINE_TYPES:
        return LINE_TYPES[key]["color"]
    return "#868e96"


def accuracy_label(key):
    item = ACCURACIES.get(key)
    return item["label"] if item else key


def maintenance_kind_label(key):
    return MAINTENANCE_KINDS.get(key, key)


def plan_status_label(key):
    item = PLAN_STATUSES.get(key)
    return item["label"] if item else key


def plan_status_badge(key):
    item = PLAN_STATUSES.get(key)
    return item["badge"] if item else "bg-secondary-lt"


def is_valid_type(feature_type, geometry_kind):
    if geometry_kind == "line":
        return feature_type in LINE_TYPES
    return feature_type in POINT_TYPES


def as_client_dict():
    """Serialisierbares Dict fuer das Karten-JS (``window.TECHNIK.vocab``)."""
    return {
        "pointTypes": POINT_TYPES,
        "lineTypes": LINE_TYPES,
        "accuracies": ACCURACIES,
        "materials": MATERIALS,
        "maintenanceKinds": MAINTENANCE_KINDS,
        "maintenanceResults": MAINTENANCE_RESULTS,
    }
