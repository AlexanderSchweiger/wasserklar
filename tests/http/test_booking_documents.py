"""Belege an Buchungen: Beleg-Auswahl im Formular, Vorbefuellung aus dem Beleg, Panel + Bueroklammer.

Buchungsformular (Modal, ``hx-post``) und Sammelbuchung (normales Formular) nehmen eine oder mehrere
``document_ids`` entgegen; die Buchungsliste zeigt je Buchung eine Bueroklammer, das Panel im Modal
haengt Belege an, loest sie oder laedt neue hoch.
"""
from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal

import pytest

from app.documents import service as svc
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Document, DocumentEvent, DocumentLink, FiscalYear,
    RealAccount, User,
)
from tests.conftest import _ensure_role
from tests.integration.test_document_service import pdf
from tests.unit.test_einvoice_incoming import _fixture

HX = {"HX-Request": "true"}
CII = "kosit-01.01a-INVOICE_uncefact.xml"


@pytest.fixture
def pdf_dir(app, tmp_path, monkeypatch):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return folder


@pytest.fixture
def admin(app):
    u = User(username="admin", email="admin@test.com", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": "secret"})


@pytest.fixture
def fx(app, pdf_dir):
    today = date.today()
    db.session.add_all([
        FiscalYear(year=today.year, start_date=date(today.year, 1, 1), end_date=date(today.year, 12, 31)),
        Account(name="Materialaufwand", code="M01"), Account(name="Wassereinnahmen", code="W01"),
        RealAccount(name="Girokonto", iban="AT001", opening_balance=Decimal("0"), is_default=True),
    ])
    db.session.commit()
    return {"acc": Account.query.filter_by(code="M01").one(), "acc2": Account.query.filter_by(code="W01").one(),
            "ra": RealAccount.query.one()}


def doc(n=0, **kw):
    d = svc.store_upload(f"beleg{n}.pdf", pdf(n), None, **kw)
    return d


def payload(fx, *docs, **over):
    data = {"date": date.today().isoformat(), "amount": "-145.00", "tax_rate": "20", "account_id": fx["acc"].id,
            "project_id": "", "description": "Rechnung Baumarkt", "reference": "BM-1", "customer_id": "",
            "real_account_id": fx["ra"].id, "action": "", "document_ids": [str(d.id) for d in docs]}
    data.update(over)
    return data


def make_booking(fx, amount="-10.00", **kw):
    b = Booking(date=date.today(), account_id=fx["acc"].id, amount=Decimal(amount), description="Buchung", **kw)
    db.session.add(b)
    db.session.commit()
    return b


def make_group(fx):
    g = BookingGroup(date=date.today(), description="Sammel", total_amount=Decimal("-30"), status="Aktiv")
    db.session.add(g)
    db.session.flush()
    for amount in ("-10", "-20"):
        db.session.add(Booking(date=g.date, account_id=fx["acc"].id, amount=Decimal(amount),
                               description="Zeile", group_id=g.id))
    db.session.commit()
    return g


class TestBookingForm:
    def test_the_form_offers_the_picker(self, client, admin, fx):
        inbox = doc(1)
        _login(client)
        html = client.get("/accounting/bookings/new", headers=HX).get_data(as_text=True)
        assert "data-doc-picker" in html and "data-doc-pick-upload" in html and "Belege" in html
        assert f'value="{inbox.id}"' in html and 'data-title="beleg1.pdf"' in html       # Auswahl aus dem Eingang

    def test_a_new_booking_takes_the_documents(self, client, admin, fx):
        a, b = doc(1), doc(2)
        _login(client)
        r = client.post("/accounting/bookings/new", data=payload(fx, a, b), headers=HX)
        assert r.status_code == 200 and "booking-saved" in r.headers["HX-Trigger"]
        booking = Booking.query.one()
        assert sorted(row.document_id for row in DocumentLink.query.filter_by(booking_id=booking.id)) == sorted([a.id, b.id])
        assert svc.is_booked(a) and svc.is_booked(b)
        assert DocumentEvent.query.filter_by(action="linked").count() == 2

    def test_a_booking_without_documents_still_works(self, client, admin, fx):
        _login(client)
        r = client.post("/accounting/bookings/new", data=payload(fx), headers=HX)
        assert r.status_code == 200 and Booking.query.count() == 1 and DocumentLink.query.count() == 0

    def test_the_choice_survives_a_validation_error(self, client, admin, fx):
        a = doc(1)
        _login(client)
        r = client.post("/accounting/bookings/new", data=payload(fx, a, description=""), headers=HX)
        html = r.get_data(as_text=True)
        assert Booking.query.count() == 0 and "Bitte eine Beschreibung eingeben" in html
        assert f'<input type="hidden" name="document_ids" value="{a.id}">' in html     # noch gewaehlt
        assert 'data-title="beleg1.pdf"' not in html                                    # nicht doppelt anbieten

    @pytest.mark.parametrize("bad, message", [("9999", "existiert nicht mehr"), ("abc", None)])
    def test_unknown_documents_are_refused(self, client, admin, fx, bad, message):
        _login(client)
        data = payload(fx)
        data["document_ids"] = [bad]
        r = client.post("/accounting/bookings/new", data=data, headers=HX)
        if message:
            assert message in r.get_data(as_text=True) and Booking.query.count() == 0
        else:
            assert Booking.query.count() == 1                       # Muell wird ignoriert wie bei den anderen FKs

    def test_a_discarded_document_is_refused(self, client, admin, fx):
        a = doc(1)
        svc.discard(a)
        db.session.commit()
        _login(client)
        r = client.post("/accounting/bookings/new", data=payload(fx, a), headers=HX)
        assert "verworfen" in r.get_data(as_text=True) and Booking.query.count() == 0

    def test_a_full_page_post_works_too(self, client, admin, fx):
        a = doc(1)
        _login(client)
        r = client.post("/accounting/bookings/new", data=payload(fx, a))
        assert r.status_code == 302 and DocumentLink.query.one().document_id == a.id

    def test_editing_adds_documents_and_lists_the_linked_ones(self, client, admin, fx):
        first, second = doc(1), doc(2)
        b = make_booking(fx)
        svc.link(first, booking=b)
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX).get_data(as_text=True)
        assert "beleg1.pdf" in html and "zugeordnet" in html
        data = payload(fx, second, amount="-10.00", date=b.date.isoformat())
        r = client.post(f"/accounting/bookings/{b.id}/edit", data=data, headers=HX)
        assert r.status_code == 200 and "booking-saved" in r.headers["HX-Trigger"]
        assert DocumentLink.query.filter_by(booking_id=b.id).count() == 2

    def test_editing_never_drops_existing_links(self, client, admin, fx):
        first = doc(1)
        b = make_booking(fx)
        svc.link(first, booking=b)
        db.session.commit()
        _login(client)
        client.post(f"/accounting/bookings/{b.id}/edit", data=payload(fx, amount="-10.00", date=b.date.isoformat()), headers=HX)
        assert DocumentLink.query.filter_by(booking_id=b.id).count() == 1

    def test_prefill_from_a_document(self, client, admin, fx):
        e = svc.store_upload("r.xml", _fixture(CII), None)
        _login(client)
        r = client.get(f"/accounting/bookings/new?document_id={e.id}")
        html = r.get_data(as_text=True)
        assert 'value="123456XX"' in html and 'value="[Seller name]"' in html and 'value="-336.90"' in html
        assert f'name="document_ids" value="{e.id}"' in html and f'name="return_doc" value="{e.id}"' in html
        assert 'value="2016-04-04"' in html                            # Belegdatum

    def test_a_discarded_or_unknown_document_prefills_nothing(self, client, admin, fx):
        gone = doc(1)
        svc.discard(gone)
        db.session.commit()
        _login(client)
        for query in (f"?document_id={gone.id}", "?document_id=9999", "?document_id=abc"):
            html = client.get("/accounting/bookings/new" + query).get_data(as_text=True)
            assert not re.search(r'name="return_doc" value="\d+"', html)
            assert not re.search(r'name="document_ids" value="\d+"', html)

    def test_saving_from_the_document_page_returns_there(self, client, admin, fx):
        e = svc.store_upload("r.xml", _fixture(CII), None)
        _login(client)
        data = payload(fx, e, return_doc=str(e.id))
        r = client.post("/accounting/bookings/new", data=data)
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/accounting/documents/{e.id}")
        assert svc.is_booked(db.session.get(Document, e.id))

    def test_the_return_target_is_always_a_document_page(self, client, admin, fx):
        _login(client)
        r = client.post("/accounting/bookings/new", data=payload(fx, return_doc="http://evil.example/"))
        assert r.status_code == 302 and "evil.example" not in r.headers["Location"]


