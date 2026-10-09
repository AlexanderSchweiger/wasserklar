"""HTTP: offene Buchungen sofort verbuchen (Hinweisleiste „N offen — Jetzt verbuchen“ in der Buchungsliste).

Verbucht wird alles Offene bis heute — dasselbe, was der nächtliche ``mark-posted``-Lauf am
Folgetag täte. Später datierte Buchungen bleiben offen; ``next`` führt nur auf Pfade dieser Seite.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Account, Booking, FiscalYear, Role, User
from tests.conftest import _ensure_role


@pytest.fixture
def setup(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    today = date.today()
    db.session.add_all([u, FiscalYear(year=today.year, start_date=date(today.year, 1, 1),
                                      end_date=date(today.year, 12, 31), closed=False)])
    acc = Account(name="Wasserzins")
    db.session.add(acc)
    db.session.flush()
    today_b = Booking(date=today, account_id=acc.id, amount=Decimal("10"), description="heute", status="Offen")
    future = Booking(date=today + timedelta(days=3), account_id=acc.id, amount=Decimal("5"),
                     description="vorausdatiert", status="Offen")
    posted = Booking(date=today - timedelta(days=2), account_id=acc.id, amount=Decimal("7"),
                     description="schon verbucht", status="Verbucht")
    db.session.add_all([today_b, future, posted])
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return dict(today=today_b.id, future=future.id, posted=posted.id)


def _status(booking_id):
    return db.session.get(Booking, booking_id).status


def test_hint_shows_open_count(client, setup):
    html = client.get(f"/accounting/bookings?year={date.today().year}").get_data(as_text=True)
    assert 'id="bookings-open-hint"' in html and "1 offen" in html     # die vorausdatierte zählt nicht
    assert 'id="postOpenModal"' in html


def test_post_open_marks_today_but_not_future(client, setup):
    r = client.post("/accounting/bookings/post-open")
    assert r.status_code == 302 and r.headers["Location"].endswith("/accounting/bookings")
    assert _status(setup["today"]) == "Verbucht"
    assert _status(setup["future"]) == "Offen"
    assert _status(setup["posted"]) == "Verbucht"
    html = client.get(f"/accounting/bookings?year={date.today().year}").get_data(as_text=True)
    assert 'id="bookings-open-hint"' not in html


@pytest.mark.parametrize("target, expected", [
    ("/datev/?date_from=2026-01-01", "/datev/?date_from=2026-01-01"),
    ("//evil.example/x", "/accounting/bookings"),
    ("https://evil.example/x", "/accounting/bookings"),
    ("/\\evil.example", "/accounting/bookings"),
])
def test_next_only_on_site(client, setup, target, expected):
    r = client.post("/accounting/bookings/post-open", data={"next": target})
    assert r.headers["Location"].endswith(expected)


def test_requires_bookkeeping_right(client, app):
    role = Role(name="Nur Zähler")
    db.session.add(role)
    db.session.flush()
    u = User(username="zaehler", email="z@z.test", role_id=role.id)
    u.set_password("secret")
    acc = Account(name="X")
    db.session.add_all([u, acc])
    db.session.flush()
    b = Booking(date=date.today(), account_id=acc.id, amount=Decimal("1"), description="x", status="Offen")
    db.session.add(b)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "zaehler", "password": "secret"})
    client.post("/accounting/bookings/post-open")
    assert _status(b.id) == "Offen"
