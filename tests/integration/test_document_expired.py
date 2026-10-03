"""Belegablage Stufe 2: Belege nach Ablauf der Aufbewahrungsfrist (AT 7 / DE 8 Jahre) loeschen."""
import json
from datetime import date, datetime

import pytest

from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import AppSetting, Booking, Document, DocumentEvent, DocumentLink
from tests.integration.test_document_service import account, booking, group, pdf, store, tenant, user  # noqa: F401

TODAY = date(2026, 10, 3)


def aged(doc, year, *, document_date=None):
    """Datiert Ablage- und Belegdatum zurueck, damit die Frist des Jahres ``year`` gilt."""
    doc.created_at = datetime(year, 6, 1)
    doc.document_date = document_date if document_date is not None else date(year, 3, 1)
    db.session.commit()
    return doc


def country(code):
    AppSetting.set("org.country", code)
    db.session.commit()


def ids(pairs):
    return [doc.id for doc, _end in pairs]


class TestExpiredDocuments:
    def test_austria_keeps_seven_full_years(self, app, tenant):
        country("AT")
        ripe = aged(store(pdf(1)), 2018)       # Ende 31.12.2025 → vorbei
        edge = aged(store(pdf(2)), 2019)       # Ende 31.12.2026 → laeuft noch
        assert ids(svc.expired_documents(TODAY)) == [ripe.id]
        assert edge.id not in ids(svc.expired_documents(TODAY))

    def test_germany_keeps_eight_years(self, app, tenant):
        country("DE")
        aged(store(pdf(1)), 2018)              # Ende 31.12.2026 → laeuft noch
        ripe = aged(store(pdf(2)), 2017)       # Ende 31.12.2025 → vorbei
        assert ids(svc.expired_documents(TODAY)) == [ripe.id]

    def test_the_last_day_of_the_term_still_counts_as_running(self, app, tenant):
        country("AT")
        aged(store(pdf(1)), 2018)
        assert svc.expired_documents(date(2025, 12, 31)) == []
        assert len(svc.expired_documents(date(2026, 1, 1))) == 1

    def test_a_recent_booking_extends_an_old_document(self, app, tenant, account):
        country("AT")
        old = aged(store(pdf(1)), 2015)
        svc.link(old, booking=booking(account, day=date(2024, 5, 1)))
        db.session.commit()
        assert svc.expired_documents(TODAY) == []              # die Buchung von 2024 haelt ihn bis 2031
        assert svc.retention_end(old) == date(2031, 12, 31)

    def test_old_bookings_do_not_keep_a_document_alive(self, app, tenant, account):
        country("AT")
        old = aged(store(pdf(1)), 2015)
        svc.link(old, booking=booking(account, day=date(2016, 5, 1)))
        svc.link(old, group=group(account, day=date(2016, 6, 1)))
        db.session.commit()
        assert ids(svc.expired_documents(TODAY)) == [old.id]

    def test_the_oldest_end_comes_first(self, app, tenant):
        country("AT")
        newer = aged(store(pdf(1)), 2017)
        older = aged(store(pdf(2)), 2014)
        assert ids(svc.expired_documents(TODAY)) == [older.id, newer.id]

    def test_without_any_date_the_upload_day_counts(self, app, tenant):
        country("AT")
        doc = store(pdf(1))
        doc.created_at = datetime(2015, 1, 1)
        doc.document_date = None
        db.session.commit()
        assert ids(svc.expired_documents(TODAY)) == [doc.id]


