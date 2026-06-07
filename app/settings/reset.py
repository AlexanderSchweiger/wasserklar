"""Mandant zuruecksetzen ("Danger Zone").

Loescht ALLE Geschaefts-Daten des aktuellen Mandanten und stellt anschliessend
den Baseline-Zustand eines frisch angelegten Mandanten wieder her. Erhalten
bleiben bewusst:

- ``app_settings``  — die Einstellungen (Kontakt, Mail, Rechnungsformat, …)
- ``users`` / ``user_preferences`` / ``roles`` / ``role_permissions`` — Login + Rechte
- ``alembic_version`` — der Schema-Versionsstand

Danach werden die Standard-Seeds (Steuersaetze, Mahnvorlage, eine aktive
Abrechnungsperiode, Default-Rollen) idempotent neu eingespielt — der Mandant ist
sofort wieder nutzbar.

WICHTIG (SaaS): Die Loeschung laeuft ueber ``db.session`` und NICHT ueber eine
frische ``db.engine``-Connection. Im SaaS setzt der Pool-``checkout``-Listener
den ``search_path`` jeder neuen Connection auf ``public`` zurueck; nur die
Session-Connection traegt via ``after_begin``-Listener den Tenant-``search_path``.
Unqualifizierte DDL/DML trifft damit ausschliesslich das Schema des aktuellen
Mandanten — das ist die Garantie fuer "und nur diesen Mandanten".
"""

from __future__ import annotations

import os
import shutil

from flask import current_app, g
from sqlalchemy import inspect as sa_inspect, text

from app.extensions import db


# Tabellen, die der Reset NIE leert.
KEEP_TABLES = {
    "app_settings",        # Einstellungen — laut Anforderung nie loeschen
    "users",               # Login-Konten (der ausfuehrende Admin bleibt drin)
    "user_preferences",    # per-User-Einstellungen
    "roles",               # Rollen
    "role_permissions",    # Rollen-Rechte-Zuordnung
    "alembic_version",     # Schema-Versionsstand (nie anfassen)
}


def _active_schema():
    """Aktives Tenant-Schema (SaaS) oder None (OSS-Standalone).

    Wird dem Inspector mitgegeben, damit er im SaaS die Tenant-Tabellen findet —
    eine frische Inspector-Connection haette sonst ``search_path = public``.
    """
    try:
        return g.get("tenant_schema")
    except (RuntimeError, AttributeError):
        return None


def _tables_to_clear() -> list[str]:
    """Alle physisch vorhandenen Tabellen des aktiven Schemas ausser KEEP_TABLES.

    Bewusst gegen die tatsaechlich existierenden Tabellen (Inspector) statt gegen
    ``db.metadata`` — so werden auch Tabellen erfasst, die (noch) nicht im
    aktuellen Modell stehen, und es kracht nicht an Tabellen, die in einer
    aelteren Schema-Revision fehlen.
    """
    schema = _active_schema()
    existing = set(sa_inspect(db.engine).get_table_names(schema=schema))
    return sorted(t for t in existing if t not in KEEP_TABLES)


def _delete_all(tables: list[str]) -> None:
    """Leert die Tabellen FK-sicher ueber die Session-Connection (Tenant-Schema).

    Dialekt-portabel (die OSS-App laeuft auf SQLite/MySQL/Postgres):
    - Postgres: eine ``TRUNCATE ... RESTART IDENTITY CASCADE`` — loest zirkulaere
      und Self-FKs ohne Reihenfolge auf und setzt die Sequences zurueck.
    - SQLite/MySQL: FK-Pruefung temporaer aus, dann ``DELETE FROM`` je Tabelle.
    """
    if not tables:
        return

    dialect = db.engine.dialect.name

    if dialect == "postgresql":
        # Unqualifizierte Namen -> search_path entscheidet (Tenant-Schema im SaaS).
        names = ", ".join(tables)
        db.session.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
    elif dialect == "sqlite":
        db.session.execute(text("PRAGMA foreign_keys=OFF"))
        for tname in tables:
            db.session.execute(text(f"DELETE FROM {tname}"))
        db.session.execute(text("PRAGMA foreign_keys=ON"))
    else:  # mysql / mariadb
        db.session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for tname in tables:
            db.session.execute(text(f"DELETE FROM {tname}"))
        db.session.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    db.session.commit()


def _reseed_baseline() -> None:
    """Die Defaults eines frisch angelegten Mandanten idempotent neu einspielen.

    Quelle der Wahrheit sind dieselben Seed-Funktionen wie ``flask init-db`` —
    lazy importiert, damit dieser Service rein additiv bleibt und ``cli`` (das
    erst zur Request-Zeit laengst geladen ist) nicht beim Modul-Import zieht.
    """
    from cli import (
        seed_default_tax_rates,
        seed_default_dunning_policy,
        seed_default_billing_period,
        seed_default_roles,
    )

    seed_default_tax_rates(db)
    seed_default_dunning_policy(db)
    seed_default_billing_period(db)
    seed_default_roles(db)  # No-op falls die behaltenen Rollen schon existieren


def _clear_tenant_files() -> None:
    """Erzeugte Dokumente und Foto-Anhaenge des Mandanten von der Platte raeumen.

    Alle Datei-Ablagen reiten auf dem pro Request umgebogenen ``PDF_DIR``:
    ``PDF_DIR`` selbst (Rechnungs-/Mahn-PDFs), das Geschwister-Verzeichnis
    ``schriftverkehr`` und ``network`` (Feature-Fotos). Es werden NUR diese drei
    namentlich bekannten Verzeichnisse entfernt — niemals das Eltern-Verzeichnis
    (im OSS-Standalone ist das ``instance/`` samt SQLite-DB!).

    Laeuft nach dem DB-Commit und ist best-effort: schlaegt das Filesystem fehl,
    bleibt die DB trotzdem konsistent (verwaiste Dateien koennen neu erzeugt
    werden, die Pfade in der DB sind ohnehin gerade geloescht worden).
    """
    pdf_dir = current_app.config.get("PDF_DIR")
    if not pdf_dir:
        return
    parent = os.path.dirname(pdf_dir)
    targets = [
        pdf_dir,
        os.path.join(parent, "schriftverkehr"),
        os.path.join(parent, "network"),
    ]
    for path in targets:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    # PDF_DIR wieder anlegen — die App erwartet, dass es existiert.
    try:
        os.makedirs(pdf_dir, exist_ok=True)
    except OSError:
        pass


def reset_tenant_data() -> dict:
    """Setzt den aktuellen Mandanten zurueck. Siehe Modul-Docstring.

    Liefert ein kleines Statistik-Dict zurueck (Anzahl geleerter Tabellen).
    """
    tables = _tables_to_clear()
    _delete_all(tables)
    _reseed_baseline()
    _clear_tenant_files()
    return {"cleared_tables": len(tables)}