class TestGroupForm:
    def _children(self, fx):
        return {"child_account_id[]": [str(fx["acc"].id), str(fx["acc2"].id)], "child_project_id[]": ["", ""],
                "child_tax_rate[]": ["20", "10"], "child_amount[]": ["-100", "-50"],
                "child_description[]": ["Zeile 1", "Zeile 2"]}

    def _data(self, fx, *docs, **over):
        data = {"date": date.today().isoformat(), "description": "Sammelrechnung", "reference": "S-1",
                "customer_id": "", "invoice_id": "", "real_account_id": str(fx["ra"].id),
                "document_ids": [str(d.id) for d in docs], **self._children(fx)}
        data.update(over)
        return data

    def test_the_group_form_offers_the_picker(self, client, admin, fx):
        _login(client)
        html = client.get("/accounting/booking-groups/new").get_data(as_text=True)
        assert "data-doc-picker" in html

    def test_a_new_group_takes_the_documents_at_its_header(self, client, admin, fx):
        a = doc(1)
        _login(client)
        r = client.post("/accounting/booking-groups/new", data=self._data(fx, a))
        group = BookingGroup.query.one()
        assert r.status_code == 302 and DocumentLink.query.one().booking_group_id == group.id
        assert svc.is_booked(a)

    def test_a_validation_error_keeps_the_choice(self, client, admin, fx):
        a = doc(1)
        _login(client)
        r = client.post("/accounting/booking-groups/new", data=self._data(fx, a, description=""))
        html = r.get_data(as_text=True)
        assert BookingGroup.query.count() == 0 and f'name="document_ids" value="{a.id}"' in html

    def test_editing_a_group_keeps_its_documents_and_adds_new_ones(self, client, admin, fx):
        first, second = doc(1), doc(2)
        _login(client)
        client.post("/accounting/booking-groups/new", data=self._data(fx, first))
        group = BookingGroup.query.one()
        old_children = {c.id for c in group.children}
        r = client.post(f"/accounting/booking-groups/{group.id}/edit", data=self._data(fx, second, description="geändert"))
        assert r.status_code == 302
        db.session.expire_all()
        group = BookingGroup.query.one()
        assert group.description == "geändert" and len(group.children) == 2 and old_children      # Kinder neu angelegt
        assert sorted(l.document_id for l in DocumentLink.query.all()) == sorted([first.id, second.id])
        assert all(l.booking_group_id == group.id for l in DocumentLink.query.all())

    def test_the_edit_page_lists_the_linked_documents(self, client, admin, fx):
        a = doc(1)
        _login(client)
        client.post("/accounting/booking-groups/new", data=self._data(fx, a))
        group = BookingGroup.query.one()
        assert "beleg1.pdf" in client.get(f"/accounting/booking-groups/{group.id}/edit").get_data(as_text=True)


