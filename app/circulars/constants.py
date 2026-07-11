"""Rundschreiben-Domäne: deutsche Labels, Badges und die eingebauten Vorlagen.

Englische Enum-Keys (Naming-Konvention), deutsche Anzeige-Labels. Badges folgen
der Tabler-Konvention: solide ``bg-{color} text-white`` für prominente
Notfall-Arten, dezente ``bg-{color}-lt`` sonst — nie ``text-white-lt``.

Die Vorlagen sind **Code-Konstanten** (keine DB-Tabelle, kein Vorlagen-CRUD).
Das Auswählen einer Vorlage füllt Betreff + Text vor; der Text wird pro
Rundschreiben angepasst. Auto-Platzhalter im Text sind ``{anrede}`` und
``{name}`` (je Empfänger ersetzt, siehe ``services.render_circular_text``);
eckige Klammern ``[…]`` sind manuelle Lückentexte, die der Verfasser ersetzt.
"""

from app.models import Circular, CircularDeliveryLog, CircularRecipient

KIND_LABELS = {
    Circular.KIND_BOIL_WATER: "Abkochempfehlung",
    Circular.KIND_ALL_CLEAR: "Entwarnung",
    Circular.KIND_PLANNED_OUTAGE: "Geplante Abschaltung",
    Circular.KIND_OUTAGE: "Rohrbruch / Störung",
    Circular.KIND_GENERAL: "Rundschreiben",
}

# Prominente Notfall-Arten solide (rot/grün), Rest dezent.
KIND_BADGES = {
    Circular.KIND_BOIL_WATER: "bg-red text-white",
    Circular.KIND_ALL_CLEAR: "bg-green text-white",
    Circular.KIND_PLANNED_OUTAGE: "bg-orange-lt",
    Circular.KIND_OUTAGE: "bg-red text-white",
    Circular.KIND_GENERAL: "bg-blue-lt",
}

KIND_ICONS = {
    Circular.KIND_BOIL_WATER: "fa-exclamation-triangle",
    Circular.KIND_ALL_CLEAR: "fa-check-circle",
    Circular.KIND_PLANNED_OUTAGE: "fa-wrench",
    Circular.KIND_OUTAGE: "fa-bolt",
    Circular.KIND_GENERAL: "fa-bullhorn",
}

STATUS_LABELS = {
    Circular.STATUS_DRAFT: "Entwurf",
    Circular.STATUS_SENT: "Versendet",
}
STATUS_BADGES = {
    Circular.STATUS_DRAFT: "bg-secondary-lt",
    Circular.STATUS_SENT: "bg-success-lt",
}

DELIVERY_METHOD_LABELS = {
    CircularRecipient.METHOD_EMAIL: "E-Mail",
    CircularRecipient.METHOD_POST: "Post",
    CircularRecipient.METHOD_NONE: "Kein Versand",
}

DELIVERY_ACTION_LABELS = {
    CircularDeliveryLog.ACTION_SENT: "Versendet",
    CircularDeliveryLog.ACTION_RESENT: "Erneut versendet",
    CircularDeliveryLog.ACTION_PRINTED: "Gedruckt",
    CircularDeliveryLog.ACTION_TEST: "Testversand",
}


# ── Eingebaute Vorlagen ──────────────────────────────────────────────────────
# Jede Vorlage: key, label, kind, subject, body. ``[Anlass der Abkochempfehlung]``
# wird beim Anlegen aus einem Wasserproben-Alarm automatisch durch den konkreten
# Befundtext ersetzt (siehe routes.py); sonst ist es ein manueller Lückentext.

_GREETING = "{anrede},"

BOIL_WATER_BODY = (
    _GREETING + "\n\n"
    "aufgrund einer festgestellten Verunreinigung des Trinkwassers geben wir für "
    "das von uns versorgte Gebiet eine ABKOCHEMPFEHLUNG aus.\n\n"
    "[Anlass der Abkochempfehlung]\n\n"
    "Bitte kochen Sie das Leitungswasser vor dem Verbrauch mindestens 3 Minuten "
    "sprudelnd ab und lassen Sie es anschließend abgedeckt abkühlen. Dies gilt "
    "insbesondere für:\n\n"
    "- Trinken sowie die Zubereitung von Speisen und Getränken\n"
    "- Zähneputzen\n"
    "- die Herstellung von Eiswürfeln\n"
    "- das Waschen von Obst, Salat und Gemüse, das roh verzehrt wird\n"
    "- die Zubereitung von Säuglingsnahrung\n\n"
    "Betreiben Sie Kaffeemaschinen, Wasserspender und ähnliche Geräte in dieser "
    "Zeit nicht mit Leitungswasser. Für Körperpflege, Duschen, Wäschewaschen und "
    "die WC-Spülung kann das Wasser weiterhin uneingeschränkt verwendet werden.\n\n"
    "Die Abkochempfehlung gilt bis auf Widerruf. Sobald die Laborbefunde wieder "
    "einwandfrei sind, informieren wir Sie umgehend über die Aufhebung.\n\n"
    "Für Rückfragen erreichen Sie uns unter [Telefon / E-Mail].\n\n"
    "Diese Information erfolgt zum Schutz Ihrer Gesundheit "
    "(Art. 6 Abs. 1 lit. d DSGVO).\n\n"
    "Mit freundlichen Grüßen"
)

