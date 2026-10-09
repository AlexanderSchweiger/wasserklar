"""HTTP: Storno einer Buchung als Generalumkehr — Gegenbuchung mit heutigem Datum, sichtbar in der Liste."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Account, Booking, BookingGroup, FiscalYear, RealAccount, User
from tests.conftest import _ensure_role


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
    db.session.add_all([acc, acc2, ra])
    db.session.commit()
    return acc, acc2, ra


def _login(client):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _booking(acc, ra, d, amount="150.00", **kw):
    b = Booking(date=d, account_id=acc.id, real_account_id=ra.id, amount=Decimal(amount),
                description="Wasser Huber", status=Booking.STATUS_VERBUCHT, **kw)
    db.session.add(b)
    db.session.commit()
    return b


def _early_date():
    today = date.today()
    return date(today.year, 1, 2) if today != date(today.year, 1, 2) else date(today.year, 1, 1)


class TestSingleStorno:
    def test_partner_is_dated_today_and_listed(self, client, admin, books):
        acc, _acc2, ra = books
        b = _booking(acc, ra, _early_date())
        _login(client)
        r = client.post(f"/accounting/bookings/{b.id}/stornieren", data={"storno_reason": "falsch erfasst"})
        assert r.status_code == 302

        partner = Booking.query.filter_by(storno_of_id=b.id).one()
        assert partner.date == date.today()
        assert partner.real_account_id == ra.id
        assert db.session.get(Booking, b.id).status == Booking.STATUS_STORNIERT

        html = client.get(f"/accounting/bookings?year={date.today().year}").get_data(as_text=True)
        assert "Storno: Wasser Huber" in html
        assert f"zur Buchung vom {b.date.strftime('%d.%m.%Y')}" in html
        assert f"storniert am {date.today().strftime('%d.%m.%Y')}" in html

    def test_list_total_counts_both_halves(self, client, admin, books):
        acc, _acc2, ra = books
        _booking(acc, ra, _early_date(), amount="40.00")
        b = _booking(acc, ra, _early_date())
        _login(client)
        client.post(f"/accounting/bookings/{b.id}/stornieren", data={"storno_reason": "falsch erfasst"})
        html = client.get(f"/accounting/bookings?year={date.today().year}").get_data(as_text=True)
        assert "40,00&nbsp;€</span>" in html          # Summe: 40 + 150 − 150

    def test_income_filter_keeps_partner_on_income_side(self, client, admin, books):
        acc, _acc2, ra = books
        b = _booking(acc, ra, _early_date())
        _login(client)
        client.post(f"/accounting/bookings/{b.id}/stornieren", data={"storno_reason": "falsch erfasst"})
        year = date.today().year
        assert "Storno: Wasser Huber" in client.get(
            f"/accounting/bookings?year={year}&kind=income").get_data(as_text=True)
        assert "Storno: Wasser Huber" not in client.get(
            f"/accounting/bookings?year={year}&kind=expense").get_data(as_text=True)

    def test_booking_from_closed_prior_year_can_be_cancelled(self, client, admin, books):
        acc, _acc2, ra = books
        prior = date.today().year - 1
        db.session.add(FiscalYear(year=prior, start_date=date(prior, 1, 1),
                                  end_date=date(prior, 12, 31), closed=True))
        b = _booking(acc, ra, date(prior, 12, 15))
        _login(client)
        client.post(f"/accounting/bookings/{b.id}/stornieren", data={"storno_reason": "Fehler Vorjahr"})
        partner = Booking.query.filter_by(storno_of_id=b.id).one()
        assert partner.date == date.today()
        assert db.session.get(Booking, b.id).date == date(prior, 12, 15)

    def test_closed_current_year_blocks(self, client, admin, books):
        acc, _acc2, ra = books
        year = date.today().year
        db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1),
                                  end_date=date(year, 12, 31), closed=True))
        b = _booking(acc, ra, _early_date())
        _login(client)
        client.post(f"/accounting/bookings/{b.id}/stornieren", data={"storno_reason": "zu spät"})
        assert Booking.query.filter_by(storno_of_id=b.id).count() == 0
        assert db.session.get(Booking, b.id).status == Booking.STATUS_VERBUCHT


class TestGroupStorno:
    def test_group_storno_gets_its_own_row(self, client, admin, books):
        acc, acc2, ra = books
        group = BookingGroup(date=_early_date(), description="Rechnung Huber",
                             total_amount=Decimal("80.00"))
        db.session.add(group)
        db.session.flush()
        for a, amount in ((acc, "50.00"), (acc2, "30.00")):
            db.session.add(Booking(date=group.date, account_id=a.id, real_account_id=ra.id,
                                   amount=Decimal(amount), description="Zeile",
                                   status=Booking.STATUS_VERBUCHT, group_id=group.id))
        db.session.commit()

        _login(client)
        client.post(f"/accounting/booking-groups/{group.id}/stornieren", data={"storno_reason": "doppelt"})
        partners = Booking.query.filter(Booking.storno_of_id.isnot(None)).all()
        assert len(partners) == 2 and {p.date for p in partners} == {date.today()}

        html = client.get(f"/accounting/bookings?year={date.today().year}").get_data(as_text=True)
        assert "Storno: Rechnung Huber" in html
        assert f"zur Sammelbuchung vom {group.date.strftime('%d.%m.%Y')}" in html
        assert "-80,00&nbsp;€" in html
