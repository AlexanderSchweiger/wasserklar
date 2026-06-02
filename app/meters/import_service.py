"""Heavy-Lifting fuer den Ablesungs-Import-Wizard.

Halt die Mapping-Resolver-, Auto-Detection- und Commit-Logik aus
``app/meters/routes.py`` raus -- die Routen-Datei darf duenn bleiben.

Konventionen:
- Reine Funktionen, kein Flask-Request-Zugriff (Form-Daten werden als Dict
  uebergeben). Nur ``db`` + Models.
- ``Decimal`` als Geld-/Mengen-Typ konsistent mit Models.
- DataFrame-Index ``i`` ist die Identitaet einer Zeile durch den ganzen
  Wizard hindurch (Pickle survived den Roundtrip ohne Re-Index).

Die allgemeinen Daten-/IO-Helfer und Parse-Utilities liegen jetzt in
``app.imports.common`` und werden hier re-exportiert, damit bestehende
Aufrufer (routes.py, Tests) unveraendert weiterfunktionieren.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from flask import current_app

from app.extensions import db
from app.models import (
    Customer, MeterReading, Property, PropertyOwnership, WaterMeter,
    BillingPeriod,
)
from app.meters.services import previous_reading, recompute_meter_chain

# ---------------------------------------------------------------------------
# Re-exports from app.imports.common
# (routes.py and tests reference these as import_service.<name>)
# ---------------------------------------------------------------------------
from app.imports.common import (  # noqa: F401 — re-exported on purpose
    NUMBER_FORMATS,
    DATE_FORMATS,
    DUPLICATE_MODES,
    _RE_PURE_INT,
    _RE_NUM_AT_DE_END,
    _RE_NUM_US_END,
    _RE_NUM_HAS_DOT_3,
    _RE_DATE_ISO,
    _RE_DATE_DEUS,
    _RE_TRAILING_TIME,
    _series_strings,
    detect_number_format,
    detect_date_format,
    _cell,
    parse_number,
    parse_date,
    format_value_de,
    load_dataframe,
    delete_dataframe,
    parse_row_edits as _parse_row_edits,
)
import app.imports.common as _common


# ---------------------------------------------------------------------------
# Konstanten (meter-specific)
# ---------------------------------------------------------------------------

MAPPING_MODES = ("meter_number", "customer_number", "customer_name")

STATUS_OK = "ok"
STATUS_OK_PREFERRED_MAIN = "ok_preferred_main"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NOT_FOUND = "not_found"
STATUS_PARSE_ERROR = "parse_error"


# ---------------------------------------------------------------------------
# save_dataframe — thin wrapper that forces the meter_import_ prefix
# ---------------------------------------------------------------------------

def save_dataframe(df) -> str:
    """Pickle the DataFrame with the ``meter_import_`` prefix.

    Thin wrapper around ``app.imports.common.save_dataframe`` to keep the
    prefix stable for existing session keys and clean-up logic.
    """
    return _common.save_dataframe(df, prefix="meter_import_")


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class MappingConfig:
    mode: str = "meter_number"
    col_lookup: str = ""
    col_value: str = ""
    col_date: str = ""
    col_consumption: str = ""  # optional: vom Excel mitgelieferter Verbrauch zum Vergleich
    billing_period_id: int = 0
    duplicate_mode: str = "update"
    value_format: str = "auto"
    date_format: str = "auto"

    @classmethod
    def from_form(cls, form) -> "MappingConfig":
        mode = form.get("mode") or "meter_number"
        if mode not in MAPPING_MODES:
            mode = "meter_number"
        dup = form.get("duplicate_mode") or "update"
        if dup not in DUPLICATE_MODES:
            dup = "update"
        vf = form.get("value_format") or "auto"
        if vf not in NUMBER_FORMATS:
            vf = "auto"
        df_ = form.get("date_format") or "auto"
        if df_ not in DATE_FORMATS:
            df_ = "auto"
        try:
            bpid = int(form.get("billing_period_id") or 0)
        except (TypeError, ValueError):
            bpid = 0
        return cls(
            mode=mode,
            col_lookup=(form.get("col_lookup") or "").strip(),
            col_value=(form.get("col_value") or "").strip(),
            col_date=(form.get("col_date") or "").strip(),
            col_consumption=(form.get("col_consumption") or "").strip(),
            billing_period_id=bpid,
            duplicate_mode=dup,
            value_format=vf,
            date_format=df_,
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "col_lookup": self.col_lookup,
            "col_value": self.col_value,
            "col_date": self.col_date,
            "col_consumption": self.col_consumption,
            "billing_period_id": self.billing_period_id,
            "duplicate_mode": self.duplicate_mode,
            "value_format": self.value_format,
            "date_format": self.date_format,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "MappingConfig":
        if not d:
            return cls()
        return cls(**{k: d.get(k, getattr(cls(), k)) for k in cls().__dict__})


@dataclass
class ResolveResult:
    status: str
    candidates: list[WaterMeter]
    chosen: WaterMeter | None
    message: str


@dataclass
class ResolvedRow:
    idx: int
    raw_data: dict
    lookup_value: str
    value: Decimal | None
    reading_date: date | None
    status: str
    candidate_meter_ids: list[int]
    chosen_meter_id: int | None
    skip: bool
    message: str
    parse_errors: list[str] = field(default_factory=list)
    # Vorjahres-/Verbrauchs-Anzeige (nur Vorschau, nicht persistiert):
    prior_value: Decimal | None = None
    prior_label: str = ""           # "2023" | "Anfang 15.06.2024" | "—"
    computed_consumption: Decimal | None = None
    imported_consumption: Decimal | None = None
    consumption_mismatch: bool = False
    replacement_info: str = ""      # Hinweistext bei Zaehlerwechsel im Jahr


@dataclass
class ImportStats:
    """Meter-reading-specific import stats (different from app.imports.common.ImportStats)."""
    created: int = 0
    updated: int = 0
    skipped: int = 0
    skipped_dup: int = 0
    skipped_unmapped: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "skipped_dup": self.skipped_dup,
            "skipped_unmapped": self.skipped_unmapped,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Mapping / Resolver
# ---------------------------------------------------------------------------

def _customer_meters(customer_id: int) -> list[WaterMeter]:
    """Aktuelle Meter eines Kunden ueber aktive Ownership."""
    return (
        WaterMeter.query
        .join(Property, Property.id == WaterMeter.property_id)
        .join(PropertyOwnership, PropertyOwnership.property_id == Property.id)
        .filter(
            PropertyOwnership.customer_id == customer_id,
            PropertyOwnership.valid_to.is_(None),
            WaterMeter.active.is_(True),
        )
        .order_by(WaterMeter.meter_type.desc(), WaterMeter.meter_number.asc())
        .all()
    )


def _classify(meters: list[WaterMeter], customer_label: str) -> ResolveResult:
    if not meters:
        return ResolveResult(STATUS_NOT_FOUND, [], None,
                             f"{customer_label}: keine aktiven Zähler")
    if len(meters) == 1:
        return ResolveResult(STATUS_OK, meters, meters[0], "")
    mains = [m for m in meters if m.meter_type == "main"]
    if len(mains) == 1:
        return ResolveResult(
            STATUS_OK_PREFERRED_MAIN, meters, mains[0],
            f"{len(meters)} Zähler — Hauptzähler vorausgewählt",
        )
    return ResolveResult(
        STATUS_AMBIGUOUS, meters, None,
        f"{len(meters)} Zähler ({len(mains)} Hauptz.) — bitte wählen",
    )


def resolve_meter(lookup_value: str, mode: str) -> ResolveResult:
    lv = (lookup_value or "").strip()
    if not lv:
        return ResolveResult(STATUS_NOT_FOUND, [], None, "Lookup-Wert leer")

    if mode == "meter_number":
        m = WaterMeter.query.filter_by(meter_number=lv, active=True).first()
        if m:
            return ResolveResult(STATUS_OK, [m], m, "")
        return ResolveResult(STATUS_NOT_FOUND, [], None,
                             f"Zähler '{lv}' nicht gefunden")

    if mode == "customer_number":
        try:
            cnum = int(float(lv))
        except (TypeError, ValueError):
            return ResolveResult(STATUS_NOT_FOUND, [], None,
                                 f"Kunden-Nr. '{lv}' ungültig")
        c = Customer.query.filter_by(customer_number=cnum, active=True).first()
        if not c:
            return ResolveResult(STATUS_NOT_FOUND, [], None,
                                 f"Kunden-Nr. {cnum} nicht gefunden")
        return _classify(_customer_meters(c.id), f"Kunde {c.name}")

    if mode == "customer_name":
        norm = lv.lower()
        candidates = (
            Customer.query
            .filter(db.func.lower(Customer.name) == norm,
                    Customer.active.is_(True))
            .all()
        )
        if not candidates:
            return ResolveResult(STATUS_NOT_FOUND, [], None,
                                 f"Kunde '{lv}' nicht gefunden")
        if len(candidates) > 1:
            all_meters = []
            for c in candidates:
                all_meters.extend(_customer_meters(c.id))
            return ResolveResult(
                STATUS_AMBIGUOUS, all_meters, None,
                f"{len(candidates)} Kunden mit Namen '{lv}' — bitte wählen",
            )
        return _classify(_customer_meters(candidates[0].id),
                         f"Kunde {candidates[0].name}")

    return ResolveResult(STATUS_NOT_FOUND, [], None,
                         f"Unbekannter Mapping-Modus '{mode}'")


# ---------------------------------------------------------------------------
# Vorjahresstand + Verbrauchs-Berechnung (inkl. Zaehlerwechsel im Jahr)
# ---------------------------------------------------------------------------

CONSUMPTION_TOLERANCE = Decimal("0.5")


def compute_prior_and_consumption(
    meter: WaterMeter, reading_date: date | None, value: Decimal | None,
    *, billing_period: BillingPeriod | None = None,
) -> tuple[Decimal | None, str, Decimal | None, str]:
    """Liefert (prior_value, prior_label, consumption, replacement_info)."""
    if value is None or reading_date is None:
        return (None, "—", None, "")

    if (
        billing_period is not None
        and meter.installed_from is not None
        and billing_period.start_date <= meter.installed_from <= billing_period.end_date
    ):
        old_meter = (
            WaterMeter.query
            .filter_by(property_id=meter.property_id, active=False)
            .filter(WaterMeter.installed_to == meter.installed_from)
            .first()
        )
        if old_meter is not None:
            return _compute_swap_chain(meter, value, old_meter, billing_period)

    prev = previous_reading(meter, reading_date)
    if prev is not None:
        return (
            prev.value,
            prev.reading_date.strftime("%d.%m.%Y"),
            value - prev.value,
            "",
        )

    if meter.initial_value is not None:
        label = (
            f"Anfang {meter.installed_from.strftime('%d.%m.%Y')}"
            if meter.installed_from else "Anfangsstand"
        )
        return (meter.initial_value, label, value - meter.initial_value, "")

    return (None, "—", None, "")


def _compute_swap_chain(
    new_meter: WaterMeter, new_value: Decimal,
    old_meter: WaterMeter, period: BillingPeriod,
) -> tuple[Decimal | None, str, Decimal | None, str]:
    """Swap-aware Vorjahresstand + Verbrauch."""
    old_closing = MeterReading.query.filter_by(
        meter_id=old_meter.id, billing_period_id=period.id,
    ).first()
    prev_period = (
        BillingPeriod.query
        .filter(BillingPeriod.start_date < period.start_date)
        .order_by(BillingPeriod.start_date.desc())
        .first()
    )
    old_prev = (
        MeterReading.query.filter_by(
            meter_id=old_meter.id, billing_period_id=prev_period.id,
        ).first()
        if prev_period is not None else None
    )

    if old_prev is not None:
        prior_value = old_prev.value
        prior_label = (
            f"Vorjahr {prev_period.name} (Altz. {old_meter.meter_number})"
        )
    elif old_meter.initial_value is not None:
        prior_value = old_meter.initial_value
        prior_label = f"Anfang Altz. {old_meter.meter_number}"
    else:
        prior_value = None
        prior_label = f"Altz. {old_meter.meter_number} — kein Vorwert"

    new_initial = new_meter.initial_value
    new_part = (new_value - new_initial) if new_initial is not None else None
    old_part = (
        (old_closing.value - prior_value)
        if (old_closing is not None and prior_value is not None) else None
    )

    if old_part is not None and new_part is not None:
        consumption = old_part + new_part
    else:
        consumption = None

    swap_date = new_meter.installed_from.strftime("%d.%m.%Y") if new_meter.installed_from else "?"
    info_parts = [f"Zählerwechsel am {swap_date}: Altz. {old_meter.meter_number}"]
    if old_closing is not None and prior_value is not None:
        info_parts.append(
            f"({format_value_de(prior_value)} → {format_value_de(old_closing.value)}, "
            f"Verbrauch {format_value_de(old_part)})"
        )
    elif old_closing is None:
        info_parts.append("(Abschluss-Ablesung Altz. fehlt)")
    if new_part is not None:
        info_parts.append(
            f"+ Neu {new_meter.meter_number} "
            f"({format_value_de(new_initial)} → {format_value_de(new_value)}, "
            f"Verbrauch {format_value_de(new_part)})"
        )
    replacement_info = " ".join(info_parts)

    return (prior_value, prior_label, consumption, replacement_info)


def _check_mismatch(computed: Decimal | None, imported: Decimal | None) -> bool:
    """True wenn beide Werte vorliegen und sich um mehr als CONSUMPTION_TOLERANCE
    unterscheiden."""
    if computed is None or imported is None:
        return False
    return abs(computed - imported) > CONSUMPTION_TOLERANCE


# ---------------------------------------------------------------------------
# Build resolved rows
# ---------------------------------------------------------------------------

def detect_formats_for_config(df, cfg: MappingConfig) -> tuple[str, str]:
    """Liefert das tatsaechlich zu verwendende Zahlen-/Datumsformat."""
    vf = cfg.value_format
    df_ = cfg.date_format
    if vf == "auto" and cfg.col_value and cfg.col_value in df.columns:
        vf = detect_number_format(df[cfg.col_value])
    if df_ == "auto" and cfg.col_date and cfg.col_date in df.columns:
        df_ = detect_date_format(df[cfg.col_date])
    return vf or "auto", df_ or "auto"


def build_resolved_rows(df, cfg: MappingConfig) -> list[ResolvedRow]:
    if df is None or df.empty or not cfg.col_lookup or not cfg.col_value:
        return []

    value_fmt, date_fmt = detect_formats_for_config(df, cfg)
    period = (
        db.session.get(BillingPeriod, cfg.billing_period_id)
        if cfg.billing_period_id else None
    )
    fallback_date = period.end_date if period is not None else None
    rows: list[ResolvedRow] = []

    for idx, row in df.iterrows():
        row_dict = {col: row[col] for col in df.columns}
        lookup = _cell(row_dict, cfg.col_lookup)
        value_raw = _cell(row_dict, cfg.col_value)
        date_raw = row_dict.get(cfg.col_date) if cfg.col_date else None
        consumption_raw = _cell(row_dict, cfg.col_consumption) if cfg.col_consumption else ""

        parse_errors: list[str] = []

        value = parse_number(value_raw, value_fmt) if value_raw else None
        if value_raw and value is None:
            parse_errors.append(f"Wert '{value_raw}' nicht parsbar")

        imported_cons = parse_number(consumption_raw, value_fmt) if consumption_raw else None

        rdate = parse_date(date_raw, date_fmt) if date_raw is not None else None
        if cfg.col_date and date_raw is not None and rdate is None:
            d_str = _cell(row_dict, cfg.col_date)
            if d_str:
                parse_errors.append(f"Datum '{d_str}' nicht parsbar")

        if rdate is None:
            rdate = fallback_date

        resolve = resolve_meter(lookup, cfg.mode)
        candidate_ids = [m.id for m in resolve.candidates]
        chosen_id = resolve.chosen.id if resolve.chosen else None

        status = resolve.status
        message = resolve.message
        if value is None:
            status = STATUS_PARSE_ERROR
            message = "; ".join(parse_errors) or "Wert fehlt"

        prior_value: Decimal | None = None
        prior_label = "—"
        computed_cons: Decimal | None = None
        replacement_info = ""
        if resolve.chosen is not None and value is not None and rdate is not None:
            prior_value, prior_label, computed_cons, replacement_info = (
                compute_prior_and_consumption(
                    resolve.chosen, rdate, value, billing_period=period,
                )
            )

        rows.append(ResolvedRow(
            idx=int(idx) if isinstance(idx, (int, float)) else 0,
            raw_data=row_dict,
            lookup_value=lookup,
            value=value,
            reading_date=rdate,
            status=status,
            candidate_meter_ids=candidate_ids,
            chosen_meter_id=chosen_id,
            skip=False,
            message=message,
            parse_errors=parse_errors,
            prior_value=prior_value,
            prior_label=prior_label,
            computed_consumption=computed_cons,
            imported_consumption=imported_cons,
            consumption_mismatch=_check_mismatch(computed_cons, imported_cons),
            replacement_info=replacement_info,
        ))

    return rows


# ---------------------------------------------------------------------------
# Form-Edits parsen und auf baseline_rows anwenden
# ---------------------------------------------------------------------------

_RE_FORM_KEY = re.compile(r"^rows\[(\d+)\]\[(\w+)\]$")


def parse_form_edits(form, baseline_rows: list[ResolvedRow]) -> list[ResolvedRow]:
    """Mergt die User-Edits aus dem Confirm-Form auf die baseline-Liste.

    Uses ``app.imports.common.parse_row_edits`` for the generic key parsing,
    then applies meter-specific re-validation (value/date/meter_id + status
    updates) on top.
    """
    # Generic key parsing via common helper
    edits = _parse_row_edits(form)

    # Skip-Checkboxen: HTML schickt unchecked checkboxes nicht mit, also
    # explizit auf False initialisieren.
    for r in baseline_rows:
        r.skip = False

    for r in baseline_rows:
        e = edits.get(r.idx)
        if not e:
            continue

        # skip
        if "skip" in e and str(e["skip"]).lower() in ("on", "1", "true", "yes"):
            r.skip = True

        # value
        if "value" in e:
            new_raw = (e.get("value") or "").strip()
            if new_raw:
                v = parse_number(new_raw, "auto")
                if v is not None:
                    r.value = v
                    if r.status == STATUS_PARSE_ERROR:
                        r.status = STATUS_OK if r.chosen_meter_id else STATUS_NOT_FOUND
                else:
                    r.value = None
                    r.status = STATUS_PARSE_ERROR
                    r.message = f"Wert '{new_raw}' nicht parsbar"
            else:
                r.value = None
                r.status = STATUS_PARSE_ERROR
                r.message = "Wert fehlt"

        # date (HTML <input type=date> liefert immer ISO YYYY-MM-DD)
        if "date" in e:
            d_raw = (e.get("date") or "").strip()
            if d_raw:
                d = parse_date(d_raw, "iso")
                r.reading_date = d
            else:
                r.reading_date = None

        # meter_id
        if "meter_id" in e:
            mid_raw = (e.get("meter_id") or "").strip()
            try:
                r.chosen_meter_id = int(mid_raw) if mid_raw else None
            except ValueError:
                r.chosen_meter_id = None
            if r.chosen_meter_id and r.status in (STATUS_AMBIGUOUS, STATUS_OK_PREFERRED_MAIN):
                r.status = STATUS_OK

    return baseline_rows


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit_import(rows: list[ResolvedRow], user_id: int,
                  billing_period: BillingPeriod,
                  duplicate_mode: str) -> ImportStats:
    """Persistiert die resolved + edited Zeilen in die gewaehlte Abrechnungsperiode."""
    stats = ImportStats()
    affected_meters: dict[int, WaterMeter] = {}
    for row in rows:
        if row.skip:
            stats.skipped += 1
            continue
        if not row.chosen_meter_id or row.value is None:
            stats.skipped_unmapped += 1
            continue

        sp = db.session.begin_nested()
        try:
            meter = db.session.get(WaterMeter, row.chosen_meter_id)
            if meter is None:
                sp.rollback()
                stats.errors.append(
                    f"Zeile {row.idx + 2}: Zähler-ID {row.chosen_meter_id} nicht gefunden"
                )
                continue

            rdate = row.reading_date or billing_period.end_date

            existing = MeterReading.query.filter_by(
                meter_id=meter.id, billing_period_id=billing_period.id,
            ).first()

            if existing:
                if duplicate_mode == "skip":
                    sp.rollback()
                    stats.skipped_dup += 1
                    continue
                existing.value = row.value
                existing.reading_date = rdate
                existing.created_by_id = user_id
                stats.updated += 1
            else:
                db.session.add(MeterReading(
                    meter_id=meter.id,
                    billing_period_id=billing_period.id,
                    value=row.value,
                    reading_date=rdate,
                    created_by_id=user_id,
                ))
                stats.created += 1
            affected_meters[meter.id] = meter
            sp.commit()
        except Exception as e:  # pragma: no cover - defensive
            sp.rollback()
            stats.errors.append(f"Zeile {row.idx + 2}: {e}")

    db.session.flush()
    for meter in affected_meters.values():
        recompute_meter_chain(meter)

    db.session.commit()
    return stats


# ---------------------------------------------------------------------------
# Helpers fuer Templates (meter-resolution status — different from common ROW_*)
# ---------------------------------------------------------------------------

def status_row_class(status: str) -> str:
    """Maps meter-resolution STATUS_* to a Bootstrap/Tabler table-row CSS class."""
    if status == STATUS_OK:
        return "table-success"
    if status == STATUS_OK_PREFERRED_MAIN:
        return "table-success"
    if status == STATUS_AMBIGUOUS:
        return "table-warning"
    if status in (STATUS_NOT_FOUND, STATUS_PARSE_ERROR):
        return "table-danger"
    return ""


def status_badge(status: str) -> tuple[str, str]:
    """Liefert (Label, CSS-Klasse) fuer den Status-Badge (meter-resolution)."""
    if status == STATUS_OK:
        return ("OK", "bg-success text-white")
    if status == STATUS_OK_PREFERRED_MAIN:
        return ("OK (Hauptz.)", "bg-success text-white")
    if status == STATUS_AMBIGUOUS:
        return ("Mehrdeutig", "bg-warning text-dark")
    if status == STATUS_NOT_FOUND:
        return ("Nicht gemappt", "bg-danger text-white")
    if status == STATUS_PARSE_ERROR:
        return ("Parse-Fehler", "bg-danger text-white")
    return (status, "bg-secondary text-white")


# ---------------------------------------------------------------------------
# Helpers fuer Routes
# ---------------------------------------------------------------------------

def all_active_meters() -> list[WaterMeter]:
    """Alle aktiven Meter sortiert nach meter_number (fuer not_found-Dropdowns)."""
    return (
        WaterMeter.query
        .filter(WaterMeter.active.is_(True))
        .order_by(WaterMeter.meter_number.asc())
        .all()
    )


def owner_name_for(meter: WaterMeter) -> str:
    """Aktueller Besitzer-Name eines Meters (oder leerer String)."""
    names = (
        db.session.query(Customer.name)
        .join(PropertyOwnership, PropertyOwnership.customer_id == Customer.id)
        .filter(
            PropertyOwnership.property_id == meter.property_id,
            PropertyOwnership.valid_to.is_(None),
        )
        .order_by(Customer.name.asc())
        .all()
    )
    return ", ".join(n for (n,) in names)
