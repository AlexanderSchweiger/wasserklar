"""Service-Schicht des Notiz-Moduls: Entity-Registry, Farb-Allowlist und die
(dialekt-portablen, N+1-freien) Lade-Helfer.

Polymorphie-Konvention: ``entity_type`` ist der String-Diskriminator,
``entity_id`` der nullable Fremdschlüssel-Wert (NULL bei Tenant-Scope). Es gibt
**keinen** DB-FK — Existenz/Integrität prüft die App (``entity_exists``).
"""
from app.extensions import db
from app.models import Note, Customer, Property, Invoice, Booking


# Erlaubte Notizzettel-Farben (Tabler-Farbnamen). Gerendert als Soft-Variante
# ``bg-<color>-lt`` (heller Hintergrund + dunkler Text → Badge-/Kontrast-Konvention).
NOTE_COLORS = {
    "yellow": "Gelb",
    "lime": "Grün",
    "azure": "Blau",
    "pink": "Pink",
    "orange": "Orange",
    "red": "Rot",
}
DEFAULT_COLOR = "yellow"


# Registry der pinnbaren Entitäten. ``endpoint``/``id_arg`` = Detailseiten-Route
# (None ⇒ keine eigene Detailseite, z.B. Buchung → nur Zeilen-Pin). ``label`` ist
# der Default-Anzeigename; im WG-Modus wird ``customer`` im Template zu „Mitglied".
ENTITY_TYPES = {
    "customer": {"label": "Kontakt", "model": Customer,
                 "endpoint": "customers.detail", "id_arg": "customer_id"},
    "property": {"label": "Liegenschaft", "model": Property,
                 "endpoint": "properties.detail", "id_arg": "property_id"},
    "invoice": {"label": "Rechnung", "model": Invoice,
                "endpoint": "invoices.detail", "id_arg": "invoice_id"},
    "booking": {"label": "Buchung", "model": Booking,
                "endpoint": None, "id_arg": None},
}

SCOPE_LABELS = {Note.SCOPE_TENANT: "Mandant"}
SCOPE_LABELS.update({k: v["label"] for k, v in ENTITY_TYPES.items()})


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------

def is_valid_scope(entity_type):
    """True für 'tenant' oder einen registrierten Entitäts-Typ."""
    return entity_type == Note.SCOPE_TENANT or entity_type in ENTITY_TYPES


def normalize_color(color):
    return color if color in NOTE_COLORS else DEFAULT_COLOR


def entity_exists(entity_type, entity_id):
    """Prüft, ob die Zielentität existiert (App-seitige referenzielle Integrität,
    da es keinen DB-FK auf ``entity_id`` gibt). Tenant-Scope ist immer gültig."""
    if entity_type == Note.SCOPE_TENANT:
        return True
    spec = ENTITY_TYPES.get(entity_type)
    if not spec or entity_id is None:
        return False
    return db.session.get(spec["model"], entity_id) is not None


# ---------------------------------------------------------------------------
# Laden (pinned = prominent sichtbar)
# ---------------------------------------------------------------------------

def notes_for(entity_type, entity_id=None, pinned_only=True):
    """Notizen einer Ebene, neueste zuerst. Tenant-Scope ⇒ entity_id IS NULL."""
    if not is_valid_scope(entity_type):
        return []
    q = Note.query.filter(Note.entity_type == entity_type)
    if entity_type == Note.SCOPE_TENANT:
        q = q.filter(Note.entity_id.is_(None))
    else:
        if entity_id is None:
            return []
        q = q.filter(Note.entity_id == entity_id)
    if pinned_only:
        q = q.filter(Note.pinned.is_(True))
    return q.order_by(Note.created_at.desc()).all()


def notes_by_entity_for(entity_type, ids, pinned_only=True):
    """{entity_id: [Note, ...]} für eine ganze Listen-Seite in EINER Query
    (kein N+1). Leere ID-Liste ⇒ {} (vermeidet ``IN ()``-Sonderfall)."""
    clean = [int(i) for i in ids if i is not None]
    if not clean or entity_type not in ENTITY_TYPES:
        return {}
    q = Note.query.filter(Note.entity_type == entity_type, Note.entity_id.in_(clean))
    if pinned_only:
        q = q.filter(Note.pinned.is_(True))
    out = {}
    for n in q.order_by(Note.created_at.desc()).all():
        out.setdefault(n.entity_id, []).append(n)
    return out


def tenant_notes(pinned_only=True):
    return notes_for(Note.SCOPE_TENANT, None, pinned_only=pinned_only)


# ---------------------------------------------------------------------------
# Überblick / Auflösung
# ---------------------------------------------------------------------------

def all_notes(scope=None):
    """Alle Notizen (inkl. unpinned) für die Übersichtsseite, optional auf einen
    Scope gefiltert. Gepinnte zuerst, dann nach letzter Änderung."""
    q = Note.query
    if scope and is_valid_scope(scope):
        q = q.filter(Note.entity_type == scope)
    return q.order_by(Note.pinned.desc(), Note.updated_at.desc()).all()


def entity_display(note):
    """{'label', 'name', 'endpoint', 'id_arg', 'entity_id'} zur Anzeige/Verlinkung
    der Zielentität einer Notiz in der Übersicht. Für Tenant-Scope: name=None."""
    if note.is_tenant:
        return {"label": SCOPE_LABELS[Note.SCOPE_TENANT], "name": None,
                "endpoint": None, "id_arg": None, "entity_id": None}
    spec = ENTITY_TYPES.get(note.entity_type, {})
    obj = db.session.get(spec["model"], note.entity_id) if spec.get("model") else None
    name = None
    if obj is not None:
        # Customer/Property/Invoice/Booking haben unterschiedliche Anzeigefelder.
        name = (getattr(obj, "name", None)
                or getattr(obj, "object_number", None)
                or getattr(obj, "invoice_number", None)
                or f"#{note.entity_id}")
    return {
        "label": spec.get("label", note.entity_type),
        "name": name,
        "endpoint": spec.get("endpoint"),
        "id_arg": spec.get("id_arg"),
        "entity_id": note.entity_id,
    }
