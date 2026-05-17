"""Heavy-Lifting fuer den Zaehlertausch-Import-Wizard.

Importiert CSV/Excel-Dateien mit Zaehlertaeuschen: pro Zeile wird der alte
Zaehler ausgebaut (Abschlussablesung + Deaktivierung) und der neue angelegt.
Findet sich der alte Zaehler nicht im System, wird der neue als reine
Neuanlage importiert (Objekt-Zuordnung ueber Datei-Spalte oder Vorschau).

Konventionen analog ``import_service.py``:
- Reine Funktionen, kein Flask-Request-Zugriff (Form-Daten als Dict).
- ``Decimal`` als Mengen-Typ konsistent mit Models.
- DataFrame-Index ``idx`` ist die Zeilen-Identitaet durch den Wizard hindurch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.extensions import db
from app.models import (
    BillingPeriod, Customer, MeterReading, Property, PropertyOwnership, WaterMeter,
)
from app.meters.services import recompute_meter_chain
from app.meters.import_service import (
    delete_dataframe,
    format_value_de,
    load_dataframe,
    parse_date,
    parse_number,
    save_dataframe,
)

__all__ = [
    "STATUS_TAUSCH", "STATUS_NEUANLAGE", "STATUS_FEHLER",
    "SwapRow", "SwapImportStats",
    "save_dataframe", "load_dataframe", "delete_dataframe", "format_value_de",
    "detect_columns", "missing_required_columns",
    "build_swap_rows", "parse_swap_form_edits", "commit_swap_import",
    "status_row_class", "status_badge", "active_properties",
]


# ---------------------------------------------------------------------------
# Status-Klassen
# ---------------------------------------------------------------------------

STATUS_TAUSCH = "tausch"        # alter Zaehler gefunden -> Ausbau + Neuanlage
STATUS_NEUANLAGE = "neuanlage"  # alter Zaehler unbekannt -> nur Neuanlage
STATUS_FEHLER = "fehler"        # Zeile nicht importierbar


# ---------------------------------------------------------------------------
# Spalten-Erkennung
# ---------------------------------------------------------------------------
#
# Die Datei hat feste Spaltennamen (siehe Upload-Maske). Header-Matching ist
# tolerant gegenueber Gross-/Kleinschreibung, Leerzeichen und Satzzeichen.

# Pflicht-Spalten -- ohne die kann gar nicht importiert werden.
REQUIRED_TARGETS = ("old_meter_number", "new_meter_number")

# Menschenlesbare Bezeichnung pro Ziel-Feld (Upload-Maske / Fehlermeldungen).
COLUMN_LABELS = {
    "old_meter_number": "WasserzählerNr. alt",
    "old_dismount_value": "Ausbau Zählerstand",
    "new_meter_number": "Wasserzähler Nr. neu",
    "new_initial_value": "Zählerstand neu",
    "new_eichjahr": "Eichjahr",
    "swap_date": "Tauschdatum",
    "object_number": "Objekt-Nr.",
}


def _norm_compact(s: str) -> str:
    """Lowercase, ohne Leerzeichen/Punkte/Bindestriche -- robustes Matching."""
    return re.sub(r"[\s.\-_]+", "", (s or "").strip().lower())


def detect_columns(columns: list[str]) -> dict[str, str]:
    """Ordnet die Datei-Spalten den Ziel-Feldern zu.

    Liefert ``{ziel_feld: spaltenname}``. Reihenfolge der Pruefungen ist
    bewusst gewaehlt: spezifischere Treffer (Ausbau, Eichjahr, Tausch) vor
    den generischen Zaehlernummer-/Stand-Heuristiken.
    """
    result: dict[str, str] = {}
    for col in columns:
        nc = _norm_compact(str(col))
        has_zaehler = "zähler" in nc or "zaehler" in nc
        if "objekt" in nc:
            result.setdefault("object_number", col)
        elif "eichjahr" in nc:
            result.setdefault("new_eichjahr", col)
        elif "tausch" in nc or nc == "datum":
            result.setdefault("swap_date", col)
        elif "ausbau" in nc:
            result.setdefault("old_dismount_value", col)
        elif has_zaehler and "alt" in nc:
            result.setdefault("old_meter_number", col)
        elif has_zaehler and "neu" in nc and "nr" in nc:
            result.setdefault("new_meter_number", col)
        elif "neu" in nc and "stand" in nc:
            result.setdefault("new_initial_value", col)
    return result


def missing_required_columns(cols: dict[str, str]) -> list[str]:
    """Liste der fehlenden Pflicht-Spalten (Labels) -- leer wenn alles da."""
    return [COLUMN_LABELS[t] for t in REQUIRED_TARGETS if t not in cols]


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class SwapRow:
    idx: int
    old_meter_number: str
    new_meter_number: str
    dismount_value: Decimal | None
    new_initial_value: Decimal
    new_eichjahr: int | None
    swap_date: date
    object_number_raw: str

    # aufgeloeste DB-Referenzen (pro Request frisch ermittelt)
    old_meter: WaterMeter | None = None
    old_meter_inactive: bool = False
    new_number_exists: bool = False
    property_id: int | None = None

    status: str = STATUS_FEHLER
    skip: bool = False
    messages: list[str] = field(default_factory=list)

    # Anzeige-Felder fuer die Vorschau (nicht persistiert)
    old_last_value: Decimal | None = None
    old_last_date: date | None = None
    old_object_label: str = ""
    old_owner: str = ""

    @property
    def human_row(self) -> int:
        """1-basierte Zeilennummer inkl. Header -- fuer Fehlermeldungen."""
        return self.idx + 2


@dataclass
class SwapImportStats:
    swapped: int = 0       # durchgefuehrte Taeusche
    created: int = 0       # reine Neuanlagen
    skipped: int = 0       # via Skip-Haekchen uebersprungen
    skipped_error: int = 0  # Fehler-Zeilen / kein Objekt
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "swapped": self.swapped,
            "created": self.created,
            "skipped": self.skipped,
            "skipped_error": self.skipped_error,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Zellen-Helfer
# ---------------------------------------------------------------------------

def _cell(row: dict, col: str | None) -> str:
    """Robuster String-Zugriff auf eine DataFrame-Zelle (NaN/None -> '')."""
    import pandas as pd
    if not col:
        return ""
    v = row.get(col, "")
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    return s


def _parse_year(raw: str) -> int | None:
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Eigentuemer / Objekt-Lookup
# ---------------------------------------------------------------------------

def _owner_name(property_id: int) -> str:
    """Aktuelle(r) Eigentuemer eines Objekts, mit ', ' gejoint.

    Objekte koennen mehrere parallele aktive Ownerships haben (Ehepaare,
    Erbengemeinschaften) -- daher ``.all()`` statt ``.scalar()``.
    """
    names = (
        db.session.query(Customer.name)
        .join(PropertyOwnership, PropertyOwnership.customer_id == Customer.id)
        .filter(
            PropertyOwnership.property_id == property_id,
            PropertyOwnership.valid_to.is_(None),
        )
        .order_by(Customer.name.asc())
        .all()
    )
    return ", ".join(n for (n,) in names)


def active_properties() -> list[Property]:
    """Alle aktiven Objekte fuer das Neuanlage-Dropdown der Vorschau."""
    return (
        Property.query
        .filter(Property.active.is_(True))
        .order_by(Property.object_number.asc(), Property.ort.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Zeilen-Aufbau + Klassifizierung
# ---------------------------------------------------------------------------

def _classify(row: SwapRow) -> None:
    """Setzt ``status`` + ``messages`` aus dem aktuellen Zeilen-Zustand.

    Re-runnable: wird nach User-Edits in der Vorschau erneut aufgerufen.
    """
    msgs: list[str] = []

    if not row.new_meter_number:
        row.status = STATUS_FEHLER
        row.messages = ["Neue Zählernummer fehlt"]
        return
    if row.new_number_exists:
        row.status = STATUS_FEHLER
        row.messages = [f"Zählernummer '{row.new_meter_number}' existiert bereits"]
        return

    if row.old_meter is not None:
        # Alter Zaehler gefunden -> Tausch
        if row.old_meter_inactive:
            row.status = STATUS_FEHLER
            row.messages = [
                f"Alter Zähler '{row.old_meter_number}' ist bereits ausgebaut"
            ]
            return
        if row.dismount_value is None:
            row.status = STATUS_FEHLER
            row.messages = ["Ausbau-Zählerstand fehlt oder nicht lesbar"]
            return
        row.status = STATUS_TAUSCH
        row.messages = msgs
        return

    # Alter Zaehler nicht gefunden -> Neuanlage
    row.status = STATUS_NEUANLAGE
    if row.old_meter_number:
        msgs.append(
            f"Alter Zähler '{row.old_meter_number}' nicht gefunden — wird als Neuanlage importiert"
        )
    if row.property_id is None:
        msgs.append("Objekt wählen")
    row.messages = msgs


def _build_one(idx: int, raw: dict, cols: dict[str, str]) -> SwapRow:
    old_num = _cell(raw, cols.get("old_meter_number"))
    new_num = _cell(raw, cols.get("new_meter_number"))
    dismount_raw = _cell(raw, cols.get("old_dismount_value"))
    initial_raw = _cell(raw, cols.get("new_initial_value"))
    eichjahr_raw = _cell(raw, cols.get("new_eichjahr"))
    date_raw = raw.get(cols["swap_date"]) if cols.get("swap_date") else None
    object_raw = _cell(raw, cols.get("object_number"))

    dismount_value = parse_number(dismount_raw, "auto") if dismount_raw else None

    initial_value = parse_number(initial_raw, "auto") if initial_raw else None
    if initial_value is None:
        initial_value = Decimal("0")

    new_eichjahr = _parse_year(eichjahr_raw)

    # Tauschdatum: 'auto' erkennt jedes Format (TT.MM.JJJJ, ISO, US,
    # Excel-Timestamp/-Serial) -- sonst faellt es still auf date.today() zurueck.
    swap_date = parse_date(date_raw, "auto") if date_raw is not None else None
    if swap_date is None:
        swap_date = date.today()

    row = SwapRow(
        idx=idx,
        old_meter_number=old_num,
        new_meter_number=new_num,
        dismount_value=dismount_value,
        new_initial_value=initial_value,
        new_eichjahr=new_eichjahr,
        swap_date=swap_date,
        object_number_raw=object_raw,
    )

    # Neue Nummer schon vergeben?
    if new_num:
        row.new_number_exists = (
            WaterMeter.query.filter_by(meter_number=new_num).first() is not None
        )

    # Alten Zaehler aufloesen
    if old_num:
        old = WaterMeter.query.filter_by(meter_number=old_num).first()
        if old is not None:
            row.old_meter = old
            row.old_meter_inactive = not old.active
            last = old.last_reading()
            if last is not None:
                row.old_last_value = last.value
                row.old_last_date = last.reading_date
            row.old_object_label = old.property.label() if old.property else ""
            row.old_owner = _owner_name(old.property_id)

    # Objekt fuer Neuanlage aus der Datei-Spalte vorbelegen
    if row.old_meter is None and object_raw:
        prop = Property.query.filter_by(
            object_number=object_raw, active=True,
        ).first()
        if prop is not None:
            row.property_id = prop.id

    _classify(row)
    return row


def build_swap_rows(df) -> tuple[list[SwapRow], dict[str, str]]:
    """Baut die Vorschau-Zeilen aus dem hochgeladenen DataFrame.

    Liefert ``(rows, detected_columns)``.
    """
    cols = detect_columns(list(df.columns))
    if df is None or df.empty:
        return [], cols

    rows: list[SwapRow] = []
    for idx, row in df.iterrows():
        raw = {col: row[col] for col in df.columns}
        row_idx = int(idx) if isinstance(idx, (int, float)) else len(rows)
        rows.append(_build_one(row_idx, raw, cols))
    return rows, cols


# ---------------------------------------------------------------------------
# User-Edits aus dem Confirm-Form mergen
# ---------------------------------------------------------------------------

_RE_FORM_KEY = re.compile(r"^rows\[(\d+)\]\[(\w+)\]$")


def parse_swap_form_edits(form, baseline_rows: list[SwapRow]) -> list[SwapRow]:
    """Mergt User-Edits (Skip, Objekt, Ausbau-Stand, Anfangsstand, Eichjahr,
    Tauschdatum) auf die frisch aufgebauten baseline-Zeilen und klassifiziert
    jede betroffene Zeile neu.
    """
    edits: dict[int, dict[str, str]] = {}
    for key in form.keys():
        m = _RE_FORM_KEY.match(key)
        if not m:
            continue
        edits.setdefault(int(m.group(1)), {})[m.group(2)] = form.get(key, "")

    # Unchecked checkboxes werden nicht mitgeschickt -> explizit zuruecksetzen.
    for r in baseline_rows:
        r.skip = False

    for r in baseline_rows:
        e = edits.get(r.idx)
        if not e:
            continue

        if str(e.get("skip", "")).lower() in ("on", "1", "true", "yes"):
            r.skip = True

        if "property_id" in e:
            pid_raw = (e.get("property_id") or "").strip()
            try:
                r.property_id = int(pid_raw) if pid_raw else None
            except ValueError:
                r.property_id = None

        if "dismount_value" in e:
            dv_raw = (e.get("dismount_value") or "").strip()
            r.dismount_value = parse_number(dv_raw, "auto") if dv_raw else None

        if "new_initial_value" in e:
            iv_raw = (e.get("new_initial_value") or "").strip()
            iv = parse_number(iv_raw, "auto") if iv_raw else None
            r.new_initial_value = iv if iv is not None else Decimal("0")

        if "new_eichjahr" in e:
            r.new_eichjahr = _parse_year((e.get("new_eichjahr") or "").strip())

        if "swap_date" in e:
            # <input type="date"> liefert immer ISO (YYYY-MM-DD), unabhaengig
            # davon, wie der Browser das Datum lokalisiert anzeigt.
            d_raw = (e.get("swap_date") or "").strip()
            d = parse_date(d_raw, "iso") if d_raw else None
            if d is not None:
                r.swap_date = d

        _classify(r)

    return baseline_rows


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit_swap_import(rows: list[SwapRow], user_id: int,
                       billing_period: BillingPeriod) -> SwapImportStats:
    """Persistiert die Zaehlertaeusche und Neuanlagen in die gewaehlte
    Abrechnungsperiode.

    Pro Zeile ein Savepoint -- ein Fehler rollt nur diese eine Zeile zurueck.
    Spiegelt die Logik aus ``meters.routes.meter_replace``; der Verbrauch der
    Abschlussablesung wird nach dem Lauf via ``recompute_meter_chain`` gesetzt.
    """
    stats = SwapImportStats()
    affected_meters: dict[int, WaterMeter] = {}
    for row in rows:
        if row.skip:
            stats.skipped += 1
            continue
        if row.status == STATUS_FEHLER:
            stats.skipped_error += 1
            continue

        sp = db.session.begin_nested()
        try:
            if row.status == STATUS_TAUSCH:
                old = row.old_meter
                if old is None or not old.active:
                    sp.rollback()
                    stats.errors.append(
                        f"Zeile {row.human_row}: alter Zähler nicht (mehr) verfügbar"
                    )
                    stats.skipped_error += 1
                    continue

                # 1. Alten Zaehler ausbauen
                old.installed_to = row.swap_date
                old.active = False

                # 2. Abschlussablesung anlegen/aktualisieren
                existing = MeterReading.query.filter_by(
                    meter_id=old.id, billing_period_id=billing_period.id,
                ).first()
                if existing is not None:
                    existing.value = row.dismount_value
                    existing.reading_date = row.swap_date
                    existing.created_by_id = user_id
                else:
                    db.session.add(MeterReading(
                        meter_id=old.id,
                        billing_period_id=billing_period.id,
                        value=row.dismount_value,
                        reading_date=row.swap_date,
                        created_by_id=user_id,
                    ))
                affected_meters[old.id] = old

                # 3. Neuen Zaehler anlegen
                db.session.add(WaterMeter(
                    property_id=old.property_id,
                    meter_number=row.new_meter_number,
                    location=old.location,
                    installed_from=row.swap_date,
                    initial_value=row.new_initial_value,
                    eichjahr=row.new_eichjahr,
                    meter_type=old.meter_type,
                    parent_meter_id=old.parent_meter_id,
                    notes=f"Nachfolger von {old.meter_number}",
                ))
                stats.swapped += 1

            elif row.status == STATUS_NEUANLAGE:
                if not row.property_id:
                    sp.rollback()
                    stats.errors.append(
                        f"Zeile {row.human_row}: kein Objekt gewählt — übersprungen"
                    )
                    stats.skipped_error += 1
                    continue
                db.session.add(WaterMeter(
                    property_id=row.property_id,
                    meter_number=row.new_meter_number,
                    installed_from=row.swap_date,
                    initial_value=row.new_initial_value,
                    eichjahr=row.new_eichjahr,
                    meter_type="main",
                ))
                stats.created += 1

            sp.commit()
        except Exception as e:  # pragma: no cover - defensive
            sp.rollback()
            stats.errors.append(f"Zeile {row.human_row}: {e}")
            stats.skipped_error += 1

    # Verbrauch der betroffenen (alten) Zaehler neu berechnen.
    db.session.flush()
    for meter in affected_meters.values():
        recompute_meter_chain(meter)

    db.session.commit()
    return stats


# ---------------------------------------------------------------------------
# Template-Helfer
# ---------------------------------------------------------------------------

def status_row_class(status: str) -> str:
    if status == STATUS_TAUSCH:
        return "table-success"
    if status == STATUS_NEUANLAGE:
        return "table-info"
    if status == STATUS_FEHLER:
        return "table-danger"
    return ""


def status_badge(status: str) -> tuple[str, str]:
    """Liefert (Label, CSS-Klasse) fuer den Status-Badge."""
    if status == STATUS_TAUSCH:
        return ("Tausch", "bg-success text-white")
    if status == STATUS_NEUANLAGE:
        return ("Neuanlage", "bg-info text-white")
    if status == STATUS_FEHLER:
        return ("Fehler", "bg-danger text-white")
    return (status, "bg-secondary text-white")
