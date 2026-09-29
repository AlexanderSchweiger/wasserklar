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
    @staticmethod
    def _ct():
        """Gebuehrenart-IDs nach Schluessel (System-Arten werden geseedet)."""
        from app.invoices.charges import ensure_system_charge_types
        from app.models import ChargeType
        ensure_system_charge_types()
        db.session.commit()
        return {ct.key: ct.id for ct in ChargeType.query.all()}

    def test_get_modal_body_is_fragment(self, client, admin):
        _login(client)
        ct = self._ct()
        r = client.get("/invoices/tariffs/new", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<html" not in html.lower()
        assert f'name="comp_amount_{ct["water"]}"' in html and 'name="valid_from"' in html

    def test_post_modal_creates_and_triggers(self, client, admin):
        _login(client)
        ct = self._ct()
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Tarif 2026", "valid_from": "2026",
                              "valid_to": "", "notes": "",
                              f"comp_amount_{ct['water']}": "1,2345",
                              f"comp_on_{ct['base_fee']}": "1",
                              f"comp_amount_{ct['base_fee']}": "50,00"})
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeTariffModal" in trig and "tariffSaved" in trig
        t = WaterTariff.query.filter_by(name="Tarif 2026").one()
        assert t.water_price == Decimal("1.2345")
        assert t.component("base_fee").amount == Decimal("50.00")
        assert t.component("additional_fee") is None   # nicht angehakt
        assert t.valid_to is None

    def test_post_modal_invalid_amount_returns_fragment(self, client, admin):
        _login(client)
        ct = self._ct()
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Kaputt", "valid_from": "2026",
                              f"comp_amount_{ct['water']}": "keine Zahl"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert WaterTariff.query.filter_by(name="Kaputt").count() == 0

    def test_post_modal_missing_water_price_rejected(self, client, admin):
        _login(client)
        ct = self._ct()
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Ohne", "valid_from": "2026",
                              f"comp_amount_{ct['water']}": ""})
        assert r.status_code == 200
        assert "Preis pro m³" in r.get_data(as_text=True)
        assert WaterTariff.query.filter_by(name="Ohne").count() == 0

    def test_post_modal_valid_to_before_valid_from_rejected(self, client, admin):
        _login(client)
        ct = self._ct()
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Verdreht", "valid_from": "2026",
                              "valid_to": "2024", f"comp_amount_{ct['water']}": "1,00"})
        assert r.status_code == 200
        assert "HX-Trigger" not in r.headers
        assert WaterTariff.query.filter_by(name="Verdreht").count() == 0

    def test_edit_modal_body_prefills(self, client, admin):
        from app.invoices.charges import build_tariff
        _login(client)
        t = build_tariff(name="Alttarif", valid_from=2024, water_price=Decimal("0.9500"))
        db.session.commit()
        r = client.get(f"/invoices/tariffs/{t.id}/edit", headers=MODAL)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Alttarif" in html
        assert 'value="0,9500"' in html

    def test_edit_modal_updates_and_triggers(self, client, admin):
        from app.invoices.charges import build_tariff
        _login(client)
        ct = self._ct()
        t = build_tariff(name="Alt", valid_from=2024, water_price=Decimal("0.95"),
                         base_fee=Decimal("20"))
        db.session.commit()
        r = client.post(f"/invoices/tariffs/{t.id}/edit", headers=MODAL,
                        data={"name": "Neu", "valid_from": "2024",
                              "valid_to": "2025", f"comp_amount_{ct['water']}": "1,10",
                              # Grundgebuehr abgewaehlt, Zusatzgebuehr neu
                              f"comp_on_{ct['additional_fee']}": "1",
                              f"comp_amount_{ct['additional_fee']}": "12,00"})
        assert r.status_code == 204
        assert "closeTariffModal" in json.loads(r.headers["HX-Trigger"])
        db.session.expire_all()
        t = db.session.get(WaterTariff, t.id)
        assert t.name == "Neu"
        assert t.valid_to == 2025
        assert t.water_price == Decimal("1.10")
        assert t.component("additional_fee").amount == Decimal("12.00")
        assert t.component("base_fee") is None

    def test_levy_component_with_valid_from(self, client, admin):
        _login(client)
        ct = self._ct()
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "DE", "valid_from": "2026",
                              f"comp_amount_{ct['water']}": "1,85",
                              f"comp_tax_{ct['water']}": "7",
                              f"comp_on_{ct['water_levy']}": "1",
                              f"comp_amount_{ct['water_levy']}": "0,10",
                              f"comp_tax_{ct['water_levy']}": "7",
                              f"comp_valid_from_{ct['water_levy']}": "2026-07-01"})
        assert r.status_code == 204
        t = WaterTariff.query.filter_by(name="DE").one()
        levy = t.component("water_levy")
        assert levy.amount == Decimal("0.10")
        assert levy.tax_rate == Decimal("7")
        assert levy.valid_from.isoformat() == "2026-07-01"

    def test_copy_prefills_new_tariff(self, client, admin):
        from app.invoices.charges import build_tariff
        _login(client)
        t = build_tariff(name="T2025", valid_from=2025, water_price=Decimal("1.40"),
                         base_fee=Decimal("32"))
        db.session.commit()
        html = client.get(f"/invoices/tariffs/new?copy_from={t.id}",
                          headers=MODAL).get_data(as_text=True)
        assert 'value="T2025 (Kopie)"' in html
        assert 'value="2026"' in html
        assert 'value="1,4000"' in html and 'value="32,00"' in html

    def test_standalone_page_still_renders(self, client, admin):
        _login(client)
        r = client.get("/invoices/tariffs/new")
        assert r.status_code == 200
        assert "<html" in r.get_data(as_text=True).lower()

    def test_modal_body_offers_account_selects(self, client, admin):
        """Kontierung je Tarifposition (v1.43.0) — auch im Modal-Fragment."""
        _login(client)
        ct = self._ct()
        a = Account(name="Wassererlöse", code="W01")
        db.session.add(a)
        db.session.commit()
        html = client.get("/invoices/tariffs/new", headers=MODAL).get_data(as_text=True)
        for key in ("water", "base_fee", "additional_fee"):
            assert f'name="comp_account_{ct[key]}"' in html
        assert "Wassererlöse" in html

    def test_modal_saves_accounts(self, client, admin):
        _login(client)
        ct = self._ct()
        a1 = Account(name="Wasser", code="W01")
        a2 = Account(name="Grundgebühren", code="G01")
        db.session.add_all([a1, a2])
        db.session.commit()
        r = client.post("/invoices/tariffs/new", headers=MODAL,
                        data={"name": "Kontiert", "valid_from": "2026",
                              f"comp_amount_{ct['water']}": "1,20",
                              f"comp_account_{ct['water']}": str(a1.id),
                              f"comp_on_{ct['base_fee']}": "1",
                              f"comp_amount_{ct['base_fee']}": "50,00",
                              f"comp_account_{ct['base_fee']}": str(a2.id)})
        assert r.status_code == 204
        t = WaterTariff.query.filter_by(name="Kontiert").one()
        assert t.water_component.account_id == a1.id
        assert t.component("base_fee").account_id == a2.id

    def test_edit_modal_prefills_selected_account(self, client, admin):
        from app.invoices.charges import build_tariff
        _login(client)
        a = Account(name="Wasser", code="W01")
        db.session.add(a)
        db.session.flush()
        t = build_tariff(name="Vorbelegt", valid_from=2024, water_price=Decimal("1.00"),
                         accounts={"water": a.id})
        db.session.commit()
        html = client.get(f"/invoices/tariffs/{t.id}/edit",
                          headers=MODAL).get_data(as_text=True)
        assert f'value="{a.id}" selected' in html