ALL_CLEAR_BODY = (
    _GREETING + "\n\n"
    "die am [Datum] ausgesprochene Abkochempfehlung heben wir hiermit auf. Die "
    "aktuellen Laborbefunde bestätigen, dass das Trinkwasser wieder einwandfrei "
    "und ohne Einschränkung genießbar ist.\n\n"
    "Wir empfehlen Ihnen, vor der ersten Nutzung sämtliche Zapfstellen sowie "
    "angeschlossene Geräte (z. B. Kaffeemaschine, Boiler) kurz mit frischem "
    "Wasser durchzuspülen.\n\n"
    "Wir danken Ihnen für Ihr Verständnis und Ihre Mithilfe während dieser Zeit.\n\n"
    "Mit freundlichen Grüßen"
)

PLANNED_OUTAGE_BODY = (
    _GREETING + "\n\n"
    "wegen [Grund – z. B. Reparaturarbeiten / Erweiterung des Leitungsnetzes] "
    "wird die Wasserversorgung in folgendem Bereich vorübergehend unterbrochen:\n\n"
    "Betroffener Bereich: [Straße / Ortsteil]\n"
    "Zeitraum: am [Datum] von [Uhrzeit] bis voraussichtlich [Uhrzeit]\n\n"
    "Wir bitten Sie, für diese Zeit einen ausreichenden Wasservorrat "
    "bereitzuhalten.\n\n"
    "Nach Wiederinbetriebnahme kann es kurzzeitig zu Druckschwankungen oder "
    "einer leichten Trübung (Braunfärbung) des Wassers kommen. Lassen Sie das "
    "Wasser in diesem Fall einige Minuten laufen, bis es wieder klar ist.\n\n"
    "Wir bemühen uns, die Unterbrechung so kurz wie möglich zu halten, und "
    "danken für Ihr Verständnis.\n\n"
    "Mit freundlichen Grüßen"
)

OUTAGE_BODY = (
    _GREETING + "\n\n"
    "aufgrund [Grund – z. B. eines Rohrbruchs] ist die Wasserversorgung in "
    "folgendem Bereich derzeit unterbrochen bzw. eingeschränkt:\n\n"
    "Betroffener Bereich: [Straße / Ortsteil]\n"
    "Störung seit: [Datum / Uhrzeit]\n"
    "Voraussichtliche Behebung: [Datum / Uhrzeit]\n\n"
    "Unsere Mitarbeiter sind bereits mit der Behebung befasst. Bitte halten Sie "
    "für die Dauer der Störung einen Wasservorrat bereit.\n\n"
    "Nach Wiederinbetriebnahme kann das Wasser kurzzeitig getrübt sein — lassen "
    "Sie es in diesem Fall einige Minuten laufen, bis es wieder klar ist.\n\n"
    "Bei dringenden Fragen erreichen Sie uns unter [Notfall-Telefon].\n\n"
    "Wir danken für Ihr Verständnis.\n\n"
    "Mit freundlichen Grüßen"
)

GENERAL_BODY = (
    _GREETING + "\n\n"
    "[Ihr Text]\n\n"
    "Mit freundlichen Grüßen"
)

BUILTIN_TEMPLATES = [
    {
        "key": "boil_water",
        "label": "Abkochempfehlung",
        "kind": Circular.KIND_BOIL_WATER,
        "subject": "Wichtige Mitteilung: Abkochempfehlung für Ihr Trinkwasser",
        "body": BOIL_WATER_BODY,
    },
    {
        "key": "all_clear",
        "label": "Entwarnung (Abkochempfehlung aufgehoben)",
        "kind": Circular.KIND_ALL_CLEAR,
        "subject": "Entwarnung: Ihr Trinkwasser ist wieder einwandfrei",
        "body": ALL_CLEAR_BODY,
    },
    {
        "key": "planned_outage",
        "label": "Geplante Abschaltung / Reparatur",
        "kind": Circular.KIND_PLANNED_OUTAGE,
        "subject": "Ankündigung: geplante Unterbrechung der Wasserversorgung",
        "body": PLANNED_OUTAGE_BODY,
    },
    {
        "key": "outage",
        "label": "Rohrbruch / akute Störung",
        "kind": Circular.KIND_OUTAGE,
        "subject": "Störung der Wasserversorgung",
        "body": OUTAGE_BODY,
    },
    {
        "key": "general",
        "label": "Freies Rundschreiben",
        "kind": Circular.KIND_GENERAL,
        "subject": "",
        "body": GENERAL_BODY,
    },
]

# key -> template (für Prefill-Lookups).
TEMPLATES_BY_KEY = {t["key"]: t for t in BUILTIN_TEMPLATES}


def template_for_kind(kind):
    """Erste eingebaute Vorlage zu einer Art (für CTA-Prefill)."""
    for t in BUILTIN_TEMPLATES:
        if t["kind"] == kind:
            return t
    return TEMPLATES_BY_KEY["general"]