class TestBookingsList:
    def test_each_booking_has_a_paperclip(self, client, admin, fx):
        b = make_booking(fx)
        _login(client)
        html = client.get("/accounting/bookings").get_data(as_text=True)
        assert f'id="doc-pin-booking-{b.id}"' in html and f"openDocsModal('booking', {b.id})" in html
        assert "documents/_modal" not in html and 'id="docsModal"' in html          # das Modal ist eingebunden

    def test_the_count_shows_for_several_documents(self, client, admin, fx):
        b = make_booking(fx)
        for n in (1, 2, 3):
            svc.link(doc(n), booking=b)
        db.session.commit()
        _login(client)
        html = client.get("/accounting/bookings").get_data(as_text=True)
        assert 'title="Belege anzeigen (3)"' in html and 'bg-azure-lt ms-1">3<' in html

    def test_a_group_has_one_paperclip_at_its_header(self, client, admin, fx):
        g = make_group(fx)
        svc.link(doc(1), group=g)
        db.session.commit()
        _login(client)
        html = client.get("/accounting/bookings").get_data(as_text=True)
        assert f'id="doc-pin-booking_group-{g.id}"' in html and 'title="Belege anzeigen (1)"' in html
        assert f"doc-pin-booking-{g.children[0].id}" not in html                    # Kinder haben keine eigene

    def test_in_the_flat_view_the_children_show_the_group_paperclip(self, client, admin, fx):
        g = make_group(fx)
        svc.link(doc(1), group=g)
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/bookings?account_id={fx['acc'].id}").get_data(as_text=True)
        assert f'id="doc-pin-booking_group-{g.id}"' in html

    def test_the_table_fragment_needs_no_extra_queries_per_row(self, client, admin, fx, app):
        from sqlalchemy import event
        for n in range(8):
            make_booking(fx, amount=f"-{n + 1}.00")
        _login(client)
        statements = []

        def count(conn, cursor, statement, *args):
            if "document_links" in statement:
                statements.append(statement)
        event.listen(db.engine, "before_cursor_execute", count)
        try:
            client.get("/accounting/bookings", headers=HX)
        finally:
            event.remove(db.engine, "before_cursor_execute", count)
        assert 0 < len(statements) <= 4                                              # je Tabelle, nicht je Zeile


