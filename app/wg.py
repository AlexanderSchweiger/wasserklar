"""Wassergenossenschafts-Domaene (Mandant-Typ-spezifisch).

Buendelt die Konstanten und Regeln, die nur im Mandant-Typ
*Wassergenossenschaft* greifen: Status-Lebenszyklus eines Kontakts,
Vorstands-/Pruef-Funktionen und ihre Plausibilitaets-Warnungen.

Enum-Keys sind englisch (Naming-Konvention), die Anzeige-Labels deutsch.
Die Regeln sind bewusst *Warnungen* (kein Hard-Block) — ``function_warnings``
liefert eine Liste deutscher Hinweistexte, das Speichern bleibt erlaubt.
"""

# Mandant-Typ (AppSetting-Key ``org.type``)
ORG_COOPERATIVE = "cooperative"   # Wassergenossenschaft (Default)
ORG_UTILITY = "utility"           # Versorger (Gemeinde/Stadt/GmbH/Dorfgemeinschaft)
ORG_TYPES = (ORG_COOPERATIVE, ORG_UTILITY)

ORG_TYPE_LABELS = {
    ORG_COOPERATIVE: "Wassergenossenschaft",
    ORG_UTILITY: "Versorger",
}

# ---------------------------------------------------------------------------
# Mitglieds-Status
# ---------------------------------------------------------------------------
STATUS_PROSPECT = "prospect"      # Interessent
STATUS_MEMBER = "member"          # Mitglied
STATUS_RESIGNED = "resigned"      # Ausgeschieden (Vererbt/Tod/Verkauf)

# Reihenfolge = Reihenfolge im <select>
STATUS_LABELS = {
    STATUS_PROSPECT: "Interessent",
    STATUS_MEMBER: "Mitglied",
    STATUS_RESIGNED: "Ausgeschieden",
}

# Dezente Tabler-Soft-Badges (Konvention: bg-{color}-lt, nie text-white-lt).
STATUS_BADGE = {
    STATUS_PROSPECT: "bg-yellow-lt",
    STATUS_MEMBER: "bg-success-lt",
    STATUS_RESIGNED: "bg-secondary-lt",
}


def status_label(key):
    return STATUS_LABELS.get(key, STATUS_LABELS[STATUS_PROSPECT])


# Synonyme aus Import-Dateien (Label-/Key-Varianten) → STATUS_*-Key.
_STATUS_SYNONYMS = {
    "interessent": STATUS_PROSPECT,
    "anwärter": STATUS_PROSPECT,
    "anwaerter": STATUS_PROSPECT,
    "mitglied": STATUS_MEMBER,
    "aktiv": STATUS_MEMBER,
    "member": STATUS_MEMBER,
    "ausgeschieden": STATUS_RESIGNED,
    "ausgetreten": STATUS_RESIGNED,
    "inaktiv": STATUS_RESIGNED,
    "ehemalig": STATUS_RESIGNED,
}


def parse_status(raw):
    """Parst einen Status-Rohwert aus einem Import (Key, deutsches Label oder
    gaengiges Synonym, beliebige Gross-/Kleinschreibung) zu einem STATUS_*-Key.
    Gibt ``None`` zurueck bei leerem oder unbekanntem Wert."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in STATUS_LABELS:                       # direkter Key
        return s
    for key, label in STATUS_LABELS.items():     # deutsches Label
        if s == label.lower():
            return key
    return _STATUS_SYNONYMS.get(s)


# ---------------------------------------------------------------------------
# Funktionen (Vorstand + Rechnungspruefer)
# ---------------------------------------------------------------------------
FUNC_CHAIRMAN = "chairman"                # Obmann
FUNC_DEPUTY_CHAIRMAN = "deputy_chairman"  # Obmann-Stellvertreter
FUNC_COMMITTEE = "committee"              # Ausschuss-Mitglied
FUNC_SECRETARY = "secretary"             # Schriftfuehrer
FUNC_TREASURER = "treasurer"             # Kassier
FUNC_WATER_WARDEN = "water_warden"       # Wasserwart
FUNC_AUDITOR = "auditor"                 # Rechnungspruefer

# Reihenfolge = Anzeige-Reihenfolge im Formular
FUNCTION_LABELS = {
    FUNC_CHAIRMAN: "Obmann",
    FUNC_DEPUTY_CHAIRMAN: "Obmann-Stellvertreter",
    FUNC_COMMITTEE: "Ausschuss-Mitglied",
    FUNC_SECRETARY: "Schriftführer",
    FUNC_TREASURER: "Kassier",
    FUNC_WATER_WARDEN: "Wasserwart",
    FUNC_AUDITOR: "Rechnungsprüfer",
}

# Vorstandsfunktionen (alles ausser Rechnungspruefer). Ein Rechnungspruefer
# darf keine davon haben, weil er nicht im Vorstand sein darf.
BOARD_FUNCTIONS = {
    FUNC_CHAIRMAN, FUNC_DEPUTY_CHAIRMAN, FUNC_COMMITTEE,
    FUNC_SECRETARY, FUNC_TREASURER, FUNC_WATER_WARDEN,
}

# Funktionen, die ein Mitglied voraussetzen (sonst Warnung).
MEMBER_REQUIRED = {FUNC_CHAIRMAN, FUNC_DEPUTY_CHAIRMAN}


def function_label(key):
    return FUNCTION_LABELS.get(key, key)


def function_keys_ordered(keys):
    """Sortiert eine Menge Funktions-Keys in die kanonische Anzeige-Reihenfolge."""
    order = list(FUNCTION_LABELS.keys())
    return [k for k in order if k in set(keys)]


# ---------------------------------------------------------------------------
# Regeln
# ---------------------------------------------------------------------------
def suggested_status(customer):
    """Hybrider Status-Vorschlag: 'member', sobald der Kontakt eine
    Liegenschaft mit Anteilen besitzt, sonst 'prospect'. 'resigned' wird nie
    vorgeschlagen (rein manuell)."""
    return STATUS_MEMBER if customer.has_paid_shares() else STATUS_PROSPECT


def function_warnings(status, funcs):
    """Liefert deutsche Warntexte fuer unplausible Kombinationen — blockiert
    nichts (Speichern bleibt erlaubt).

    ``status``: einer der STATUS_*-Keys; ``funcs``: Iterable von Funktions-Keys.
    """
    funcs = set(funcs)
    warnings = []
    bad_member = funcs & MEMBER_REQUIRED
    if status != STATUS_MEMBER and bad_member:
        names = ", ".join(function_label(f) for f in function_keys_ordered(bad_member))
        warnings.append(
            f"{names} sollte nur ein Mitglied sein — aktueller Status: "
            f"{status_label(status)}."
        )
    if FUNC_AUDITOR in funcs and (funcs & BOARD_FUNCTIONS):
        warnings.append(
            "Rechnungsprüfer darf keine Vorstandsfunktion haben "
            "(er darf nicht im Vorstand sein)."
        )
    return warnings
