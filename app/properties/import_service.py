"""Objekte-Import-Service für den 3-stufigen Wizard.

Stellt ``PropertyImportConfig``, ``build_preview_rows``, ``apply_edits`` und
``commit`` bereit.  Kein Flask-Request-Zugriff — alle Funktionen sind
request-frei und werden von den Routen in ``properties/routes.py`` aufgerufen.

Konventionen:
- UI-Strings sind deutsch; Identifier und Kommentare englisch.
- ORM (``db.session.add``) statt Roh-SQL, damit Python-Defaults greifen.
- Dialekt-portabel (SQLite/MariaDB/Postgres): nur ``filter_by``-Queries und
  ``db.func.lower`` für Adressvergleiche.
- ``Property.object_type`` ist NOT NULL → beim Neuanlegen IMMER setzen
  (Default "Haus"), beim Update nie auf leeren Wert setzen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.imports.common import (
    PreviewRow,
    ImportStats,
    ROW_NEW,
    ROW_UPDATE,
    ROW_EXISTS,
    ROW_ERROR,
    _cell,
    parse_row_edits,
    suggest_column,
)

# ---------------------------------------------------------------------------
# Column hints (Heuristik für Auto-Mapping)
# ---------------------------------------------------------------------------

HINTS: dict[str, list[str]] = {
    "object_number": [
        "objekt-nr.", "objektnr", "objektnummer", "objekt nr", "objekt-nr",
    ],
    "object_type": [
        "typ", "objekttyp", "art",
    ],
    "strasse": [
        "strasse", "straße", "adresse",
    ],
    "hausnummer": [
        "hausnummer", "hausnr", "haus-nr",
    ],
    "plz": [
        "plz", "postleitzahl",
    ],
    "ort": [
        "ort", "stadt", "gemeinde",
    ],
    "land": [
        "land",
    ],
    "notes": [
        "kommentar", "bemerkung", "notiz", "info", "anmerkung",
    ],
    "owner_customer_number": [
        "besitzer", "eigentümer", "eigentuemer", "kunden-nr.", "besitzer kunden-nr",
    ],
}


# ---------------------------------------------------------------------------
# PropertyImportConfig
# ---------------------------------------------------------------------------

@dataclass
class PropertyImportConfig:
    """Holds all user-chosen column mappings and the duplicate handling mode."""

    col_object_number: str = ""
    col_object_type: str = ""
    col_strasse: str = ""
    col_hausnummer: str = ""
    col_plz: str = ""
    col_ort: str = ""
    col_land: str = ""
    col_notes: str = ""
    col_owner_customer_number: str = ""  # optional: "Besitzer (Kunden-Nr.)"
    duplicate_mode: str = "skip"  # Default: Überspringen

    # --- serialisation helpers -----------------------------------------------

    def to_dict(self) -> dict:
        return {
            "col_object_number": self.col_object_number,
            "col_object_type": self.col_object_type,
            "col_strasse": self.col_strasse,
            "col_hausnummer": self.col_hausnummer,
            "col_plz": self.col_plz,
            "col_ort": self.col_ort,
            "col_land": self.col_land,
            "col_notes": self.col_notes,
            "col_owner_customer_number": self.col_owner_customer_number,
            "duplicate_mode": self.duplicate_mode,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "PropertyImportConfig":
        """Reconstruct from a dict (e.g. from the session).  Robust against missing keys."""
        if not d:
            return cls()
        dup = d.get("duplicate_mode", "skip")
        if dup not in ("update", "skip"):
            dup = "skip"
        return cls(
            col_object_number=d.get("col_object_number", ""),
            col_object_type=d.get("col_object_type", ""),
            col_strasse=d.get("col_strasse", ""),
            col_hausnummer=d.get("col_hausnummer", ""),
            col_plz=d.get("col_plz", ""),
            col_ort=d.get("col_ort", ""),
            col_land=d.get("col_land", ""),
            col_notes=d.get("col_notes", ""),
            col_owner_customer_number=d.get("col_owner_customer_number", ""),
            duplicate_mode=dup,
        )

    @classmethod
    def from_form(cls, form) -> "PropertyImportConfig":
        """Build from a Werkzeug form (request.form)."""
        dup = form.get("duplicate_mode", "skip")
        if dup not in ("update", "skip"):
            dup = "skip"
        return cls(
            col_object_number=form.get("col_object_number", ""),
            col_object_type=form.get("col_object_type", ""),
            col_strasse=form.get("col_strasse", ""),
            col_hausnummer=form.get("col_hausnummer", ""),
            col_plz=form.get("col_plz", ""),
            col_ort=form.get("col_ort", ""),
            col_land=form.get("col_land", ""),
            col_notes=form.get("col_notes", ""),
            col_owner_customer_number=form.get("col_owner_customer_number", ""),
            duplicate_mode=dup,
        )


# ---------------------------------------------------------------------------
# Auto-suggest
# ---------------------------------------------------------------------------

def suggest_config(columns: list[str]) -> PropertyImportConfig:
    """Auto-suggest column mappings for the given list of column names."""
    return PropertyImportConfig(
        col_object_number=suggest_column(columns, HINTS["object_number"]),
        col_object_type=suggest_column(columns, HINTS["object_type"]),
        col_strasse=suggest_column(columns, HINTS["strasse"]),
        col_hausnummer=suggest_column(columns, HINTS["hausnummer"]),
        col_plz=suggest_column(columns, HINTS["plz"]),
        col_ort=suggest_column(columns, HINTS["ort"]),
        col_land=suggest_column(columns, HINTS["land"]),
        col_notes=suggest_column(columns, HINTS["notes"]),
        col_owner_customer_number=suggest_column(columns, HINTS["owner_customer_number"]),
    )


# ---------------------------------------------------------------------------
# Object type resolver
# ---------------------------------------------------------------------------

def _resolve_object_type(raw: str) -> str:
    """Map a raw cell value to a valid Property.TYPES entry.

    - Valid type (case-insensitive match against Property.TYPES) → exact value
    - Non-empty but unrecognised (e.g. "Stall", "Scheune") → "Sonstiges"
    - Empty or whitespace-only → "Haus" (default)
    """
    from app.models import Property
    if not raw or not raw.strip():
        return "Haus"
    raw_lower = raw.strip().lower()
    for t in Property.TYPES:
        if t.lower() == raw_lower:
            return t
    # Non-empty but not in TYPES → Sonstiges
    return "Sonstiges"


# ---------------------------------------------------------------------------
# Preview row builder
# ---------------------------------------------------------------------------

def _address_key(strasse: str, hausnummer: str, plz: str, ort: str) -> tuple:
    """Return a normalised address tuple for duplicate detection."""
    return (
        strasse.strip().lower(),
        hausnummer.strip().lower(),
        plz.strip().lower(),
        ort.strip().lower(),
    )


def _find_property_by_number(object_number: str):
    """Look up a property by object_number. Returns None if not found."""
    from app.models import Property
    return Property.query.filter_by(object_number=object_number).first()


def _find_property_by_address(strasse: str, hausnummer: str, plz: str, ort: str):
    """Look for a property with the same address (case-insensitive)."""
    from app.models import Property
    from app.extensions import db
    if not (strasse or hausnummer or plz or ort):
        return None
    return (
        Property.query
        .filter(
            db.func.lower(Property.strasse) == strasse.strip().lower(),
            db.func.lower(Property.hausnummer) == hausnummer.strip().lower(),
            db.func.lower(Property.plz) == plz.strip().lower(),
            db.func.lower(Property.ort) == ort.strip().lower(),
        )
        .first()
    )


def build_preview_rows(df, cfg: PropertyImportConfig) -> list[PreviewRow]:
    """Build a list of PreviewRow from the DataFrame and config."""
    from app.imports.relations import OwnerConflictTracker
    from app.models import Customer, PropertyOwnership

    tracker = OwnerConflictTracker()
    rows: list[PreviewRow] = []

    for idx, raw_row in enumerate(df.to_dict(orient="records")):
        # --- read cells -------------------------------------------------------
        object_number = _cell(raw_row, cfg.col_object_number)
        object_type_raw = _cell(raw_row, cfg.col_object_type)
        strasse = _cell(raw_row, cfg.col_strasse)
        hausnummer = _cell(raw_row, cfg.col_hausnummer)
        plz = _cell(raw_row, cfg.col_plz)
        ort = _cell(raw_row, cfg.col_ort)
        land = _cell(raw_row, cfg.col_land)
        notes = _cell(raw_row, cfg.col_notes)
        owner_cnum_raw = _cell(raw_row, cfg.col_owner_customer_number)

        # --- empty row guard --------------------------------------------------
        has_any = any([object_number, object_type_raw, strasse, hausnummer,
                       plz, ort, land, notes, owner_cnum_raw])
        if not has_any:
            rows.append(PreviewRow(
                idx=idx,
                status=ROW_ERROR,
                message="Leere Zeile",
                raw=raw_row,
            ))
            continue

        # --- match against DB -------------------------------------------------
        existing = None
        if object_number:
            existing = _find_property_by_number(object_number)

        if existing:
            status = ROW_UPDATE if cfg.duplicate_mode == "update" else ROW_EXISTS
        else:
            status = ROW_NEW

        warnings: list[str] = []

        # --- address duplicate warning (ROW_NEW only, no merge) ---------------
        if status == ROW_NEW and (strasse or hausnummer or plz or ort):
            addr_dup = _find_property_by_address(strasse, hausnummer, plz, ort)
            if addr_dup:
                warnings.append(
                    f"Mögliches Duplikat: gleiche Adresse wie bestehendes Objekt "
                    f"{addr_dup.label()}"
                )

        # --- owner integrity check -------------------------------------------
        if cfg.col_owner_customer_number and owner_cnum_raw:
            customer = Customer.query.filter_by(
                customer_number=owner_cnum_raw
            ).first()
            if customer is None:
                # Try numeric lookup in case owner_cnum_raw is a number string
                try:
                    cnum_int = int(float(owner_cnum_raw))
                    customer = Customer.query.filter_by(
                        customer_number=cnum_int
                    ).first()
                except (ValueError, TypeError):
                    customer = None

            if customer is None:
                warnings.append(
                    f"Besitzer Kunden-Nr. {owner_cnum_raw} nicht gefunden — "
                    "keine Zuordnung"
                )
            else:
                # Determine the property key for the tracker
                property_key = object_number if object_number else f"row{idx}"

                # Existing owner keys from DB (for update rows)
                existing_owner_keys: list = []
                if existing:
                    existing_owner_keys = [
                        str(o.customer_id)
                        for o in PropertyOwnership.query.filter_by(
                            property_id=existing.id, valid_to=None
                        ).all()
                    ]

                w = tracker.check_and_register(
                    property_key,
                    str(customer.id),
                    existing_owner_keys,
                )
                if w:
                    warnings.append(w)

        fields = {
            "object_number": object_number,
            # Store the raw value; resolution to Haus/Garten/Sonstiges happens
            # at commit time.  An empty raw value signals "use default/keep
            # existing" and must be preserved so commit can distinguish it.
            "object_type": object_type_raw,
            "strasse": strasse,
            "hausnummer": hausnummer,
            "plz": plz,
            "ort": ort,
            "land": land,
            "notes": notes,
            "owner_customer_number": owner_cnum_raw,
        }

        rows.append(PreviewRow(
            idx=idx,
            status=status,
            fields=fields,
            warnings=warnings,
            raw=raw_row,
        ))

    return rows


# ---------------------------------------------------------------------------
# Apply edits
# ---------------------------------------------------------------------------

def apply_edits(form, rows: list[PreviewRow]) -> list[PreviewRow]:
    """Merge user edits from the preview form back into the row list.

    Re-resolves the property match when ``object_number`` was edited so the
    status stays accurate.
    Owner warnings are not re-calculated here — they are refreshed on the
    next build_preview_rows/Refresh call.
    """
    edits = parse_row_edits(form)

    for row in rows:
        row_edits = edits.get(row.idx)
        if not row_edits:
            continue

        # skip checkbox
        skip_val = row_edits.get("skip", "")
        row.skip = skip_val.lower() in ("on", "1", "true")

        # overwrite editable fields
        for field_name in (
            "object_number", "object_type",
            "strasse", "hausnummer", "plz", "ort", "land",
            "notes", "owner_customer_number",
        ):
            if field_name in row_edits:
                row.fields[field_name] = row_edits[field_name]

        # re-resolve status if object_number was touched
        if "object_number" in row_edits and row.status != ROW_ERROR:
            object_number = row.fields.get("object_number", "")
            if object_number:
                existing = _find_property_by_number(object_number)
                if existing:
                    if row.status == ROW_UPDATE:
                        row.status = ROW_UPDATE
                    else:
                        row.status = ROW_EXISTS
                else:
                    row.status = ROW_NEW
            else:
                row.status = ROW_NEW

    return rows


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit(rows: list[PreviewRow], cfg: PropertyImportConfig) -> ImportStats:
    """Write the (non-skipped, non-error) rows to the database.

    Uses ``db.session.begin_nested()`` savepoints so a single bad row does
    not abort the entire import.
    """
    from datetime import date

    from app.extensions import db
    from app.models import Customer, Property, PropertyOwnership
    from app.imports.relations import OwnerConflictTracker

    stats = ImportStats()
    tracker = OwnerConflictTracker()

    for row in rows:
        # skip checkbox
        if row.skip:
            stats.skipped += 1
            continue
        # error rows are always skipped
        if row.status == ROW_ERROR:
            stats.skipped_error += 1
            continue

        sp = db.session.begin_nested()
        try:
            object_number = row.fields.get("object_number", "").strip() or None
            object_type_raw = row.fields.get("object_type", "")
            strasse = row.fields.get("strasse", "").strip() or None
            hausnummer = row.fields.get("hausnummer", "").strip() or None
            plz = row.fields.get("plz", "").strip() or None
            ort = row.fields.get("ort", "").strip() or None
            land = row.fields.get("land", "").strip() or "Österreich"
            notes = row.fields.get("notes", "").strip() or None
            owner_cnum_raw = row.fields.get("owner_customer_number", "").strip()

            # Re-resolve property in the DB
            existing = None
            if object_number:
                existing = Property.query.filter_by(
                    object_number=object_number
                ).first()

            if existing and cfg.duplicate_mode == "skip":
                stats.skipped += 1
                sp.commit()
                continue

            row_had_warning = bool(row.warnings)

            if existing and cfg.duplicate_mode == "update":
                # Update all mapped fields.  object_type: never set to empty;
                # use _resolve_object_type only when col is mapped.
                if cfg.col_strasse:
                    existing.strasse = strasse
                if cfg.col_hausnummer:
                    existing.hausnummer = hausnummer
                if cfg.col_plz:
                    existing.plz = plz
                if cfg.col_ort:
                    existing.ort = ort
                if cfg.col_land:
                    existing.land = land or "Österreich"
                if cfg.col_notes:
                    existing.notes = notes
                if cfg.col_object_type and object_type_raw and object_type_raw.strip():
                    # Only update object_type when column is mapped AND cell has a non-empty value.
                    # Never set object_type to empty/NULL — it is NOT NULL.
                    existing.object_type = _resolve_object_type(object_type_raw)
                # Never set object_type to None / empty
                prop = existing
                stats.updated += 1

            else:
                # New property
                object_type = _resolve_object_type(object_type_raw)
                prop = Property(
                    object_number=object_number,
                    object_type=object_type,
                    strasse=strasse,
                    hausnummer=hausnummer,
                    plz=plz,
                    ort=ort,
                    land=land,
                    notes=notes,
                    active=True,
                )
                db.session.add(prop)
                db.session.flush()  # get prop.id
                stats.created += 1

            # --- Owner assignment -------------------------------------------
            if cfg.col_owner_customer_number and owner_cnum_raw:
                owner_customer = None
                # Try direct string match first (customer_number may be string)
                owner_customer = Customer.query.filter_by(
                    customer_number=owner_cnum_raw
                ).first()
                if owner_customer is None:
                    try:
                        cnum_int = int(float(owner_cnum_raw))
                        owner_customer = Customer.query.filter_by(
                            customer_number=cnum_int
                        ).first()
                    except (ValueError, TypeError):
                        owner_customer = None

                if owner_customer is not None:
                    # Check if this ownership already exists (active)
                    already_owned = PropertyOwnership.query.filter_by(
                        property_id=prop.id,
                        customer_id=owner_customer.id,
                        valid_to=None,
                    ).first()

                    if not already_owned:
                        # Check/register with tracker for warning counting
                        existing_owner_keys = [
                            str(o.customer_id)
                            for o in PropertyOwnership.query.filter_by(
                                property_id=prop.id, valid_to=None
                            ).all()
                        ]
                        property_key = (
                            object_number if object_number else f"prop{prop.id}"
                        )
                        w = tracker.check_and_register(
                            property_key,
                            str(owner_customer.id),
                            existing_owner_keys,
                        )
                        if w and not row_had_warning:
                            row_had_warning = True

                        db.session.add(PropertyOwnership(
                            property_id=prop.id,
                            customer_id=owner_customer.id,
                            valid_from=date.today(),
                            valid_to=None,
                        ))

            if row_had_warning:
                stats.warnings += 1

            sp.commit()

        except Exception as exc:  # noqa: BLE001
            sp.rollback()
            stats.errors.append(f"Zeile {row.idx + 2}: {exc}")

    db.session.commit()
    return stats
