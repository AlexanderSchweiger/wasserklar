"""Pfad-Sicherheit fuer Dateipfade, die in der DB stehen.

Rechnungs- und Mahnungs-PDFs, eingefrorene E-Rechnungs-XMLs, Eingangsrechnungen
und der Schriftverkehr liegen im Dateibaum des Mandanten — dem Elternordner von
``PDF_DIR`` (OSS: ``instance/``, SaaS: ``instance/tenants/<slug>/``, pro Request
gesetzt). Die DB haelt den absoluten Pfad. Jede Stelle, die so einen Pfad liest
oder ausliefert, geht ueber ``safe_tenant_path``: ein Pfad ausserhalb (fremder
Mandant, Systemdatei, Altlast eines manipulierten Imports) verhaelt sich wie
eine fehlende Datei.
"""
import os
from pathlib import Path

from flask import current_app


def tenant_file_root() -> Path:
    """Wurzel des Mandanten-Dateibaums (aufgeloest): der Elternordner von PDF_DIR."""
    return Path(current_app.config["PDF_DIR"]).resolve().parent


def safe_tenant_path(path, subdir=None) -> str | None:
    """Aufgeloester Pfad (str), wenn ``path`` eine existierende Datei im
    Dateibaum des Mandanten ist — mit ``subdir`` nur unterhalb von
    ``<wurzel>/<subdir>``. Sonst ``None``.

    Relative Pfade (haengen vom Arbeitsverzeichnis ab) und UNC-/Netzwerkpfade
    werden ohne Dateisystemzugriff abgelehnt. ``resolve()`` folgt Symlinks, ein
    Link nach draussen faellt deshalb ebenfalls durch.
    """
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path) or path.startswith(("\\\\", "//")):
        return None
    root = tenant_file_root()
    try:
        if subdir:
            root = (root / subdir).resolve()
        resolved = Path(path).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return str(resolved) if resolved.is_file() else None
