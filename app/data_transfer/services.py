"""Export-/Import-Services fuer das Daten-Transfer-Feature.

Format: ZIP mit ``manifest.json`` + einer JSON-Datei pro Tabelle (+ optional
``pdfs/``-Ordner). Decimal/Date/DateTime werden type-stabil als String
serialisiert, damit der Roundtrip ueber SQLite/MariaDB/Postgres unveraendert
ueberlebt.

Kontrakte:
- Vollersatz: TRUNCATE der Tabellen der gewaehlten Kategorien (in reverser
  FK-Reihenfolge), dann Insert mit Original-IDs, dann Postgres-Sequence-Reset.
- Merge: Neue IDs vergeben, FKs ueber alt→neu-Mapping remappen, Duplikate
  per natuerlichem Schluessel finden und entweder skippen oder updaten.
- Beide Modi laufen in einer einzigen DB-Transaktion (Rollback bei jedem
  Fehler, kein Partial-State).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from flask import current_app
from sqlalchemy import and_, func, inspect as sa_inspect, or_, text

from app.extensions import db
from app.models import (
    AppSetting, Booking, BookingGroup, Customer, CustomerCounter,
    DunningNotice, FiscalYear, Invoice, InvoiceCounter, InvoiceItem,
)
from app.data_transfer.registry import (
    CATEGORIES, INSERT_ORDER, YEAR_FILTERS, NATURAL_KEYS, FOREIGN_KEYS,
    DEFERRED_FK_UPDATES, EXCLUDED_TABLES, models_for_selection,
    is_excluded_setting,
)
from app.data_transfer.serializers import (
    appsetting_skip_filter, decode_value, deserialize_record, encode_value,
    model_columns, primary_key_columns, primary_key_value,
)
from app.__version__ import __version__ as APP_VERSION


FORMAT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _current_alembic_revision() -> str | None:
    """Aktuelle Alembic-Revision der laufenden DB."""
    try:
        with db.engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
            return row[0] if row else None
    except Exception:
        return None


def _detect_variant() -> str:
    """oss / saas — anhand registrierter Blueprints."""
    try:
        if "tenant" in current_app.blueprints or "saas_main" in current_app.blueprints:
            return "saas"
        # SaaS ergaenzt registrierte Blueprints wie "bank_import", "export", "files".
        if any(bp in current_app.blueprints for bp in ("bank_import", "files")):
            return "saas"
    except RuntimeError:
        pass
    return "oss"


def _apply_year_filter(model, query, years: list[int]):
    """Filter eine Query auf die gewaehlten Jahre, falls das Model ein
    Jahresfeld hat. Wenn nicht, wird die Query unveraendert zurueckgegeben."""
    if not years:
        return query
    spec = YEAR_FILTERS.get(model)
    if spec is None:
        return query
    # Direkter Spaltenname
    if isinstance(spec, str):
        col = getattr(model, spec)
        return query.filter(col.in_(years))
    # Tuple: ("date", "period_year") → period_year bevorzugen, sonst date-Jahr
    # Tuple: ("date_year",) → year aus date-Spalte extrahieren
    if isinstance(spec, tuple):
        if spec == ("date_year",):
            return query.filter(func.extract("year", model.date).in_(years))
        # Invoice-Pattern: period_year bevorzugen, fallback Jahr aus date
        if spec == ("date", "period_year"):
            return query.filter(
                or_(
                    model.period_year.in_(years),
                    and_(
                        model.period_year.is_(None),
                        func.extract("year", model.date).in_(years),
                    ),
                )
            )
    return query


def _filtered_invoice_ids(years: list[int]) -> set:
    """IDs aller Invoices, die der Jahresfilter behaelt — fuer FK-Kaskade auf
    InvoiceItem, OpenItem, BookingGroup, DunningNotice, Booking."""
    q = db.session.query(Invoice.id)
    q = _apply_year_filter(Invoice, q, years)
    return {r[0] for r in q.all()}


def _filtered_booking_group_ids(years: list[int]) -> set:
    """IDs aller BookingGroups, die der Jahresfilter behaelt (ueber Booking.date)."""
    if not years:
        return None  # type: ignore
    sub = db.session.query(Booking.group_id).filter(
        Booking.group_id.is_not(None),
        func.extract("year", Booking.date).in_(years),
    )
    return {r[0] for r in sub.all()}


def _build_query_filtered(model, years: list[int], invoice_ids: set | None,
                          booking_group_ids: set | None, actual_cols: set):
    """Wie _build_query, aber selektiert nur die Spalten, die in der DB
    tatsaechlich existieren (toleriert Schema-Drift in alten DBs).

    Liefert iterable von Row-Tuples (in Reihenfolge der select_cols).
    """
    cols_in_order = [c for c in model_columns(model) if c.name in actual_cols]
    if not cols_in_order:
        return []
    col_attrs = [getattr(model, c.name) for c in cols_in_order]
    q = db.session.query(*col_attrs)

    # Jahresfilter
    if model in YEAR_FILTERS and years:
        spec = YEAR_FILTERS[model]
        if isinstance(spec, str):
            q = q.filter(getattr(model, spec).in_(years))
        elif spec == ("date_year",):
            q = q.filter(func.extract("year", model.date).in_(years))
        elif spec == ("date", "period_year"):
            q = q.filter(or_(
                model.period_year.in_(years),
                and_(model.period_year.is_(None),
                     func.extract("year", model.date).in_(years)),
            ))
    elif model is InvoiceItem and invoice_ids is not None:
        q = q.filter(InvoiceItem.invoice_id.in_(invoice_ids or [-1]))
    elif model is DunningNotice and invoice_ids is not None:
        q = q.filter(DunningNotice.invoice_id.in_(invoice_ids or [-1]))
    elif model is BookingGroup and booking_group_ids is not None:
        q = q.filter(BookingGroup.id.in_(booking_group_ids or [-1]))

    return q


def _serialize_rows(model, query, actual_cols: set, skip_filter=None) -> list:
    """Iteriert Row-Tuples und baut typsichere Records.

    skip_filter erhaelt das fertige dict, nicht die ORM-Instanz, damit es
    auch im Schema-Drift-Pfad konsistent funktioniert.
    """
    cols_in_order = [c for c in model_columns(model) if c.name in actual_cols]
    if not cols_in_order:
        return []
    records = []
    rows = query.all() if hasattr(query, "all") else query
    for row in rows:
        rec = {}
        for col, raw_val in zip(cols_in_order, row):
            rec[col.name] = encode_value(raw_val)
        if skip_filter is not None:
            # Adapter: appsetting_skip_filter erwartet ORM-Instanz mit .key
            class _Stub:
                pass
            stub = _Stub()
            for k, v in rec.items():
                setattr(stub, k, v)
            if skip_filter(stub):
                continue
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_to_zip(selection: dict, fileobj, *, exported_by: str = "system") -> dict:
    """Exportiert die ausgewaehlten Kategorien in eine ZIP-Datei.

    selection: {
        "stammdaten": bool, "buchungen": bool, "mahnwesen": bool,
        "einstellungen": bool, "include_pdfs": bool, "years": [2024, 2025] | []
    }

    Schreibt direkt in fileobj (BytesIO oder File-Handle).
    Liefert das Manifest-Dict zurueck (fuer Logging).
    """
    years = selection.get("years") or []
    include_pdfs = bool(selection.get("include_pdfs", True))
    models = models_for_selection(selection)

    existing_tables_pre = set(sa_inspect(db.engine).get_table_names())
    invoice_ids = (_filtered_invoice_ids(years)
                   if (years and any(m in models for m in (Invoice, InvoiceItem, DunningNotice))
                       and Invoice.__tablename__ in existing_tables_pre)
                   else None)
    booking_group_ids = (_filtered_booking_group_ids(years)
                         if (years and BookingGroup in models
                             and Booking.__tablename__ in existing_tables_pre)
                         else None)

    table_meta = []
    pdf_files = []  # list of (zip_path, source_path)
    insp = sa_inspect(db.engine)
    existing_tables = set(insp.get_table_names())
    checksum = hashlib.sha256()

    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # Tabellen
        for model in models:
            tname = model.__tablename__
            if tname in EXCLUDED_TABLES:
                continue
            if tname not in existing_tables:
                # Tabelle fehlt physisch in dieser DB (aelteres Schema) —
                # ueberspringen statt Fehler, damit Export weiterlaeuft.
                continue
            # Schema-Drift: nur Spalten selektieren, die wirklich in der DB
            # existieren. Aelterer Schema-Stand hat ggf. weniger Columns als
            # das Model — ORM-Query mit allen Modell-Columns wuerde scheitern.
            actual_cols = {c["name"] for c in insp.get_columns(tname)}
            q = _build_query_filtered(
                model, years, invoice_ids, booking_group_ids, actual_cols,
            )
            skip_filter = appsetting_skip_filter if model is AppSetting else None
            records = _serialize_rows(model, q, actual_cols, skip_filter)

            # PDF-Pfade umschreiben (falls Bundle gewollt)
            if include_pdfs:
                if model is Invoice:
                    for rec in records:
                        for col in ("pdf_path", "doc_path"):
                            src = rec.get(col)
                            if src and os.path.isfile(src):
                                bundle_name = f"pdfs/invoices/{rec.get('invoice_number','id_'+str(rec.get('id')))}.{col.split('_')[0]}"
                                if col == "doc_path":
                                    bundle_name = f"pdfs/invoices/{rec.get('invoice_number','id_'+str(rec.get('id')))}.docx"
                                pdf_files.append((bundle_name, src))
                                rec[col] = bundle_name
                if model is DunningNotice:
                    for rec in records:
                        for col in ("pdf_path", "doc_path"):
                            src = rec.get(col)
                            if src and os.path.isfile(src):
                                ext = "pdf" if col == "pdf_path" else "docx"
                                bundle_name = f"pdfs/dunning/{rec.get('id')}.{ext}"
                                pdf_files.append((bundle_name, src))
                                rec[col] = bundle_name
            else:
                # Pfade nullen — Empfaenger soll PDFs neu generieren
                if model in (Invoice, DunningNotice):
                    for rec in records:
                        for col in ("pdf_path", "doc_path"):
                            if col in rec:
                                rec[col] = None

            data = json.dumps(records, ensure_ascii=False, indent=2, default=_json_default)
            data_bytes = data.encode("utf-8")
            checksum.update(data_bytes)
            zf.writestr(f"tables/{tname}.json", data_bytes)
            table_meta.append({
                "name": tname,
                "rows": len(records),
                "filtered": bool(years) and (
                    model in YEAR_FILTERS or model in (InvoiceItem, DunningNotice, BookingGroup)
                ),
            })

        # PDFs einbetten
        for bundle_name, src in pdf_files:
            try:
                zf.write(src, bundle_name)
            except OSError:
                pass  # Datei verschwunden zwischen Listing und Lesen — ueberspringen

        manifest = {
            "format_version": FORMAT_VERSION,
            "alembic_revision": _current_alembic_revision(),
            "source_app_version": APP_VERSION,
            "source_variant": _detect_variant(),
            "source_dialect": db.engine.dialect.name,
            "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "exported_by_user": exported_by,
            "selection": {
                "stammdaten": bool(selection.get("stammdaten")),
                "buchungen": bool(selection.get("buchungen")),
                "buchungen_years": list(years),
                "mahnwesen": bool(selection.get("mahnwesen")),
                "einstellungen": bool(selection.get("einstellungen")),
                "include_pdfs": include_pdfs,
            },
            "tables": table_meta,
            "checksum_sha256": checksum.hexdigest(),
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return manifest


def _json_default(o):
    """Fallback-Encoder fuer json.dumps (Decimal/Date/Datetime kommen schon
    als String aus encode_value, das hier ist nur Sicherheitsnetz)."""
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"Cannot serialize {type(o)}")


# ---------------------------------------------------------------------------
# Import — Validierung & Preview
# ---------------------------------------------------------------------------

def extract_to_temp(uploaded_fileobj, instance_path: str) -> tuple[Path, dict]:
    """Extrahiert das hochgeladene ZIP nach instance/tmp/imports/<uuid>/ und
    liest das Manifest. Liefert (extract_dir, manifest)."""
    import uuid
    base = Path(instance_path) / "tmp" / "imports"
    base.mkdir(parents=True, exist_ok=True)
    # Aufraeumen alter Sessions (>24h)
    _cleanup_stale_imports(base, max_age_hours=24)

    extract_dir = base / uuid.uuid4().hex
    extract_dir.mkdir(parents=True)

    with zipfile.ZipFile(uploaded_fileobj) as zf:
        zf.extractall(extract_dir)

    manifest_path = extract_dir / "manifest.json"
    if not manifest_path.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise ValueError("manifest.json fehlt — keine gueltige Wasserklar-Export-Datei.")

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    return extract_dir, manifest


def _cleanup_stale_imports(base: Path, max_age_hours: int = 24):
    """Loescht alle Subverzeichnisse aelter als max_age_hours."""
    if not base.exists():
        return
    cutoff = datetime.now().timestamp() - max_age_hours * 3600
    for child in base.iterdir():
        if child.is_dir():
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass


def validate_manifest(manifest: dict, extract_dir: Path) -> dict:
    """Validierungs-Pass. Liefert dict mit:
    - errors: blockieren den Import
    - warnings: erlauben Import mit Confirm
    - schema_status: "ok" | "older" | "unknown" | "newer"
    - tables_overview: [{"name", "rows", "current_count"}]
    """
    errors: list[str] = []
    warnings: list[str] = []

    fmt = manifest.get("format_version")
    if fmt != FORMAT_VERSION:
        errors.append(f"Format-Version {fmt!r} wird nicht unterstuetzt (erwartet {FORMAT_VERSION!r}).")

    # Alembic-Revision-Check
    src_rev = manifest.get("alembic_revision")
    cur_rev = _current_alembic_revision()
    schema_status = "ok"
    if src_rev != cur_rev:
        # Liegt src_rev in unserer Migrations-History?
        history_revs = _alembic_history_revisions()
        if src_rev in history_revs:
            schema_status = "older"
            warnings.append(
                f"Export wurde mit aelterer Schema-Revision {src_rev!r} erstellt "
                f"(aktuell {cur_rev!r}). Roundtrip kann fehlschlagen."
            )
        else:
            schema_status = "unknown" if src_rev else "newer"
            errors.append(
                f"Schema-Revision {src_rev!r} ist diesem System unbekannt. "
                f"Bitte erst 'flask --app run upgrade-db' ausfuehren."
            )

    # Checksum
    expected = manifest.get("checksum_sha256")
    if expected:
        actual = hashlib.sha256()
        for tm in manifest.get("tables", []):
            tpath = extract_dir / "tables" / f"{tm['name']}.json"
            if tpath.exists():
                with open(tpath, "rb") as fh:
                    actual.update(fh.read())
        if actual.hexdigest() != expected:
            errors.append("Checksum-Mismatch — Datei beschaedigt oder manipuliert.")

    # Tabellen-Overview (Zaehler vs. aktuelle DB)
    tables_overview = []
    for tm in manifest.get("tables", []):
        cur_count = _current_table_count(tm["name"])
        tables_overview.append({
            "name": tm["name"],
            "rows": tm.get("rows", 0),
            "current_count": cur_count,
        })

    # PDF-Plausibilitaet
    if manifest.get("selection", {}).get("include_pdfs"):
        if not (extract_dir / "pdfs").exists():
            warnings.append("Manifest sagt include_pdfs=true, aber pdfs/-Ordner fehlt im Bundle.")

    return {
        "errors": errors,
        "warnings": warnings,
        "schema_status": schema_status,
        "tables_overview": tables_overview,
    }


def _alembic_history_revisions() -> set:
    """Liefert Set aller Revisions in unserer Migrations-History."""
    try:
        from flask_migrate import current as alembic_current  # noqa: F401
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config()
        # Flask-Migrate's Konfig zu finden ist umstaendlich — wir lesen direkt
        # aus dem migrations/-Verzeichnis der App.
        migrations_dir = Path(current_app.root_path).parent / "migrations"
        cfg.set_main_option("script_location", str(migrations_dir))
        script = ScriptDirectory.from_config(cfg)
        return {r.revision for r in script.walk_revisions()}
    except Exception:
        return set()


def _current_table_count(tname: str) -> int:
    """Zaehlt die Records einer Tabelle in der aktuellen DB. 0 bei Fehler."""
    try:
        with db.engine.connect() as conn:
            row = conn.execute(text(f"SELECT COUNT(*) FROM {tname}")).first()
            return int(row[0]) if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Import — Apply
# ---------------------------------------------------------------------------

class ImportError_(Exception):
    """Datentransfer-Import-Fehler. Eigener Name, damit kein Clash mit
    Builtin ``ImportError``."""


def import_from_zip(extract_dir: Path, manifest: dict, *, mode: str = "replace",
                    update_existing: bool = False, instance_path: str | None = None) -> dict:
    """Wendet den Import an. Laeuft komplett in einer Transaktion.

    mode: "replace" (Vollersatz) oder "merge" (alt-IDs neu vergeben)
    update_existing: nur in merge-mode relevant — bestehende Records updaten
    instance_path: Pfad fuer PDF-Cleanup/-Kopie (nur wenn pdfs/ im Bundle)

    Liefert Statistik-Dict mit per-Tabelle (inserted, updated, skipped).
    """
    if mode not in ("replace", "merge"):
        raise ImportError_(f"Unbekannter Import-Modus: {mode}")

    selection = manifest.get("selection", {})
    models = models_for_selection(selection)
    stats: dict[str, dict[str, int]] = {}
    id_map: dict = {m: {} for m in models}

    # Records pro Tabelle laden
    table_records: dict = {}
    for model in models:
        tname = model.__tablename__
        if tname in EXCLUDED_TABLES:
            continue
        tpath = extract_dir / "tables" / f"{tname}.json"
        if not tpath.exists():
            continue
        with open(tpath, "r", encoding="utf-8") as fh:
            table_records[model] = json.load(fh)

    try:
        if mode == "replace":
            _truncate_models(models)
            for model in models:
                if model not in table_records:
                    continue
                _insert_replace(model, table_records[model], stats)
            _apply_deferred_updates(table_records, mode="replace", id_map=id_map, stats=stats)
            _reset_sequences(models)
        else:  # merge
            for model in models:
                if model not in table_records:
                    continue
                _insert_merge(model, table_records[model], id_map, update_existing, stats)
            _apply_deferred_updates(table_records, mode="merge", id_map=id_map, stats=stats)

        # Counter auf MAX nachziehen (gegen Nummern-Kollision bei spaeteren Inserts)
        _bump_counters(stats)

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise ImportError_(f"Import fehlgeschlagen: {exc}") from exc

    # PDFs erst nach Commit kopieren (idempotent — Failure laesst PDFs zurueck,
    # aber verfaelscht keine DB)
    if selection.get("include_pdfs") and instance_path:
        _copy_pdfs(extract_dir, instance_path, table_records)

    return stats


def _truncate_models(models: list):
    """Loescht alle Records der gewaehlten Models in reverser FK-Reihenfolge.

    Postgres: TRUNCATE ... CASCADE haette Side-Effects auf nicht-gewaehlte
    Tabellen — daher bewusst ohne CASCADE per DELETE. Dadurch FK-Violations
    bei orphaned dependents → klare Error-Message statt heimlichem Datenverlust.
    """
    for model in reversed(models):
        tname = model.__tablename__
        if tname in EXCLUDED_TABLES:
            continue
        db.session.execute(text(f"DELETE FROM {tname}"))
    db.session.flush()


def _insert_replace(model, records: list, stats: dict):
    """Vollersatz-Insert: IDs 1:1 uebernehmen, ORM-Bulk-Insert pro Record."""
    deferred = DEFERRED_FK_UPDATES.get(model, [])
    inserted = 0
    for rec in records:
        if model is AppSetting and is_excluded_setting(rec.get("key", "")):
            continue
        data = deserialize_record(model, rec, skip_columns=deferred)
        # Bei AppSetting: nicht ueberschreiben wenn Schluessel schon existiert
        # (z.B. lokal gesetzte Mail-Konfig nach Truncate eigentlich leer, aber
        # Defensive: kein Doppel-Insert)
        db.session.add(model(**data))
        inserted += 1
    db.session.flush()
    stats[model.__tablename__] = {"inserted": inserted, "updated": 0, "skipped": 0}


def _insert_merge(model, records: list, id_map: dict, update_existing: bool, stats: dict):
    """Merge-Insert: Duplikate per natuerlichem Schluessel finden, sonst neu inserten.

    id_map[model] = {old_id: new_id} wird fuer FK-Remapping spaeterer Tabellen aufgebaut.
    """
    natural_key = NATURAL_KEYS.get(model)
    fk_cols = FOREIGN_KEYS.get(model, {})
    deferred = DEFERRED_FK_UPDATES.get(model, [])
    pk_cols = primary_key_columns(model)

    inserted = updated = skipped = 0

    for rec in records:
        if model is AppSetting and is_excluded_setting(rec.get("key", "")):
            skipped += 1
            continue

        old_pk = primary_key_value(model, rec)
        data = deserialize_record(model, rec, skip_columns=deferred)

        # FKs remappen (ausser deferred)
        for col_name, target in fk_cols.items():
            if col_name in deferred:
                continue
            if data.get(col_name) is None:
                continue
            old_fk = data[col_name]
            new_fk = id_map.get(target, {}).get(old_fk)
            if new_fk is None:
                # FK-Ziel nicht im Mapping → Versuche auf existierende DB-Row
                # zu zeigen (z.B. Mahnwesen-Import in DB mit Stammdaten):
                # nimm alt_id 1:1, wenn der Record dort existiert.
                if db.session.get(target, old_fk) is not None:
                    new_fk = old_fk
                else:
                    raise ImportError_(
                        f"FK-Ziel fehlt: {model.__name__}.{col_name}={old_fk} "
                        f"(weder in Import noch in Ziel-DB vorhanden)"
                    )
            data[col_name] = new_fk

        # Natuerlicher Schluessel — Duplikat-Check
        existing = None
        if natural_key:
            filters = {k: data.get(k) for k in natural_key if data.get(k) is not None}
            if len(filters) == len(natural_key):
                existing = model.query.filter_by(**filters).first()

        if existing is not None:
            # Bestehender Record gefunden
            for pk in pk_cols:
                id_map[model][rec.get(pk)] = getattr(existing, pk)
            if update_existing:
                # Felder updaten (PKs nicht anfassen)
                for col_name, val in data.items():
                    if col_name in pk_cols:
                        continue
                    setattr(existing, col_name, val)
                updated += 1
            else:
                skipped += 1
            continue

        # Neu inserten:
        # - Models mit natuerlichem PK (FiscalYear.year, AppSetting.key,
        #   InvoiceCounter.year, CustomerCounter Singleton): PK uebernehmen
        # - sonst: Auto-Increment-PK aus data entfernen, DB vergibt neue ID
        if _has_natural_pk(model):
            new_data = data.copy()
        else:
            new_data = {k: v for k, v in data.items() if k not in pk_cols}

        instance = model(**new_data)
        db.session.add(instance)
        db.session.flush()
        for pk in pk_cols:
            id_map[model][rec.get(pk)] = getattr(instance, pk)
        inserted += 1

    stats[model.__tablename__] = {"inserted": inserted, "updated": updated, "skipped": skipped}


_NATURAL_PK_MODELS = {FiscalYear, AppSetting, InvoiceCounter, CustomerCounter}


def _has_natural_pk(model) -> bool:
    """True wenn das Model einen nicht-Auto-Increment-PK hat."""
    return model in _NATURAL_PK_MODELS


def _apply_deferred_updates(table_records: dict, *, mode: str, id_map: dict, stats: dict):
    """Setzt zirkulaere/Self-FKs nach (storno_of_id, dunning_notice_id)."""
    for model, deferred_cols in DEFERRED_FK_UPDATES.items():
        if model not in table_records:
            continue
        records = table_records[model]
        fk_cols = FOREIGN_KEYS.get(model, {})
        pk_cols = primary_key_columns(model)
        for rec in records:
            updates = {}
            for col in deferred_cols:
                old_val = rec.get(col)
                if old_val is None:
                    continue
                target = fk_cols.get(col)
                if target is None:
                    continue
                if mode == "replace":
                    new_val = old_val
                else:
                    new_val = id_map.get(target, {}).get(old_val)
                    if new_val is None:
                        # Ziel evtl. in vorhandener DB
                        if db.session.get(target, old_val) is not None:
                            new_val = old_val
                if new_val is not None:
                    updates[col] = new_val
            if not updates:
                continue
            old_pk = primary_key_value(model, rec)
            if mode == "replace":
                instance = db.session.get(model, old_pk)
            else:
                # PK ueber id_map auflösen
                if len(pk_cols) == 1:
                    new_pk = id_map.get(model, {}).get(old_pk)
                else:
                    new_pk = tuple(id_map.get(model, {}).get(p) for p in old_pk)
                instance = db.session.get(model, new_pk) if new_pk else None
            if instance is not None:
                for k, v in updates.items():
                    setattr(instance, k, v)
    db.session.flush()


def _reset_sequences(models: list):
    """Postgres-Sequences nach Vollersatz auf MAX(pk)+1 setzen.
    SQLite/MariaDB ziehen Auto-Increment selbst nach."""
    if db.engine.dialect.name != "postgresql":
        return
    for model in models:
        tname = model.__tablename__
        if tname in EXCLUDED_TABLES:
            continue
        pk_cols = primary_key_columns(model)
        if len(pk_cols) != 1:
            continue
        pk = pk_cols[0]
        try:
            seq = db.session.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": tname, "c": pk},
            ).scalar()
            if not seq:
                continue
            max_id = db.session.execute(text(f"SELECT COALESCE(MAX({pk}), 0) FROM {tname}")).scalar() or 0
            db.session.execute(text("SELECT setval(:s, :v)"),
                               {"s": seq, "v": max(int(max_id), 1)})
        except Exception:
            # Sequence existiert nicht (z.B. nicht-Auto-Increment-PK) — egal
            continue
    db.session.flush()


def _bump_counters(stats: dict):
    """InvoiceCounter / CustomerCounter auf MAX nachziehen, falls Buchungen
    importiert wurden — schuetzt vor Nummer-Kollisionen bei zukuenftigen Inserts."""
    # InvoiceCounter pro Jahr
    rows = db.session.query(
        Invoice.period_year,
        func.count(Invoice.id),
    ).filter(Invoice.period_year.is_not(None)).group_by(Invoice.period_year).all()
    for year, _ in rows:
        # Suffix der hoechsten Rechnung des Jahres
        max_inv = db.session.query(func.max(Invoice.invoice_number)).filter(
            Invoice.period_year == year
        ).scalar()
        if not max_inv or "-" not in max_inv:
            continue
        try:
            seq_used = int(max_inv.split("-")[-1])
        except ValueError:
            continue
        counter = db.session.get(InvoiceCounter, year)
        if counter is None:
            db.session.add(InvoiceCounter(year=year, next_seq=seq_used + 1))
        elif counter.next_seq <= seq_used:
            counter.next_seq = seq_used + 1

    # CustomerCounter (Singleton id=1)
    from app.models import Customer
    max_cn = db.session.query(func.max(Customer.customer_number)).scalar() or 0
    counter = db.session.get(CustomerCounter, 1)
    if counter is None:
        db.session.add(CustomerCounter(id=1, next_seq=int(max_cn) + 1))
    elif counter.next_seq <= max_cn:
        counter.next_seq = int(max_cn) + 1
    db.session.flush()


def _copy_pdfs(extract_dir: Path, instance_path: str, table_records: dict):
    """Kopiert PDFs aus dem Bundle in instance/pdfs/ und korrigiert die
    Pfade in der DB auf neue absolute Pfade.

    Wird NACH dem Commit aufgerufen — wenn das Filesystem hier scheitert,
    bleibt die DB konsistent (Pfade zeigen dann auf nicht-existente Files,
    PDFs koennen ueber die UI neu generiert werden).
    """
    pdfs_src = extract_dir / "pdfs"
    if not pdfs_src.exists():
        return
    pdfs_dst = Path(instance_path) / "pdfs"
    pdfs_dst.mkdir(parents=True, exist_ok=True)

    # Invoices
    if Invoice in table_records:
        for rec in table_records[Invoice]:
            for col, subdir in (("pdf_path", "invoices"), ("doc_path", "invoices")):
                bundle_path = rec.get(col)
                if not bundle_path or not bundle_path.startswith("pdfs/"):
                    continue
                src = extract_dir / bundle_path
                if not src.exists():
                    continue
                dst = pdfs_dst / subdir / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                # DB-Pfad auf neuen absoluten Pfad setzen
                inv = Invoice.query.filter_by(invoice_number=rec.get("invoice_number")).first()
                if inv is not None:
                    setattr(inv, col, str(dst))
    # Dunning
    if DunningNotice in table_records:
        for rec in table_records[DunningNotice]:
            for col, subdir in (("pdf_path", "dunning"), ("doc_path", "dunning")):
                bundle_path = rec.get(col)
                if not bundle_path or not bundle_path.startswith("pdfs/"):
                    continue
                src = extract_dir / bundle_path
                if not src.exists():
                    continue
                dst = pdfs_dst / subdir / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    db.session.commit()
