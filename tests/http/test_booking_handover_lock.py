"""HTTP: Sperre übergebener Buchungen/Umbuchungen (Übergabe an die Steuerberatung).

Eine aktive Übergabe sperrt Konto, Bank/Kasse und Projekt einer Buchung (Formular +
Massenbearbeitung) und das Löschen einer Umbuchung — unabhängig davon, ob das Format
gerade eingeschaltet ist. Eine zurückgezogene Übergabe sperrt nichts mehr.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (AccountingHandover, AccountingHandoverItem, Account, Booking,
                        Customer, FiscalYear, Project, RealAccount, Transfer, User)
from tests.conftest import _ensure_role

HX = {"HX-Request": "true"}


@pytest.fixture
def setup(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    today = date.today()
    fy = FiscalYear(year=today.year, start_date=date(today.year, 1, 1),
                    end_date=date(today.year, 12, 31), closed=False)
    acc1, acc2 = Account(name="Erlöse"), Account(name="Sonstiges")
    proj = Project(name="Sanierung")
    ra1 = RealAccount(name="Giro", opening_balance=Decimal("0"))
    ra2 = RealAccount(name="Kasse", opening_balance=Decimal("0"), account_type="cash")
    cust = Customer(name="Huber")
    db.session.add_all([u, fy, acc1, acc2, proj, ra1, ra2, cust])
    db.session.flush()
    b = Booking(date=date(today.year, 1, 2), account_id=acc1.id, amount=Decimal("100"),
                description="Wasserzins", status="Verbucht", real_account_id=ra1.id)
    t = Transfer(date=date(today.year, 1, 3), amount=Decimal("50"), description="Bareinzahlung",
                 from_real_account_id=ra2.id, to_real_account_id=ra1.id)
    db.session.add_all([b, t])
    db.session.flush()
    h = AccountingHandover(format="datev", fiscal_year=today.year, date_from=date(today.year, 1, 1),
                           date_to=date(today.year, 1, 31), created_at=datetime(today.year, 2, 1))
    db.session.add(h)
    db.session.flush()
    db.session.add_all([AccountingHandoverItem(handover_id=h.id, booking_id=b.id),
                        AccountingHandoverItem(handover_id=h.id, transfer_id=t.id)])
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return dict(b=b, t=t, h=h, acc1=acc1, acc2=acc2, proj=proj, ra1=ra1, ra2=ra2, cust=cust)


def _payload(s, **over):
    data = {"account_id": s["acc1"].id, "description": "Wasserzins", "project_id": "",
            "customer_id": "", "real_account_id": s["ra1"].id, "action": ""}
    data.update(over)
    return data


def test_form_shows_lock(client, setup):
    html = client.get(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX).get_data(as_text=True)
    assert "an die Steuerberatung übergeben" in html
    assert f'<input type="hidden" name="account_id" value="{setup["acc1"].id}">' in html


def test_locked_field_change_is_rejected(client, setup):
    r = client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                    data=_payload(setup, account_id=setup["acc2"].id))
    assert "an die Steuerberatung übergeben" in r.get_data(as_text=True)
    db.session.refresh(setup["b"])
    assert setup["b"].account_id == setup["acc1"].id
    r = client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                    data=_payload(setup, real_account_id=setup["ra2"].id))
    db.session.refresh(setup["b"])
    assert setup["b"].real_account_id == setup["ra1"].id


def test_free_fields_still_editable(client, setup):
    r = client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                    data=_payload(setup, description="Wasserzins 2026", customer_id=setup["cust"].id))
    assert "booking-saved" in r.headers.get("HX-Trigger", "")
    db.session.refresh(setup["b"])
    assert setup["b"].description == "Wasserzins 2026"
    assert setup["b"].customer_id == setup["cust"].id


def test_bulk_edit_skips_handed_over(client, setup):
    r = client.post("/accounting/bookings/bulk-edit", data={
        "booking_ids": [setup["b"].id], "bulk_account_id": setup["acc2"].id,
        "bulk_customer_id": setup["cust"].id, "year": date.today().year})
    assert "bereits an die Steuerberatung übergeben" in r.get_data(as_text=True)
    db.session.refresh(setup["b"])
    assert setup["b"].account_id == setup["acc1"].id      # gesperrt
    assert setup["b"].customer_id == setup["cust"].id     # frei


def test_transfer_delete_blocked_until_withdrawn(client, setup):
    client.post(f"/accounting/transfers/{setup['t'].id}/delete")
    assert db.session.get(Transfer, setup["t"].id) is not None
    setup["h"].status = AccountingHandover.STATUS_WITHDRAWN
    db.session.commit()
    client.post(f"/accounting/transfers/{setup['t'].id}/delete")
    assert db.session.get(Transfer, setup["t"].id) is None


def test_withdrawn_handover_unlocks(client, setup):
    setup["h"].status = AccountingHandover.STATUS_WITHDRAWN
    db.session.commit()
    client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                data=_payload(setup, account_id=setup["acc2"].id))
    db.session.refresh(setup["b"])
    assert setup["b"].account_id == setup["acc2"].id
