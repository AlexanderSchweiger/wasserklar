"""Belegablage: Upload, Verknuepfung mit Buchungen, Regeln, Aufbewahrung.

Regeln (Begruendungen: CLAUDE.md, Abschnitt „Belegablage"):

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
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from flask import current_app
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from app import country
from app.documents import storage
from app.einvoice import incoming
from app.extensions import db
from app.models import (
    Booking, BookingGroup, Customer, Document, DocumentEvent, DocumentLink, IncomingInvoice,
)

MB = 1024 * 1024
ENTITY_TYPES = {"booking": Booking, "booking_group": BookingGroup}

_CONTENT_TYPES = {
    "pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "xml": "application/xml",
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

def sniff(data):
    """Dateityp anhand der Magic Bytes (nie anhand der Endung): ``pdf|jpg|png|webp|xml``.

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
    if data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<":
        return "xml"
    raise DocumentError(
        "Dieser Dateityp wird nicht unterstützt. Erlaubt sind PDF, Fotos (JPG, PNG, WebP) "
        "und E-Rechnungen als XML.")


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
    """``{"used", "limit"}`` in Bytes (``limit`` ``None`` = unbegrenzt)."""
    used = db.session.query(func.coalesce(func.sum(Document.size_bytes), 0)).scalar()
    return {"used": int(used or 0), "limit": _quota_limit_bytes()}


def check_capacity(size):
    """Wirft ``QuotaExceeded`` bzw. ``StorageUnavailable``, wenn ``size`` Byte nicht mehr passen."""
    status = quota_status()
    if status["limit"] is not None and status["used"] + size > status["limit"]:
        raise QuotaExceeded(
            f"Der Belegspeicher ist voll ({format_size(status['used'])} von "
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
    existing = Document.query.filter_by(sha256=digest).first()
    if existing is not None:
        raise DuplicateUpload(existing)

    parsed, warning = None, None
    if ext == "pdf":
        _check_pdf(data)
        try:
            parsed = incoming.parse(data, filename)
        except incoming.NotAnEInvoice:
            pass
        except incoming.IncomingError as exc:
            warning = str(exc)
    elif ext == "xml":
        try:
            parsed = incoming.parse(data, filename)
        except incoming.IncomingError as exc:
            raise DocumentError(f"XML-Dateien werden nur als E-Rechnung angenommen — {exc}") from exc

    check_capacity(len(data))

    doc = Document(
        area=Document.AREA_ACCOUNTING, kind=kind or Document.KIND_OTHER, status=Document.STATUS_NEW,
        original_name=_display_name(filename), content_type=_CONTENT_TYPES[ext],
        size_bytes=len(data), sha256=digest, created_by_id=user_id)
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
    db.session.add(doc)
    db.session.flush()

    key = storage.new_key(doc.id, digest, ext)
    try:
        storage.put(key, data)
    except storage.StorageError as exc:
        db.session.rollback()
        raise StorageUnavailable(str(exc)) from exc
    doc.storage_key = key
    log(doc, "uploaded", user_id, size=len(data), kind=doc.kind, **(upload_detail or {}))
    try:
        db.session.commit()
    except IntegrityError:
        # Zwei parallele Uploads derselben Datei: der zweite scheitert am Unique auf sha256.
        db.session.rollback()
        storage.delete(key)
        existing = Document.query.filter_by(sha256=digest).first()
        if existing is not None:
            raise DuplicateUpload(existing)
        raise
    except Exception:
        db.session.rollback()
        storage.delete(key)
        raise
    doc.parse_warning = warning
    return doc


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


def tab_counts():
    return {tab: Document.query.filter(tab_filter(tab)).count()
            for tab in ("inbox", "booked", "filed", "discarded")}


def search_filter(term):
    like = f"%{term.strip()}%"
    return or_(Document.original_name.ilike(like), Document.title.ilike(like),
               Document.number.ilike(like),
               Document.supplier.has(Customer.name.ilike(like)))


def inbox_choices(limit=50):
    """Die neuesten Belege im Eingang (fuer die Auswahl im Buchungsformular)."""
    return (Document.query.filter(tab_filter("inbox"))
            .order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).all())


# ---------------------------------------------------------------------------
# Verknuepfung mit Buchungen
# ---------------------------------------------------------------------------

def link(doc, *, booking=None, group=None, user_id=None):
    """Haengt den Beleg an eine Buchung **oder** Sammelbuchung (idempotent). Der Aufrufer committet."""
    if (booking is None) == (group is None):
        raise ValueError("Genau eine Buchung oder eine Sammelbuchung angeben.")
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

def by_ids(ids):
    """Belege zu den IDs, in der Reihenfolge der IDs (unbekannte entfallen)."""
    ids = [int(i) for i in ids]
    if not ids:
        return []
    found = {d.id: d for d in Document.query.filter(Document.id.in_(ids)).all()}
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
    query = Booking.query.filter(
        Booking.status != Booking.STATUS_STORNIERT, Booking.storno_of_id.is_(None),
        Booking.group_id.is_(None), Booking.amount.in_(amounts), ~Booking.document_links.any())
    group_query = BookingGroup.query.filter(
        BookingGroup.status == BookingGroup.STATUS_AKTIV, BookingGroup.total_amount.in_(amounts),
        ~BookingGroup.document_links.any())
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
    if changes:
        log(doc, "edited", user_id, **changes)
    return changes


def can_delete(doc):
    """``(erlaubt, grund)``: nur ein Beleg, der nie verknuepft oder abgelegt war."""
    if doc.status == Document.STATUS_FILED:
        return False, "Abgelegte Belege sind aufbewahrungspflichtig und lassen sich nicht löschen."
    if doc.links or any(e.action in ("linked", "filed") for e in doc.events):
        return False, ("Der Beleg war schon einer Buchung zugeordnet oder abgelegt und ist "
                       "aufbewahrungspflichtig — er lässt sich nicht mehr löschen.")
    return True, None


def delete(doc, user_id=None):
    """Loescht einen nie verknuepften Beleg samt Datei (protokolliert). Committet."""
    allowed, reason = can_delete(doc)
    if not allowed:
        raise DocumentError(reason)
    key = doc.storage_key
    snapshot = {"size": doc.size_bytes, "kind": doc.kind, "title": doc.title}
    db.session.add(DocumentEvent(
        document_id=None, action="deleted", document_name=doc.original_name,
        document_sha256=doc.sha256, user_id=user_id,
        detail=json.dumps(snapshot, ensure_ascii=False, default=str)))
    db.session.delete(doc)         # das Protokoll bleibt (document_id wird NULL)
    db.session.commit()
    if key:
        storage.delete(key)


# ---------------------------------------------------------------------------
# Aufbewahrung + Buchungsvorschlag
# ---------------------------------------------------------------------------

def retention_end(doc):
    """Ende der Aufbewahrungsfrist (nur Anzeige): 31.12. des spaetesten Jahres aus
    Buchungsdaten (auch stornierter), Belegdatum und Ablagedatum + Jahre des Landes."""
    dates = [d for d in (doc.document_date, doc.created_at.date() if doc.created_at else None) if d]
    for row in doc.links:
        owner = row.booking if row.booking is not None else row.booking_group
        if owner is not None and owner.date:
            dates.append(owner.date)
    latest = max(dates) if dates else date.today()
    return date(latest.year + country.current_profile().document_retention_years, 12, 31)


def retention_hint():
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