class TestClauseMatchesRetentionEnd:
    """Die SQL-Bedingung (billiger Zaehler) und ``retention_end`` (Anzeige, Loeschen) muessen dasselbe sagen."""

    @pytest.mark.parametrize("code", ["AT", "DE"])
    def test_every_combination_of_dates(self, app, tenant, account, code):
        country(code)
        created_years = (2010, 2017, 2018, 2019, 2025)
        doc_dates = (None, date(2010, 12, 31), date(2018, 1, 1), date(2019, 12, 31))
        booking_dates = (None, date(2012, 6, 1), date(2018, 12, 31), date(2019, 1, 1), date(2024, 1, 1))
        n = 0
        for created in created_years:
            for doc_date in doc_dates:
                for booked in booking_dates:
                    n += 1
                    doc = store(pdf(n))
                    doc.created_at = datetime(created, 5, 5)
                    doc.document_date = doc_date
                    if booked is not None:
                        svc.link(doc, booking=booking(account, day=booked))
                    db.session.commit()
        by_clause = {d.id for d in Document.query.filter(svc.expired_clause(TODAY)).all()}
        by_python = {d.id for d in Document.query.all() if svc.retention_end(d) < TODAY}
        assert by_clause == by_python and by_clause, (len(by_clause), len(by_python))
        assert len(by_clause) < Document.query.count()                # nicht alle, nicht keiner
        assert svc.expired_count(TODAY) == len(by_clause) == len(svc.expired_documents(TODAY))


class TestDeleteExpired:
    def test_an_expired_booked_document_is_removed_with_file_and_links(self, app, tenant, account, user):
        country("AT")
        doc = aged(store(pdf(1)), 2015)
        paid = booking(account, day=date(2016, 5, 1))
        svc.link(doc, booking=paid, user_id=user.id)
        db.session.commit()
        key, sha = doc.storage_key, doc.sha256

        svc.delete_expired(doc, user.id, today=TODAY)

        assert Document.query.count() == 0 and DocumentLink.query.count() == 0
        assert not storage.exists(key)
        assert db.session.get(Booking, paid.id) is not None              # die Buchung bleibt
        event = DocumentEvent.query.filter_by(action="deleted").one()
        assert event.document_id is None and event.document_sha256 == sha and event.user_id == user.id
        detail = json.loads(event.detail)
        assert detail["grund"] == "Aufbewahrungsfrist abgelaufen" and detail["buchungen"] == [paid.id]
        assert detail["aufbewahrt_bis"] == "2023-12-31"

    def test_a_running_term_refuses_and_changes_nothing(self, app, tenant):
        country("AT")
        doc = aged(store(pdf(1)), 2024)
        key = doc.storage_key
        with pytest.raises(svc.DocumentError, match="läuft noch bis 31.12.2031"):
            svc.delete_expired(doc, None, today=TODAY)
        assert Document.query.count() == 1 and storage.exists(key)
        assert not DocumentEvent.query.filter_by(action="deleted").count()

    def test_the_term_is_recomputed_not_trusted(self, app, tenant, account):
        """Eine spaetere Buchung verlaengert die Frist auch dann, wenn der Beleg einmal in der Liste stand."""
        country("AT")
        doc = aged(store(pdf(1)), 2015)
        assert ids(svc.expired_documents(TODAY)) == [doc.id]
        svc.link(doc, booking=booking(account, day=date(2025, 1, 1)))
        db.session.commit()
        with pytest.raises(svc.DocumentError):
            svc.delete_expired(doc, None, today=TODAY)
        assert Document.query.count() == 1

    def test_an_einvoice_goes_with_its_data(self, app, tenant):
        from tests.unit.test_einvoice_incoming import _fixture
        from app.models import IncomingInvoice
        country("AT")
        doc = aged(store(_fixture("kosit-01.01a-INVOICE_uncefact.xml"), name="r.xml"), 2015)
        svc.delete_expired(doc, None, today=TODAY)
        assert Document.query.count() == 0 and IncomingInvoice.query.count() == 0

    def test_a_missing_file_does_not_stop_the_deletion(self, app, tenant):
        country("AT")
        doc = aged(store(pdf(1)), 2015)
        storage.delete(doc.storage_key)
        svc.delete_expired(doc, None, today=TODAY)
        assert Document.query.count() == 0