class TestPanel:
    def test_the_panel_lists_documents_and_offers_the_actions(self, client, admin, fx):
        b = make_booking(fx)
        svc.link(doc(1), booking=b)
        inbox = doc(2)
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/documents/panel/booking/{b.id}").get_data(as_text=True)
        assert "beleg1.pdf" in html and "data-doc-upload" in html and "Zuordnung lösen" in html
        assert f'<option value="{inbox.id}"' in html and "Aus dem Belegeingang" in html
        assert f'data-entity-type="booking" data-entity-id="{b.id}"' in html

    def test_a_group_panel(self, client, admin, fx):
        g = make_group(fx)
        svc.link(doc(1), group=g)
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/documents/panel/booking_group/{g.id}").get_data(as_text=True)
        assert "Sammelbuchung vom" in html and "beleg1.pdf" in html

    def test_attach_from_the_inbox(self, client, admin, fx):
        b = make_booking(fx)
        d = doc(1)
        _login(client)
        r = client.post(f"/accounting/documents/panel/booking/{b.id}/attach", data={"document_id": str(d.id)})
        assert r.status_code == 200 and "documents:changed" in r.headers["HX-Trigger"]
        assert f'"entity_id": {b.id}' in r.headers["HX-Trigger"]
        assert DocumentLink.query.one().booking_id == b.id and "beleg1.pdf" in r.get_data(as_text=True)

    def test_attach_errors_are_shown_in_the_panel(self, client, admin, fx):
        b = make_booking(fx)
        _login(client)
        r = client.post(f"/accounting/documents/panel/booking/{b.id}/attach", data={"document_id": ""})
        assert "Bitte einen Beleg wählen" in r.get_data(as_text=True) and DocumentLink.query.count() == 0
        gone = doc(1)
        svc.discard(gone)
        db.session.commit()
        r = client.post(f"/accounting/documents/panel/booking/{b.id}/attach", data={"document_id": str(gone.id)})
        assert "verworfen" in r.get_data(as_text=True) and DocumentLink.query.count() == 0

    def test_unlink_from_the_panel(self, client, admin, fx):
        b = make_booking(fx)
        row = svc.link(doc(1), booking=b)
        db.session.commit()
        _login(client)
        r = client.post(f"/accounting/documents/links/{row.id}/unlink", data={"ctx": "panel"})
        assert r.status_code == 200 and "documents:changed" in r.headers["HX-Trigger"]
        assert DocumentLink.query.count() == 0 and "Zu dieser Buchung gibt es noch keinen Beleg" in r.get_data(as_text=True)

    def test_a_locked_link_has_no_unlink_button(self, client, admin, fx):
        year = date.today().year
        FiscalYear.query.filter_by(year=year).one().closed = True
        db.session.commit()
        b = make_booking(fx)
        row = svc.link(doc(1), booking=b)
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/documents/panel/booking/{b.id}").get_data(as_text=True)
        assert "fa-lock" in html and "Zuordnung lösen" not in html
        r = client.post(f"/accounting/documents/links/{row.id}/unlink", data={"ctx": "panel"})
        assert "abgeschlossen" in r.get_data(as_text=True) and DocumentLink.query.count() == 1
        # anhaengen bleibt im abgeschlossenen Jahr erlaubt
        extra = doc(2)
        client.post(f"/accounting/documents/panel/booking/{b.id}/attach", data={"document_id": str(extra.id)})
        assert DocumentLink.query.count() == 2

    def test_a_group_child_cannot_take_a_document_directly(self, client, admin, fx):
        g = make_group(fx)
        child = g.children[0]
        _login(client)
        html = client.get(f"/accounting/documents/panel/booking/{child.id}").get_data(as_text=True)
        assert "gehört zu einer Sammelbuchung" in html and "data-doc-upload" not in html
        r = client.post(f"/accounting/documents/panel/booking/{child.id}/attach", data={"document_id": str(doc(1).id)})
        assert "Sammelbuchung" in r.get_data(as_text=True) and DocumentLink.query.count() == 0

    def test_a_cancelled_booking_takes_nothing_new(self, client, admin, fx):
        b = make_booking(fx)
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/documents/panel/booking/{b.id}").get_data(as_text=True)
        assert "Die Buchung ist storniert" in html and "data-doc-upload" not in html

    def test_unknown_entities(self, client, admin, fx):
        _login(client)
        assert client.get("/accounting/documents/panel/booking/9999").status_code == 404
        assert client.get("/accounting/documents/panel/customer/1").status_code == 404
        assert client.get("/accounting/documents/pin/booking/9999").status_code == 404

    def test_the_pin_fragment(self, client, admin, fx):
        b = make_booking(fx)
        svc.link(doc(1), booking=b)
        svc.link(doc(2), booking=b)
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/documents/pin/booking/{b.id}").get_data(as_text=True)
        assert f'id="doc-pin-booking-{b.id}"' in html and 'bg-azure-lt ms-1">2<' in html

    def test_upload_through_the_panel_links_at_once(self, client, admin, fx):
        b = make_booking(fx)
        _login(client)
        r = client.post("/accounting/documents/upload", data={
            "file": (io.BytesIO(pdf(5)), "neu.pdf"), "link_type": "booking", "link_id": str(b.id)},
            content_type="multipart/form-data", headers={"Accept": "application/json"})
        assert r.get_json()["linked"] is True and DocumentLink.query.one().booking_id == b.id
        assert "neu.pdf" in client.get(f"/accounting/documents/panel/booking/{b.id}").get_data(as_text=True)


class TestRights:
    def test_the_modal_and_pins_need_the_accounting_permission(self, client, admin, fx):
        role = _ensure_role("Nur Stammdaten", perms=("stammdaten",))
        user = User(username="leser", email="l@a.test", role_id=role.id)
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()
        _login(client, "leser")
        html = client.get("/customers/").get_data(as_text=True)
        assert 'id="docsModal"' not in html                          # nicht fuer Benutzer ohne Buchhaltungsrecht
        r = client.get("/accounting/documents/panel/booking/1")
        assert r.status_code == 302
        _login(client)
        assert 'id="docsModal"' in client.get("/customers/").get_data(as_text=True)
