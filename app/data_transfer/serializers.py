"""Type-stabile (De-)Serialisierung von SQLAlchemy-Models nach/von dict.

Decimals werden als String exportiert (kein Praezisionsverlust durch JSON-
Number), Daten als ISO-8601, Datetime als UTC-ISO. Das ueberlebt den
Roundtrip ueber alle drei Dialekte (SQLite/MariaDB/Postgres) ohne Drift.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.types import (
    Boolean, Date, DateTime, Integer, Numeric, String, Text,
)

from app.data_transfer.registry import is_excluded_setting


def encode_value(value):
    """Python-Wert -> JSON-vertraeglicher Wert."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        # Auf UTC normalisieren falls naive (utcnow() liefert naive)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value  # int, str, bool, float


def decode_value(column, raw):
    """JSON-Wert -> Python-Typ passend zur Column."""
    if raw is None:
        return None
    col_type = column.type
    if isinstance(col_type, Numeric):
        # Decimal-String oder Number → Decimal
        return Decimal(str(raw))
    if isinstance(col_type, DateTime):
        # ISO-String mit "Z" oder "+00:00"
        s = str(raw)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # In naive-UTC zurueck (Models nutzen datetime.utcnow())
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(col_type, Date):
        return date.fromisoformat(str(raw))
    if isinstance(col_type, Boolean):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.lower() in ("true", "1", "yes")
        return bool(raw)
    if isinstance(col_type, Integer):
        return int(raw)
    if isinstance(col_type, (String, Text)):
        return str(raw)
    return raw


def model_columns(model):
    """Liefert die persistenten Columns eines Models in deklarierter Reihenfolge."""
    return list(model.__table__.columns)


def appsetting_skip_filter(instance) -> bool:
    """AppSetting-Filter: schliesst instance-spezifische Secrets aus."""
    return is_excluded_setting(instance.key)


def deserialize_record(model, record: dict, *, skip_columns=None) -> dict:
    """JSON-dict -> Python-dict mit korrekt typisierten Werten.

    skip_columns: Iterable von Spaltennamen, deren Wert auf None gesetzt wird
    (fuer deferred FK-Updates).
    """
    skip = set(skip_columns or [])
    cols = {c.name: c for c in model_columns(model)}
    out = {}
    for col_name, col in cols.items():
        if col_name in skip:
            out[col_name] = None
            continue
        if col_name in record:
            out[col_name] = decode_value(col, record[col_name])
        # else: Spalte fehlt im Export (alteres Schema) → ueberspringen,
        # SA setzt Default oder NULL
    return out


def primary_key_columns(model):
    """Liefert die Namen der Primaerschluessel-Spalten."""
    return [c.name for c in model.__table__.primary_key.columns]


def primary_key_value(model, record: dict):
    """Primaerschluessel-Wert eines Records (Tuple bei Composite, sonst skalar)."""
    pks = primary_key_columns(model)
    if len(pks) == 1:
        return record.get(pks[0])
    return tuple(record.get(p) for p in pks)
