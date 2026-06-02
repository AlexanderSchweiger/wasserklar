"""Kunden-Import-Service für den 3-stufigen Wizard.

Stellt ``CustomerImportConfig``, ``build_preview_rows``, ``apply_edits`` und
``commit`` bereit.  Kein Flask-Request-Zugriff — alle Funktionen sind
request-frei und werden von den Routen in ``customers/routes.py`` aufgerufen.

Konventionen:
- UI-Strings sind deutsch; Identifier und Kommentare englisch.
- ORM (``db.session.add``) statt Roh-SQL, damit Python-Defaults greifen.
- Dialekt-portabel (SQLite/MariaDB/Postgres): nur ``filter_by``-Queries.
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
    "customer_number": [
        "kunden-nr.", "kundennr", "kunden nr", "kundennummer", "kunden-nr", "kdnr",
    ],
    "externe_kennung": [
        "externe kennung", "externe_kennung", "kennung", "fremdnummer", "externe id",
    ],
    "name": [
        "kombinierter name", "kundenname", "name",
    ],
    "name_last": ["nachname", "familienname"],
    "name_first": ["vorname"],
    "strasse": ["strasse", "straße"],
    "hausnummer": ["hausnummer", "hausnr", "haus-nr"],
    "plz": ["plz", "postleitzahl"],
    "ort": ["ort", "stadt", "gemeinde"],
    "land": ["land"],
    "email": ["e-mail", "email"],
    "phone": ["telefon", "tel", "handy", "mobil"],
    "notes": ["kommentar", "bemerkung", "notiz", "info", "anmerkung"],
}


# ---------------------------------------------------------------------------
# CustomerImportConfig
# ---------------------------------------------------------------------------

@dataclass
class CustomerImportConfig:
    """Holds all user-chosen column mappings and the duplicate handling mode."""

    col_customer_number: str = ""
    col_externe_kennung: str = ""
    col_name: str = ""
    col_name_last: str = ""
    col_name_first: str = ""
    col_strasse: str = ""
    col_hausnummer: str = ""
    col_plz: str = ""
    col_ort: str = ""
    col_land: str = ""
    col_email: str = ""
    col_phone: str = ""
    col_notes: str = ""
    duplicate_mode: str = "skip"  # Default: Überspringen

    # --- serialisation helpers -----------------------------------------------

    def to_dict(self) -> dict:
        return {
            "col_customer_number": self.col_customer_number,
            "col_externe_kennung": self.col_externe_kennung,
            "col_name": self.col_name,
            "col_name_last": self.col_name_last,
            "col_name_first": self.col_name_first,
            "col_strasse": self.col_strasse,
            "col_hausnummer": self.col_hausnummer,
            "col_plz": self.col_plz,
            "col_ort": self.col_ort,
            "col_land": self.col_land,
            "col_email": self.col_email,
            "col_phone": self.col_phone,
            "col_notes": self.col_notes,
            "duplicate_mode": self.duplicate_mode,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "CustomerImportConfig":
        """Reconstruct from a dict (e.g. from the session).  Robust against missing keys."""
        if not d:
            return cls()
        dup = d.get("duplicate_mode", "skip")
        if dup not in ("update", "skip"):
            dup = "skip"
        return cls(
            col_customer_number=d.get("col_customer_number", ""),
            col_externe_kennung=d.get("col_externe_kennung", ""),
            col_name=d.get("col_name", ""),
            col_name_last=d.get("col_name_last", ""),
            col_name_first=d.get("col_name_first", ""),
            col_strasse=d.get("col_strasse", ""),
            col_hausnummer=d.get("col_hausnummer", ""),
            col_plz=d.get("col_plz", ""),
            col_ort=d.get("col_ort", ""),
            col_land=d.get("col_land", ""),
            col_email=d.get("col_email", ""),
            col_phone=d.get("col_phone", ""),
            col_notes=d.get("col_notes", ""),
            duplicate_mode=dup,
        )

    @classmethod
    def from_form(cls, form) -> "CustomerImportConfig":
        """Build from a Werkzeug form (request.form)."""
        dup = form.get("duplicate_mode", "skip")
        if dup not in ("update", "skip"):
            dup = "skip"
        return cls(
            col_customer_number=form.get("col_customer_number", ""),
            col_externe_kennung=form.get("col_externe_kennung", ""),
            col_name=form.get("col_name", ""),
            col_name_last=form.get("col_name_last", ""),
            col_name_first=form.get("col_name_first", ""),
            col_strasse=form.get("col_strasse", ""),
            col_hausnummer=form.get("col_hausnummer", ""),
            col_plz=form.get("col_plz", ""),
            col_ort=form.get("col_ort", ""),
            col_land=form.get("col_land", ""),
            col_email=form.get("col_email", ""),
            col_phone=form.get("col_phone", ""),
            col_notes=form.get("col_notes", ""),
            duplicate_mode=dup,
        )


# ---------------------------------------------------------------------------
# Auto-suggest
# ---------------------------------------------------------------------------

def suggest_config(columns: list[str]) -> CustomerImportConfig:
    """Auto-suggest column mappings for the given list of column names."""
    return CustomerImportConfig(
        col_customer_number=suggest_column(columns, HINTS["customer_number"]),
        col_externe_kennung=suggest_column(columns, HINTS["externe_kennung"]),
        col_name=suggest_column(columns, HINTS["name"]),
        col_name_last=suggest_column(columns, HINTS["name_last"]),
        col_name_first=suggest_column(columns, HINTS["name_first"]),
        col_strasse=suggest_column(columns, HINTS["strasse"]),
        col_hausnummer=suggest_column(columns, HINTS["hausnummer"]),
        col_plz=suggest_column(columns, HINTS["plz"]),
        col_ort=suggest_column(columns, HINTS["ort"]),
        col_land=suggest_column(columns, HINTS["land"]),
        col_email=suggest_column(columns, HINTS["email"]),
        col_phone=suggest_column(columns, HINTS["phone"]),
        col_notes=suggest_column(columns, HINTS["notes"]),
    )


# ---------------------------------------------------------------------------
# Preview row builder
# ---------------------------------------------------------------------------

def _parse_customer_number(raw: str) -> int | None:
    """Parse a customer number string to int.  Returns None if not parseable."""
    if not raw:
        return None
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _resolve_customer(cnum: int | None, ext_key: str) -> "Customer | None":
    """Look up a customer by number or external key."""
    from app.models import Customer
    if cnum is not None:
        c = Customer.query.filter_by(customer_number=cnum).first()
        if c:
            return c
    if ext_key:
        return Customer.query.filter_by(externe_kennung=ext_key).first()
    return None


def build_preview_rows(df, cfg: CustomerImportConfig) -> list[PreviewRow]:
    """Build a list of PreviewRow from the DataFrame and config."""
    rows: list[PreviewRow] = []

    for idx, raw_row in enumerate(df.to_dict(orient="records")):
        # --- read cells -------------------------------------------------------
        cnum_raw = _cell(raw_row, cfg.col_customer_number)
        ext_key = _cell(raw_row, cfg.col_externe_kennung)
        name_raw = _cell(raw_row, cfg.col_name)
        name_last = _cell(raw_row, cfg.col_name_last)
        name_first = _cell(raw_row, cfg.col_name_first)
        strasse = _cell(raw_row, cfg.col_strasse)
        hausnummer = _cell(raw_row, cfg.col_hausnummer)
        plz = _cell(raw_row, cfg.col_plz)
        ort = _cell(raw_row, cfg.col_ort)
        land = _cell(raw_row, cfg.col_land)
        email = _cell(raw_row, cfg.col_email)
        phone = _cell(raw_row, cfg.col_phone)
        notes = _cell(raw_row, cfg.col_notes)

        # --- empty row guard --------------------------------------------------
        if not name_raw and not name_last and not cnum_raw and not ext_key:
            rows.append(PreviewRow(
                idx=idx,
                status=ROW_ERROR,
                message="Leere Zeile",
                raw=raw_row,
            ))
            continue

        # --- parse customer number --------------------------------------------
        cnum: int | None = None
        if cnum_raw:
            cnum = _parse_customer_number(cnum_raw)
            if cnum is None:
                rows.append(PreviewRow(
                    idx=idx,
                    status=ROW_ERROR,
                    message=f"Ungültige Kunden-Nr. '{cnum_raw}'",
                    raw=raw_row,
                ))
                continue

        # --- name fallback ----------------------------------------------------
        if not name_raw:
            parts = [p for p in [name_last, name_first] if p]
            name_raw = " ".join(parts).strip()

        # --- match against DB -------------------------------------------------
        existing = _resolve_customer(cnum, ext_key)
        if existing:
            status = ROW_UPDATE if cfg.duplicate_mode == "update" else ROW_EXISTS
        else:
            status = ROW_NEW

        fields = {
            "customer_number": str(cnum) if cnum is not None else "",
            "externe_kennung": ext_key,
            "name": name_raw,
            "strasse": strasse,
            "hausnummer": hausnummer,
            "plz": plz,
            "ort": ort,
            "land": land,
            "email": email,
            "phone": phone,
            "notes": notes,
        }

        rows.append(PreviewRow(
            idx=idx,
            status=status,
            fields=fields,
            raw=raw_row,
        ))

    return rows


# ---------------------------------------------------------------------------
# Apply edits
# ---------------------------------------------------------------------------

def apply_edits(form, rows: list[PreviewRow]) -> list[PreviewRow]:
    """Merge user edits from the preview form back into the row list.

    Re-resolves the customer match when ``customer_number`` was edited so the
    status stays accurate.
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
            "customer_number", "externe_kennung", "name",
            "strasse", "hausnummer", "plz", "ort", "land",
            "email", "phone", "notes",
        ):
            if field_name in row_edits:
                row.fields[field_name] = row_edits[field_name]

        # re-resolve status if customer_number was touched
        if "customer_number" in row_edits and row.status != ROW_ERROR:
            cnum_raw = row.fields.get("customer_number", "")
            ext_key = row.fields.get("externe_kennung", "")
            cnum = _parse_customer_number(cnum_raw) if cnum_raw else None
            existing = _resolve_customer(cnum, ext_key)
            # Retain whatever duplicate_mode is implied by the original status
            # context: if duplicate_mode was "update", update → ROW_UPDATE; else ROW_EXISTS
            if existing:
                # Preserve the previous duplicate_mode decision:
                # if it was ROW_UPDATE before, keep that; new default is ROW_EXISTS
                if row.status in (ROW_UPDATE,):
                    row.status = ROW_UPDATE
                else:
                    row.status = ROW_EXISTS
            else:
                row.status = ROW_NEW

    return rows


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit(rows: list[PreviewRow], cfg: CustomerImportConfig) -> ImportStats:
    """Write the (non-skipped, non-error) rows to the database.

    Uses ``db.session.begin_nested()`` savepoints so a single bad row does
    not abort the entire import.
    """
    from app.extensions import db
    from app.models import Customer
    from app.utils import bump_customer_counter_to, next_customer_number

    stats = ImportStats()

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
            cnum_str = row.fields.get("customer_number", "")
            ext_key = row.fields.get("externe_kennung", "")
            cnum = _parse_customer_number(cnum_str) if cnum_str else None

            existing = _resolve_customer(cnum, ext_key)

            if existing and cfg.duplicate_mode == "skip":
                stats.skipped += 1
                sp.commit()
                continue

            name = row.fields.get("name", "").strip()

            if existing and cfg.duplicate_mode == "update":
                # Update all mapped fields (empty value clears the field)
                if cfg.col_name or name:
                    existing.name = name or existing.name
                if cfg.col_externe_kennung:
                    existing.externe_kennung = ext_key or None
                if cfg.col_strasse:
                    existing.strasse = row.fields.get("strasse") or None
                if cfg.col_hausnummer:
                    existing.hausnummer = row.fields.get("hausnummer") or None
                if cfg.col_plz:
                    existing.plz = row.fields.get("plz") or None
                if cfg.col_ort:
                    existing.ort = row.fields.get("ort") or None
                if cfg.col_land:
                    existing.land = row.fields.get("land") or None
                if cfg.col_email:
                    existing.email = row.fields.get("email") or None
                if cfg.col_phone:
                    existing.phone = row.fields.get("phone") or None
                if cfg.col_notes:
                    existing.notes = row.fields.get("notes") or None
                stats.updated += 1

            else:
                # New customer
                if cnum is not None:
                    bump_customer_counter_to(cnum)
                    nr = cnum
                else:
                    nr = next_customer_number()

                if not name:
                    name = f"Kunde {nr}"

                customer = Customer(
                    customer_number=nr,
                    is_customer=True,
                    active=True,
                    name=name,
                    externe_kennung=ext_key or None,
                    strasse=row.fields.get("strasse") or None,
                    hausnummer=row.fields.get("hausnummer") or None,
                    plz=row.fields.get("plz") or None,
                    ort=row.fields.get("ort") or None,
                    land=row.fields.get("land") or "Österreich",
                    email=row.fields.get("email") or None,
                    phone=row.fields.get("phone") or None,
                    notes=row.fields.get("notes") or None,
                )
                db.session.add(customer)
                stats.created += 1

            if row.warnings:
                stats.warnings += 1

            sp.commit()

        except Exception as exc:  # noqa: BLE001
            sp.rollback()
            stats.errors.append(f"Zeile {row.idx + 2}: {exc}")

    db.session.commit()
    return stats
