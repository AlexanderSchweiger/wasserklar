"""Zähler-Stammdaten-Import-Service für den 3-stufigen Wizard.

Stellt ``MeterImportConfig``, ``build_preview_rows``, ``apply_edits`` und
``commit`` bereit.  Kein Flask-Request-Zugriff — alle Funktionen sind
request-frei und werden von den Routen in ``meters/routes.py`` aufgerufen.

KOLLISIONSFREI vom bestehenden Ablesungs-Import:
- Diese Datei: meter_import_service.py  (Zähler-Stammdaten)
- Bestehende:  import_service.py        (Ablesungen / Readings)
- Routen-Endpoints: meters.meter_master_import_*
- Session-Keys:     meter_master_import_*

Konventionen:
- UI-Strings sind deutsch; Identifier und Kommentare englisch.
- ORM (``db.session.add``) statt Roh-SQL, damit Python-Defaults greifen.
- Dialekt-portabel (SQLite/MariaDB/Postgres): nur ``filter_by``-Queries.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.imports.common import (
    PreviewRow,
    ImportStats,
    ROW_NEW,
    ROW_UPDATE,
    ROW_EXISTS,
    ROW_ERROR,
    _cell,
    parse_number,
    parse_date,
    parse_row_edits,
    suggest_column,
    format_value_de,
)


# ---------------------------------------------------------------------------
# Column hints (Heuristik für Auto-Mapping)
# ---------------------------------------------------------------------------

HINTS: dict[str, list[str]] = {
    "meter_number": [
        "zählernummer", "zahlernummer", "zähler-nr", "zaehlernummer",
        "zähler nr", "zählernr",
    ],
    "object_number": [
        "objekt-nr.", "objektnr", "objektnummer", "objekt nr",
        "objekt-nr", "objekt",
    ],
    "location": [
        "standort", "ort des zählers", "lage", "location",
    ],
    "eichjahr": [
        "eichjahr",
    ],
    "installed_from": [
        "einbaudatum", "einbau", "installiert", "installed",
    ],
    "initial_value": [
        "anfangsstand", "anfangswert", "stand bei einbau", "initial",
    ],
    "meter_type": [
        "typ", "zählertyp", "art",
    ],
    "notes": [
        "kommentar", "bemerkung", "notiz", "info", "anmerkung",
    ],
}


# ---------------------------------------------------------------------------
# MeterImportConfig
# ---------------------------------------------------------------------------

@dataclass
class MeterImportConfig:
    """Holds all user-chosen column mappings and the duplicate handling mode."""

    col_meter_number: str = ""
    col_object_number: str = ""
    col_location: str = ""
    col_eichjahr: str = ""
    col_installed_from: str = ""
    col_initial_value: str = ""
    col_meter_type: str = ""
    col_notes: str = ""
    duplicate_mode: str = "skip"  # Default: Überspringen

    # --- serialisation helpers -----------------------------------------------

    def to_dict(self) -> dict:
        return {
            "col_meter_number": self.col_meter_number,
            "col_object_number": self.col_object_number,
            "col_location": self.col_location,
            "col_eichjahr": self.col_eichjahr,
            "col_installed_from": self.col_installed_from,
            "col_initial_value": self.col_initial_value,
            "col_meter_type": self.col_meter_type,
            "col_notes": self.col_notes,
            "duplicate_mode": self.duplicate_mode,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "MeterImportConfig":
        """Reconstruct from a dict (e.g. from the session).  Robust against missing keys."""
        if not d:
            return cls()
        dup = d.get("duplicate_mode", "skip")
        if dup not in ("update", "skip"):
            dup = "skip"
        return cls(
            col_meter_number=d.get("col_meter_number", ""),
            col_object_number=d.get("col_object_number", ""),
            col_location=d.get("col_location", ""),
            col_eichjahr=d.get("col_eichjahr", ""),
            col_installed_from=d.get("col_installed_from", ""),
            col_initial_value=d.get("col_initial_value", ""),
            col_meter_type=d.get("col_meter_type", ""),
            col_notes=d.get("col_notes", ""),
            duplicate_mode=dup,
        )

    @classmethod
    def from_form(cls, form) -> "MeterImportConfig":
        """Build from a Werkzeug form (request.form)."""
        dup = form.get("duplicate_mode", "skip")
        if dup not in ("update", "skip"):
            dup = "skip"
        return cls(
            col_meter_number=form.get("col_meter_number", ""),
            col_object_number=form.get("col_object_number", ""),
            col_location=form.get("col_location", ""),
            col_eichjahr=form.get("col_eichjahr", ""),
            col_installed_from=form.get("col_installed_from", ""),
            col_initial_value=form.get("col_initial_value", ""),
            col_meter_type=form.get("col_meter_type", ""),
            col_notes=form.get("col_notes", ""),
            duplicate_mode=dup,
        )


# ---------------------------------------------------------------------------
# Auto-suggest
# ---------------------------------------------------------------------------

def suggest_config(columns: list[str]) -> MeterImportConfig:
    """Auto-suggest column mappings for the given list of column names."""
    return MeterImportConfig(
        col_meter_number=suggest_column(columns, HINTS["meter_number"]),
        col_object_number=suggest_column(columns, HINTS["object_number"]),
        col_location=suggest_column(columns, HINTS["location"]),
        col_eichjahr=suggest_column(columns, HINTS["eichjahr"]),
        col_installed_from=suggest_column(columns, HINTS["installed_from"]),
        col_initial_value=suggest_column(columns, HINTS["initial_value"]),
        col_meter_type=suggest_column(columns, HINTS["meter_type"]),
        col_notes=suggest_column(columns, HINTS["notes"]),
    )


# ---------------------------------------------------------------------------
# Meter type resolver
# ---------------------------------------------------------------------------

def _resolve_meter_type(raw: str) -> str:
    """Map a raw cell value to 'sub' or 'main' (default).

    'sub' / 'subzähler' / 'subzaehler' (case-insensitive) → 'sub'
    Everything else (including empty) → 'main'
    """
    if not raw or not raw.strip():
        return "main"
    lower = raw.strip().lower()
    if lower in ("sub", "subzähler", "subzaehler", "subzähler"):
        return "sub"
    return "main"


# ---------------------------------------------------------------------------
# Preview row builder
# ---------------------------------------------------------------------------

def build_preview_rows(df, cfg: MeterImportConfig) -> list[PreviewRow]:
    """Build a list of PreviewRow from the DataFrame and config."""
    from app.imports.relations import MeterObjectTracker
    from app.models import Property, WaterMeter

    tracker = MeterObjectTracker()
    rows: list[PreviewRow] = []

    for idx, raw_row in enumerate(df.to_dict(orient="records")):
        # --- read cells -------------------------------------------------------
        meter_number = _cell(raw_row, cfg.col_meter_number)
        object_number = _cell(raw_row, cfg.col_object_number)
        location = _cell(raw_row, cfg.col_location)
        eichjahr_raw = _cell(raw_row, cfg.col_eichjahr)
        installed_from_raw = _cell(raw_row, cfg.col_installed_from)
        initial_value_raw = _cell(raw_row, cfg.col_initial_value)
        meter_type_raw = _cell(raw_row, cfg.col_meter_type)
        notes = _cell(raw_row, cfg.col_notes)

        # --- meter_number required -------------------------------------------
        if not meter_number:
            rows.append(PreviewRow(
                idx=idx,
                status=ROW_ERROR,
                message="Zählernummer fehlt",
                raw=raw_row,
            ))
            continue

        # --- object resolution (required) ------------------------------------
        if not object_number:
            rows.append(PreviewRow(
                idx=idx,
                status=ROW_ERROR,
                message="Objekt-Nr. fehlt",
                raw=raw_row,
            ))
            continue

        prop = Property.query.filter_by(object_number=object_number).first()
        if prop is None:
            rows.append(PreviewRow(
                idx=idx,
                status=ROW_ERROR,
                message=f"Objekt-Nr. '{object_number}' nicht gefunden",
                raw=raw_row,
            ))
            continue

        # --- match on meter_number -------------------------------------------
        existing_meter = WaterMeter.query.filter_by(meter_number=meter_number).first()

        if existing_meter:
            status = ROW_UPDATE if cfg.duplicate_mode == "update" else ROW_EXISTS
        else:
            status = ROW_NEW

        warnings: list[str] = []

        # --- Meter↔Objekt tracker --------------------------------------------
        object_key = prop.id
        existing_object_key = existing_meter.property_id if existing_meter else None
        w = tracker.check_and_register(meter_number, object_key, existing_object_key)
        if w:
            warnings.append(w)

        # --- parse values for display ----------------------------------------
        installed_from_date = parse_date(installed_from_raw, "auto")
        installed_from_str = (
            installed_from_date.isoformat() if installed_from_date else installed_from_raw
        )

        initial_value_decimal = parse_number(initial_value_raw, "auto")
        initial_value_str = (
            format_value_de(initial_value_decimal)
            if initial_value_decimal is not None
            else initial_value_raw
        )

        fields = {
            "meter_number": meter_number,
            "object_number": object_number,
            "location": location,
            "eichjahr": eichjahr_raw,
            "installed_from": installed_from_str,
            "initial_value": initial_value_str,
            "meter_type": meter_type_raw,
            "notes": notes,
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

    Re-resolves the meter/object match when ``meter_number`` or
    ``object_number`` was edited so the status stays accurate.
    """
    from app.models import Property, WaterMeter

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
            "meter_number", "object_number", "location",
            "eichjahr", "installed_from", "initial_value",
            "meter_type", "notes",
        ):
            if field_name in row_edits:
                row.fields[field_name] = row_edits[field_name]

        # re-resolve status if meter_number or object_number was touched
        if ("meter_number" in row_edits or "object_number" in row_edits) \
                and row.status != ROW_ERROR:
            meter_number = row.fields.get("meter_number", "")
            object_number = row.fields.get("object_number", "")

            # Validate required fields
            if not meter_number:
                row.status = ROW_ERROR
                row.message = "Zählernummer fehlt"
                continue

            if not object_number:
                row.status = ROW_ERROR
                row.message = "Objekt-Nr. fehlt"
                continue

            prop = Property.query.filter_by(object_number=object_number).first()
            if prop is None:
                row.status = ROW_ERROR
                row.message = f"Objekt-Nr. '{object_number}' nicht gefunden"
                continue

            # Clear error state
            row.message = ""

            existing_meter = WaterMeter.query.filter_by(meter_number=meter_number).first()
            if existing_meter:
                if row.status == ROW_UPDATE:
                    row.status = ROW_UPDATE
                else:
                    row.status = ROW_EXISTS
            else:
                row.status = ROW_NEW

    return rows


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit(rows: list[PreviewRow], cfg: MeterImportConfig) -> ImportStats:
    """Write the (non-skipped, non-error) rows to the database.

    Uses ``db.session.begin_nested()`` savepoints so a single bad row does
    not abort the entire import.
    """
    from decimal import Decimal

    from app.extensions import db
    from app.models import Property, WaterMeter
    from app.imports.relations import MeterObjectTracker

    stats = ImportStats()
    tracker = MeterObjectTracker()

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
            meter_number = row.fields.get("meter_number", "").strip()
            object_number = row.fields.get("object_number", "").strip()
            location = row.fields.get("location", "").strip() or None
            eichjahr_raw = row.fields.get("eichjahr", "").strip()
            installed_from_raw = row.fields.get("installed_from", "").strip()
            initial_value_raw = row.fields.get("initial_value", "").strip()
            meter_type_raw = row.fields.get("meter_type", "").strip()
            notes = row.fields.get("notes", "").strip() or None

            # defensive: ensure required fields are still valid
            if not meter_number or not object_number:
                stats.skipped_error += 1
                sp.commit()
                continue

            prop = Property.query.filter_by(object_number=object_number).first()
            if prop is None:
                stats.skipped_error += 1
                sp.commit()
                continue

            # parse typed values
            eichjahr: int | None = None
            if eichjahr_raw:
                try:
                    eichjahr = int(float(eichjahr_raw))
                except (ValueError, TypeError):
                    eichjahr = None

            installed_from = parse_date(installed_from_raw, "auto")

            # initial_value: field contains a DE-formatted string (e.g. "1.234,567")
            # produced by format_value_de in build_preview_rows.
            # parse_number handles DE comma natively — no pre-conversion needed.
            initial_value = parse_number(initial_value_raw, "auto")

            meter_type = _resolve_meter_type(meter_type_raw)

            existing = WaterMeter.query.filter_by(meter_number=meter_number).first()

            # --- Meter↔Objekt tracker ----------------------------------------
            existing_object_key = existing.property_id if existing else None
            w = tracker.check_and_register(meter_number, prop.id, existing_object_key)
            row_had_warning = bool(row.warnings) or bool(w)
            if w:
                stats.warnings += 1
            elif row.warnings:
                stats.warnings += 1

            if existing and cfg.duplicate_mode == "skip":
                stats.skipped += 1
                sp.commit()
                continue

            if existing and cfg.duplicate_mode == "update":
                # Update all mapped fields
                if cfg.col_location:
                    existing.location = location
                if cfg.col_eichjahr:
                    existing.eichjahr = eichjahr
                if cfg.col_installed_from:
                    existing.installed_from = installed_from
                if cfg.col_initial_value:
                    existing.initial_value = initial_value
                if cfg.col_meter_type:
                    existing.meter_type = meter_type
                if cfg.col_notes:
                    existing.notes = notes
                # Meter↔Objekt: re-assign to new property in update mode
                if existing.property_id != prop.id:
                    existing.property_id = prop.id
                stats.updated += 1

            else:
                # New meter
                meter = WaterMeter(
                    property_id=prop.id,
                    meter_number=meter_number,
                    location=location,
                    eichjahr=eichjahr,
                    installed_from=installed_from,
                    initial_value=initial_value,
                    meter_type=meter_type,
                    notes=notes,
                    active=True,
                )
                db.session.add(meter)
                stats.created += 1

            sp.commit()

        except Exception as exc:  # noqa: BLE001
            sp.rollback()
            stats.errors.append(f"Zeile {row.idx + 2}: {exc}")

    db.session.commit()
    return stats
