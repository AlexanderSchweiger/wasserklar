"""HTTP: Storno, Sammelbuchung und Umbuchung als Modal der Buchungsliste.

Modal-Modus = Header ``X-From-Modal`` (setupEditModal): GET liefert den Modal-Inhalt
(modal-body + modal-footer, kein ``<html>``), POST bei Erfolg ``204`` + ``HX-Trigger``
(close + saved), bei Fehler ``200`` + Inhalt mit Meldung. Die Erfolgsmeldung der Route
erscheint beim anschließenden Tabellen-Refresh in ``#bookings-flash``.
"""
import json
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, FiscalYear, RealAccount, Transfer, User,
)
from tests.conftest import _ensure_role

MODAL = {"X-From-Modal": "1"}
TODAY = date.today()


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def books(app):
    acc = Account(name="Wassergebühren", code="WG1")
    acc2 = Account(name="Grundgebühr", code="GG1")
    ra = RealAccount(name="Raiba", opening_balance=Decimal("0"), is_default=True, active=True)
    ra2 = RealAccount(name="Kassa", opening_balance=Decimal("0"), active=True)
    fy = FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                    end_date=date(TODAY.year, 12, 31), closed=False)
    db.session.add_all([acc, acc2, ra, ra2, fy])
    db.session.commit()
    return acc, acc2, ra, ra2


def _login(client):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _booking(acc, ra, status=Booking.STATUS_VERBUCHT):
    b = Booking(date=TODAY, account_id=acc.id, real_account_id=ra.id, amount=Decimal("150.00"),
                description="Wasser Huber", status=status)
    db.session.add(b)
    db.session.commit()
    return b


def _group(acc, acc2, ra, status=Booking.STATUS_VERBUCHT):
    group = BookingGroup(date=TODAY, description="Rechnung Huber", total_amount=Decimal("80.00"))
    db.session.add(group)
    db.session.flush()
    for a, amount in ((acc, "50.00"), (acc2, "30.00")):
        db.session.add(Booking(date=TODAY, account_id=a.id, real_account_id=ra.id, amount=Decimal(amount),
                               description="Zeile", status=status, group_id=group.id))
    db.session.commit()
    return group


def _trigger(resp):
    return json.loads(resp.headers["HX-Trigger"])


def _table_refresh(client):
    return client.get(f"/accounting/bookings?year={TODAY.year}",
                      headers={"HX-Request": "true"}).get_data(as_text=True)


