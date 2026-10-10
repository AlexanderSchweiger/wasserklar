"""HTTP: Aufbau der Buchungsliste — Monatstrenner, eingeklappte Sammelbuchungen, Offen-Markierung."""
import re
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Account, Booking, BookingGroup, RealAccount, User
from tests.conftest import _ensure_role

YEAR = date.today().year


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


def _group(acc, acc2, ra, d, status=Booking.STATUS_VERBUCHT):
    group = BookingGroup(date=d, description="Rechnung Huber", total_amount=Decimal("80.00"))
    db.session.add(group)
    db.session.flush()
    for a, amount in ((acc, "50.00"), (acc2, "30.00")):
        db.session.add(Booking(date=d, account_id=a.id, real_account_id=ra.id, amount=Decimal(amount),
                               description="Zeile", status=status, group_id=group.id))
    db.session.commit()
    return group


def _single(acc, ra, d, status=Booking.STATUS_VERBUCHT, description="Wasser Huber"):
    b = Booking(date=d, account_id=acc.id, real_account_id=ra.id, amount=Decimal("-12.50"),
                description=description, status=status)
    db.session.add(b)
    db.session.commit()
    return b


class TestBookingsListLayout:
    def test_month_separators_and_collapsed_group(self, client, admin, books):
        acc, acc2, ra = books
        group = _group(acc, acc2, ra, date(YEAR, 3, 5))
        _single(acc, ra, date(YEAR, 2, 10))
        _login(client)
        html = client.get(f"/accounting/bookings?year={YEAR}").get_data(as_text=True)

        # Ein Trenner je Monat, neuester Monat zuerst.
        assert html.index(f"März {YEAR}") < html.index(f"Februar {YEAR}")
        assert html.count('class="bk-month"') == 2
        # Sammelbuchung: eine Kopfzeile mit Aufklapp-Knopf, Positionen als Kindzeilen.
        assert f'data-bk-group="g{group.id}"' in html
        assert "2 Positionen" in html
        assert html.count(f'data-bk-parent="g{group.id}"') == 2
        assert "80,00&nbsp;€" in html

    def test_filtered_view_stays_flat(self, client, admin, books):
        acc, acc2, ra = books
        _group(acc, acc2, ra, date(YEAR, 3, 5))
        _login(client)
        html = client.get(f"/accounting/bookings?year={YEAR}&account_id={acc.id}").get_data(as_text=True)
        assert 'class="bk-toggle"' not in html
        assert 'data-bk-parent="g' not in html
        assert "50,00&nbsp;€" in html

    def test_open_booking_is_marked(self, client, admin, books):
        acc, _acc2, ra = books
        _single(acc, ra, date(YEAR, 2, 10), status=Booking.STATUS_OFFEN, description="Offen Huber")
        _single(acc, ra, date(YEAR, 2, 11), description="Verbucht Huber")
        _login(client)
        html = client.get(f"/accounting/bookings?year={YEAR}").get_data(as_text=True)
        rows = re.findall(r'<tr class="bk-row[^"]*">.*?</tr>', html, re.S)
        open_rows = [r for r in rows if "Offen Huber" in r]
        posted_rows = [r for r in rows if "Verbucht Huber" in r]
        assert len(open_rows) == 1 and "bk-open" in open_rows[0]
        assert len(posted_rows) == 1 and "bk-open" not in posted_rows[0]

    def test_group_storno_row_is_collapsible(self, client, admin, books):
        acc, acc2, ra = books
        group = _group(acc, acc2, ra, date(YEAR, 1, 2) if date.today() != date(YEAR, 1, 2) else date(YEAR, 1, 1))
        _login(client)
        client.post(f"/accounting/booking-groups/{group.id}/stornieren", data={"storno_reason": "doppelt"})
        html = client.get(f"/accounting/bookings?year={YEAR}").get_data(as_text=True)
        key = f"s{group.id}-{date.today():%Y%m%d}"
        assert f'data-bk-group="{key}"' in html
        assert html.count(f'data-bk-parent="{key}"') == 2
        assert "-80,00&nbsp;€" in html
