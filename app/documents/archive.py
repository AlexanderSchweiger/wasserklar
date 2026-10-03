"""Belegarchiv je Jahr (fuer Steuerberater/Betriebspruefung) und Integritaetspruefung der Ablage.

Das Archiv ist ein ZIP mit den **unveraenderten** Belegdateien, einem Verzeichnis
(``index.csv``, Excel-tauglich) und ``sha256sums.txt`` im Format von ``sha256sum -c`` — so kann der
Empfaenger ohne unsere Software pruefen, dass jede Datei noch dem abgelegten Original entspricht.

Zuordnung zum Jahr: ein Beleg gehoert in das Archiv eines Jahres, wenn er an eine Buchung oder
Sammelbuchung mit Buchungsdatum in diesem Zeitraum geknuepft ist (auch an eine stornierte — die
Verknuepfung bleibt als Nachweis). Belege ohne Buchung zaehlen nach ihrem Belegdatum, ohne
Belegdatum nach dem Ablagedatum. Ein Beleg zu Buchungen mehrerer Jahre steht in jedem dieser
Archive. Der Zeitraum eines Jahres ist der des ``FiscalYear`` (sonst das Kalenderjahr).
"""
import csv
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app import country
from app.__version__ import __version__ as APP_VERSION
from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import AppSetting, Booking, BookingGroup, Document, DocumentLink, FiscalYear

CHUNK = 1024 * 1024
_ALREADY_COMPRESSED = {"pdf", "jpg", "png", "webp"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Welche Belege gehoeren zu einem Jahr?
# ---------------------------------------------------------------------------

def period(year):
    """``(von, bis)`` des Jahres: der Zeitraum des Buchungsjahres, sonst das Kalenderjahr."""
    fiscal_year = db.session.get(FiscalYear, year)
    if fiscal_year is not None:
        return fiscal_year.start_date, fiscal_year.end_date
    return date(year, 1, 1), date(year, 12, 31)


def year_filter(year):
    """SQL-Ausdruck: der Beleg gehoert in das Archiv dieses Jahres (nur Bereich ``accounting`` — das
    Register haelt auch Ausgangsrechnungen, Mahnungen und Protokolle)."""
    start, end = period(year)
    in_period = Booking.date.between(start, end)
    linked = Document.links.any(or_(
        DocumentLink.booking_id.in_(select(Booking.id).where(in_period)),
        DocumentLink.booking_group_id.in_(select(BookingGroup.id).where(BookingGroup.date.between(start, end)))))
    day_start, day_end = datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)
    unlinked = and_(~Document.links.any(), or_(
        Document.document_date.between(start, end),
        and_(Document.document_date.is_(None), Document.created_at >= day_start, Document.created_at < day_end)))
    return and_(Document.area == Document.AREA_ACCOUNTING, or_(linked, unlinked))


def _candidate_years():
    years = {fy.year for fy in FiscalYear.query.all()}
    for column in (Booking.date, Document.document_date, Document.created_at):
        years.update(int(y) for (y,) in db.session.query(func.extract("year", column)).distinct().all() if y)
    return sorted(years, reverse=True)


def year_summary():
    """``[{year, von, bis, count, size}]`` aller Jahre, fuer die es Belege gibt — neueste zuerst."""
    rows = []
    for year in _candidate_years():
        count, size = (db.session.query(func.count(Document.id), func.coalesce(func.sum(Document.size_bytes), 0))
                       .filter(year_filter(year)).one())
        if count:
            start, end = period(year)
            rows.append({"year": year, "von": start, "bis": end, "count": int(count), "size": int(size)})
    return rows


def year_size(year):
    """Gesamtgroesse der Belegdateien eines Jahres in Byte."""
    return int(db.session.query(func.coalesce(func.sum(Document.size_bytes), 0)).filter(year_filter(year)).scalar() or 0)


# ---------------------------------------------------------------------------
# Archiv schreiben
# ---------------------------------------------------------------------------

@dataclass
class ArchiveResult:
    year: int
    documents: int = 0
    size: int = 0
    missing: list = field(default_factory=list)       # Beleg-IDs, deren Datei fehlt
    mismatched: list = field(default_factory=list)    # Beleg-IDs, deren Datei nicht mehr zur Pruefsumme passt

    @property
    def ok(self):
        return not self.missing and not self.mismatched


def _stored_extension(doc):
    ext = (doc.storage_key or "").rsplit(".", 1)[-1].lower()
    return ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else "bin"


def archive_name(doc):
    """Dateiname im Archiv: ``<Beleg-Nr.>_<Name>.<Endung>`` — die Nummer ist eindeutig und steht im Index."""
    safe = _SAFE.sub("_", Path(doc.original_name or "").stem).strip("._-")[:60] or "beleg"
    return f"{doc.id:06d}_{safe}.{_stored_extension(doc)}"


