"""Heavy-Lifting fuer den Ablesungs-Import-Wizard.

Halt die Mapping-Resolver-, Auto-Detection- und Commit-Logik aus
``app/meters/routes.py`` raus -- die Routen-Datei darf duenn bleiben.

Konventionen:
- Reine Funktionen, kein Flask-Request-Zugriff (Form-Daten werden als Dict
  uebergeben). Nur ``db`` + Models.
- ``Decimal`` als Geld-/Mengen-Typ konsistent mit Models.
- DataFrame-Index ``i`` ist die Identitaet einer Zeile durch den ganzen
  Wizard hindurch (Pickle survived den Roundtrip ohne Re-Index).
"""
from __future__ import annotations

import os
import pickle
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from flask import current_app

from app.extensions import db
from app.models import (
    Customer, MeterReading, Property, PropertyOwnership, WaterMeter,
)


# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

MAPPING_MODES = ("meter_number", "customer_number", "customer_name")
NUMBER_FORMATS = ("auto", "at_de", "us", "plain")
DATE_FORMATS = ("auto", "iso", "de", "us", "excel_ts")
DUPLICATE_MODES = ("update", "skip")

STATUS_OK = "ok"
STATUS_OK_PREFERRED_MAIN = "ok_preferred_main"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NOT_FOUND = "not_found"
STATUS_PARSE_ERROR = "parse_error"


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class MappingConfig:
    mode: str = "meter_number"
    col_lookup: str = ""
    col_value: str = ""
    col_date: str = ""
    col_year: str = ""
    col_consumption: str = ""  # optional: vom Excel mitgelieferter Verbrauch zum Vergleich
    default_year: int = 0
    duplicate_mode: str = "update"
    value_format: str = "auto"
    date_format: str = "auto"

    @classmethod
    def from_form(cls, form, default_year_fallback: int) -> "MappingConfig":
        try:
            dy = int(form.get("default_year") or default_year_fallback)
        except (TypeError, ValueError):
            dy = default_year_fallback
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
        return cls(
            mode=mode,
            col_lookup=(form.get("col_lookup") or "").strip(),
            col_value=(form.get("col_value") or "").strip(),
            col_date=(form.get("col_date") or "").strip(),
            col_year=(form.get("col_year") or "").strip(),
            col_consumption=(form.get("col_consumption") or "").strip(),
            default_year=dy,
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
            "col_year": self.col_year,
            "col_consumption": self.col_consumption,
            "default_year": self.default_year,
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
    year: int | None
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
# Pickle-Persistenz fuer das hochgeladene DataFrame
# ---------------------------------------------------------------------------

def _instance_dir() -> str:
    return current_app.instance_path


def save_dataframe(df: pd.DataFrame) -> str:
    """Schreibt das DataFrame als Pickle ins instance/-Verzeichnis und gibt
    den absoluten Pfad zurueck. Der Caller speichert den Pfad in der
    Session.
    """
    os.makedirs(_instance_dir(), exist_ok=True)
    fname = f"meter_import_{uuid.uuid4().hex}.pkl"
    path = os.path.join(_instance_dir(), fname)
    df.to_pickle(path)
    return path


def load_dataframe(path: str) -> pd.DataFrame | None:
    import pandas as pd
    if not path or not os.path.exists(path):
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def delete_dataframe(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Format-Detection (pro Spalte)
# ---------------------------------------------------------------------------

_RE_PURE_INT = re.compile(r"^-?\d+$")
_RE_NUM_AT_DE_END = re.compile(r",\d{1,3}$")
_RE_NUM_US_END = re.compile(r"\.\d{1,3}$")
_RE_NUM_HAS_DOT_3 = re.compile(r"\.\d{3}$")
_RE_DATE_ISO = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_RE_DATE_DEUS = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$")


def _series_strings(series: pd.Series, limit: int | None = None) -> list[str]:
    import pandas as pd
    out: list[str] = []
    for v in series:
        if v is None:
            continue
        if isinstance(v, float) and pd.isna(v):
            continue
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            continue
        out.append(s)
        if limit is not None and len(out) >= limit:
            break
    return out


def detect_number_format(series: pd.Series) -> str:
    """Liefert 'at_de' | 'us' | 'plain' | 'unknown'.

    Heuristik pro Spalte (mehrheits-basiert): wenn alle Werte bloss Ziffern
    sind, ist 'plain' der sichere Default. Bei Mix aus ',' und '.' wird die
    Position des letzten Trennzeichens entscheidend (rechts steht das
    Dezimaltrennzeichen).
    """
    samples = _series_strings(series)
    if not samples:
        return "unknown"
    if all(_RE_PURE_INT.match(s) for s in samples):
        return "plain"
    has_comma = sum(1 for s in samples if "," in s)
    has_dot = sum(1 for s in samples if "." in s)
    if has_comma and has_dot:
        at_de_votes = sum(1 for s in samples if _RE_NUM_AT_DE_END.search(s))
        us_votes = sum(1 for s in samples if _RE_NUM_US_END.search(s))
        if at_de_votes >= us_votes:
            return "at_de"
        return "us"
    if has_comma:
        return "at_de"
    if has_dot:
        # Punkt nur: Tausenderpunkt (3 Stellen rechts) vs. Dezimalpunkt
        if any(_RE_NUM_HAS_DOT_3.search(s) for s in samples):
            return "at_de"
        return "plain"
    return "unknown"


def detect_date_format(series: pd.Series) -> str:
    """Liefert 'excel_ts' | 'iso' | 'de' | 'us' | 'unknown'.

    'excel_ts' = pandas hat die Spalte als Timestamp eingelesen
    (passiert bei nativen Excel-Datumszellen, auch mit dtype=str).
    """
    import pandas as pd
    # 1. Echte Timestamp-Objekte?
    for v in series:
        if isinstance(v, pd.Timestamp):
            return "excel_ts"
        if isinstance(v, (date, datetime)):
            return "excel_ts"

    samples = _series_strings(series, limit=10)
    if not samples:
        return "unknown"
    if all(_RE_DATE_ISO.match(s) for s in samples):
        return "iso"

    de_votes = us_votes = 0
    matched = 0
    for s in samples:
        m = _RE_DATE_DEUS.match(s)
        if not m:
            continue
        matched += 1
        a, b, _ = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12 and b <= 12:
            de_votes += 1
        elif b > 12 and a <= 12:
            us_votes += 1
    if matched and matched == len(samples):
        if us_votes > de_votes:
            return "us"
        # bei Tie oder de-Mehrheit lokal AT-Default
        return "de"
    return "unknown"


# ---------------------------------------------------------------------------
# Per-Zellen-Parser
# ---------------------------------------------------------------------------

def _cell(row: dict, col: str) -> str:
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


def parse_number(raw: str, fmt: str) -> Decimal | None:
    """Parst eine einzelne Zahl gemaess vorgegebenem Format.

    'auto' versucht alle Formate in dieser Reihenfolge: at_de, us, plain.
    Gibt None zurueck wenn kein Parsing klappt -- der Caller markiert die
    Zeile dann als parse_error.
    """
    if not raw:
        return None
    s = raw.strip().replace(" ", "")
    if not s or s.lower() in ("nan", "none"):
        return None

    if fmt == "plain":
        try:
            return Decimal(s.replace(",", "."))
        except InvalidOperation:
            return None

    if fmt == "at_de":
        # Punkt = Tausender, Komma = Dezimal
        cleaned = s.replace(".", "").replace(",", ".") if ("," in s) else s.replace(".", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    if fmt == "us":
        cleaned = s.replace(",", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    # auto / unknown: probiere durch
    for try_fmt in ("at_de", "us", "plain"):
        v = parse_number(s, try_fmt)
        if v is not None:
            return v
    return None


def parse_date(raw: Any, fmt: str) -> date | None:
    """Parst ein einzelnes Datum gemaess vorgegebenem Format.

    Akzeptiert auch direkt pd.Timestamp / date / datetime fuer
    'excel_ts'-Spalten -- in dem Fall ignorieren wir 'fmt' und konvertieren
    direkt.
    """
    import pandas as pd
    if raw is None:
        return None
    if isinstance(raw, pd.Timestamp):
        return raw.to_pydatetime().date()
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, float) and pd.isna(raw):
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None

    if fmt == "iso":
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None
    if fmt == "de":
        for f in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(s, f).date()
            except ValueError:
                continue
        return None
    if fmt == "us":
        for f in ("%m/%d/%Y", "%m/%d/%y", "%m.%d.%Y"):
            try:
                return datetime.strptime(s, f).date()
            except ValueError:
                continue
        return None
    if fmt == "excel_ts":
        # Sollte oben schon behandelt sein, aber als Fallback:
        try:
            return pd.to_datetime(s, dayfirst=True, errors="coerce").date()
        except (ValueError, AttributeError):
            return None

    # auto / unknown: probiere ISO, DE, US, dann pandas mit dayfirst=True
    for try_fmt in ("iso", "de", "us"):
        d = parse_date(s, try_fmt)
        if d is not None:
            return d
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
    except (ValueError, AttributeError):
        return None


def parse_year(raw: Any, default_year: int) -> int | None:
    import pandas as pd
    if raw is None:
        return default_year if default_year else None
    if isinstance(raw, float) and pd.isna(raw):
        return default_year if default_year else None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return default_year if default_year else None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default_year if default_year else None


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
        # 'main' kommt vor 'sub' im desc-Sort (m > s lexikographisch falsch -- ok,
        # wir sortieren explizit unten in _classify, das hier ist nur Default-Order)
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

# Toleranz fuer den Vergleich "berechneter vs. importierter Verbrauch".
# Excel-Eingaben mit gerundeten Vorjahres-Werten weichen oft um <1 m^3 ab,
# sind aber materiell identisch -- erst groessere Diskrepanzen sind eine
# echte Warnung wert.
CONSUMPTION_TOLERANCE = Decimal("0.5")


def _find_predecessor(meter: WaterMeter) -> WaterMeter | None:
    """Sucht den Vorgaenger eines Meters: gleiche property_id, sein
    installed_to == unserem installed_from. Spiegelt _build_replacement_map
    in routes.py.
    """
    if not meter.installed_from:
        return None
    return (
        WaterMeter.query
        .filter(
            WaterMeter.property_id == meter.property_id,
            WaterMeter.installed_to == meter.installed_from,
            WaterMeter.id != meter.id,
        )
        .first()
    )


def compute_prior_and_consumption(
    meter: WaterMeter, year: int, value: Decimal | None,
) -> tuple[Decimal | None, str, Decimal | None, str]:
    """Liefert (prior_value, prior_label, consumption, replacement_info).

    Standardfall: prior_value = Vorjahres-Reading; consumption = value - prior.

    Bei Zaehlerwechsel im selben Jahr (meter wurde in `year` installiert,
    Vorgaenger via property_id + installed_to-Match): consumption ist die
    SUMME aus dem Verbrauch dieses Meters seit Einbau plus dem Verbrauch
    des Vorgaengers von Jahresbeginn bis zum Wechseldatum -- so wie der
    Customer das im Excel typischerweise als Jahres-Total sieht.
    Der replacement_info-String erklaert die Aufteilung im UI.

    Wenn kein Vergleichswert ermittelbar ist (neuer Meter, kein initial,
    kein Vorgaenger): consumption = None.
    """
    if value is None:
        return (None, "—", None, "")

    # 1) Vorjahres-Reading desselben Meters?
    prev = MeterReading.query.filter_by(meter_id=meter.id, year=year - 1).first()
    if prev is not None:
        return (prev.value, str(year - 1), value - prev.value, "")

    # 2) Wechsel im aktuellen Jahr -> Vorgaenger einbeziehen
    pred = None
    if meter.installed_from and meter.installed_from.year == year:
        pred = _find_predecessor(meter)

    # Wert, von dem aus diesem Meter weg gerechnet wird:
    init = meter.initial_value if meter.initial_value is not None else None

    if pred:
        # Verbrauch dieses Meters seit Einbau:
        this_meter_cons = (value - init) if init is not None else None

        # Verbrauch des Vorgaengers im laufenden Jahr (Vorjahresende -> Wechsel)
        pred_year_reading = MeterReading.query.filter_by(
            meter_id=pred.id, year=year,
        ).first()
        pred_prev_reading = MeterReading.query.filter_by(
            meter_id=pred.id, year=year - 1,
        ).first()
        pred_cons = None
        if pred_year_reading is not None:
            if pred_prev_reading is not None:
                pred_cons = pred_year_reading.value - pred_prev_reading.value
            elif pred.initial_value is not None:
                pred_cons = pred_year_reading.value - pred.initial_value

        total = None
        if this_meter_cons is not None and pred_cons is not None:
            total = this_meter_cons + pred_cons
        elif this_meter_cons is not None:
            total = this_meter_cons  # ohne Vorgaenger-Anteil

        info_parts = [
            f"Wechsel von {pred.meter_number} am "
            f"{meter.installed_from.strftime('%d.%m.%Y')}"
        ]
        if pred_cons is not None:
            info_parts.append(f"Vorgaenger-Verbrauch: {pred_cons} m³")
        else:
            info_parts.append("Vorgaenger-Verbrauch unbekannt (Abschluss-Ablesung fehlt)")
        return (
            init,
            f"Anfang {meter.installed_from.strftime('%d.%m.%Y')}",
            total,
            "; ".join(info_parts),
        )

    # 3) Kein Vorjahres-Reading, kein Wechsel -> initial_value Fallback
    if init is not None:
        label = (
            f"Anfang {meter.installed_from.strftime('%d.%m.%Y')}"
            if meter.installed_from else "Anfangsstand"
        )
        return (init, label, value - init, "")

    return (None, "—", None, "")


def _check_mismatch(computed: Decimal | None, imported: Decimal | None) -> bool:
    """True wenn beide Werte vorliegen und sich um mehr als CONSUMPTION_TOLERANCE
    unterscheiden. Ohne import-Wert oder ohne berechneten Wert keine Warnung.
    """
    if computed is None or imported is None:
        return False
    return abs(computed - imported) > CONSUMPTION_TOLERANCE


# ---------------------------------------------------------------------------
# Build resolved rows
# ---------------------------------------------------------------------------

def detect_formats_for_config(df: pd.DataFrame, cfg: MappingConfig) -> tuple[str, str]:
    """Liefert das tatsaechlich zu verwendende Zahlen-/Datumsformat.

    Wenn der User 'auto' gewaehlt hat, wird die Spalten-Heuristik angewendet.
    Sonst wird das vom User explizit gewaehlte Format zurueckgegeben.
    """
    vf = cfg.value_format
    df_ = cfg.date_format
    if vf == "auto" and cfg.col_value and cfg.col_value in df.columns:
        vf = detect_number_format(df[cfg.col_value])
    if df_ == "auto" and cfg.col_date and cfg.col_date in df.columns:
        df_ = detect_date_format(df[cfg.col_date])
    return vf or "auto", df_ or "auto"


def build_resolved_rows(df: pd.DataFrame, cfg: MappingConfig) -> list[ResolvedRow]:
    if df is None or df.empty or not cfg.col_lookup or not cfg.col_value:
        return []

    value_fmt, date_fmt = detect_formats_for_config(df, cfg)
    rows: list[ResolvedRow] = []

    for idx, row in df.iterrows():
        row_dict = {col: row[col] for col in df.columns}
        lookup = _cell(row_dict, cfg.col_lookup)
        value_raw = _cell(row_dict, cfg.col_value)
        date_raw = row_dict.get(cfg.col_date) if cfg.col_date else None
        year_raw = row_dict.get(cfg.col_year) if cfg.col_year else None
        consumption_raw = _cell(row_dict, cfg.col_consumption) if cfg.col_consumption else ""

        parse_errors: list[str] = []

        value = parse_number(value_raw, value_fmt) if value_raw else None
        if value_raw and value is None:
            parse_errors.append(f"Wert '{value_raw}' nicht parsbar")

        # Importierter Verbrauch: gleiches Format wie Wert, da er typischerweise
        # in derselben Spalten-Gruppe steht (DE/AT-Komma).
        imported_cons = parse_number(consumption_raw, value_fmt) if consumption_raw else None

        rdate = parse_date(date_raw, date_fmt) if date_raw is not None else None
        if cfg.col_date and date_raw is not None and rdate is None:
            d_str = _cell(row_dict, cfg.col_date)
            if d_str:
                parse_errors.append(f"Datum '{d_str}' nicht parsbar")

        year = parse_year(year_raw, cfg.default_year)

        # Fallback: wenn kein Datum geparst, aber Jahr da, dann 31.12. des Jahres
        if rdate is None and year:
            rdate = date(year, 12, 31)

        # Mapping-Resolve
        resolve = resolve_meter(lookup, cfg.mode)
        candidate_ids = [m.id for m in resolve.candidates]
        chosen_id = resolve.chosen.id if resolve.chosen else None

        # Status: parse_errors uebersteuern Mapping-Status (Wert/Datum kaputt
        # ist wichtiger zu signalisieren als Mapping-OK).
        status = resolve.status
        message = resolve.message
        if value is None:
            status = STATUS_PARSE_ERROR
            message = "; ".join(parse_errors) or "Wert fehlt"

        # Vorjahresstand + Verbrauch berechnen (nur wenn wir einen Meter UND
        # einen Wert UND ein Jahr haben).
        prior_value: Decimal | None = None
        prior_label = "—"
        computed_cons: Decimal | None = None
        replacement_info = ""
        if resolve.chosen is not None and value is not None and year:
            prior_value, prior_label, computed_cons, replacement_info = (
                compute_prior_and_consumption(resolve.chosen, year, value)
            )

        rows.append(ResolvedRow(
            idx=int(idx) if isinstance(idx, (int, float)) else 0,
            raw_data=row_dict,
            lookup_value=lookup,
            value=value,
            reading_date=rdate,
            year=year,
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

    baseline_rows kommt aus build_resolved_rows() und ist bereits resolved
    (Mapping + Auto-Parse). Fuer jede Zeile wird die User-Eingabe (Wert,
    Datum, Jahr, Ziel-Meter, Skip) re-validiert und ueberschreibt die
    Vorbelegung.
    """
    edits: dict[int, dict[str, str]] = {}
    for key in form.keys():
        m = _RE_FORM_KEY.match(key)
        if not m:
            continue
        ridx = int(m.group(1))
        field_name = m.group(2)
        edits.setdefault(ridx, {})[field_name] = form.get(key, "")

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
                # User-Edits sind frei eingegeben -- 'auto' ist tolerant.
                v = parse_number(new_raw, "auto")
                if v is not None:
                    r.value = v
                    if r.status == STATUS_PARSE_ERROR:
                        # Wenn der User den Wert repariert hat, Status neu setzen.
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

        # year
        if "year" in e:
            y_raw = (e.get("year") or "").strip()
            try:
                r.year = int(y_raw) if y_raw else r.year
            except ValueError:
                pass

        # meter_id
        if "meter_id" in e:
            mid_raw = (e.get("meter_id") or "").strip()
            try:
                r.chosen_meter_id = int(mid_raw) if mid_raw else None
            except ValueError:
                r.chosen_meter_id = None
            # Wenn User explizit einen Meter waehlt, "ambiguous" -> "ok"
            if r.chosen_meter_id and r.status in (STATUS_AMBIGUOUS, STATUS_OK_PREFERRED_MAIN):
                r.status = STATUS_OK

    return baseline_rows


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit_import(rows: list[ResolvedRow], user_id: int,
                  duplicate_mode: str) -> ImportStats:
    """Persistiert die resolved + edited Zeilen.

    Pro Zeile ein Savepoint -- ein Fehler einer Zeile rollt nur diese eine
    zurueck. Am Ende ein einziger commit auf den outer-trans.
    """
    stats = ImportStats()
    for row in rows:
        if row.skip:
            stats.skipped += 1
            continue
        if not row.chosen_meter_id or row.value is None or not row.year:
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

            existing = MeterReading.query.filter_by(
                meter_id=meter.id, year=row.year,
            ).first()

            # Per-Meter-Verbrauch (was in die DB-Spalte geht). Die
            # Vorschau-Total-Berechnung mit Vorgaenger-Anteil ist nur
            # Anzeige -- DB-Konsistenz mit save_reading bleibt: ein
            # MeterReading kennt nur seinen eigenen Verbrauchs-Delta.
            prev = MeterReading.query.filter_by(
                meter_id=meter.id, year=row.year - 1,
            ).first()
            if prev is not None:
                consumption = row.value - prev.value
            elif meter.initial_value is not None:
                consumption = row.value - meter.initial_value
            else:
                consumption = None

            rdate = row.reading_date or date(row.year, 12, 31)

            if existing:
                if duplicate_mode == "skip":
                    sp.rollback()
                    stats.skipped_dup += 1
                    continue
                existing.value = row.value
                existing.reading_date = rdate
                existing.consumption = consumption
                existing.created_by_id = user_id
                stats.updated += 1
            else:
                db.session.add(MeterReading(
                    meter_id=meter.id,
                    year=row.year,
                    value=row.value,
                    reading_date=rdate,
                    consumption=consumption,
                    created_by_id=user_id,
                ))
                stats.created += 1
            sp.commit()
        except Exception as e:  # pragma: no cover - defensive
            sp.rollback()
            stats.errors.append(f"Zeile {row.idx + 2}: {e}")

    db.session.commit()
    return stats


# ---------------------------------------------------------------------------
# Helpers fuer Templates
# ---------------------------------------------------------------------------

def status_row_class(status: str) -> str:
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
    """Liefert (Label, CSS-Klasse) fuer den Status-Badge."""
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


def format_value_de(v: Decimal | None) -> str:
    """Format Decimal im DE-Stil (Komma als Dezimal) fuer das Vorschau-Input."""
    if v is None:
        return ""
    s = format(v.normalize(), "f") if isinstance(v, Decimal) else str(v)
    # Decimal "100" -> "100", "100.5" -> "100.5"; jetzt . -> ,
    return s.replace(".", ",")


def all_active_meters() -> list[WaterMeter]:
    """Fuer den Fall, dass eine Zeile als 'not_found' steht und der User
    manuell einen Meter waehlen koennen soll. Liefert alle aktiven Meter
    sortiert nach meter_number.
    """
    return (
        WaterMeter.query
        .filter(WaterMeter.active.is_(True))
        .order_by(WaterMeter.meter_number.asc())
        .all()
    )


def owner_name_for(meter: WaterMeter) -> str:
    """Aktueller Besitzer-Name eines Meters (oder leerer String).

    Properties koennen mehrere parallele aktive Eigentuemer haben (Ehepaare,
    Erbengemeinschaften) -- in dem Fall mit ', ' gejoint zurueck.
    """
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