class TestBookingsPage:
    def test_page_has_modals_and_triggers(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        _booking(acc, ra)
        _group(acc, acc2, ra, status=Booking.STATUS_OFFEN)
        _login(client)
        html = client.get(f"/accounting/bookings?year={TODAY.year}").get_data(as_text=True)
        for skeleton in ('id="stornoModal"', 'id="bookingGroupModal"', 'id="transferModal"'):
            assert skeleton in html
        assert 'onclick="openStornoModal(this)"' in html
        assert 'onclick="openBookingGroupModal(this)"' in html
        assert 'onclick="openTransferModal(this)"' in html
        # Keine Seitenwechsel mehr aus der Liste heraus.
        assert 'href="/accounting/booking-groups/new"' not in html
        assert 'href="/accounting/transfers/new"' not in html


class TestStornoModal:
    def test_get_is_modal_content(self, client, admin, books):
        acc, _acc2, ra, _ra2 = books
        b = _booking(acc, ra)
        _login(client)
        r = client.get(f"/accounting/bookings/{b.id}/stornieren", headers=MODAL)
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "<html" not in html.lower()
        assert 'name="storno_reason"' in html and 'class="modal-footer"' in html
        assert "Storno durchführen" in html

    def test_missing_reason_stays_in_modal(self, client, admin, books):
        acc, _acc2, ra, _ra2 = books
        b = _booking(acc, ra)
        _login(client)
        r = client.post(f"/accounting/bookings/{b.id}/stornieren", headers=MODAL, data={"storno_reason": ""})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert "Bitte einen Storno-Grund angeben." in r.get_data(as_text=True)
        assert db.session.get(Booking, b.id).status == Booking.STATUS_VERBUCHT

    def test_post_stornos_and_message_comes_with_refresh(self, client, admin, books):
        acc, _acc2, ra, _ra2 = books
        b = _booking(acc, ra)
        _login(client)
        r = client.post(f"/accounting/bookings/{b.id}/stornieren", headers=MODAL,
                        data={"storno_reason": "falsch erfasst"})
        assert r.status_code == 204
        trig = _trigger(r)
        assert "closeStornoModal" in trig and "stornoSaved" in trig
        assert db.session.get(Booking, b.id).status == Booking.STATUS_STORNIERT
        html = _table_refresh(client)
        assert 'id="bookings-flash" hx-swap-oob="true"' in html
        assert "Buchung erfolgreich storniert." in html

    def test_blocked_shows_message_only(self, client, admin, books):
        acc, _acc2, ra, _ra2 = books
        b = _booking(acc, ra, status=Booking.STATUS_STORNIERT)
        _login(client)
        r = client.get(f"/accounting/bookings/{b.id}/stornieren", headers=MODAL)
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "bereits storniert" in html
        assert 'name="storno_reason"' not in html and "Schließen" in html

    def test_group_storno(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        group = _group(acc, acc2, ra)
        _login(client)
        html = client.get(f"/accounting/booking-groups/{group.id}/stornieren", headers=MODAL).get_data(as_text=True)
        assert "Sammelbuchung stornieren" in html and "<html" not in html.lower()
        r = client.post(f"/accounting/booking-groups/{group.id}/stornieren", headers=MODAL,
                        data={"storno_reason": "doppelt erfasst"})
        assert r.status_code == 204
        assert "stornoSaved" in _trigger(r)
        assert db.session.get(BookingGroup, group.id).status == BookingGroup.STATUS_STORNIERT

    def test_standalone_page_still_works(self, client, admin, books):
        acc, _acc2, ra, _ra2 = books
        b = _booking(acc, ra)
        _login(client)
        html = client.get(f"/accounting/bookings/{b.id}/stornieren").get_data(as_text=True)
        assert "<html" in html.lower() and 'name="storno_reason"' in html


def _group_form(acc, acc2, ra, rows=None, **extra):
    rows = rows if rows is not None else [(acc.id, "-60"), (acc2.id, "-22")]
    data = {"date": TODAY.isoformat(), "description": "Lieferung Rohre", "real_account_id": str(ra.id),
            "child_account_id[]": [str(a) for a, _ in rows],
            "child_project_id[]": ["" for _ in rows],
            "child_description[]": ["" for _ in rows],
            "child_tax_rate[]": ["20" for _ in rows],
            "child_amount[]": [amount for _, amount in rows]}
    data.update(extra)
    return data


class TestBookingGroupModal:
    def test_new_get_is_modal_content(self, client, admin, books):
        _login(client)
        html = client.get("/accounting/booking-groups/new", headers=MODAL).get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'id="rowsContainer"' in html and "data-group-submit" in html

    def test_new_post_creates(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        _login(client)
        r = client.post("/accounting/booking-groups/new", headers=MODAL, data=_group_form(acc, acc2, ra))
        assert r.status_code == 204
        trig = _trigger(r)
        assert "closeBookingGroupModal" in trig and "bookingGroupSaved" in trig
        group = BookingGroup.query.one()
        assert sorted(c.amount for c in group.children) == [Decimal("-60.00"), Decimal("-22.00")]
        assert "Sammelbuchung angelegt." in _table_refresh(client)

    def test_error_keeps_entered_rows(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        _login(client)
        r = client.post("/accounting/booking-groups/new", headers=MODAL,
                        data=_group_form(acc, acc2, ra, rows=[(acc.id, "-60")]))
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and "HX-Trigger" not in r.headers
        assert "mindestens 2 Zeilen" in html
        # Die eingegebene Zeile ist noch da (früher gingen alle Zeilen verloren).
        assert 'value="-60"' in html
        assert BookingGroup.query.count() == 0

    def test_edit_open_group(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        group = _group(acc, acc2, ra, status=Booking.STATUS_OFFEN)
        _login(client)
        html = client.get(f"/accounting/booking-groups/{group.id}/edit", headers=MODAL).get_data(as_text=True)
        assert "Änderungen speichern" in html and 'value="50.00"' in html
        # Ohne Belegnummer bleibt das Feld leer (früher stand dort „None“).
        assert 'name="reference" class="form-control" value=""' in html
        r = client.post(f"/accounting/booking-groups/{group.id}/edit", headers=MODAL,
                        data=_group_form(acc, acc2, ra, rows=[(acc.id, "70"), (acc2.id, "30")]))
        assert r.status_code == 204
        assert db.session.get(BookingGroup, group.id).total_amount == Decimal("100.00")

    def test_posted_group_is_readonly(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        group = _group(acc, acc2, ra)
        _login(client)
        html = client.get(f"/accounting/booking-groups/{group.id}/edit", headers=MODAL).get_data(as_text=True)
        assert "Schreibgeschützt" in html and 'type="submit"' not in html and "Schließen" in html

    def test_tax_rate_is_select_of_configured_rates(self, client, admin, books):
        _login(client)
        html = client.get("/accounting/booking-groups/new", headers=MODAL).get_data(as_text=True)
        # Auswahl statt freier Eingabe (auch in der Zeilen-Vorlage für „Zeile hinzufügen“).
        assert '<select name="child_tax_rate[]"' in html
        assert 'type="number" name="child_tax_rate[]"' not in html
        assert '<option value="20"' in html

    def test_edit_preselects_stored_rate(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        group = _group(acc, acc2, ra, status=Booking.STATUS_OFFEN)
        for c in group.children:
            c.tax_rate = Decimal("20.00")
        db.session.commit()
        _login(client)
        html = client.get(f"/accounting/booking-groups/{group.id}/edit", headers=MODAL).get_data(as_text=True)
        assert html.count('<option value="20" selected>') == 2

    def test_unknown_rate_is_dropped(self, client, admin, books):
        acc, acc2, ra, _ra2 = books
        _login(client)
        data = _group_form(acc, acc2, ra)
        data["child_tax_rate[]"] = ["20", "17"]
        r = client.post("/accounting/booking-groups/new", headers=MODAL, data=data)
        assert r.status_code == 204
        rates = sorted((c.tax_rate for c in BookingGroup.query.one().children), key=lambda v: v is None)
        assert rates == [Decimal("20.00"), None]


def _transfer_form(a, b, amount="50", **extra):
    data = {"date": TODAY.isoformat(), "amount": amount, "from_real_account_id": str(a.id),
            "to_real_account_id": str(b.id), "description": "Bareinzahlung"}
    data.update(extra)
    return data


class TestTransferEdit:
    def _transfer(self, a, b, d=TODAY):
        t = Transfer(date=d, amount=Decimal("50.00"), description="Bareinzahlung",
                     from_real_account_id=a.id, to_real_account_id=b.id)
        db.session.add(t)
        db.session.commit()
        return t

    def test_get_prefilled(self, client, admin, books):
        _acc, _acc2, ra, ra2 = books
        t = self._transfer(ra2, ra)
        _login(client)
        html = client.get(f"/accounting/transfers/{t.id}/edit", headers=MODAL).get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'value="50.00"' in html and 'value="Bareinzahlung"' in html

    def test_post_updates(self, client, admin, books):
        _acc, _acc2, ra, ra2 = books
        t = self._transfer(ra2, ra)
        _login(client)
        r = client.post(f"/accounting/transfers/{t.id}/edit", headers=MODAL,
                        data=_transfer_form(ra, ra2, amount="75,50", description="Rückbuchung"))
        assert r.status_code == 204
        assert "transferSaved" in _trigger(r)
        t = db.session.get(Transfer, t.id)
        assert (t.amount, t.from_real_account_id, t.description) == (Decimal("75.50"), ra.id, "Rückbuchung")
        assert Transfer.query.count() == 1

    def test_invalid_stays_in_modal(self, client, admin, books):
        _acc, _acc2, ra, ra2 = books
        t = self._transfer(ra2, ra)
        _login(client)
        r = client.post(f"/accounting/transfers/{t.id}/edit", headers=MODAL,
                        data=_transfer_form(ra, ra, description="Selbe"))
        assert r.status_code == 200 and "HX-Trigger" not in r.headers
        assert "dürfen nicht gleich sein" in r.get_data(as_text=True)
        assert db.session.get(Transfer, t.id).description == "Bareinzahlung"

    def test_bad_input_is_no_server_error(self, client, admin, books):
        _acc, _acc2, ra, ra2 = books
        _login(client)
        r = client.post("/accounting/transfers/new", headers=MODAL,
                        data=_transfer_form(ra, ra2, date="", from_real_account_id=""))
        assert r.status_code == 200
        assert Transfer.query.count() == 0

    def test_closed_year_is_locked(self, client, admin, books):
        _acc, _acc2, ra, ra2 = books
        last = TODAY.year - 1
        db.session.add(FiscalYear(year=last, start_date=date(last, 1, 1), end_date=date(last, 12, 31), closed=True))
        t = self._transfer(ra2, ra, d=date(last, 6, 1))
        _login(client)
        html = client.get(f"/accounting/transfers/{t.id}/edit", headers=MODAL).get_data(as_text=True)
        assert "abgeschlossen" in html and 'name="amount"' not in html
        r = client.post(f"/accounting/transfers/{t.id}/edit", headers=MODAL, data=_transfer_form(ra2, ra, amount="99"))
        assert r.status_code == 200
        assert db.session.get(Transfer, t.id).amount == Decimal("50.00")
        list_html = client.get(f"/accounting/transfers?year={last}").get_data(as_text=True)
        assert f"/accounting/transfers/{t.id}/edit" not in list_html

    def test_standalone_edit_page(self, client, admin, books):
        _acc, _acc2, ra, ra2 = books
        t = self._transfer(ra2, ra)
        _login(client)
        html = client.get(f"/accounting/transfers/{t.id}/edit").get_data(as_text=True)
        assert "<html" in html.lower() and "Umbuchung bearbeiten" in html
