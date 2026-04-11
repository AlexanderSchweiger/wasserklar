"""Gemeinsame Fixtures für Integration-Tests."""
from decimal import Decimal
from datetime import date

import pytest

from app.extensions import db
from app.models import Account, Customer, RealAccount, User


@pytest.fixture
def user(app):
    u = User(username="tester", email="tester@test.com", role="admin")
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def account(app):
    a = Account(name="Wassereinnahmen", code="W01")
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def account2(app):
    a = Account(name="Grundgebühren", code="G01")
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def real_account(app):
    ra = RealAccount(name="Girokonto", iban="AT12345", opening_balance=Decimal("0"))
    db.session.add(ra)
    db.session.commit()
    return ra


@pytest.fixture
def customer(app):
    c = Customer(name="Test Kunde")
    db.session.add(c)
    db.session.commit()
    return c
