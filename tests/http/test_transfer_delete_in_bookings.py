"""HTTP: Umbuchungen lassen sich aus der Buchungsliste löschen.

Kein Storno (eine Umbuchung gleicht man mit der Rückbuchung aus) — gesperrt nur im
abgeschlossenen Buchungsjahr oder nach der Übergabe an die Steuerberatung.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (AccountingHandover, AccountingHandoverItem, FiscalYear, RealAccount,
                        Transfer, User)
from tests.conftest import _ensure_role


@pytest.fixture
def setup(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    year = date.today().year
    fy = FiscalYear(year=year, start_date=date(year, 1, 1), end_date=date(year, 12, 31), closed=False)
    ra1 = RealAccount(name="Giro", opening_balance=Decimal("0"))
    ra2 = RealAccount(name="Kasse", opening_balance=Decimal("0"), account_type="cash")
    db.session.add_all([u, fy, ra1, ra2])
    db.session.flush()
    t = Transfer(date=date(year, 1, 3), amount=Decimal("50"), description="Bareinzahlung",
                 from_real_account_id=ra2.id, to_real_account_id=ra1.id)
    db.session.add(t)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return dict(t=t, fy=fy, year=year)


def _delete_url(t):
    return f"/accounting/transfers/{t.id}/delete"


def test_bookings_list_offers_delete(client, setup):
    html = client.get(f"/accounting/bookings?year={setup['year']}").get_data(as_text=True)
    assert _delete_url(setup["t"]) in html


def test_delete_from_bookings_returns_to_bookings(client, setup):
    r = client.post(_delete_url(setup["t"]), data={"next": "/accounting/bookings"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/accounting/bookings")
    assert db.session.get(Transfer, setup["t"].id) is None


def test_foreign_next_is_ignored(client, setup):
    r = client.post(_delete_url(setup["t"]), data={"next": "//evil.example/x"})
    assert r.headers["Location"].endswith("/accounting/transfers")


def test_closed_fiscal_year_locks(client, setup):
    setup["fy"].closed = True
    db.session.commit()
    html = client.get(f"/accounting/bookings?year={setup['year']}").get_data(as_text=True)
    assert _delete_url(setup["t"]) not in html
    client.post(_delete_url(setup["t"]), data={"next": "/accounting/bookings"})
    assert db.session.get(Transfer, setup["t"].id) is not None


def test_handed_over_locks(client, setup):
    year = setup["year"]
    h = AccountingHandover(format="datev", fiscal_year=year, date_from=date(year, 1, 1),
                           date_to=date(year, 1, 31), created_at=datetime(year, 2, 1))
    db.session.add(h)
    db.session.flush()
    db.session.add(AccountingHandoverItem(handover_id=h.id, transfer_id=setup["t"].id))
    db.session.commit()
    html = client.get(f"/accounting/bookings?year={year}").get_data(as_text=True)
    assert _delete_url(setup["t"]) not in html
