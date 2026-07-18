"""HTTP-Tests fuer die Modal-faehigen Anlage-/Bearbeiten-Formulare von
Konto (Kontenplan), Bankkonto, Umbuchung, Projekt und Tarif.

Jedes Formular wird sowohl als Standalone-Seite (GET ohne ``X-From-Modal``)
als auch im Modal-Modus betrieben. Im Modal-Modus liefert
  - GET  → den reinen Form-Body (Fragment, kein ``<html>``),
  - POST → bei Erfolg ``204`` + ``HX-Trigger`` (close + saved),
           bei Validierungsfehler ``200`` + Fragment mit Flash.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, FiscalYear, Project, RealAccount, Transfer, User, WaterTariff,
)
from tests.conftest import _ensure_role

MODAL = {"X-From-Modal": "1"}


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.com", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")  # Werkzeug-3 CookieJar-Workaround
    return client.post("/auth/login", data={"username": username, "password": password})


# --------------------------------------------------------------------------- #
# Kontenplan
# --------------------------------------------------------------------------- #

class TestAccountModal:
    def test_get_modal_body_is_fragment(self, client, admin):
        _login(client)
        r = client.get("/accounting/accounts/new", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'name="name"' in html

    def test_post_modal_creates_and_triggers(self, client, admin):
        _login(client)
        r = client.post("/accounting/accounts/new", headers=MODAL,
                        data={"code": "ABC", "name": "Testkonto", "description": "x"})
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeAccountModal" in trig and "accountSaved" in trig
        assert Account.query.filter_by(name="Testkonto").count() == 1

    def test_post_modal_invalid_code_returns_fragment(self, client, admin):
        _login(client)
        r = client.post("/accounting/accounts/new", headers=MODAL,
                        data={"code": "TOOLONG", "name": "X"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert Account.query.filter_by(name="X").count() == 0

    def test_edit_modal_body_prefills(self, client, admin):
        _login(client)
        a = Account(name="Bestand", code="BST")
        db.session.add(a)
        db.session.commit()
        r = client.get(f"/accounting/accounts/{a.id}/edit", headers=MODAL)
        assert r.status_code == 200
        assert "Bestand" in r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Bankkonto
# --------------------------------------------------------------------------- #

class TestRealAccountModal:
    def test_get_modal_body_is_fragment(self, client, admin):
        _login(client)
        r = client.get("/accounting/real-accounts/new", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'name="icon"' in html and 'name="name"' in html

    def test_post_modal_creates_and_triggers(self, client, admin):
        _login(client)
        r = client.post("/accounting/real-accounts/new", headers=MODAL,
                        data={"name": "Sparbuch", "iban": "AT99",
                              "opening_balance": "100,50", "icon": "fa-piggy-bank",
                              "is_default": "on"})
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeRealAccountModal" in trig and "realAccountSaved" in trig
        ra = RealAccount.query.filter_by(name="Sparbuch").one()
        assert ra.opening_balance == Decimal("100.50")
        assert ra.is_default is True

    def test_edit_modal_body_prefills(self, client, admin):
        _login(client)
        ra = RealAccount(name="Giro", iban="AT1", opening_balance=Decimal("0"))
        db.session.add(ra)
        db.session.commit()
        r = client.get(f"/accounting/real-accounts/{ra.id}/edit", headers=MODAL)
        assert r.status_code == 200
        assert "Giro" in r.get_data(as_text=True)

    def test_standalone_page_renders_shared_body(self, client, admin):
        """Die Standalone-Seite bindet denselben Form-Body ein wie das Modal."""
        _login(client)
        r = client.get("/accounting/real-accounts/new")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" in html.lower()          # Vollseite, kein Fragment
        assert 'name="account_type"' in html    # Kontoart-Auswahl aus dem Body
        assert 'name="icon"' in html

    def test_list_warns_on_negative_cash_balance(self, client, admin):
        _login(client)
        db.session.add(RealAccount(name="Barkassa", opening_balance=Decimal("-5"),
                                   account_type=RealAccount.TYPE_CASH))
        db.session.add(RealAccount(name="Giro", opening_balance=Decimal("-5")))
        db.session.commit()
        html = client.get("/accounting/real-accounts").get_data(as_text=True)
        # Warnung nur bei der Kassa — ein Bankkonto darf im Minus sein.
        assert "Negativer Kassastand" in html
        assert html.count("Negativer Kassastand") == 1

    def test_defaults_to_bank_when_type_missing(self, client, admin):
        """Altes Formular ohne account_type-Feld darf nicht auf NULL laufen."""
        _login(client)
        r = client.post("/accounting/real-accounts/new", headers=MODAL,
                        data={"name": "Ohne Typ", "opening_balance": "0"})
        assert r.status_code == 204
        assert RealAccount.query.filter_by(name="Ohne Typ").one().account_type == "bank"

    def test_creates_cash_account_and_drops_iban(self, client, admin):
        """Kassa: Typ wird uebernommen, eine mitgeschickte IBAN verworfen."""
        _login(client)
        r = client.post("/accounting/real-accounts/new", headers=MODAL,
                        data={"name": "Kassa", "account_type": "cash",
                              "iban": "AT99", "opening_balance": "250,00",
                              "icon": "fa-cash-register"})
        assert r.status_code == 204
        ra = RealAccount.query.filter_by(name="Kassa").one()
        assert ra.is_cash is True
        assert ra.iban == ""
        assert ra.opening_balance == Decimal("250.00")

    def test_unknown_type_falls_back_to_bank(self, client, admin):
        _login(client)
        r = client.post("/accounting/real-accounts/new", headers=MODAL,
                        data={"name": "Krypto", "account_type": "wallet",
                              "opening_balance": "0"})
        assert r.status_code == 204
        assert RealAccount.query.filter_by(name="Krypto").one().is_cash is False

    def test_edit_switches_bank_to_cash(self, client, admin):
        _login(client)
        ra = RealAccount(name="Giro", iban="AT1", opening_balance=Decimal("0"))
        db.session.add(ra)
        db.session.commit()
        r = client.post(f"/accounting/real-accounts/{ra.id}/edit", headers=MODAL,
                        data={"name": "Kassa", "account_type": "cash",
                              "opening_balance": "0", "active": "on"})
        assert r.status_code == 204
        db.session.refresh(ra)
        assert ra.is_cash is True
        assert ra.iban == ""


# --------------------------------------------------------------------------- #
# Umbuchung (nur Anlage)
# --------------------------------------------------------------------------- #

class TestTransferModal:
    @pytest.fixture
    def accounts(self, app):
        today = date.today()
        fy = FiscalYear(year=today.year, start_date=date(today.year, 1, 1),
                        end_date=date(today.year, 12, 31), closed=False)
        a = RealAccount(name="Konto A", opening_balance=Decimal("0"))
        b = RealAccount(name="Konto B", opening_balance=Decimal("0"))
        db.session.add_all([fy, a, b])
        db.session.commit()
        return a, b

    def test_get_modal_body_is_fragment(self, client, admin, accounts):
        _login(client)
        r = client.get("/accounting/transfers/new", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'name="from_real_account_id"' in html

    def test_post_modal_creates_and_triggers(self, client, admin, accounts):
        a, b = accounts
        _login(client)
        r = client.post("/accounting/transfers/new", headers=MODAL,
                        data={"date": date.today().isoformat(), "amount": "50",
                              "from_real_account_id": str(a.id),
                              "to_real_account_id": str(b.id),
                              "description": "Umbuchung Test"})
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeTransferModal" in trig and "transferSaved" in trig
        assert Transfer.query.count() == 1

    def test_post_modal_same_account_returns_fragment(self, client, admin, accounts):
        a, _ = accounts
        _login(client)
        r = client.post("/accounting/transfers/new", headers=MODAL,
                        data={"date": date.today().isoformat(), "amount": "50",
                              "from_real_account_id": str(a.id),
                              "to_real_account_id": str(a.id),
                              "description": "Selbe"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert Transfer.query.count() == 0


# --------------------------------------------------------------------------- #
# Projekt
# --------------------------------------------------------------------------- #

class TestProjectModal:
    def test_get_modal_body_is_fragment(self, client, admin):
        _login(client)
        r = client.get("/projekte/neu", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'name="color"' in html and 'name="name"' in html

    def test_post_modal_creates_and_triggers(self, client, admin):
        _login(client)
        r = client.post("/projekte/neu", headers=MODAL,
                        data={"code": "INV", "name": "Sanierung", "color": "#e74c3c"})
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeProjectModal" in trig and "projectSaved" in trig
        assert Project.query.filter_by(name="Sanierung").count() == 1

    def test_post_modal_duplicate_name_returns_fragment(self, client, admin):
        _login(client)
        db.session.add(Project(name="Doppelt", color="#3498db"))
        db.session.commit()
        r = client.post("/projekte/neu", headers=MODAL,
                        data={"name": "Doppelt", "color": "#3498db"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert Project.query.filter_by(name="Doppelt").count() == 1

    def test_edit_modal_body_prefills(self, client, admin):
        _login(client)
        p = Project(name="Altprojekt", color="#2ecc71")
        db.session.add(p)
        db.session.commit()
        r = client.get(f"/projekte/{p.id}/bearbeiten", headers=MODAL)
        assert r.status_code == 200
        assert "Altprojekt" in r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Tarif
# --------------------------------------------------------------------------- #

class TestTariffModal:
    def test_get_modal_body_is_fragment(self, client, admin):
        _login(client)
        r = client.get("/invoices/tariffs/new", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" not in html.lower()
        assert 'name="price_per_m3"' in html and 'name="valid_from"' in html

    def test_post_modal_creates_and_triggers(self, client, admin):
        _login(client)
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Tarif 2026", "valid_from": "2026",
                              "valid_to": "", "base_fee": "50,00",
                              "price_per_m3": "1,2345", "notes": ""})
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeTariffModal" in trig and "tariffSaved" in trig
        t = WaterTariff.query.filter_by(name="Tarif 2026").one()
        assert t.price_per_m3 == Decimal("1.2345")
        assert t.base_fee == Decimal("50.00")
        assert t.valid_to is None

    def test_post_modal_invalid_amount_returns_fragment(self, client, admin):
        _login(client)
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Kaputt", "valid_from": "2026",
                              "price_per_m3": "keine Zahl"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert WaterTariff.query.filter_by(name="Kaputt").count() == 0

    def test_post_modal_valid_to_before_valid_from_rejected(self, client, admin):
        _login(client)
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Verdreht", "valid_from": "2026",
                              "valid_to": "2024", "price_per_m3": "1,00"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert WaterTariff.query.filter_by(name="Verdreht").count() == 0

    def test_edit_modal_body_prefills(self, client, admin):
        _login(client)
        t = WaterTariff(name="Alttarif", valid_from=2024,
                        price_per_m3=Decimal("0.9500"))
        db.session.add(t)
        db.session.commit()
        r = client.get(f"/invoices/tariffs/{t.id}/edit", headers=MODAL)
        assert r.status_code == 200
        assert "Alttarif" in r.get_data(as_text=True)

    def test_edit_modal_updates_and_triggers(self, client, admin):
        _login(client)
        t = WaterTariff(name="Alt", valid_from=2024, price_per_m3=Decimal("0.95"))
        db.session.add(t)
        db.session.commit()
        r = client.post(f"/invoices/tariffs/{t.id}/edit", headers=MODAL,
                        data={"name": "Neu", "valid_from": "2024",
                              "valid_to": "2025", "price_per_m3": "1,10",
                              "additional_fee": "12,00"})
        assert r.status_code == 204
        assert "closeTariffModal" in json.loads(r.headers["HX-Trigger"])
        db.session.refresh(t)
        assert t.name == "Neu"
        assert t.valid_to == 2025
        assert t.additional_fee == Decimal("12.00")

    def test_standalone_page_still_renders(self, client, admin):
        _login(client)
        r = client.get("/invoices/tariffs/new")
        assert r.status_code == 200
        assert "<html" in r.get_data(as_text=True).lower()
