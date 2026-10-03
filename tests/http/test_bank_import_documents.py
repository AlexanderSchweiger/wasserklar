"""Bankimport und Belegablage: die Auszugsdatei wird automatisch abgelegt und an die Buchungen geknuepft."""
import io
from datetime import date
from decimal import Decimal

import pytest

from app.bank_import import services as bank_services
from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import (
    Account, BankStatement, BankStatementLine, Booking, BookingGroup, Document, DocumentEvent, DocumentLink,
    FiscalYear, RealAccount, User,
)
from tests.conftest import _ensure_role
from tests.http.test_documents import pdf_dir  # noqa: F401  (Fixture)
from tests.unit.test_bank_import_ofx import OFX_1X, OFX_2X


@pytest.fixture
def setup(app, pdf_dir):
    role = _ensure_role("Admin")
    admin = User(username="admin", email="admin@test.com", role_id=role.id)
    admin.set_password("secret")
    ra = RealAccount(name="Giro", iban="AT942070604500050440", opening_balance=Decimal("0"))
    account = Account(name="Wassereinnahmen", code="W01")
    db.session.add_all([admin, ra, account, FiscalYear(year=2026, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))])
    db.session.commit()
    return admin, ra, account


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _import(client, content=OFX_2X, name="auszug.ofx"):
    return client.post("/bank-import/upload", data={"file": (io.BytesIO(content), name)},
                       content_type="multipart/form-data", follow_redirects=False)


def _statement_doc():
    return Document.query.filter_by(kind="bank_statement").one()


def _commit_all(client, stmt, account):
    for line in stmt.lines:
        line.selected = True
        line.override_account_id = account.id
    db.session.commit()
    return client.post(f"/bank-import/statements/{stmt.id}/commit", follow_redirects=True)


class TestFiling:
    def test_the_file_is_filed_unchanged_when_the_statement_is_read(self, client, setup):
        _login(client)
        r = _import(client)
        assert r.status_code == 302
        stmt = BankStatement.query.one()
        doc = _statement_doc()
        assert doc.sha256 == stmt.file_hash and storage.read(doc.storage_key) == OFX_2X
        assert (doc.status, doc.content_type, doc.original_name) == ("Abgelegt", "application/xml", "auszug.ofx")
        assert doc.storage_key.endswith(".xml") and doc.size_bytes == len(OFX_2X)
        # der Zeitraum steht so im Auszug: erste bis letzte Buchungszeile
        assert doc.title == "Kontoauszug Giro 09.06.2026 – 19.06.2026" and doc.document_date == date(2026, 6, 19)
        assert [e.action for e in doc.events] == ["uploaded"]
        assert doc.events[0].detail_dict["quelle"] == "Bankimport"
        assert svc.statement_document(stmt).id == doc.id

    def test_a_text_statement_is_filed_as_plain_text(self, client, setup):
        _login(client)
        _import(client, OFX_1X, "auszug.sta")
        doc = _statement_doc()
        assert doc.content_type == "text/plain" and doc.storage_key.endswith(".txt")
        assert storage.read(doc.storage_key) == OFX_1X

    def test_it_waits_in_the_filed_tab_not_in_the_inbox(self, client, setup):
        _login(client)
        _import(client)
        doc = _statement_doc()
        assert doc.id not in {d.id for d in svc.inbox_choices()}
        assert Document.query.filter(svc.tab_filter("filed")).one().id == doc.id

    def test_the_same_file_twice_is_one_document(self, client, setup):
        _login(client)
        _import(client)
        r = _import(client)                                           # derselbe Auszug: schon importiert
        assert r.status_code == 302 and Document.query.count() == 1 and BankStatement.query.count() == 1

    def test_the_file_is_served_as_a_download_never_inline(self, client, setup):
        _login(client)
        _import(client, OFX_1X, "auszug.sta")
        doc = _statement_doc()
        r = client.get(f"/accounting/documents/{doc.id}/file")
        assert r.status_code == 200 and r.mimetype == "text/plain"
        assert r.headers["Content-Disposition"].startswith("attachment") and r.headers["X-Content-Type-Options"] == "nosniff"
        r.close()

    def test_the_import_goes_on_when_the_file_cannot_be_filed(self, client, setup, monkeypatch):
        _login(client)
        monkeypatch.setattr(svc, "_quota_limit_bytes", lambda: 1)   # Belegspeicher voll
        r = client.post("/bank-import/upload", data={"file": (io.BytesIO(OFX_2X), "auszug.ofx")},
                        content_type="multipart/form-data", follow_redirects=True)
        html = r.get_data(as_text=True)
        assert BankStatement.query.count() == 1 and BankStatementLine.query.count() == 2   # Import steht
        assert Document.query.count() == 0
        assert "konnte aber nicht als Beleg abgelegt werden" in html and "Dokumentenspeicher ist voll" in html