def _zip_time(doc):
    """Zeitstempel des ZIP-Eintrags = Ablagezeit (reproduzierbar; ZIP kennt nichts vor 1980)."""
    return max(doc.created_at or datetime(1980, 1, 1), datetime(1980, 1, 1)).timetuple()[:6]


def _cell(value):
    """Schuetzt vor CSV-/Formel-Injektion: ``index.csv`` geht an Dritte und oeffnet in Excel — ein Titel
    aus einer fremden E-Rechnung wie ``=HYPERLINK(...)`` wuerde dort als Formel laufen."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _date(value):
    return value.strftime("%d.%m.%Y") if value else ""


def _money(value):
    return f"{value:.2f}".replace(".", ",") if value is not None else ""


def _links_text(doc):
    parts = []
    for row in doc.links:
        if row.booking is not None:
            parts.append(f"Buchung {row.booking.id} vom {_date(row.booking.date)} ({_money(row.booking.amount)} €)"
                         + (", storniert" if not svc.is_effective_booking(row.booking) else ""))
        elif row.booking_group is not None:
            parts.append(f"Sammelbuchung {row.booking_group.id} vom {_date(row.booking_group.date)} "
                         f"({_money(row.booking_group.total_amount)} €)")
    return " | ".join(parts)


def _status_label(doc):
    if svc.is_booked(doc):
        return "Verbucht"
    return {Document.STATUS_DISCARDED: "Verworfen", Document.STATUS_FILED: "Abgelegt"}.get(doc.status, "Eingang")


INDEX_HEADER = (
    "Beleg-Nr.", "Datei im Archiv", "Originalname", "Art", "Titel", "Belegnummer", "Belegdatum",
    "Betrag brutto (EUR)", "Lieferant", "Status", "Zugeordnete Buchungen", "Abgelegt am", "Abgelegt von",
    "Aufbewahren bis", "Größe (Byte)", "SHA-256", "Prüfung", "Hinweis")


def _index_row(doc, name, check):
    notes = []
    if doc.meta_auto:
        notes.append("Angaben automatisch erkannt, nicht bestätigt")
    if doc.einvoice is not None:
        notes.append(doc.einvoice.format_label)
    row = (doc.id, name or "", doc.original_name, doc.kind_label, doc.title or "", doc.number or "",
           _date(doc.document_date), _money(doc.amount), doc.supplier.name if doc.supplier else "",
           _status_label(doc), _links_text(doc), _date(doc.created_at), doc.created_by.username if doc.created_by else "",
           _date(svc.retention_end(doc)), doc.size_bytes or 0, doc.sha256, check, "; ".join(notes))
    return tuple(_cell(value) for value in row)


def _readme(year, start, end, result, created_by):
    profile = country.current_profile()
    name = AppSetting.get("wg.name") or ""
    lines = [
        f"Belegarchiv {year}", name, "",
        f"Erstellt am {datetime.now():%d.%m.%Y %H:%M}" + (f" von {created_by}" if created_by else "")
        + f" (Version {APP_VERSION}).", "",
        "INHALT",
        "  belege/          Die Belegdateien, unverändert so wie abgelegt. (Fotos wurden gegebenenfalls schon vor",
        "                   dem Hochladen im Browser verkleinert; das steht im Änderungsprotokoll des Belegs.)",
        "  index.csv        Verzeichnis aller Belege (Trennzeichen Semikolon, UTF-8 — in Excel „Daten > Aus Text/CSV“).",
        "  sha256sums.txt   Prüfsummen (SHA-256) der Belegdateien zum Zeitpunkt der Ablage.", "",
        "ZUORDNUNG ZUM JAHR",
        f"  Ein Beleg steht in diesem Archiv, wenn er an eine Buchung oder Sammelbuchung mit Buchungsdatum von",
        f"  {_date(start)} bis {_date(end)} geknüpft ist (auch an eine stornierte — die Zuordnung bleibt als Nachweis).",
        "  Belege ohne Buchung zählen nach ihrem Belegdatum, ohne Belegdatum nach dem Ablagedatum. Ein Beleg zu",
        "  Buchungen mehrerer Jahre steht in jedem dieser Archive.", "",
        "DATEIEN PRÜFEN",
        "  Linux/macOS:         im entpackten Ordner   sha256sum -c sha256sums.txt",
        "  Windows PowerShell:  Get-FileHash -Algorithm SHA256 .\\belege\\<Datei>   und mit sha256sums.txt vergleichen",
        "  Weicht eine Prüfsumme ab, unterscheidet sich die Datei vom abgelegten Original.", "",
        "AUFBEWAHRUNG",
        f"  {profile.document_retention_hint}".rstrip(), "",
    ]
    if result.missing or result.mismatched:
        lines += ["HINWEISE ZU DIESEM ARCHIV"]
        if result.missing:
            lines.append(f"  Bei {len(result.missing)} Beleg(en) fehlte die Datei (Spalte „Prüfung“ im Index): "
                         + ", ".join(str(i) for i in result.missing))
        if result.mismatched:
            lines.append(f"  Bei {len(result.mismatched)} Beleg(en) passte die Datei nicht mehr zur Prüfsumme: "
                         + ", ".join(str(i) for i in result.mismatched))
        lines.append("")
    return "\r\n".join(lines)


def build(year, fileobj, *, created_by=""):
    """Schreibt das Belegarchiv des Jahres als ZIP in ``fileobj`` (seekbar, z. B. eine Temp-Datei).

    Die Dateien werden in einem Zug kopiert und dabei gehasht; eine Abweichung von der gespeicherten
    Pruefsumme wird im Index vermerkt (nicht verschwiegen) — fehlende Dateien ebenso.
    """
    start, end = period(year)
    docs = (Document.query.filter(year_filter(year))
            .options(selectinload(Document.links).selectinload(DocumentLink.booking),
                     selectinload(Document.links).selectinload(DocumentLink.booking_group),
                     selectinload(Document.supplier), selectinload(Document.created_by),
                     selectinload(Document.einvoice))
            .all())
    docs.sort(key=lambda d: (d.document_date or d.created_at.date(), d.id))
    root = f"belegarchiv-{year}"
    result = ArchiveResult(year=year, documents=len(docs))
    rows, sums = [], []

    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for doc in docs:
            path = storage.path_for(doc.storage_key) if doc.storage_key else None
            if path is None or not path.is_file():
                result.missing.append(doc.id)
                rows.append(_index_row(doc, None, "Datei fehlt"))
                continue
            name = archive_name(doc)
            info = zipfile.ZipInfo(f"{root}/belege/{name}", date_time=_zip_time(doc))
            info.compress_type = zipfile.ZIP_STORED if _stored_extension(doc) in _ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
            digest = hashlib.sha256()
            with zf.open(info, "w", force_zip64=True) as out, open(path, "rb") as src:
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    digest.update(chunk)
                    out.write(chunk)
            matches = digest.hexdigest() == doc.sha256
            if not matches:
                result.mismatched.append(doc.id)
            result.size += doc.size_bytes or 0
            sums.append(f"{doc.sha256}  belege/{name}")
            rows.append(_index_row(doc, f"belege/{name}", "ok" if matches else "ABWEICHUNG"))

        index = io.StringIO()
        index.write("﻿")                                   # BOM: Excel erkennt UTF-8
        writer = csv.writer(index, delimiter=";", lineterminator="\r\n")
        writer.writerow(INDEX_HEADER)
        writer.writerows(rows)
        zf.writestr(f"{root}/index.csv", index.getvalue().encode("utf-8"))
        zf.writestr(f"{root}/sha256sums.txt", ("\n".join(sums) + "\n" if sums else "").encode("utf-8"))
        zf.writestr(f"{root}/LIESMICH.txt", _readme(year, start, end, result, created_by).encode("utf-8"))
    return result


# ---------------------------------------------------------------------------
# Integritaetspruefung der Ablage
# ---------------------------------------------------------------------------

@dataclass
class VerifyReport:
    checked: int = 0
    issues: list = field(default_factory=list)       # [(Beleg-ID, Name, Art, Text)]
    orphans: list = field(default_factory=list)      # Schluessel von Dateien ohne Beleg

    @property
    def ok(self):
        return not self.issues


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_all(*, orphans=False):
    """Prueft jede Belegdatei gegen die gespeicherte SHA-256.

    Arten: ``missing`` (Datei fehlt), ``invalid_key`` (Schluessel ungueltig), ``mismatch`` (Inhalt
    weicht ab), ``read_error``. Mit ``orphans=True`` zusaetzlich Dateien unter ``documents/`` und
    ``incoming/``, die kein Beleg kennt (nur melden, nie loeschen).
    """
    report = VerifyReport()
    known = set()
    for doc_id, name, key, sha in (db.session.query(Document.id, Document.original_name, Document.storage_key,
                                                    Document.sha256).order_by(Document.id).yield_per(200)):
        report.checked += 1
        if not key:
            report.issues.append((doc_id, name, "missing", "keine Datei abgelegt (Schlüssel leer)"))
            continue
        known.add(key)
        path = storage.path_for(key)
        if path is None:
            report.issues.append((doc_id, name, "invalid_key", f"ungültiger Ablage-Schlüssel „{key}“"))
        elif not path.is_file():
            report.issues.append((doc_id, name, "missing", f"Datei fehlt ({key})"))
        else:
            try:
                actual = _file_digest(path)
            except OSError as exc:
                report.issues.append((doc_id, name, "read_error", f"nicht lesbar ({exc.strerror or exc})"))
                continue
            if actual != sha:
                report.issues.append((doc_id, name, "mismatch", f"Prüfsumme weicht ab ({key})"))
    if orphans:
        root = storage.tenant_root()
        for folder in ("documents", "incoming"):
            for current, _dirs, files in os.walk(root / folder):
                for file_name in files:
                    key = Path(current, file_name).relative_to(root).as_posix()
                    if key not in known:
                        report.orphans.append(key)
        report.orphans.sort()
    return report
