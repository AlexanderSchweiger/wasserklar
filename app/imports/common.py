"""Shared helpers for all per-entity import wizards.

Extracted from ``app/meters/import_service.py`` so that customers, properties
and meters importers can all reuse them without duplication.

Conventions:
- pandas is imported LOCALLY inside each function that needs it (never at
  module level) to keep the import-time cost low.
- No Flask-request access.  ``current_app`` is only used for ``instance_path``
  inside the save/load/delete helpers.
- All user-facing strings (warnings, errors) are in German; identifiers and
  comments are in English.
"""
from __future__ import annotations

import os
import pickle
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from flask import current_app


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUMBER_FORMATS = ("auto", "at_de", "us", "plain")
DATE_FORMATS = ("auto", "iso", "de", "us", "excel_ts")
DUPLICATE_MODES = ("update", "skip")

# Per-entity row statuses (used by customers / properties / meters importers).
# The meter-reading importer uses its own separate STATUS_* set.
ROW_NEW = "new"
ROW_UPDATE = "update"
ROW_EXISTS = "exists"
ROW_ERROR = "error"


# ---------------------------------------------------------------------------
# Per-entity status helpers
# ---------------------------------------------------------------------------

def status_row_class(status: str) -> str:
    """Maps a ROW_* status to a Bootstrap/Tabler table-row CSS class."""
    if status == ROW_NEW:
        return "table-success"
    if status == ROW_UPDATE:
        return "table-info"
    if status == ROW_EXISTS:
        return ""
    if status == ROW_ERROR:
        return "table-danger"
    return ""


def status_badge(status: str) -> tuple[str, str]:
    """Returns (label, css_classes) for a status badge.

    Badge convention from CLAUDE.md: solid colours use ``text-white`` (or
    ``text-dark`` for light backgrounds); soft/lt variants need nothing extra.
    NEVER use ``text-white-lt``.
    """
    if status == ROW_NEW:
        return ("Neu", "bg-success text-white")
    if status == ROW_UPDATE:
        return ("Aktualisieren", "bg-info text-white")
    if status == ROW_EXISTS:
        return ("Bereits vorhanden", "bg-secondary-lt")
    if status == ROW_ERROR:
        return ("Fehler", "bg-danger text-white")
    return (status, "bg-secondary text-white")


# ---------------------------------------------------------------------------
# Dataclasses (contracts for all per-entity importers)
# ---------------------------------------------------------------------------

@dataclass
class PreviewRow:
    """One row in the import preview table."""
    idx: int                                          # 0-based DataFrame index; file line = idx+2
    status: str = ROW_NEW                             # one of ROW_*
    fields: dict = field(default_factory=dict)        # mapped, editable values as strings (form fields)
    warnings: list = field(default_factory=list)      # non-blocking integrity warnings (list[str])
    message: str = ""                                 # detail text (e.g. reason for ROW_ERROR)
    skip: bool = False
    raw: dict = field(default_factory=dict)           # original row dict for tooltip


@dataclass
class ImportStats:
    """Aggregate counters returned from a commit run."""
    created: int = 0
    updated: int = 0
    skipped: int = 0            # ROW_EXISTS in skip-mode OR skip checkbox ticked
    skipped_error: int = 0      # rows with ROW_ERROR (always skipped)
    warnings: int = 0           # number of rows that carried warnings
    errors: list = field(default_factory=list)  # list[str] — per-row error messages

    def to_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "skipped_error": self.skipped_error,
            "warnings": self.warnings,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Regex constants (shared with meters/import_service via re-export there)
# ---------------------------------------------------------------------------

_RE_PURE_INT = re.compile(r"^-?\d+$")
_RE_NUM_AT_DE_END = re.compile(r",\d{1,3}$")
_RE_NUM_US_END = re.compile(r"\.\d{1,3}$")
_RE_NUM_HAS_DOT_3 = re.compile(r"\.\d{3}$")
_RE_DATE_ISO = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?$"
)
_RE_DATE_DEUS = re.compile(
    r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?$"
)
_RE_TRAILING_TIME = re.compile(r"[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?$")


# ---------------------------------------------------------------------------
# DataFrame IO
# ---------------------------------------------------------------------------

def read_table(file_storage) -> "pd.DataFrame":
    """Read a CSV or Excel file from a Werkzeug FileStorage into a DataFrame.

    Auto-detects the CSV separator via ``sep=None, engine="python"``.
    Strips column names and string cell values after reading.

    Raises ``ValueError`` for unsupported file extensions.
    """
    import pandas as pd

    filename = (file_storage.filename or "").lower()
    if filename.endswith(".csv"):
        df = pd.read_csv(file_storage, dtype=str, sep=None, engine="python")
    elif filename.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_storage, dtype=str)
    else:
        raise ValueError(
            f"Nicht unterstütztes Dateiformat: '{file_storage.filename}'. "
            "Bitte CSV oder Excel hochladen."
        )
    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]
    # Strip all string cells
    df = df.apply(lambda col: col.map(lambda x: x.strip() if isinstance(x, str) else x))
    return df


def save_dataframe(df, prefix: str = "import_") -> str:
    """Pickle the DataFrame into the Flask instance directory.

    Returns the absolute file path (store it in the session).
    """
    instance_dir = current_app.instance_path
    os.makedirs(instance_dir, exist_ok=True)
    fname = f"{prefix}{uuid.uuid4().hex}.pkl"
    path = os.path.join(instance_dir, fname)
    df.to_pickle(path)
    return path


