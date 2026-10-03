"""Ablage der Belegdateien im Dateibaum des Mandanten.

Die DB haelt nur einen **relativen** Schluessel (``Document.storage_key``), nie einen
absoluten Pfad: er ueberlebt einen Umzug des Instanzordners, und ein manipulierter
Wert (z. B. aus einer importierten ZIP) kann nicht auf fremde Dateien zeigen. Jeder
Zugriff geht ueber ``path_for`` — Format-Pruefung per Regex **und** ``resolve()`` +
``relative_to()`` gegen die Mandanten-Wurzel (``app/file_safety.py``).

Die Wurzel ist der Elternordner von ``PDF_DIR`` und wird bei jedem Aufruf neu bestimmt:
die SaaS-Mandanten-Middleware und der Job-Worker biegen ``PDF_DIR`` pro Request/Mandant
um — so ist die Ablage dort ohne weiteren Code je Mandant getrennt.

Dateien werden nie ueberschrieben (``put`` verweigert vorhandene Schluessel) und nie
veraendert. Spaeter kann eine zweite Klasse (z. B. S3) dieselbe Schnittstelle bedienen.
"""
import os
import re
import shutil
from datetime import date
from pathlib import Path

from flask import current_app, send_file

from app.file_safety import tenant_file_root

# Erlaubte Dateiendungen im Schluessel (Inhalt wird beim Upload ueber Magic Bytes bestimmt).
# ``txt`` gibt es fuer Kontoauszugsdateien (MT940/OFX) aus dem Bankimport und fuer Schriftverkehr;
# ``docx`` fuer die Word-Fassung einer Rechnung/Mahnung; die Office-Formate nur im Schriftverkehr.
EXTENSIONS = ("pdf", "xml", "jpg", "png", "webp", "txt", "docx", "doc", "xlsx", "xls", "odt", "ods", "md")

# Dateiname eines Altschluessels: nur [A-Za-z0-9._-], beginnt nicht mit einem Punkt (keine
# versteckten Dateien, keine Namen nur aus Punkten).
_LEGACY_NAME = r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}"

# documents/<Jahr>/<Monat>/<id>_<sha8>.<ext>    — neue Ablage (jedes neue Dokument)
# incoming/<Jahr|ohne-datum>/<id>_<sha8>_<name>  — Altablage der Eingangsrechnungen (migriert)
# pdfs/<Jahr|misc>/[dunning/]<name>              — Altdateien der Rechnungen/Mahnungen (registriert,
#                                                  die Datei bleibt liegen)
# schriftverkehr/<Jahr>/<name>                   — Altdateien der Protokolle/des Schriftverkehrs
# [0-9] statt \d: \d trifft in Python-Strings auch Ziffern anderer Schriften.
_KEY_RE = re.compile(
    r"^(?:documents/[0-9]{4}/[0-9]{2}/[0-9]{1,12}_[0-9a-f]{8}\.(?:" + "|".join(EXTENSIONS) + r")"
    r"|incoming/(?:[0-9]{4}|ohne-datum)/[0-9]{1,12}_[0-9a-f]{8}_[A-Za-z0-9._-]{1,200}"
    r"|pdfs/(?:[0-9]{4}|misc)/(?:dunning/)?" + _LEGACY_NAME +
    r"|schriftverkehr/[0-9]{4}/" + _LEGACY_NAME + r")$"
)


class StorageError(Exception):
    """Ablage nicht moeglich (ungueltiger Schluessel, Schluessel schon belegt, Platte)."""


def tenant_root() -> Path:
    return tenant_file_root()


def is_valid_key(key) -> bool:
    # fullmatch: ``$`` trifft in Python auch vor einem abschliessenden Zeilenumbruch.
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None


def key_for_path(path) -> str | None:
    """Schluessel zu einem absoluten Pfad im Dateibaum des Mandanten (z. B. ``invoices.pdf_path``) —
    ``None``, wenn der Pfad ausserhalb liegt oder kein gueltiger Schluessel waere."""
    if not path:
        return None
    root = tenant_root()
    try:
        key = Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None
    return key if is_valid_key(key) else None


def new_key(doc_id, sha256, ext, when=None) -> str:
    """Schluessel fuer einen neuen Beleg: ``documents/JJJJ/MM/<id>_<sha8>.<ext>``."""
    when = when or date.today()
    if ext not in EXTENSIONS:
        raise StorageError(f"Dateityp „{ext}“ ist nicht vorgesehen.")
    return f"documents/{when.year:04d}/{when.month:02d}/{int(doc_id)}_{sha256[:8]}.{ext}"


def path_for(key) -> Path | None:
    """Aufgeloester Pfad zum Schluessel — ``None`` bei ungueltigem Schluessel oder wenn
    der Pfad (z. B. ueber einen Symlink) ausserhalb der Mandanten-Wurzel landet.

    Prueft **nicht**, ob die Datei existiert (``put`` braucht den Pfad einer neuen Datei).
    """
    if not is_valid_key(key):
        return None
    root = tenant_root()
    try:
        resolved = (root / key).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return resolved


def exists(key) -> bool:
    path = path_for(key)
    return path is not None and path.is_file()


def size_of(key) -> int:
    path = path_for(key)
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def put(key, data: bytes) -> None:
    """Schreibt die Datei atomar (Temp-Datei + ``os.replace``). Ueberschreibt nie."""
    path = path_for(key)
    if path is None:
        raise StorageError("Ungültiger Ablage-Schlüssel.")
    if path.exists():
        raise StorageError("Unter diesem Schlüssel liegt schon eine Datei.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise StorageError(f"Die Datei konnte nicht gespeichert werden ({exc.strerror or exc}).") from exc


def read(key) -> bytes | None:
    path = path_for(key)
    if path is None or not path.is_file():
        return None
    with open(path, "rb") as fh:
        return fh.read()


def delete(key) -> bool:
    """Entfernt die Datei (best effort). ``True``, wenn sie danach nicht mehr existiert."""
    path = path_for(key)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def free_bytes() -> int:
    """Freier Platz auf dem Datentraeger der Mandanten-Wurzel."""
    probe = tenant_root()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def send(key, *, download_name, mimetype, as_attachment=False):
    """Liefert die Datei aus (Flask-Response) oder ``None``, wenn sie fehlt.

    ``nosniff`` immer; der Aufrufer waehlt ``as_attachment``. Ein CSP-``sandbox`` waere
    hier falsch: Chrome zeigt damit keine PDFs mehr an.
    """
    path = path_for(key)
    if path is None or not path.is_file():
        return None
    resp = send_file(path, mimetype=mimetype, as_attachment=as_attachment,
                     download_name=download_name, conditional=True)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
