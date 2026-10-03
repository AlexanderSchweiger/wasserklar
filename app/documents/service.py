"""Dokumentenregister: Upload, erzeugte Dokumente, Verknuepfung, Regeln, Aufbewahrung.

Das Register (``Document``) haelt alle aufbewahrungspflichtigen Dateien; der Bereich (``area``)
bestimmt das Recht (``access.py``), die Art die Frist (``retention.py``). Erzeugte Dokumente
(Ausgangsrechnung, Mahnung, Protokoll-PDF) kommen ueber ``store_generated`` hinein — ohne
Kontingentpruefung, damit der Versand nie blockiert —, Uploads ueber ``store_upload``.

Regeln der Belegablage (Bereich ``accounting``; Begruendungen: CLAUDE.md, Abschnitt „Belegablage"):

* Jede akzeptierte Datei wird **Byte fuer Byte** abgelegt — nie umkodiert (GoBD: elektronisch
  empfangene Belege im empfangenen Format; AT § 131 Abs. 3 BAO „inhaltsgleich"). Fotos
  verkleinert hoechstens der Browser vor dem Upload.
* „Verbucht" ist **abgeleitet** (``booked_clause``), nicht gespeichert: eine wirksame
  Verknuepfung zu einer nicht stornierten Buchung / aktiven Sammelbuchung. Ein Storno
  braucht deshalb keinen Hook, der Beleg taucht von selbst wieder im Eingang auf.
* Verknuepfen ist immer erlaubt (es kommt nur etwas dazu); Loesen nur im offenen
  Buchungsjahr bei wirksamer Buchung. Geloescht wird nur, was nie verknuepft/abgelegt war.
* Jede Aktion landet im ``DocumentEvent``-Protokoll.
"""
import hashlib
import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import current_app
from sqlalchemy import and_, event, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import country
from app.documents import extract, retention, storage, usage
from app.einvoice import incoming
from app.extensions import db
from app.models import (
    AppSetting, Booking, BookingGroup, Customer, Document, DocumentEvent, DocumentLink,
    IncomingInvoice, RealAccount,
)

MB = 1024 * 1024
ENTITY_TYPES = {"booking": Booking, "booking_group": BookingGroup}

CONTENT_TYPES = {
    "pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "xml": "application/xml",
    "txt": "text/plain",              # Kontoauszugsdateien (MT940/OFX) aus dem Bankimport, Schriftverkehr
    # Word-Fassung einer Rechnung/Mahnung; die uebrigen Office-Formate nur im Schriftverkehr
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "md": "text/markdown",
}
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


class DocumentError(Exception):
    """Der Vorgang ist nicht zulaessig; die Meldung ist fuer den Nutzer gedacht."""


class DuplicateUpload(DocumentError):
    """Dieselbe Datei ist schon abgelegt; ``existing`` ist der vorhandene Beleg."""

    def __init__(self, existing):
        super().__init__(existing.original_name)
        self.existing = existing


class QuotaExceeded(DocumentError):
    """Das Speicher-Kontingent des Mandanten waere ueberschritten."""


class StorageUnavailable(DocumentError):
    """Die Ablage ist gerade nicht moeglich (Platte knapp oder nicht beschreibbar)."""


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def format_size(n):
    """Deutsche Groessenangabe: 812 KB, 1,2 MB, 2,0 GB."""
    n = int(n or 0)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB".replace(".", ",")
    if n >= MB:
        return (f"{n / MB:.0f} MB" if n >= 10 * MB else f"{n / MB:.1f} MB".replace(".", ","))
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def max_upload_mb():
    return int(current_app.config.get("DOCUMENT_MAX_UPLOAD_MB", 15))


def max_upload_bytes():
    return max_upload_mb() * MB


def log(doc, action, user_id=None, **detail):
    """Haengt ein Ereignis an den Beleg (Snapshot von Name + Pruefsumme). Der Aufrufer committet."""
    event = DocumentEvent(
        document_id=doc.id, action=action, document_name=doc.original_name,
        document_sha256=doc.sha256, user_id=user_id,
        detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else None)
    db.session.add(event)
    return event


# ---------------------------------------------------------------------------
# Dateityp + Kapazitaet
# ---------------------------------------------------------------------------

def sniff(data, area=Document.AREA_ACCOUNTING, filename=None):
    """Dateityp anhand der Magic Bytes (nie anhand der Endung).

    Belege (``accounting``): ``pdf|jpg|png|webp|xml``. Schriftfuehrung (``records``): PDF und Fotos,
    dazu Word/Excel/OpenOffice (``docx|xlsx|odt|ods|doc|xls``) und Text (``txt|md``), aber kein XML.
    ``filename`` entscheidet nur, wo der Inhalt es nicht kann (altes Word vs. Excel, Markdown vs. Text).
    Wirft ``DocumentError`` bei allem anderen — auch HEIC (Safari/iPhone), TIFF, SVG, HTML.
    """
    head = data[:16]
    if data[:1024].lstrip()[:5] == b"%PDF-":
        return "pdf"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        raise DocumentError(
            "HEIC-Fotos werden nicht unterstützt. Bitte das Foto als JPG hochladen "
            "(iPhone: Einstellungen → Kamera → Formate → „Maximale Kompatibilität“).")
    if area == Document.AREA_RECORDS:
        ext = _sniff_office(data, filename) or _sniff_text(data, filename)
        if ext:
            return ext
        raise DocumentError(
            "Dieser Dateityp wird nicht unterstützt. Erlaubt sind PDF, Fotos (JPG, PNG, WebP), Word, "
            "Excel, OpenOffice/LibreOffice, Markdown und Text.")
    if data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<":
        return "xml"
    raise DocumentError(
        "Dieser Dateityp wird nicht unterstützt. Erlaubt sind PDF, Fotos (JPG, PNG, WebP) "
        "und E-Rechnungen als XML.")


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Bytes, die in Text vorkommen duerfen: alles ab Leerzeichen (UTF-8 inklusive) plus \t \n \f \r.
_TEXT_BYTES = bytes([9, 10, 12, 13]) + bytes(range(32, 256))
_ODF_TYPES = {b"application/vnd.oasis.opendocument.text": "odt",
              b"application/vnd.oasis.opendocument.spreadsheet": "ods"}


def _suffix(filename):
    return os.path.splitext((filename or "").lower())[1]


def check_upload_limits(size):
    """Grenzen fuer Uploads **ausserhalb** des Registers (Fotos in Leitungsnetz/Stoerungsjournal):
    Dateigroesse (``DOCUMENT_MAX_UPLOAD_MB``), Kontingent und Plattenschutz. Wirft ``DocumentError``."""
    if size > max_upload_bytes():
        raise DocumentError(f"Die Datei ist größer als {max_upload_mb()} MB.")
    check_capacity(size)


