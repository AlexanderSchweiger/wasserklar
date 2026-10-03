"""Belegablage (app/documents/service.py): Upload, Kontingent, Verknuepfung, Loeschregeln, Aufbewahrung."""
import hashlib
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from pypdf import PdfWriter

from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import (
    Account, AppSetting, Booking, BookingGroup, Customer, Document, DocumentEvent, DocumentLink,
    FiscalYear, IncomingInvoice, User,
)
from tests.conftest import _ensure_role
from tests.unit.test_einvoice_incoming import _fixture, _pdf_with

CII = "kosit-01.01a-INVOICE_uncefact.xml"           # 336,90 € brutto
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def pdf(n=0):
    """Ein gueltiges, je ``n`` anderes PDF (andere Seitengroesse → andere Pruefsumme)."""
    writer = PdfWriter()
    writer.add_blank_page(width=100 + n, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


@pytest.fixture
def user(app):
    u = User(username="kassier", email="k@test.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def account(app):
    a = Account(name="Wasserbezug", code="WBZ")
    db.session.add(a)
    db.session.commit()
    return a


def booking(account, *, amount="-10.00", day=None, **extra):
    b = Booking(date=day or date(2026, 3, 1), account_id=account.id, amount=Decimal(amount),
                description="Testbuchung", **extra)
    db.session.add(b)
    db.session.commit()
    return b


def group(account, *, day=None):
    g = BookingGroup(date=day or date(2026, 3, 1), description="Sammel", total_amount=Decimal("-30"),
                     status=BookingGroup.STATUS_AKTIV)
    db.session.add(g)
    db.session.flush()
    for amount in ("-10", "-20"):
        db.session.add(Booking(date=g.date, account_id=account.id, amount=Decimal(amount),
                               description="Zeile", group_id=g.id))
    db.session.commit()
    return g


def store(data=None, name="beleg.pdf", user_id=None, **kw):
    return svc.store_upload(name, data if data is not None else pdf(), user_id, **kw)


def actions(doc):
    return [e.action for e in DocumentEvent.query.filter_by(document_id=doc.id).order_by(DocumentEvent.id)]


class TestStoreUpload:
    def test_a_plain_pdf_is_stored_unchanged(self, app, tenant, user):
        data = pdf()
        doc = store(data, user_id=user.id)
        assert doc.status == "Neu" and doc.kind == "other" and doc.einvoice is None
        assert doc.content_type == "application/pdf" and doc.size_bytes == len(data)
        assert doc.sha256 == hashlib.sha256(data).hexdigest() and doc.created_by_id == user.id
        assert doc.storage_key == f"documents/{date.today():%Y/%m}/{doc.id}_{doc.sha256[:8]}.pdf"
        assert storage.read(doc.storage_key) == data                      # Byte für Byte
        assert actions(doc) == ["uploaded"]

    @pytest.mark.parametrize("data, ext, mime", [
        (PNG, "png", "image/png"), (JPG, "jpg", "image/jpeg"), (WEBP, "webp", "image/webp"),
    ])
    def test_images_are_stored_unchanged(self, app, tenant, data, ext, mime):
        doc = store(data, name=f"foto.{ext}")
        assert doc.storage_key.endswith(f".{ext}") and doc.content_type == mime
        assert storage.read(doc.storage_key) == data

    def test_the_name_cannot_smuggle_a_path(self, app, tenant):
        doc = store(name="..\\..\\boese/../name.pdf")
        assert doc.original_name == "name.pdf" and doc.storage_key.startswith("documents/")
        assert not list(tenant.parent.glob("boese*"))

    def test_an_einvoice_xml_is_read(self, app, tenant):
        doc = store(_fixture(CII), name="rechnung.xml")
        assert doc.einvoice is not None and doc.kind == "invoice" and doc.content_type == "application/xml"
        assert (doc.number, doc.title, doc.document_date) == ("123456XX", "[Seller name]", date(2016, 4, 4))
        assert doc.amount == Decimal("336.90") and doc.einvoice.parsed.grand_total == Decimal("336.90")

    def test_a_hybrid_pdf_is_both(self, app, tenant):
        doc = store(_pdf_with(_fixture(CII)), name="l.pdf")
        assert doc.einvoice is not None and doc.einvoice.source_kind == "pdf" and doc.content_type == "application/pdf"

    def test_a_credit_note_is_recognised(self, app, tenant):
        parsed = None
        from app.einvoice import incoming
        xml = _fixture(CII)
        parsed = incoming.parse(xml).invoice
        assert parsed.type_code not in incoming.CREDIT_NOTE_CODES      # Ausgangslage: normale Rechnung
        doc = store(xml.replace(b"<ram:TypeCode>380</ram:TypeCode>", b"<ram:TypeCode>381</ram:TypeCode>", 1), name="g.xml")
        assert doc.kind == "credit_note"

    def test_xml_that_is_no_einvoice_is_refused(self, app, tenant):
        with pytest.raises(svc.DocumentError, match="nur als E-Rechnung"):
            store(b"<?xml version='1.0'?><html><body/></html>", name="x.xml")
        assert Document.query.count() == 0

    @pytest.mark.parametrize("data, message", [
        (b"", "leer"),
        (b"GIF89a" + b"\x00" * 30, "nicht unterstützt"),
        (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "HEIC"),
        (b"%PDF-1.7 das ist kein PDF", "beschädigt"),
    ])
    def test_refused_content(self, app, tenant, data, message):
        with pytest.raises(svc.DocumentError, match=message):
            store(data)
        assert Document.query.count() == 0 and not (tenant / "documents").exists()

    def test_a_password_protected_pdf_is_refused(self, app, tenant):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt(user_password="geheim", owner_password="chef")
        buf = io.BytesIO()
        writer.write(buf)
        with pytest.raises(svc.DocumentError, match="Passwort"):
            store(buf.getvalue())

    def test_a_pdf_with_only_an_owner_password_is_accepted(self, app, tenant):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt(user_password="", owner_password="chef")      # nur Rechte-Sperre, jeder darf lesen
        buf = io.BytesIO()
        writer.write(buf)
        assert store(buf.getvalue()).storage_key

    def test_the_size_limit(self, app, tenant, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_MAX_UPLOAD_MB", 1)
        big = pdf() + b"0" * (1024 * 1024)
        with pytest.raises(svc.DocumentError, match="größer als 1 MB"):
            store(big)
        assert Document.query.count() == 0

    def test_the_same_file_twice_is_a_duplicate(self, app, tenant):
        first = store(pdf(1))
        with pytest.raises(svc.DuplicateUpload) as dup:
            store(pdf(1), name="anderer-name.pdf")
        assert dup.value.existing.id == first.id and Document.query.count() == 1

    def test_a_failed_commit_leaves_no_file_behind(self, app, tenant, monkeypatch):
        def boom():
            raise RuntimeError("Datenbank weg")
        monkeypatch.setattr(db.session, "commit", boom)
        with pytest.raises(RuntimeError):
            store(pdf(2))
        monkeypatch.undo()
        db.session.rollback()
        assert not list((tenant / "documents").rglob("*.pdf"))

    def test_the_upload_detail_is_logged(self, app, tenant):
        doc = store(pdf(3), upload_detail={"verkleinert": True, "original_groesse": 5_000_000})
        event = DocumentEvent.query.filter_by(document_id=doc.id).one()
        assert event.detail_dict["verkleinert"] is True and event.detail_dict["original_groesse"] == 5_000_000


class TestCapacity:
    def test_no_limit_by_default(self, app, tenant):
        assert svc.quota_status()["limit"] is None
        store(pdf(10))

    def test_the_quota_from_the_config(self, app, tenant, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_QUOTA_MB", 1)
        store(pdf(11) + b"0" * 600_000)
        with pytest.raises(svc.QuotaExceeded, match="Belegspeicher ist voll"):
            store(pdf(12) + b"0" * 600_000)
        assert Document.query.count() == 1

    def test_the_resolver_wins_over_the_config(self, app, tenant, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_QUOTA_MB", 1000)
        monkeypatch.setitem(app.extensions, "documents.quota_resolver", lambda: 1)
        status = svc.quota_status()
        assert status["limit"] == 1024 * 1024
        store(pdf(13) + b"0" * 700_000)
        with pytest.raises(svc.QuotaExceeded):
            store(pdf(14) + b"0" * 700_000)

    @pytest.mark.parametrize("resolver", [lambda: None, lambda: 0, lambda: 1 / 0])
    def test_the_resolver_fails_open(self, app, tenant, monkeypatch, resolver):
        monkeypatch.setitem(app.extensions, "documents.quota_resolver", resolver)
        assert svc.quota_status()["limit"] is None
        store(pdf(15))

    def test_usage_counts_the_stored_bytes(self, app, tenant):
        a, b = store(pdf(20)), store(pdf(21))
        assert svc.quota_status()["used"] == a.size_bytes + b.size_bytes

    def test_the_disk_guard(self, app, tenant, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_MIN_FREE_DISK_MB", 1024)
        monkeypatch.setattr(storage, "free_bytes", lambda: 500 * 1024 * 1024)
        with pytest.raises(svc.StorageUnavailable, match="zu wenig Speicherplatz"):
            store(pdf(30))
        monkeypatch.setattr(storage, "free_bytes", lambda: 5 * 1024 ** 3)
        store(pdf(30))


class TestLinking:
    def test_booked_is_derived_from_an_effective_link(self, app, tenant, account, user):
        doc = store(user_id=user.id)
        b = booking(account)
        assert not svc.is_booked(doc) and Document.query.filter(svc.booked_clause()).count() == 0
        svc.link(doc, booking=b, user_id=user.id)
        db.session.commit()
        assert svc.is_booked(doc) and Document.query.filter(svc.booked_clause()).one().id == doc.id
        assert actions(doc) == ["uploaded", "linked"]

    def test_the_tabs(self, app, tenant, account):
        inbox, booked, filed, discarded = store(pdf(40)), store(pdf(41)), store(pdf(42)), store(pdf(43))
        svc.link(booked, booking=booking(account))
        svc.shelve(filed)
        svc.discard(discarded)
        db.session.commit()
        ids = lambda tab: {d.id for d in Document.query.filter(svc.tab_filter(tab))}   # noqa: E731
        assert ids("inbox") == {inbox.id} and ids("booked") == {booked.id}
        assert ids("filed") == {filed.id} and ids("discarded") == {discarded.id}
        assert svc.tab_filter("all") is None and svc.tab_counts() == {"inbox": 1, "booked": 1, "filed": 1, "discarded": 1}

    def test_linking_is_idempotent(self, app, tenant, account):
        doc, b = store(), booking(account)
        first = svc.link(doc, booking=b)
        again = svc.link(doc, booking=b)
        db.session.commit()
        assert first is again and DocumentLink.query.count() == 1 and actions(doc) == ["uploaded", "linked"]

    def test_one_document_can_serve_several_bookings(self, app, tenant, account):
        doc = store()
        svc.link(doc, booking=booking(account))
        svc.link(doc, booking=booking(account, amount="-20.00"))
        db.session.commit()
        assert DocumentLink.query.count() == 2

    def test_a_group_gets_the_document_at_its_header(self, app, tenant, account):
        doc, g = store(), group(account)
        svc.link(doc, group=g)
        db.session.commit()
        assert svc.is_booked(doc) and DocumentLink.query.one().booking_group_id == g.id
        child = g.children[0]
        with pytest.raises(svc.DocumentError, match="Sammelbuchung"):
            svc.link(store(pdf(5)), booking=child)

    def test_a_filed_document_is_back_in_the_workflow_when_linked(self, app, tenant, account):
        doc = store()
        svc.shelve(doc)
        svc.link(doc, booking=booking(account))
        assert doc.status == "Neu" and svc.is_booked(doc)

    def test_a_discarded_document_cannot_be_linked(self, app, tenant, account):
        doc = store()
        svc.discard(doc)
        with pytest.raises(svc.DocumentError, match="verworfen"):
            svc.link(doc, booking=booking(account))

    def test_a_cancelled_booking_takes_no_new_documents(self, app, tenant, account):
        b = booking(account)
        b.status = Booking.STATUS_STORNIERT
        with pytest.raises(svc.DocumentError, match="stornierte Buchung"):
            svc.link(store(), booking=b)
        g = group(account)
        g.status = BookingGroup.STATUS_STORNIERT
        with pytest.raises(svc.DocumentError, match="stornierte Sammelbuchung"):
            svc.link(store(pdf(6)), group=g)

    def test_exactly_one_target(self, app, tenant, account):
        with pytest.raises(ValueError):
            svc.link(store(), booking=booking(account), group=group(account))
        with pytest.raises(ValueError):
            svc.link(store(pdf(7)))

    def test_a_storno_sends_the_document_back_to_the_inbox_but_keeps_the_link(self, app, tenant, account):
        doc, b = store(), booking(account)
        svc.link(doc, booking=b)
        db.session.commit()
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        assert not svc.is_booked(doc)
        assert Document.query.filter(svc.tab_filter("inbox")).one().id == doc.id
        assert DocumentLink.query.count() == 1                 # bleibt als Nachweis

    def test_a_storno_of_a_group_does_the_same(self, app, tenant, account):
        doc, g = store(), group(account)
        svc.link(doc, group=g)
        db.session.commit()
        g.status = BookingGroup.STATUS_STORNIERT
        db.session.commit()
        assert not svc.is_booked(doc) and Document.query.filter(svc.tab_filter("inbox")).count() == 1

    def test_the_storno_counterpart_does_not_count(self, app, tenant, account):
        doc, b = store(), booking(account)
        svc.link(doc, booking=b)
        db.session.commit()
        storno = booking(account, amount="10.00", storno_of_id=b.id)
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        assert storno.storno_of_id == b.id and not svc.is_booked(doc)

    def test_document_of_finds_the_einvoice_behind_a_booking(self, app, tenant, account):
        einvoice, plain, b = store(_fixture(CII), name="r.xml"), store(), booking(account)
        svc.link(plain, booking=b)
        assert svc.document_of(booking_id=b.id) is None        # nur E-Rechnungen
        svc.link(einvoice, booking=b)
        assert svc.document_of(booking_id=b.id).id == einvoice.id


class TestUnlinking:
    def test_unlink_in_an_open_year(self, app, tenant, account, user):
        doc, b = store(), booking(account)
        row = svc.link(doc, booking=b)
        db.session.commit()
        svc.unlink(row, user_id=user.id, reason="falscher Beleg")
        db.session.commit()
        assert DocumentLink.query.count() == 0 and not svc.is_booked(doc)
        assert actions(doc) == ["uploaded", "linked", "unlinked"]
        assert DocumentEvent.query.filter_by(action="unlinked").one().detail_dict["reason"] == "falscher Beleg"

    def test_unlink_in_a_closed_year_is_refused(self, app, tenant, account):
        db.session.add(FiscalYear(year=2026, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), closed=True))
        db.session.commit()
        row = svc.link(store(), booking=booking(account, day=date(2026, 3, 1)))
        db.session.commit()
        assert "abgeschlossen" in svc.unlink_blocker(row)
        with pytest.raises(svc.DocumentError, match="abgeschlossen"):
            svc.unlink(row)
        assert DocumentLink.query.count() == 1

    def test_linking_in_a_closed_year_stays_possible(self, app, tenant, account):
        db.session.add(FiscalYear(year=2026, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), closed=True))
        db.session.commit()
        svc.link(store(), booking=booking(account, day=date(2026, 3, 1)))        # es kommt nur etwas dazu
        db.session.commit()
        assert DocumentLink.query.count() == 1

    def test_unlink_after_a_storno_is_refused(self, app, tenant, account):
        b = booking(account)
        row = svc.link(store(), booking=b)
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        assert "storniert" in svc.unlink_blocker(row)
        with pytest.raises(svc.DocumentError):
            svc.unlink(row)

    def test_deleting_a_booking_logs_and_removes_its_links(self, app, tenant, account, user):
        doc, b = store(), booking(account)
        svc.link(doc, booking=b)
        db.session.commit()
        assert svc.on_booking_deleted(booking=b, user_id=user.id) == 1
        db.session.delete(b)
        db.session.commit()
        assert DocumentLink.query.count() == 0 and Booking.query.count() == 0 and Document.query.count() == 1
        event = DocumentEvent.query.filter_by(action="unlinked").one()
        assert event.detail_dict["reason"] == "Buchung gelöscht" and event.user_id == user.id

    def test_deleting_a_group_does_the_same(self, app, tenant, account):
        doc, g = store(), group(account)
        svc.link(doc, group=g)
        db.session.commit()
        assert svc.on_booking_deleted(group=g) == 1
        for child in list(g.children):
            db.session.delete(child)
        db.session.delete(g)
        db.session.commit()
        assert DocumentLink.query.count() == 0 and Document.query.count() == 1

    def test_deleting_without_the_hook_still_cascades(self, app, tenant, account):
        """Sicherheitsnetz: der ORM-Cascade loest die Links auch dann, wenn ein Weg den Hook vergisst."""
        b = booking(account)
        svc.link(store(), booking=b)
        db.session.commit()
        db.session.delete(b)
        db.session.commit()
        assert DocumentLink.query.count() == 0

    def test_the_group_edit_keeps_the_link(self, app, tenant, account):
        """Das Bearbeiten loescht und legt alle Kinder neu an — der Beleg haengt am Header."""
        doc, g = store(), group(account)
        svc.link(doc, group=g)
        db.session.commit()
        for child in list(g.children):
            db.session.delete(child)
        db.session.flush()
        db.session.add(Booking(date=g.date, account_id=account.id, amount=Decimal("-30"), description="neu", group_id=g.id))
        db.session.commit()
        assert svc.is_booked(doc) and DocumentLink.query.count() == 1


class TestWorkflow:
    def test_shelve_discard_reopen(self, app, tenant, user):
        doc = store(user_id=user.id)
        svc.shelve(doc, user.id)
        assert doc.status == "Abgelegt"
        svc.reopen(doc, user.id)
        svc.discard(doc, user.id)
        assert doc.status == "Verworfen"
        svc.reopen(doc, user.id)
        assert doc.status == "Neu" and actions(doc) == ["uploaded", "filed", "reopened", "discarded", "reopened"]

    def test_a_booked_document_cannot_be_shelved_or_discarded(self, app, tenant, account):
        doc = store()
        svc.link(doc, booking=booking(account))
        with pytest.raises(svc.DocumentError, match="schon zu einer Buchung"):
            svc.shelve(doc)
        with pytest.raises(svc.DocumentError, match="nicht verwerfen"):
            svc.discard(doc)

    def test_only_a_document_in_the_inbox_can_be_shelved(self, app, tenant):
        doc = store()
        svc.discard(doc)
        with pytest.raises(svc.DocumentError):
            svc.shelve(doc)

    def test_a_cancelled_booking_no_longer_blocks_discarding(self, app, tenant, account):
        doc, b = store(), booking(account)
        svc.link(doc, booking=b)
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        svc.discard(doc)
        assert doc.status == "Verworfen"


class TestDeleting:
    def test_a_never_linked_document_can_be_deleted(self, app, tenant, user):
        doc = store(user_id=user.id)
        key = doc.storage_key
        assert svc.can_delete(doc) == (True, None)
        svc.delete(doc, user.id)
        assert Document.query.count() == 0 and not storage.exists(key)
        # das Protokoll bleibt vollstaendig erhalten (document_id wird NULL, Name + Pruefsumme stehen am Ereignis)
        assert sorted(e.action for e in DocumentEvent.query) == ["deleted", "uploaded"]
        assert all(e.document_id is None and e.document_sha256 == doc.sha256 and e.document_name == "beleg.pdf"
                   for e in DocumentEvent.query)

    def test_a_discarded_never_linked_document_can_be_deleted(self, app, tenant):
        doc = store()
        svc.discard(doc)
        svc.delete(doc)
        assert Document.query.count() == 0

    def test_a_filed_document_cannot(self, app, tenant):
        doc = store()
        svc.shelve(doc)
        allowed, reason = svc.can_delete(doc)
        assert not allowed and "aufbewahrungspflichtig" in reason
        with pytest.raises(svc.DocumentError):
            svc.delete(doc)
        assert storage.exists(doc.storage_key)

    def test_a_linked_document_cannot(self, app, tenant, account):
        doc = store()
        svc.link(doc, booking=booking(account))
        assert not svc.can_delete(doc)[0]

    def test_a_document_that_was_ever_linked_cannot_even_after_unlinking(self, app, tenant, account):
        doc, b = store(), booking(account)
        row = svc.link(doc, booking=b)
        db.session.commit()
        svc.unlink(row)
        db.session.commit()
        assert not svc.is_booked(doc) and not svc.can_delete(doc)[0]

    def test_the_einvoice_data_goes_with_the_document(self, app, tenant):
        doc = store(_fixture(CII), name="r.xml")
        svc.delete(doc)
        assert IncomingInvoice.query.count() == 0


class TestMetaAndRetention:
    def test_update_meta_logs_the_change(self, app, tenant, user):
        doc = store()
        supplier = Customer(name="Lieferant", is_supplier=True, is_customer=False)
        db.session.add(supplier)
        db.session.commit()
        changes = svc.update_meta(doc, title="Kassenbon Baumarkt", number="B-17", document_date=date(2026, 5, 1),
                                  amount=Decimal("23.40"), supplier_id=supplier.id, kind="receipt", user_id=user.id)
        db.session.commit()
        assert set(changes) == {"title", "number", "document_date", "amount", "supplier_id", "kind"}
        assert doc.display_title == "Kassenbon Baumarkt" and doc.kind_label == "Kassenbon / Quittung"
        assert svc.update_meta(doc, title="Kassenbon Baumarkt", number="B-17", document_date=date(2026, 5, 1),
                               amount=Decimal("23.40"), supplier_id=supplier.id, kind="receipt") == {}
        assert actions(doc) == ["uploaded", "edited"]

    def test_the_data_of_an_einvoice_is_not_editable(self, app, tenant):
        doc = store(_fixture(CII), name="r.xml")
        with pytest.raises(svc.DocumentError, match="aus der Datei"):
            svc.update_meta(doc, title="x", number=None, document_date=None, amount=None, supplier_id=None, kind="invoice")

    def test_an_unknown_kind_is_refused(self, app, tenant):
        with pytest.raises(svc.DocumentError, match="Belegart"):
            svc.update_meta(store(), title="", number="", document_date=None, amount=None, supplier_id=None, kind="spam")

    @pytest.mark.parametrize("country_code, years", [("AT", 7), ("DE", 8)])
    def test_the_retention_period_follows_the_country(self, app, tenant, country_code, years):
        AppSetting.set("org.country", country_code)
        db.session.commit()
        doc = store()
        doc.document_date = date(2024, 6, 1)
        assert svc.retention_end(doc) == date(max(2024, date.today().year) + years, 12, 31)
        assert svc.retention_hint()

    def test_the_latest_booking_year_counts(self, app, tenant, account):
        AppSetting.set("org.country", "AT")
        doc = store()
        doc.document_date = date(2010, 1, 1)
        doc.created_at = datetime(2010, 1, 1)
        svc.link(doc, booking=booking(account, day=date(2019, 11, 5)))
        db.session.flush()
        assert svc.retention_end(doc) == date(2019 + 7, 12, 31)

    def test_a_cancelled_booking_still_counts(self, app, tenant, account):
        AppSetting.set("org.country", "DE")
        doc, b = store(), booking(account, day=date(2031, 2, 1))
        doc.created_at = datetime(2010, 1, 1)
        doc.document_date = None
        svc.link(doc, booking=b)
        b.status = Booking.STATUS_STORNIERT
        db.session.flush()
        assert svc.retention_end(doc) == date(2031 + 8, 12, 31)


class TestQueries:
    def test_counts_by_entity(self, app, tenant, account):
        b1, b2, g = booking(account), booking(account), group(account)
        svc.link(store(pdf(50)), booking=b1)
        svc.link(store(pdf(51)), booking=b1)
        svc.link(store(pdf(52)), group=g)
        db.session.commit()
        assert svc.counts_by_entity("booking", [b1.id, b2.id]) == {b1.id: 2}
        assert svc.counts_by_entity("booking_group", [g.id]) == {g.id: 1}
        assert svc.counts_by_entity("booking", []) == {}

    def test_links_for(self, app, tenant, account):
        b = booking(account)
        svc.link(store(pdf(60)), booking=b)
        svc.link(store(pdf(61)), booking=b)
        db.session.commit()
        assert [row.document.sha256 for row in svc.links_for("booking", b.id)] == \
               [d.sha256 for d in Document.query.order_by(Document.id)]

    def test_parse_document_ids(self, app, tenant):
        a, discarded = store(pdf(70)), store(pdf(71))
        svc.discard(discarded)
        db.session.commit()
        assert svc.parse_document_ids([str(a.id), str(a.id), "x", "", "²"]) == ([a.id], None)
        ids, error = svc.parse_document_ids(["9999"])
        assert "existiert nicht mehr" in error
        ids, error = svc.parse_document_ids([str(discarded.id)])
        assert "verworfen" in error
        assert svc.parse_document_ids(None) == ([], None)

    def test_by_ids_keeps_the_order_and_skips_unknown(self, app, tenant):
        a, b = store(pdf(80)), store(pdf(81))
        assert [d.id for d in svc.by_ids([b.id, 999, a.id])] == [b.id, a.id]

    def test_inbox_choices(self, app, tenant, account):
        inbox, linked, discarded = store(pdf(90)), store(pdf(91)), store(pdf(92))
        svc.link(linked, booking=booking(account))
        svc.discard(discarded)
        db.session.commit()
        assert [d.id for d in svc.inbox_choices()] == [inbox.id]

    def test_candidates_match_amount_and_date(self, app, tenant, account):
        doc = store(_fixture(CII), name="r.xml")              # 336,90 €, 04.04.2016, Rechnung → Ausgabe
        hit = booking(account, amount="-336.90", day=date(2016, 4, 20))
        booking(account, amount="-336.90", day=date(2018, 1, 1))                # zu weit weg
        booking(account, amount="-100.00", day=date(2016, 4, 20))               # anderer Betrag
        booking(account, amount="336.90", day=date(2016, 4, 20))                # falsches Vorzeichen
        taken = booking(account, amount="-336.90", day=date(2016, 4, 21))
        svc.link(store(pdf(100)), booking=taken)                                # hat schon einen Beleg
        db.session.commit()
        assert [(kind, obj.id) for kind, obj in svc.candidates(doc)] == [("booking", hit.id)]

    def test_candidates_of_a_document_without_amount(self, app, tenant):
        assert svc.candidates(store()) == []

    def test_booking_prefill(self, app, tenant):
        doc = store(_fixture(CII), name="r.xml")
        prefill = svc.booking_prefill(doc)
        assert prefill["amount"] == Decimal("-336.90") and prefill["reference"] == "123456XX"
        assert prefill["description"] == "[Seller name]" and prefill["date"] == date(2016, 4, 4)
        doc.document_date = date.today() + timedelta(days=5)
        assert svc.booking_prefill(doc)["date"] == date.today()              # nie in der Zukunft
        doc.kind = "credit_note"
        assert svc.booking_prefill(doc)["amount"] == Decimal("336.90")
        assert svc.booking_prefill(store(pdf(110)))["amount"] is None
