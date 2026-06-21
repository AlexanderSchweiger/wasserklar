"""HTTP-Tests fuer die Massen-Bearbeitung von Buchungen
(``accounting.bookings_bulk_edit``).

Geprueft: nur ausgefuellte Felder werden uebernommen, leere bleiben
unveraendert, und nicht editierbare Buchungen (storniert / Sammelbuchungs-
Zeile) werden uebersprungen.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Account, Booking, BookingGroup, Customer, Project, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.com", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")  # Werkzeug-3 CookieJar-Workaround
    return client.post("/auth/login", data={"username": username, "password": password})


@pytest.fixture
def setup(app):
    acc1 = Account(name="Wassereinnahmen", code="W01")
    acc2 = Account(name="Grundgebühren", code="G01")
    proj = Project(name="Sanierung 2026")
    cust = Customer(name="Bestandskunde")
    db.session.add_all([acc1, acc2, proj, cust])
    db.session.commit()
    return {"acc1": acc1, "acc2": acc2, "proj": proj, "cust": cust}


def _booking(account, *, amount="100.00", status=Booking.STATUS_OFFEN, customer=None,
             group_id=None, storno_of_id=None):
    b = Booking(
        date=date.today(),
        account_id=account.id,
        amount=Decimal(amount),
        description="Test",
        status=status,
        customer_id=customer.id if customer else None,
        group_id=group_id,
        storno_of_id=storno_of_id,
    )
    db.session.add(b)
    db.session.commit()
    return b


def test_sets_only_chosen_fields(client, admin, setup):
    """Konto + Projekt gewaehlt, Kontakt leer ⇒ Kontakt bleibt unveraendert."""
    _login(client)
    b1 = _booking(setup["acc1"], customer=setup["cust"])
    b2 = _booking(setup["acc1"], customer=setup["cust"])

    r = client.post("/accounting/bookings/bulk-edit", data={
        "booking_ids": [b1.id, b2.id],
        "bulk_account_id": setup["acc2"].id,
        "bulk_project_id": setup["proj"].id,
        "bulk_customer_id": "",  # nicht aendern
    })
    assert r.status_code == 200

    db.session.expire_all()
    for bid in (b1.id, b2.id):
        b = db.session.get(Booking, bid)
        assert b.account_id == setup["acc2"].id      # geaendert
        assert b.project_id == setup["proj"].id       # geaendert
        assert b.customer_id == setup["cust"].id      # unveraendert


def test_empty_selection_changes_nothing(client, admin, setup):
    """Kein Feld gewaehlt ⇒ keine Buchung wird angefasst."""
    _login(client)
    b1 = _booking(setup["acc1"], customer=setup["cust"])

    r = client.post("/accounting/bookings/bulk-edit", data={
        "booking_ids": [b1.id],
        "bulk_account_id": "",
        "bulk_project_id": "",
        "bulk_customer_id": "",
    })
    assert r.status_code == 200

    db.session.expire_all()
    b = db.session.get(Booking, b1.id)
    assert b.account_id == setup["acc1"].id
    assert b.project_id is None
    assert b.customer_id == setup["cust"].id


def test_skips_storniert_and_group_children(client, admin, setup):
    """Stornierte Buchungen und Sammelbuchungs-Zeilen bleiben unangetastet."""
    _login(client)
    storniert = _booking(setup["acc1"], status=Booking.STATUS_STORNIERT)

    group = BookingGroup(date=date.today(), description="Sammel", total_amount=Decimal("0"),
                         status=BookingGroup.STATUS_AKTIV)
    db.session.add(group)
    db.session.commit()
    child = _booking(setup["acc1"], group_id=group.id)
    editable = _booking(setup["acc1"])

    r = client.post("/accounting/bookings/bulk-edit", data={
        "booking_ids": [storniert.id, child.id, editable.id],
        "bulk_account_id": setup["acc2"].id,
    })
    assert r.status_code == 200

    db.session.expire_all()
    assert db.session.get(Booking, storniert.id).account_id == setup["acc1"].id  # uebersprungen
    assert db.session.get(Booking, child.id).account_id == setup["acc1"].id      # uebersprungen
    assert db.session.get(Booking, editable.id).account_id == setup["acc2"].id   # geaendert