def _sniff_office(data, filename):
    """``docx|xlsx|odt|ods`` (ZIP-Container, am Inhaltsverzeichnis erkannt), ``doc|xls`` (OLE)."""
    if data[:8] == _OLE_MAGIC:
        return "xls" if _suffix(filename) == ".xls" else "doc"
    if data[:4] != b"PK\x03\x04":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if "mimetype" in names:
                kind = _ODF_TYPES.get(zf.read("mimetype").strip())
                if kind:
                    return kind
            if "[Content_Types].xml" in names:
                if any(n.startswith("word/") for n in names):
                    return "docx"
                if any(n.startswith("xl/") for n in names):
                    return "xlsx"
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        return None
    return None


def _sniff_text(data, filename):
    """``txt|md`` fuer reinen UTF-8-Text (ohne Steuerzeichen ausser Tab/Zeilenumbruch/Seitenvorschub)
    — wird immer als Download ausgeliefert."""
    if data[:8192].translate(None, _TEXT_BYTES):
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "md" if _suffix(filename) == ".md" else "txt"


def _quota_limit_bytes():
    """Kontingent des Mandanten in Bytes oder ``None`` (unbegrenzt / nicht ermittelbar)."""
    resolver = current_app.extensions.get("documents.quota_resolver")
    if resolver is not None:
        try:
            megabytes = resolver()
        except Exception:  # noqa: BLE001 — fail-open: eine Stoerung der Platform sperrt keinen Upload
            current_app.logger.warning("Speicher-Kontingent nicht ermittelbar", exc_info=True)
            megabytes = None
    else:
        megabytes = current_app.config.get("DOCUMENT_QUOTA_MB")
    return int(megabytes) * MB if megabytes else None


def quota_status():
    """``{"used", "limit"}`` in Bytes (``limit`` ``None`` = unbegrenzt).

    ``used`` zaehlt das ganze Register und die Fotos (``usage.total_used``) — gesperrt werden aber
    nur Uploads (``check_capacity``), nie erzeugte Dokumente."""
    return {"used": usage.total_used(), "limit": _quota_limit_bytes()}