class TestBooking:
    def test_every_created_booking_carries_the_statement(self, client, setup):
        _, _ra, account = setup
        _login(client)
        _import(client)
        stmt, doc = BankStatement.query.one(), _statement_doc()
        r = _commit_all(client, stmt, account)
        assert "2 Buchung(en) verbucht" in r.get_data(as_text=True)
        bookings = Booking.query.all()
        assert len(bookings) == 2
        assert {row.booking_id for row in DocumentLink.query.filter_by(document_id=doc.id)} == {b.id for b in bookings}
        doc = db.session.get(Document, doc.id)
        assert svc.is_booked(doc) and doc.status == "Neu"          # „verbucht“ ist abgeleitet
        assert [e.action for e in doc.events].count("linked") == 2

    def test_a_partial_commit_links_only_what_was_booked(self, client, setup):
        _, _ra, account = setup
        _login(client)
        _import(client)
        stmt, doc = BankStatement.query.one(), _statement_doc()
        first, second = stmt.lines.all()
        first.selected, first.override_account_id = True, account.id
        second.selected = False
        db.session.commit()
        client.post(f"/bank-import/statements/{stmt.id}/commit")
        assert DocumentLink.query.filter_by(document_id=doc.id).count() == 1

    def test_a_booking_without_an_invoice_is_still_offered_for_its_receipt(self, client, setup):
        """Der Kontoauszug zaehlt nicht als Beleg — die Buchung braucht ihre Rechnung weiterhin."""
        _, _ra, account = setup
        _login(client)
        _import(client)
        _commit_all(client, BankStatement.query.one(), account)
        invoice = svc.store_upload("rechnung.pdf", _pdf(), None)
        invoice.amount, invoice.kind, invoice.document_date = Decimal("15.50"), "invoice", date(2026, 6, 9)
        db.session.commit()
        offered = [obj for _kind, obj in svc.candidates(invoice)]
        assert [b.amount for b in offered] == [Decimal("-15.50")]
        svc.link(invoice, booking=offered[0])
        db.session.commit()
        assert svc.candidates(_second_invoice()) == []              # jetzt hat sie ihre Rechnung

    def test_nothing_breaks_for_statements_imported_before_the_filing_existed(self, client, setup):
        _, _ra, account = setup
        _login(client)
        _import(client)
        Document.query.delete()                                     # Altimport: keine Datei abgelegt
        DocumentEvent.query.delete()
        db.session.commit()
        r = _commit_all(client, BankStatement.query.one(), account)
        assert "2 Buchung(en) verbucht" in r.get_data(as_text=True) and DocumentLink.query.count() == 0