def load_dataframe(path: str) -> "pd.DataFrame | None":
    """Load a previously pickled DataFrame, or return None on any error."""
    import pandas as pd
    if not path or not os.path.exists(path):
        return None
    try:
        return pd.read_pickle(path)
    except Exception:
        return None


def delete_dataframe(path: str) -> None:
    """Remove a pickled DataFrame file; silently ignores missing files."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Series helpers
# ---------------------------------------------------------------------------

def _series_strings(series, limit: int | None = None) -> list[str]:
    """Return non-empty, non-NaN string values from a pandas Series."""
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


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_number_format(series) -> str:
    """Return ``'at_de'`` | ``'us'`` | ``'plain'`` | ``'unknown'``.

    Heuristic: majority-vote over non-empty samples from the column.
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
        if any(_RE_NUM_HAS_DOT_3.search(s) for s in samples):
            return "at_de"
        return "plain"
    return "unknown"


def detect_date_format(series) -> str:
    """Return ``'excel_ts'`` | ``'iso'`` | ``'de'`` | ``'us'`` | ``'unknown'``."""
    import pandas as pd
    # 1. Real Timestamp objects (native Excel date cells)
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
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            de_votes += 1
        elif b > 12 and a <= 12:
            us_votes += 1
    if matched and matched == len(samples):
        if us_votes > de_votes:
            return "us"
        return "de"
    return "unknown"


# ---------------------------------------------------------------------------
# Cell / value parsers
# ---------------------------------------------------------------------------

def _cell(row: dict, col: str) -> str:
    """Return a clean string for a single cell value; empty string if missing/NaN."""
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
    """Parse a single number string according to ``fmt``.

    ``'auto'`` tries ``at_de``, ``us``, ``plain`` in order.
    Returns ``None`` if nothing parses.
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
        cleaned = s.replace(".", "").replace(",", ".") if "," in s else s.replace(".", "")
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

    # auto / unknown: try through
    for try_fmt in ("at_de", "us", "plain"):
        v = parse_number(s, try_fmt)
        if v is not None:
            return v
    return None


def parse_date(raw: Any, fmt: str) -> date | None:
    """Parse a single date value according to ``fmt``.

    Accepts ``pd.Timestamp`` / ``date`` / ``datetime`` directly (e.g. native
    Excel date cells with ``dtype=str``).
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

    # Excel date cells arrive as 'YYYY-MM-DD HH:MM:SS' with dtype=str;
    # strip the time component before parsing.
    s = _RE_TRAILING_TIME.sub("", s).strip()

    if fmt == "iso":
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None
    if fmt == "de":
        for f in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y",
                  "%d-%m-%Y", "%d-%m-%y"):
            try:
                return datetime.strptime(s, f).date()
            except ValueError:
                continue
        return None
    if fmt == "us":
        for f in ("%m/%d/%Y", "%m/%d/%y", "%m.%d.%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(s, f).date()
            except ValueError:
                continue
        return None
    if fmt == "excel_ts":
        try:
            return pd.to_datetime(s, dayfirst=True, errors="coerce").date()
        except (ValueError, AttributeError):
            return None

    # auto / unknown: try ISO, DE, US, then pandas with dayfirst
    for try_fmt in ("iso", "de", "us"):
        d = parse_date(s, try_fmt)
        if d is not None:
            return d
    # Excel serial date (cell formatted as number instead of date)
    if re.fullmatch(r"\d{4,6}(\.0+)?", s):
        try:
            serial = int(float(s))
            if 20000 <= serial <= 80000:  # ~1954 to ~2089
                return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
        except (ValueError, OverflowError):
            pass
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Value formatter
# ---------------------------------------------------------------------------

def format_value_de(v: Decimal | None) -> str:
    """Format a Decimal in German style (comma as decimal separator)."""
    if v is None:
        return ""
    s = format(v.normalize(), "f") if isinstance(v, Decimal) else str(v)
    return s.replace(".", ",")


# ---------------------------------------------------------------------------
# Form-key parser
# ---------------------------------------------------------------------------

_RE_ROW_EDIT_KEY = re.compile(r"^rows\[(\d+)\]\[(\w+)\]$")


def parse_row_edits(form) -> dict[int, dict[str, str]]:
    """Parse Werkzeug form keys of the form ``rows[N][field]`` into a nested dict.

    Returns ``{row_idx: {field_name: value, ...}, ...}``.
    This is the generic key-parsing part, extracted from
    ``meters/import_service.parse_form_edits``.
    """
    edits: dict[int, dict[str, str]] = {}
    for key in form.keys():
        m = _RE_ROW_EDIT_KEY.match(key)
        if not m:
            continue
        ridx = int(m.group(1))
        field_name = m.group(2)
        edits.setdefault(ridx, {})[field_name] = form.get(key, "")
    return edits


# ---------------------------------------------------------------------------
# Column suggestion
# ---------------------------------------------------------------------------

def suggest_column(columns: list[str], hints: list[str]) -> str:
    """Return the first column whose normalised name matches a hint.

    Normalisation: ``strip().lower()``.  A match is either an exact equality
    or a substring containment (hint contained in column name).

    Returns ``""`` if nothing matches.
    """
    for col in columns:
        norm = col.strip().lower()
        for hint in hints:
            if norm == hint or hint in norm:
                return col
    return ""
