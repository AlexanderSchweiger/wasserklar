"""Vorübergehende Dateien: immer im Dateibaum des Mandanten, nie unter einem festen Namen, nie dauerhaft.

Drei Arten, alle unter ``<mandant>/tmp/`` (Mandanten-Wurzel = Elternordner von ``PDF_DIR``, im SaaS
``instance/tenants/<slug>/``, im OSS ``instance/``):

* **Downloads** (Sammel-PDF, …): ``send_temporary`` schreibt in eine *anonyme* Temp-Datei und liefert sie
  aus; sie verschwindet, sobald die Antwort geschlossen ist. Kein fester Dateiname — zwei Nutzer, die
  gleichzeitig einen Sammeldruck starten, bekommen jeder ihre eigene Datei (früher teilten sie sich
  ``pdfs/_bulk_merged.pdf``).
* **Zwischenstände der Import-Assistenten** (``tmp/wizard/``): ``wizard_path`` vergibt einen Pfad,
  ``resolve_wizard_file`` lässt nur Dateien in genau diesem Ordner zu (der Pfad steht in der Session;
  geladen wird teils per ``pickle``). Früher lagen sie im globalen ``instance_path`` — im SaaS also
  mandantenübergreifend in einem Ordner, nie aufgeräumt, vom Reset nicht erfasst.
* **Entpackte Datenimporte** (``tmp/imports/``, ``app/data_transfer``).

``cleanup_stale`` löscht alles davon, was älter als 24 Stunden ist, dazu Altlasten früherer Versionen
(``pdfs/_bulk_merged.pdf``, ``pdfs/_bulk/``). Es läuft beim Anlegen eines Assistenten-Zwischenstands,
täglich per ``flask cleanup-temp`` (SaaS: ``cleanup-temp-tenants``) und über „Aufräumen“ auf der
Speicherseite der SaaS.
"""
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from flask import send_file

from app.file_safety import safe_tenant_path, tenant_file_root

MAX_AGE_HOURS = 24
WIZARD = ("tmp", "wizard")
IMPORTS = ("tmp", "imports")

# Altlasten früherer Versionen im Mandantenordner (Sammel-PDFs unter festem Namen).
_LEGACY_TENANT_FILES = (("pdfs", "_bulk_merged.pdf"),)
_LEGACY_TENANT_DIRS = (("pdfs", "_bulk"),)
# Altlasten im globalen Instanzordner: Zwischenstände der Assistenten vor dem Umzug nach tmp/wizard.
# Bewusst eng — im OSS liegt dort auch die SQLite-Datenbank.
_LEGACY_INSTANCE_RE = re.compile(
    r"^(?:[a-z_]*import_[0-9a-f]{32}\.pkl|(?:network|technik)_import_[0-9a-f]{16,64}\.json)$")


def _dir(parts, root=None):
    path = Path(root or tenant_file_root()).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tmp_dir(root=None):
    """``<mandant>/tmp`` (angelegt)."""
    return _dir(("tmp",), root)


def imports_dir(root=None):
    """``<mandant>/tmp/imports`` — entpackte Datenimporte."""
    return _dir(IMPORTS, root)


def wizard_path(prefix, ext):
    """Neuer Pfad (str) für einen Zwischenstand eines Import-Assistenten; räumt dabei Veraltetes ab."""
    folder = _dir(WIZARD)
    _remove_older_than(folder, MAX_AGE_HOURS)
    return str(folder / f"{prefix}{uuid.uuid4().hex}.{ext}")


def resolve_wizard_file(path):
    """Aufgelöster Pfad, wenn ``path`` eine vorhandene Datei in ``<mandant>/tmp/wizard`` ist — sonst
    ``None`` (z. B. ein Altpfad aus einer Session vor dem Umzug: der Nutzer lädt dann neu hoch)."""
    return safe_tenant_path(path, "/".join(WIZARD))


def send_temporary(write, *, download_name, mimetype):
    """Schreibt per ``write(fh)`` in eine anonyme Temp-Datei im Mandantenordner und liefert sie aus.

    Die Datei hat keinen Namen, den ein zweiter Request treffen könnte, und wird geschlossen (= gelöscht),
    sobald die Antwort ausgeliefert ist. Für große Sammeldrucke besser als ``BytesIO`` (Arbeitsspeicher).
    """
    tmp = tempfile.TemporaryFile(dir=tmp_dir())
    try:
        write(tmp)
        tmp.seek(0)
    except Exception:
        tmp.close()
        raise
    resp = send_file(tmp, mimetype=mimetype, as_attachment=True, download_name=download_name)
    resp.call_on_close(tmp.close)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def send_pdf_writer(writer, download_name):
    """Ein zusammengeführtes ``pypdf.PdfWriter``-Dokument ausliefern (Sammel-PDF)."""
    def _write(fh):
        writer.compress_identical_objects()
        writer.write(fh)
        writer.close()
    return send_temporary(_write, download_name=download_name, mimetype="application/pdf")


def _remove_older_than(folder, max_age_hours):
    """Dateien und Unterordner direkt in ``folder``, die älter als ``max_age_hours`` sind; ``(Anzahl, Bytes)``."""
    cutoff = time.time() - max_age_hours * 3600
    removed = freed = 0
    if not folder.is_dir():
        return removed, freed
    for child in folder.iterdir():
        try:
            stat = child.stat()
            if stat.st_mtime >= cutoff:
                continue
            if child.is_dir():
                size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                shutil.rmtree(child, ignore_errors=True)
            else:
                size = stat.st_size
                child.unlink()
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def cleanup_stale(max_age_hours=MAX_AGE_HOURS, root=None):
    """Räumt im Mandantenordner auf: ``tmp/wizard``, ``tmp/imports`` und Altlasten. ``(Anzahl, Bytes)``."""
    root = Path(root or tenant_file_root())
    removed = freed = 0
    for parts in (WIZARD, IMPORTS):
        r, f = _remove_older_than(root.joinpath(*parts), max_age_hours)
        removed, freed = removed + r, freed + f
    cutoff = time.time() - max_age_hours * 3600
    for parts in _LEGACY_TENANT_FILES:
        path = root.joinpath(*parts)
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                size = path.stat().st_size
                path.unlink()
                removed, freed = removed + 1, freed + size
        except OSError:
            pass
    for parts in _LEGACY_TENANT_DIRS:
        r, f = _remove_older_than(root.joinpath(*parts), max_age_hours)
        removed, freed = removed + r, freed + f
    return removed, freed


def cleanup_legacy_instance_files(instance_path, max_age_hours=MAX_AGE_HOURS):
    """Alte Assistenten-Zwischenstände direkt im (globalen) Instanzordner — nur die bekannten Namen."""
    cutoff = time.time() - max_age_hours * 3600
    removed = freed = 0
    try:
        entries = list(os.scandir(instance_path))
    except OSError:
        return removed, freed
    for entry in entries:
        if not entry.is_file() or not _LEGACY_INSTANCE_RE.fullmatch(entry.name):
            continue
        try:
            stat = entry.stat()
            if stat.st_mtime < cutoff:
                os.remove(entry.path)
                removed, freed = removed + 1, freed + stat.st_size
        except OSError:
            pass
    return removed, freed