def check_capacity(size):
    """Wirft ``QuotaExceeded`` bzw. ``StorageUnavailable``, wenn ``size`` Byte nicht mehr passen.

    Nur fuer **Uploads** (Belege, Schriftverkehr, hochgeladene Protokolle, Fotos). Was die App selbst
    erzeugt (Rechnungen, Mahnungen, Protokoll-PDF), prueft nicht — der Versand darf nie blockieren."""
    status = quota_status()
    if status["limit"] is not None and status["used"] + size > status["limit"]:
        raise QuotaExceeded(
            f"Der Dokumentenspeicher ist voll ({format_size(status['used'])} von "
            f"{format_size(status['limit'])}). Bitte nicht mehr benötigte, nie gebuchte Belege "
            "löschen oder den Tarif wechseln.")
    min_free = int(current_app.config.get("DOCUMENT_MIN_FREE_DISK_MB", 0)) * MB
    if min_free:
        try:
            free = storage.free_bytes()
        except OSError:
            free = None
        if free is not None and free - size < min_free:
            raise StorageUnavailable(
                "Auf dem Server ist gerade zu wenig Speicherplatz frei. Bitte später erneut "
                "versuchen — oder den Support verständigen, falls es dauerhaft so bleibt.")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _check_pdf(data):
    """Lesbar und ohne Benutzerpasswort (eine reine Rechte-Sperre ist in Ordnung)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise DocumentError(
                "Das PDF ist mit einem Passwort geschützt und lässt sich nicht ablegen. "
                "Bitte eine Fassung ohne Passwort hochladen.")
        len(reader.pages)           # erzwingt das Einlesen der Seitenstruktur
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 — defekte PDFs sind ein Nutzerfehler
        raise DocumentError(f"Das PDF ist beschädigt und lässt sich nicht lesen ({type(exc).__name__}).") from exc


def _display_name(filename):
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    return (name or "beleg")[:255]


def store_upload(filename, data, user_id, *, kind=None, upload_detail=None):
    """Legt einen hochgeladenen Beleg ab und **committet**. Gibt das ``Document`` zurueck.

    ``upload_detail`` landet im Protokoll (z. B. ``{"verkleinert": True, "original_groesse": …}``,
    wenn der Browser ein Foto vor dem Hochladen verkleinert hat).

    Wirft ``DocumentError`` (Typ/Groesse/Passwort/XML keine E-Rechnung), ``DuplicateUpload``,
    ``QuotaExceeded``, ``StorageUnavailable``. Eine E-Rechnung (XML oder ZUGFeRD-PDF) wird
    zusaetzlich gelesen (``Document.einvoice``); ein PDF ohne E-Rechnungsdaten ist ein
    normaler Beleg. Konnte eine eingebettete E-Rechnung nicht gelesen werden, steht der
    Grund in ``doc.parse_warning`` (nicht gespeichert).
    """
    if not data:
        raise DocumentError("Die Datei ist leer.")
    if len(data) > max_upload_bytes():
        raise DocumentError(
            f"Die Datei ist größer als {max_upload_mb()} MB. "
            "Bitte ein Foto in geringerer Auflösung bzw. ein kleineres PDF hochladen.")
    ext = sniff(data)
    digest = hashlib.sha256(data).hexdigest()
    _raise_if_duplicate(digest, Document.AREA_ACCOUNTING)

    parsed, warning, text = None, None, None
    if ext == "pdf":
        _check_pdf(data)
        try:
            parsed = incoming.parse(data, filename)
        except incoming.NotAnEInvoice:
            pass
        except incoming.IncomingError as exc:
            warning = str(exc)
        text = extract.extract_pdf_text(data)
    elif ext == "xml":
        try:
            parsed = incoming.parse(data, filename)
        except incoming.IncomingError as exc:
            raise DocumentError(f"XML-Dateien werden nur als E-Rechnung angenommen — {exc}") from exc

    check_capacity(len(data))

    doc = Document(
        area=Document.AREA_ACCOUNTING, kind=kind or Document.KIND_OTHER, status=Document.STATUS_NEW,
        original_name=_display_name(filename), content_type=CONTENT_TYPES[ext],
        size_bytes=len(data), sha256=digest, created_by_id=user_id)
    if text is not None:
        doc.text_status, doc.text_content = text.status, text.text or None
    if parsed is not None:
        inv = parsed.invoice
        doc.kind = Document.KIND_CREDIT_NOTE if inv.type_code in incoming.CREDIT_NOTE_CODES else Document.KIND_INVOICE
        doc.title = (inv.seller.name or "")[:200] or None
        doc.number = (inv.number or "")[:100] or None
        doc.document_date = inv.issue_date
        doc.amount = abs(inv.grand_total) if inv.grand_total is not None else None
        doc.einvoice = IncomingInvoice(
            source_kind=parsed.source_kind, syntax=inv.syntax, guideline=(inv.guideline or "")[:255],
            number=(inv.number or "")[:100], issue_date=inv.issue_date,
            currency=(inv.currency or "EUR")[:3], type_code=(inv.type_code or "380")[:3],
            seller_name=(inv.seller.name or "")[:200], seller_vat_id=inv.seller.vat_id or None,
            grand_total=inv.grand_total, data=inv.to_json())
    # eine E-Rechnung hat ihre Daten schon aus dem Original — vorbelegt wird nur ein „normaler“ Beleg
    prefill = parsed is None and text is not None and text.status == extract.STATUS_OK
    doc = _persist(doc, ext, data, user_id, upload_detail,
                   after_flush=(lambda stored: autofill(stored, user_id)) if prefill else None)
    doc.parse_warning = warning
    return doc


def _raise_if_duplicate(digest, area):
    """``DuplicateUpload``, wenn dieselbe Datei im selben Bereich liegt; liegt sie in einem anderen
    Bereich (z. B. eine eigene Ausgangsrechnung als Beleg hochgeladen), eine Meldung ohne Verweis —
    der Hochladende hat dort womoeglich kein Recht."""
    existing = Document.query.filter_by(sha256=digest).first()
    if existing is None:
        return
    if existing.area == area:
        raise DuplicateUpload(existing)
    raise DocumentError(f"Diese Datei ist bereits im Bereich „{existing.area_label}“ abgelegt "
                        f"({existing.kind_label}).")


def _persist(doc, ext, data, user_id, detail=None, *, after_flush=None):
    """Schreibt Zeile + Datei und committet. Die Datei kommt erst nach dem Flush (die Beleg-ID steckt im
    Schluessel) und geht bei jedem Fehler wieder weg; zwei parallele Uploads derselben Datei
    enden am Unique auf ``sha256`` als ``DuplicateUpload``."""
    db.session.add(doc)
    db.session.flush()
    key = storage.new_key(doc.id, doc.sha256, ext)
    try:
        storage.put(key, data)
    except storage.StorageError as exc:
        db.session.rollback()
        raise StorageUnavailable(str(exc)) from exc
    doc.storage_key = key
    log(doc, "uploaded", user_id, size=len(data), kind=doc.kind, **(detail or {}))
    if after_flush is not None:
        after_flush(doc)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        storage.delete(key)
        existing = Document.query.filter_by(sha256=doc.sha256).first()
        if existing is not None:
            raise DuplicateUpload(existing)
        raise
    except Exception:
        db.session.rollback()
        storage.delete(key)
        raise
    return doc


# ---------------------------------------------------------------------------
# Erzeugte Dokumente (Ausgangsrechnung, Mahnung, Protokoll-PDF)
# ---------------------------------------------------------------------------

LINK_TARGETS = ("invoice", "dunning_notice", "meeting")
_PENDING = "documents.pending_keys"


def attach(doc, *, user_id=None, **target):
    """Verknuepft ein Dokument mit **einem** Bezug (``invoice=``, ``dunning_notice=`` oder
    ``meeting=``) — idempotent. Der Aufrufer committet. Buchungen laufen ueber ``link``."""
    target = {key: value for key, value in target.items() if value is not None}
    if len(target) != 1 or next(iter(target)) not in LINK_TARGETS:
        raise ValueError("Genau einen Bezug angeben (invoice, dunning_notice oder meeting).")
    (name, obj), = target.items()
    for row in doc.links:
        if getattr(row, name) is obj:
            return row
    row = DocumentLink(created_by_id=user_id, document=doc, **{name: obj})
    db.session.add(row)
    db.session.flush()
    log(doc, "linked", user_id, **{f"{name}_id": obj.id})
    return row


def store_generated(area, kind, data, ext, *, original_name, title=None, number=None,
                    document_date=None, amount=None, user_id=None, event_detail=None, **target):
    """Legt ein **von der App erzeugtes** Dokument ab und verknuepft es mit seinem Bezug.

    Ohne Kontingentpruefung (der Versand einer Rechnung darf nie am Speicher scheitern), Status
    ``Abgelegt``, Ereignis ``archived``. Liegt dieselbe Datei schon im Register (gleiche
    Pruefsumme), wird nur die Verknuepfung ergaenzt. **Committet nicht** — der Aufrufer steuert die
    Transaktion; wird sie zurueckgerollt, verschwindet die geschriebene Datei wieder
    (``_cleanup_pending``). Wirft ``StorageUnavailable``, wenn die Datei nicht geschrieben werden kann.
    """
    if not data:
        raise DocumentError("Leeres Dokument.")
    digest = hashlib.sha256(data).hexdigest()
    existing = Document.query.filter_by(sha256=digest).first()
    if existing is not None:
        if target:
            attach(existing, user_id=user_id, **target)
        return existing
    doc = Document(
        area=area, kind=kind, status=Document.STATUS_FILED, original_name=_display_name(original_name),
        content_type=CONTENT_TYPES[ext], size_bytes=len(data), sha256=digest,
        title=(title or "")[:200] or None, number=(number or "")[:100] or None,
        document_date=document_date, amount=amount, created_by_id=user_id)
    db.session.add(doc)
    db.session.flush()
    key = storage.new_key(doc.id, digest, ext)
    if _same_file_present(key, digest):
        pass                     # z. B. nach einem Reset/Restore: dieselbe Datei liegt schon da
    else:
        try:
            storage.put(key, data)
        except storage.StorageError as exc:
            raise StorageUnavailable(str(exc)) from exc
        db.session.info.setdefault(_PENDING, []).append(key)
    doc.storage_key = key
    log(doc, "archived", user_id, size=len(data), kind=kind, **(event_detail or {}))
    if target:
        attach(doc, user_id=user_id, **target)
    return doc


def _same_file_present(key, digest):
    """Liegt unter ``key`` schon eine Datei mit genau diesem Inhalt? (Gleiche ID + gleiche
    Pruefsumme — sonst bleibt ``put`` beim Nie-Ueberschreiben und meldet den Konflikt.)"""
    data = storage.read(key)
    return data is not None and hashlib.sha256(data).hexdigest() == digest


@event.listens_for(Session, "after_commit")
def _forget_pending(session):
    session.info.pop(_PENDING, None)


@event.listens_for(Session, "after_soft_rollback")
def _cleanup_pending(session, previous_transaction):
    """Rollt die aeussere Transaktion zurueck, gehoeren die gerade geschriebenen Dateien zu keinem
    Dokument mehr — weg damit (sonst bleiben sie als Waisen liegen)."""
    if previous_transaction.parent is not None or previous_transaction.nested:
        return
    keys = session.info.pop(_PENDING, None)
    for key in keys or ():
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 — Aufraeumen ist best effort (ohne App-Kontext o. Ae.)
            pass


def store_record_upload(filename, data, user_id, *, kind, title=None, document_date=None, meeting=None):
    """Upload der Schriftfuehrung (Schriftverkehr, hochgeladenes Protokoll) ins Register; **committet**.

    Bereich ``records``, Status ``Abgelegt``; gleiche Grenzen wie bei Belegen (Groesse, Kontingent,
    Plattenschutz), dazu Office- und Textformate (``sniff``). Aus einem PDF wird der Text fuer die
    Suche gelesen (keine Vorbelegung). ``meeting`` verknuepft ein Protokoll mit seiner Sitzung.
    Wirft ``DocumentError``, ``DuplicateUpload``, ``QuotaExceeded``, ``StorageUnavailable``.
    """
    if not data:
        raise DocumentError("Die Datei ist leer.")
    if len(data) > max_upload_bytes():
        raise DocumentError(f"Die Datei ist größer als {max_upload_mb()} MB.")
    ext = sniff(data, Document.AREA_RECORDS, filename)
    digest = hashlib.sha256(data).hexdigest()
    _raise_if_duplicate(digest, Document.AREA_RECORDS)
    text = extract.extract_pdf_text(data) if ext == "pdf" else None
    check_capacity(len(data))
    doc = Document(
        area=Document.AREA_RECORDS, kind=kind, status=Document.STATUS_FILED,
        original_name=_display_name(filename), content_type=CONTENT_TYPES[ext], size_bytes=len(data),
        sha256=digest, title=(title or "")[:200] or None, document_date=document_date,
        created_by_id=user_id)
    if text is not None:
        doc.text_status, doc.text_content = text.status, text.text or None
    after = (lambda stored: attach(stored, user_id=user_id, meeting=meeting)) if meeting is not None else None
    return _persist(doc, ext, data, user_id, after_flush=after)


def can_delete_record(doc, user, today=None):
    """``(erlaubt, grund)`` fuer ein Dokument der Schriftfuehrung.

    Protokolle nie (dauerhaft). Schriftverkehr: am Tag des Hochladens von der hochladenden Person
    oder einem Administrator (Fehlablage); danach nur nach Fristablauf durch Administratoren."""
    if doc.kind != Document.KIND_CORRESPONDENCE:
        return False, retention.PERMANENT_HINT
    is_admin = bool(getattr(user, "is_admin", False))
    uploaded_today = doc.created_at is not None and doc.created_at.date() == datetime.utcnow().date()
    if uploaded_today and (is_admin or doc.created_by_id == getattr(user, "id", None)):
        return True, None
    end = retention_end(doc)
    if is_admin and end is not None and end < (today or date.today()):
        return True, None
    return False, (f"Schriftverkehr ist aufbewahrungspflichtig (bis {end:%d.%m.%Y}). Löschen geht nur am "
                   "Tag des Hochladens (Fehlablage) oder nach Ablauf der Frist durch Administratoren.")


def delete_record(doc, user, reason=None):
    """Loescht ein Dokument der Schriftfuehrung samt Datei (Regeln: ``can_delete_record``). Committet."""
    allowed, why = can_delete_record(doc, user)
    if not allowed:
        raise DocumentError(why)
    _remove(doc, getattr(user, "id", None), {
        "grund": reason or "Fehlablage", "kind": doc.kind, "title": doc.title, "size": doc.size_bytes})


def store_bank_statement(filename, data, user_id, *, title=None, number=None, document_date=None):
    """Legt die Datei eines importierten Kontoauszugs (CAMT/MT940/OFX) als Beleg ab; committet.

    Der Auszug ist der Beleg der Bankbuchungen. Er wird **unveraendert** abgelegt (``xml`` bei
    CAMT/OFX 2, sonst ``txt``), als ``Abgelegt`` (er gehoert nicht in den Belegeingang) und von
    ``bank_import`` an die erzeugten Buchungen geknuepft. Liegt dieselbe Datei schon als Beleg vor,
    ist es ein ``DuplicateUpload`` (der Aufrufer nimmt ``.existing``). Der Aufrufer hat die Datei
    schon als Auszug erkannt — hier wird nichts geparst.
    """
    if not data:
        raise DocumentError("Die Datei ist leer.")
    if len(data) > max_upload_bytes():
        raise DocumentError(f"Die Datei ist größer als {max_upload_mb()} MB.")
    digest = hashlib.sha256(data).hexdigest()
    existing = Document.query.filter_by(sha256=digest).first()
    if existing is not None:
        raise DuplicateUpload(existing)
    check_capacity(len(data))
    ext = "xml" if data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<" else "txt"
    doc = Document(
        area=Document.AREA_ACCOUNTING, kind=Document.KIND_BANK_STATEMENT, status=Document.STATUS_FILED,
        original_name=_display_name(filename), content_type=CONTENT_TYPES[ext], size_bytes=len(data),
        sha256=digest, title=(title or "")[:200] or None, number=(number or "")[:100] or None,
        document_date=document_date, created_by_id=user_id)
    return _persist(doc, ext, data, user_id, {"quelle": "Bankimport"})


def statement_document(stmt):
    """Der Beleg zur Datei eines Bankauszugs (``None`` bei Auszuegen aus der Zeit vor der Ablage)."""
    return Document.query.filter_by(sha256=stmt.file_hash).first()


def release_statement(doc, user_id=None):
    """Der Bankauszug wurde geloescht, ohne dass eine Zeile verbucht war: sein nie verknuepfter Beleg geht
    mit (samt Datei). Hat der Beleg Verknuepfungen — oder gehoert dieselbe Datei zu einem weiteren
    Auszug —, bleibt er bestehen. Committet."""
    from app.models import BankStatement      # lokal: bank_import importiert diesen Service
    if doc is None or doc.kind != Document.KIND_BANK_STATEMENT or doc.links:
        return False
    if BankStatement.query.filter_by(file_hash=doc.sha256).count() > 0:
        return False
    _remove(doc, user_id, {"grund": "Importauszug gelöscht", "kind": doc.kind, "size": doc.size_bytes})
    return True


# ---------------------------------------------------------------------------
# Textauslese + Vorbelegung (Stufe 2)
# ---------------------------------------------------------------------------

def own_identifiers():
    """``(UIDs, IBANs)`` des Mandanten. Sie stehen als Empfaenger auf jeder Lieferantenrechnung und
    duerfen nie als „Lieferant“ erkannt werden."""
    vat_ids = [AppSetting.get("wg.vat_id") or ""]
    ibans = [AppSetting.get("wg.iban") or ""]
    ibans += [iban for (iban,) in db.session.query(RealAccount.iban).all() if iban]
    return vat_ids, ibans


def match_supplier(text, vat_ids):
    """Lieferant zum Belegtext: ``(Customer, "uid" | "name")`` oder ``(None, None)``.

    Erst ueber die USt-IdNr. (eindeutig), dann ueber den **ausgeschriebenen** Namen im Text
    (mindestens 5 Zeichen, der laengste Treffer gewinnt). Nur Kontakte mit Lieferant-Kennzeichen;
    inaktive nur ueber die UID.
    """
    suppliers = Customer.query.filter(Customer.is_supplier.is_(True)).all()
    wanted = {extract.normalize_vat_id(v) for v in vat_ids}
    if wanted:
        for supplier in sorted(suppliers, key=lambda s: not s.active):
            if supplier.vat_id and extract.normalize_vat_id(supplier.vat_id) in wanted:
                return supplier, "uid"
    flat = f" {extract.normalize_name(text)} "
    best = None
    for supplier in suppliers:
        if not supplier.active:
            continue
        for name in {supplier.name, supplier.letter_name}:
            norm = extract.normalize_name(name)
            if len(norm) >= 5 and f" {norm} " in flat and (best is None or len(norm) > best[0]):
                best = (len(norm), supplier)
    return (best[1], "name") if best else (None, None)


def detect(text, today=None):
    """Vorschlag aus dem Belegtext: ``(Suggestions, Lieferant | None, Grund | None)``."""
    vat_ids, ibans = own_identifiers()
    suggestions = extract.suggest(text, own_vat_ids=vat_ids, own_ibans=ibans, today=today)
    supplier, why = match_supplier(text, suggestions.vat_ids)
    return suggestions, supplier, why


def autofill(doc, user_id=None):
    """Belegt **leere** Angaben eines Belegs aus seinem Text vor und merkt sich das (``meta_auto``).

    Es sind Vorschlaege: nichts Vorhandenes wird ueberschrieben, und hat der Nutzer die Angaben
    schon einmal gespeichert, bleibt der Beleg unberuehrt. Ein Fehler der Erkennung darf nie einen
    Upload verhindern. Gibt ``{Feld: Wert}`` der gesetzten Angaben zurueck; der Aufrufer committet.
    """
    if doc.area != Document.AREA_ACCOUNTING or doc.einvoice is not None or not doc.text_content:
        return {}
    if DocumentEvent.query.filter_by(document_id=doc.id, action="edited").first() is not None:
        return {}
    try:
        suggestions, supplier, why = detect(doc.text_content)
    except Exception:  # noqa: BLE001 — Heuristik-Fehler duerfen den Upload nicht blockieren
        current_app.logger.warning("Belegtext konnte nicht ausgewertet werden (Beleg %s)", doc.id, exc_info=True)
        return {}
    filled = {}
    if doc.kind == Document.KIND_OTHER and suggestions.kind:
        doc.kind = suggestions.kind
        filled["art"] = suggestions.kind
    if not doc.number and suggestions.number:
        doc.number = suggestions.number
        filled["nummer"] = suggestions.number
    if doc.document_date is None and suggestions.date:
        doc.document_date = suggestions.date
        filled["datum"] = suggestions.date
    if doc.amount is None and suggestions.amount is not None:
        doc.amount = suggestions.amount
        filled["betrag"] = suggestions.amount
    if doc.supplier_id is None and supplier is not None:
        doc.supplier_id = supplier.id
        filled["lieferant"] = supplier.name
        filled["lieferant_erkannt_ueber"] = "USt-IdNr." if why == "uid" else "Name"
    if filled:
        doc.meta_auto = True
        log(doc, "autofilled", user_id, **filled)
    return filled


def index_text(doc, user_id=None):
    """Liest den Text eines **vorhandenen** PDF-Belegs nach (Bestand, Import) und belegt leere
    Angaben vor. Gibt den Lesestatus zurueck (``None`` = kein PDF bzw. Datei fehlt); der Aufrufer committet."""
    if not doc.is_pdf:
        return None
    data = storage.read(doc.storage_key)
    if data is None:
        return None
    result = extract.extract_pdf_text(data)
    doc.text_status, doc.text_content = result.status, result.text or None
    if result.status == extract.STATUS_OK:
        autofill(doc, user_id)
    return result.status


def detected_identifiers(doc):
    """``(UIDs, IBANs)`` aus dem Belegtext fuer die Belegseite (ohne die eigenen Kennungen)."""
    if doc.text_status != extract.STATUS_OK or not doc.text_content:
        return [], []
    vat_ids, ibans = own_identifiers()
    return extract.find_vat_ids(doc.text_content, vat_ids), extract.find_ibans(doc.text_content, ibans)


# ---------------------------------------------------------------------------
# „Verbucht" (abgeleitet) und Listen
# ---------------------------------------------------------------------------

def is_effective_booking(booking):
    return booking is not None and booking.status != Booking.STATUS_STORNIERT and booking.storno_of_id is None


def booked_clause():
    """SQL-Ausdruck: der Beleg hat mindestens eine wirksame Verknuepfung."""
    effective_bookings = select(Booking.id).where(
        Booking.status != Booking.STATUS_STORNIERT, Booking.storno_of_id.is_(None))
    effective_groups = select(BookingGroup.id).where(BookingGroup.status == BookingGroup.STATUS_AKTIV)
    return exists().where(
        DocumentLink.document_id == Document.id,
        or_(DocumentLink.booking_id.in_(effective_bookings),
            DocumentLink.booking_group_id.in_(effective_groups)))


def is_booked(doc):
    """Python-Gegenstueck zu ``booked_clause`` fuer einen einzelnen Beleg."""
    for link in doc.links:
        if is_effective_booking(link.booking):
            return True
        if link.booking_group is not None and link.booking_group.status == BookingGroup.STATUS_AKTIV:
            return True
    return False


def tab_filter(tab):
    """Filterausdruck der Reiter der Belegliste (``None`` = alle)."""
    if tab == "inbox":
        return and_(Document.status == Document.STATUS_NEW, ~booked_clause())
    if tab == "booked":
        return and_(Document.status != Document.STATUS_DISCARDED, booked_clause())
    if tab == "filed":
        return Document.status == Document.STATUS_FILED
    if tab == "discarded":
        return Document.status == Document.STATUS_DISCARDED
    return None


def accounting_documents():
    """Basisabfrage der Belegablage: nur der Bereich ``accounting``. Jede Belegliste/-zaehlung geht
    hierueber — sonst tauchten Ausgangsrechnungen, Mahnungen und Protokolle im Belegeingang auf."""
    return Document.query.filter(Document.area == Document.AREA_ACCOUNTING)


def tab_counts():
    return {tab: accounting_documents().filter(tab_filter(tab)).count()
            for tab in ("inbox", "booked", "filed", "discarded")}


def search_filter(term):
    """Suche ueber Name, Titel, Nummer, Lieferant **und den gelesenen Text** der PDFs."""
    like = f"%{term.strip()}%"
    return or_(Document.original_name.ilike(like), Document.title.ilike(like),
               Document.number.ilike(like), Document.text_content.ilike(like),
               Document.supplier.has(Customer.name.ilike(like)))


def matches_meta(doc, term):
    """Trifft der Suchbegriff Name/Titel/Nummer/Lieferant (also nicht nur den Text)?"""
    needle = term.strip().lower()
    haystack = [doc.original_name, doc.title, doc.number, doc.supplier.name if doc.supplier else None]
    return any(needle in (value or "").lower() for value in haystack)


def inbox_choices(limit=50):
    """Die neuesten Belege im Eingang (fuer die Auswahl im Buchungsformular)."""
    return (accounting_documents().filter(tab_filter("inbox"))
            .order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).all())


# ---------------------------------------------------------------------------
# Verknuepfung mit Buchungen
# ---------------------------------------------------------------------------

def link(doc, *, booking=None, group=None, user_id=None):
    """Haengt den Beleg an eine Buchung **oder** Sammelbuchung (idempotent). Der Aufrufer committet."""
    if (booking is None) == (group is None):
        raise ValueError("Genau eine Buchung oder eine Sammelbuchung angeben.")
    if doc.area != Document.AREA_ACCOUNTING:
        raise DocumentError("Nur Belege lassen sich einer Buchung zuordnen.")
    if doc.status == Document.STATUS_DISCARDED:
        raise DocumentError("Ein verworfener Beleg lässt sich nicht zuordnen — bitte zuerst wieder öffnen.")
    if booking is not None:
        if booking.group_id is not None:
            raise DocumentError("Diese Buchung gehört zu einer Sammelbuchung — der Beleg wird an der "
                                "Sammelbuchung abgelegt.")
        if not is_effective_booking(booking):
            raise DocumentError("An eine stornierte Buchung lässt sich kein Beleg mehr anhängen.")
        existing = DocumentLink.query.filter_by(document_id=doc.id, booking_id=booking.id).first()
        detail = {"booking_id": booking.id}
    else:
        if group.status != BookingGroup.STATUS_AKTIV:
            raise DocumentError("An eine stornierte Sammelbuchung lässt sich kein Beleg mehr anhängen.")
        existing = DocumentLink.query.filter_by(document_id=doc.id, booking_group_id=group.id).first()
        detail = {"booking_group_id": group.id}
    if existing is not None:
        return existing
    new_link = DocumentLink(created_by_id=user_id)
    new_link.document = doc
    if booking is not None:
        new_link.booking = booking
    else:
        new_link.booking_group = group
    db.session.add(new_link)
    if doc.status == Document.STATUS_FILED:
        doc.status = Document.STATUS_NEW         # ab jetzt ist er „verbucht" (abgeleitet)
    db.session.flush()
    log(doc, "linked", user_id, **detail)
    return new_link


def link_many(ids, *, booking=None, group=None, user_id=None):
    """Verknuepft mehrere Belege (IDs, schon mit ``parse_document_ids`` geprueft). Gibt die Zahl zurueck."""
    count = 0
    for doc in by_ids(ids):
        link(doc, booking=booking, group=group, user_id=user_id)
        count += 1
    return count


def unlink_blocker(link_row):
    """Grund (Text), warum sich die Verknuepfung nicht loesen laesst — sonst ``None``.

    Loesen geht nur bei wirksamer Buchung in einem **offenen** Buchungsjahr; bei einer
    stornierten Buchung bzw. in einem abgeschlossenen Jahr bleibt sie als Nachweis bestehen.
    """
    from app.accounting import services as acc_svc    # lokal: accounting importiert diesen Service
    booking, group = link_row.booking, link_row.booking_group
    target_date = booking.date if booking is not None else group.date
    fiscal_year = acc_svc.locked_fiscal_year(target_date)
    if fiscal_year is not None:
        return (f"Das Buchungsjahr {fiscal_year.year} ist abgeschlossen — die Zuordnung "
                "bleibt als Nachweis bestehen.")
    effective = is_effective_booking(booking) if booking is not None else group.status == BookingGroup.STATUS_AKTIV
    if not effective:
        return "Bei einer stornierten Buchung bleibt die Zuordnung als Nachweis bestehen."
    return None


def unlink(link_row, *, user_id=None, reason=None):
    """Loest die Verknuepfung (Regeln: ``unlink_blocker``). Der Beleg ist danach wieder im
    Eingang. Der Aufrufer committet."""
    blocker = unlink_blocker(link_row)
    if blocker:
        raise DocumentError(blocker)
    doc, booking, group = link_row.document, link_row.booking, link_row.booking_group
    detail = {"booking_id": link_row.booking_id} if booking is not None else {"booking_group_id": link_row.booking_group_id}
    if reason:
        detail["reason"] = reason
    db.session.delete(link_row)
    db.session.flush()
    db.session.expire(doc, ["links"])
    if booking is not None:
        db.session.expire(booking, ["document_links"])
    else:
        db.session.expire(group, ["document_links"])
    log(doc, "unlinked", user_id, **detail)


def on_booking_deleted(*, booking=None, group=None, user_id=None):
    """Vor dem Loeschen einer Buchung/Sammelbuchung: Links protokollieren und entfernen.

    Wer einen weiteren Weg zum **Loeschen** von Buchungen ergaenzt, ruft das ebenfalls auf
    (ein Storno braucht nichts: die Verknuepfung bleibt als Nachweis). Gibt die Zahl der
    geloesten Belege zurueck; der Aufrufer committet.
    """
    owner = booking if booking is not None else group
    links = list(owner.document_links)
    for row in links:
        key = {"booking_id": row.booking_id} if booking is not None else {"booking_group_id": row.booking_group_id}
        log(row.document, "unlinked", user_id, reason="Buchung gelöscht", **key)
        db.session.delete(row)
    if links:
        db.session.flush()
        db.session.expire(owner, ["document_links"])
    return len(links)


def deleted_note(docs):
    """Satz fuer die Meldung nach dem Loeschen einer Buchung/Sammelbuchung.

    ``docs`` sind die Belege, die an der geloeschten Buchung hingen (vor dem Loeschen gesammelt).
    Nur Belege, die danach an keiner wirksamen Buchung mehr haengen, sind wieder im Eingang.
    """
    if not docs:
        return ""
    back = sum(1 for d in docs if not is_booked(d))
    if back == len(docs):
        return " Die Belege sind wieder im Belegeingang."
    if back == 0:
        return " Die Belege bleiben erhalten und sind weiterhin anderen Buchungen zugeordnet."
    return (f" {back} von {len(docs)} Belegen sind wieder im Belegeingang, "
            "die übrigen sind weiterhin anderen Buchungen zugeordnet.")


# ---------------------------------------------------------------------------
# Abfragen fuer die Oberflaeche
# ---------------------------------------------------------------------------

def by_ids(ids, areas=(Document.AREA_ACCOUNTING,)):
    """Dokumente zu den IDs, in der Reihenfolge der IDs (unbekannte und Dokumente ausserhalb von
    ``areas`` entfallen; ``areas=None`` = alle Bereiche)."""
    ids = [int(i) for i in ids]
    if not ids:
        return []
    query = Document.query.filter(Document.id.in_(ids))
    if areas is not None:
        query = query.filter(Document.area.in_(areas))
    found = {d.id: d for d in query.all()}
    return [found[i] for i in ids if i in found]


def parse_document_ids(raw_values):
    """Prueft die ``document_ids`` eines Formulars. Gibt ``(ids, fehlermeldung|None)`` zurueck."""
    ids = []
    for raw in raw_values or ():
        raw = str(raw).strip()
        if raw.isascii() and raw.isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    docs = by_ids(ids)
    if len(docs) != len(ids):
        return ids, "Ein gewählter Beleg existiert nicht mehr."
    for doc in docs:
        if doc.status == Document.STATUS_DISCARDED:
            return ids, f"Der Beleg „{doc.display_title}“ ist verworfen und lässt sich nicht zuordnen."
    return ids, None


def counts_by_entity(entity_type, ids):
    """``{id: Anzahl Belege}`` fuer Buchungen bzw. Sammelbuchungen — eine Abfrage je 500 IDs."""
    column = DocumentLink.booking_id if entity_type == "booking" else DocumentLink.booking_group_id
    ids = [i for i in ids if i is not None]
    counts = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = (db.session.query(column, func.count(DocumentLink.id))
                .filter(column.in_(chunk)).group_by(column).all())
        counts.update({entity_id: n for entity_id, n in rows})
    return counts


def links_for(entity_type, entity_id):
    """Die Verknuepfungen (mit Beleg) einer Buchung bzw. Sammelbuchung, aelteste zuerst."""
    column = DocumentLink.booking_id if entity_type == "booking" else DocumentLink.booking_group_id
    return DocumentLink.query.filter(column == entity_id).order_by(DocumentLink.id).all()


def document_of(*, booking_id=None, group_id=None):
    """Der Beleg einer **Eingangsrechnung** (E-Rechnung) zu dieser Buchung — fuer den Rueckverweis."""
    column = DocumentLink.booking_id if booking_id is not None else DocumentLink.booking_group_id
    value = booking_id if booking_id is not None else group_id
    if value is None:
        return None
    return (Document.query.join(DocumentLink, DocumentLink.document_id == Document.id)
            .join(IncomingInvoice, IncomingInvoice.document_id == Document.id)
            .filter(column == value).order_by(DocumentLink.id).first())


def candidates(doc, limit=10):
    """Passende Buchungen/Sammelbuchungen ohne Beleg: gleicher Betrag, nahes Datum."""
    if doc.amount is None:
        return []
    amount = abs(doc.amount)
    if doc.kind == Document.KIND_CREDIT_NOTE:
        amounts = [amount]
    elif doc.kind in (Document.KIND_INVOICE, Document.KIND_RECEIPT):
        amounts = [-amount]
    else:
        amounts = [amount, -amount]
    # „ohne Beleg“ heisst ohne Rechnung/Bon: der Kontoauszug, den der Bankimport an jede erzeugte
    # Buchung haengt, zaehlt nicht — sonst kaeme gerade die Buchung, die noch ihre Rechnung braucht,
    # nie als Vorschlag.
    has_receipt = DocumentLink.document.has(Document.kind != Document.KIND_BANK_STATEMENT)
    query = Booking.query.filter(
        Booking.status != Booking.STATUS_STORNIERT, Booking.storno_of_id.is_(None),
        Booking.group_id.is_(None), Booking.amount.in_(amounts), ~Booking.document_links.any(has_receipt))
    group_query = BookingGroup.query.filter(
        BookingGroup.status == BookingGroup.STATUS_AKTIV, BookingGroup.total_amount.in_(amounts),
        ~BookingGroup.document_links.any(has_receipt))
    if doc.document_date:
        start, end = doc.document_date - timedelta(days=30), doc.document_date + timedelta(days=90)
        query = query.filter(Booking.date.between(start, end))
        group_query = group_query.filter(BookingGroup.date.between(start, end))
    found = [("booking", b) for b in query.order_by(Booking.date.desc(), Booking.id.desc()).limit(limit).all()]
    found += [("booking_group", g) for g in group_query.order_by(BookingGroup.date.desc()).limit(limit).all()]
    return found[:limit]


def link_choices(limit=200):
    """Neueste wirksame Einzel- und Sammelbuchungen fuer das Zuordnen-Dropdown."""
    bookings = (Booking.query.filter(Booking.status != Booking.STATUS_STORNIERT,
                                     Booking.storno_of_id.is_(None), Booking.group_id.is_(None))
                .order_by(Booking.date.desc(), Booking.id.desc()).limit(limit).all())
    groups = (BookingGroup.query.filter(BookingGroup.status == BookingGroup.STATUS_AKTIV)
              .order_by(BookingGroup.date.desc(), BookingGroup.id.desc()).limit(limit // 2).all())
    return bookings, groups


# ---------------------------------------------------------------------------
# Arbeitsstand, Pflege, Loeschen
# ---------------------------------------------------------------------------

def shelve(doc, user_id=None):
    """Legt den Beleg ohne Buchung ab (z. B. Lieferschein, Vertrag)."""
    if doc.status != Document.STATUS_NEW:
        raise DocumentError("Nur ein Beleg im Eingang lässt sich ablegen.")
    if is_booked(doc):
        raise DocumentError("Der Beleg gehört schon zu einer Buchung.")
    doc.status = Document.STATUS_FILED
    log(doc, "filed", user_id)


def discard(doc, user_id=None):
    """Verwirft den Beleg (z. B. Dublette, falsche Datei). Die Datei bleibt aufbewahrt."""
    if doc.status == Document.STATUS_DISCARDED:
        return
    if is_booked(doc):
        raise DocumentError("Der Beleg gehört zu einer Buchung und lässt sich nicht verwerfen — "
                            "zuerst die Zuordnung lösen.")
    doc.status = Document.STATUS_DISCARDED
    log(doc, "discarded", user_id)


def reopen(doc, user_id=None):
    """Holt einen verworfenen oder abgelegten Beleg zurueck in den Eingang."""
    if doc.status == Document.STATUS_NEW:
        return
    doc.status = Document.STATUS_NEW
    log(doc, "reopened", user_id)


def _same(a, b):
    return (a or None) == (b or None)


def update_meta(doc, *, title, number, document_date, amount, supplier_id, kind, user_id=None):
    """Aendert die Metadaten eines **nicht** aus einer E-Rechnung gelesenen Belegs; protokolliert die Aenderung."""
    if doc.einvoice is not None:
        raise DocumentError("Bei einer E-Rechnung stammen die Daten aus der Datei und lassen sich nicht ändern.")
    if kind not in Document.KIND_LABELS:
        raise DocumentError("Ungültige Belegart.")
    new = {"title": (title or "").strip()[:200] or None, "number": (number or "").strip()[:100] or None,
           "document_date": document_date, "amount": amount, "supplier_id": supplier_id, "kind": kind}
    changes = {}
    for field, value in new.items():
        old = getattr(doc, field)
        if not _same(old, value):
            changes[field] = {"von": old, "nach": value}
            setattr(doc, field, value)
    confirmed = doc.meta_auto
    doc.meta_auto = False                       # „Speichern“ bestaetigt, was auf der Seite steht
    if changes or confirmed:
        log(doc, "edited", user_id, **changes, **({"bestaetigt": True} if confirmed else {}))
    return changes


def can_delete(doc):
    """``(erlaubt, grund)``: nur ein Beleg, der nie verknuepft oder abgelegt war."""
    if doc.status == Document.STATUS_FILED:
        return False, "Abgelegte Belege sind aufbewahrungspflichtig und lassen sich nicht löschen."
    if doc.links or any(e.action in ("linked", "filed") for e in doc.events):
        return False, ("Der Beleg war schon einer Buchung zugeordnet oder abgelegt und ist "
                       "aufbewahrungspflichtig — er lässt sich nicht mehr löschen.")
    return True, None


def _remove(doc, user_id, detail):
    """Entfernt Beleg, Verknuepfungen und Datei; das Protokoll bleibt als „deleted“ mit Snapshot. Committet."""
    key = doc.storage_key
    db.session.add(DocumentEvent(
        document_id=None, action="deleted", document_name=doc.original_name,
        document_sha256=doc.sha256, user_id=user_id,
        detail=json.dumps(detail, ensure_ascii=False, default=str)))
    db.session.delete(doc)         # Verknuepfungen gehen mit, das Protokoll bleibt (document_id wird NULL)
    db.session.commit()
    if key:
        storage.delete(key)


def delete(doc, user_id=None):
    """Loescht einen nie verknuepften Beleg samt Datei (protokolliert). Committet."""
    allowed, reason = can_delete(doc)
    if not allowed:
        raise DocumentError(reason)
    _remove(doc, user_id, {"size": doc.size_bytes, "kind": doc.kind, "title": doc.title})


def expired_clause(today=None, areas=(Document.AREA_ACCOUNTING,)):
    """SQL-Ausdruck: die Aufbewahrungsfrist ist abgelaufen (Regel: ``retention.expired_clause``).
    Standard ist die Belegablage; ``areas=None`` = alle Bereiche."""
    return retention.expired_clause(today, areas)


def expired_count(today=None, areas=(Document.AREA_ACCOUNTING,)):
    return Document.query.filter(expired_clause(today, areas)).count()


def expired_documents(today=None, areas=(Document.AREA_ACCOUNTING,)):
    """``[(Dokument, Ende der Aufbewahrung)]`` aller Dokumente, deren Frist abgelaufen ist — aelteste
    zuerst. Standard ist die Belegablage (``areas=None`` = alle Bereiche).

    Die Abfrage (``expired_clause``) waehlt vor, ``retention_end`` bestaetigt das Ergebnis je
    Dokument und liefert das Fristende fuer die Anzeige.
    """
    today = today or date.today()
    candidates = (Document.query.filter(expired_clause(today, areas))
                  .options(selectinload(Document.links).selectinload(DocumentLink.booking),
                           selectinload(Document.links).selectinload(DocumentLink.booking_group),
                           selectinload(Document.links).selectinload(DocumentLink.invoice),
                           selectinload(Document.links).selectinload(DocumentLink.dunning_notice))
                  .order_by(Document.created_at, Document.id).all())
    expired = [(doc, end) for doc in candidates if (end := retention_end(doc)) is not None and end < today]
    expired.sort(key=lambda pair: (pair[1], pair[0].id))
    return expired


def delete_expired(doc, user_id=None, today=None):
    """Loescht einen Beleg **nach Ablauf der Aufbewahrungsfrist** samt Verknuepfungen und Datei.

    Die Frist wird hier neu berechnet — nie aus einer Formular-Angabe uebernommen. Die Buchungen
    bleiben, nur ihr Beleg ist weg. Das Protokoll haelt Name, Pruefsumme und die frueheren
    Verknuepfungen fest. Committet.
    """
    today = today or date.today()
    end = retention_end(doc)
    if end is None:
        raise DocumentError("Dieses Dokument wird dauerhaft aufbewahrt und lässt sich nicht löschen.")
    if end >= today:
        raise DocumentError(
            f"Die Aufbewahrungsfrist läuft noch bis {end:%d.%m.%Y} — der Beleg lässt sich nicht löschen.")
    _remove(doc, user_id, {
        "grund": "Aufbewahrungsfrist abgelaufen", "aufbewahrt_bis": end.isoformat(), "kind": doc.kind,
        "title": doc.title, "number": doc.number, "amount": doc.amount, "size": doc.size_bytes,
        "buchungen": [row.booking_id for row in doc.links if row.booking_id is not None],
        "sammelbuchungen": [row.booking_group_id for row in doc.links if row.booking_group_id is not None]})


# ---------------------------------------------------------------------------
# Aufbewahrung + Buchungsvorschlag
# ---------------------------------------------------------------------------

def retention_end(doc):
    """Ende der Aufbewahrungsfrist (nur Anzeige) oder ``None`` bei dauerhafter Aufbewahrung. Bei
    Belegen: 31.12. des spaetesten Jahres aus Buchungsdaten (auch stornierter), Belegdatum und
    Ablagedatum + Jahre des Landes; Regeln der anderen Bereiche: ``retention.py``."""
    return retention.retention_end(doc)


def retention_hint(doc=None):
    if doc is not None:
        return retention.hint(doc)
    return country.current_profile().document_retention_hint


def booking_prefill(doc):
    """Vorbelegung des Buchungsformulars aus einem Beleg (Python-Werte; Datum nie in der Zukunft)."""
    amount = None
    if doc.amount is not None:
        # Eingangsrechnung/Kassenbon = Ausgabe (negativ); Gutschrift des Lieferanten = Einnahme.
        amount = abs(doc.amount) if doc.kind == Document.KIND_CREDIT_NOTE else -abs(doc.amount)
    return {
        "date": min(doc.document_date, date.today()) if doc.document_date else date.today(),
        "amount": amount, "reference": doc.number or "",
        "description": doc.title or doc.original_name, "customer_id": doc.supplier_id,
    }


def parse_decimal(raw):
    """Betrag aus einem Formularfeld (deutsches Komma erlaubt) oder ``None``; ``ValueError`` bei Muell."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "," in raw:                                   # 1.234,56 → 1234.56
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Ungültiger Betrag.") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("Der Betrag muss eine positive Zahl sein.")
    return value