class TestAttach:
    def _doc(self):
        return svc.store_bank_statement("a.ofx", OFX_2X, None)

    def test_a_child_booking_hands_the_document_to_its_group(self, app, setup):
        _, ra, account = setup
        group = BookingGroup(date=date(2026, 3, 1), description="Sammel", total_amount=Decimal("-30"), status="Aktiv")
        db.session.add(group)
        db.session.flush()
        child = Booking(date=group.date, account_id=account.id, amount=Decimal("-30"), description="Zeile", group_id=group.id)
        db.session.add(child)
        db.session.commit()
        doc = self._doc()
        bank_services._attach_document(doc, [(child.id, None)], None)
        db.session.commit()
        assert [(l.booking_id, l.booking_group_id) for l in doc.links] == [(None, group.id)]

    def test_groups_and_single_bookings_and_garbage(self, app, setup):
        _, ra, account = setup
        group = BookingGroup(date=date(2026, 3, 1), description="Sammel", total_amount=Decimal("-30"), status="Aktiv")
        single = Booking(date=date(2026, 3, 2), account_id=account.id, amount=Decimal("-5"), description="Einzel")
        cancelled = Booking(date=date(2026, 3, 3), account_id=account.id, amount=Decimal("-6"), description="Storno",
                            status=Booking.STATUS_STORNIERT)
        db.session.add_all([group, single, cancelled])
        db.session.commit()
        doc = self._doc()
        bank_services._attach_document(doc, [(None, group.id), (single.id, None), (cancelled.id, None), (None, None)], None)
        db.session.commit()
        assert {(l.booking_id, l.booking_group_id) for l in doc.links} == {(None, group.id), (single.id, None)}

    def test_linking_twice_is_harmless(self, app, setup):
        _, ra, account = setup
        single = Booking(date=date(2026, 3, 2), account_id=account.id, amount=Decimal("-5"), description="Einzel")
        db.session.add(single)
        db.session.commit()
        doc = self._doc()
        for _ in range(2):
            bank_services._attach_document(doc, [(single.id, None)], None)
        db.session.commit()
        assert len(doc.links) == 1


class TestRelease:
    def test_deleting_an_unbooked_statement_takes_its_file_along(self, client, setup):
        _login(client)
        _import(client)
        stmt, doc = BankStatement.query.one(), _statement_doc()
        key = doc.storage_key
        client.post(f"/bank-import/statements/{stmt.id}/delete")
        assert BankStatement.query.count() == 0 and Document.query.count() == 0 and not storage.exists(key)
        event = DocumentEvent.query.filter_by(action="deleted").one()
        assert event.detail_dict["grund"] == "Importauszug gelöscht" and event.document_sha256 == doc.sha256

    def test_a_booked_statement_cannot_be_deleted_and_keeps_its_document(self, client, setup):
        _, _ra, account = setup
        _login(client)
        _import(client)
        _commit_all(client, BankStatement.query.one(), account)
        r = client.post(f"/bank-import/statements/{BankStatement.query.one().id}/delete", follow_redirects=True)
        assert "nicht gelöscht werden" in r.get_data(as_text=True)
        assert Document.query.count() == 1 and BankStatement.query.count() == 1

    def test_a_document_that_another_statement_still_needs_stays(self, app, setup):
        _, ra, _account = setup
        other = RealAccount(name="Sparkonto", iban="AT111", opening_balance=Decimal("0"))
        db.session.add(other)
        db.session.commit()
        doc = svc.store_bank_statement("a.ofx", OFX_2X, None)
        for account in (ra, other):
            db.session.add(BankStatement(format="ofx", filename="a.ofx", file_hash=doc.sha256, real_account_id=account.id))
        db.session.commit()
        first = BankStatement.query.filter_by(real_account_id=ra.id).one()
        db.session.delete(first)
        db.session.commit()
        assert svc.release_statement(doc, None) is False and Document.query.count() == 1

    def test_a_linked_document_is_never_released(self, app, setup):
        _, _ra, account = setup
        single = Booking(date=date(2026, 3, 2), account_id=account.id, amount=Decimal("-5"), description="Einzel")
        db.session.add(single)
        db.session.commit()
        doc = svc.store_bank_statement("a.ofx", OFX_2X, None)
        svc.link(doc, booking=single)
        db.session.commit()
        assert svc.release_statement(doc, None) is False and Document.query.count() == 1

    def test_something_that_is_no_statement_is_never_released(self, app, setup):
        assert svc.release_statement(None) is False
        doc = svc.store_upload("r.pdf", _pdf(), None)
        assert svc.release_statement(doc) is False and Document.query.count() == 1


def _pdf(n=1):
    from tests.integration.test_document_service import pdf
    return pdf(n)


def _second_invoice():
    doc = svc.store_upload("rechnung2.pdf", _pdf(2), None)
    doc.amount, doc.kind, doc.document_date = Decimal("15.50"), "invoice", date(2026, 6, 9)
    db.session.commit()
    return doc
