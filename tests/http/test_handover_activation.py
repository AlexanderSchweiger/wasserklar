"""HTTP: Übergabe an die Steuerberatung — Aktivierung und Zuordnungsfelder.

Ohne angemeldetes Format gibt es weder Einstellungskarte noch Felder. Mit Format,
aber ausgeschaltet, ebenfalls keine Felder. Eingeschaltet erscheinen Sachkonto
(Konto, Bank/Kasse) und Steuerschlüssel (Steuersatz) und werden gespeichert;
Ausschalten löscht nichts.
"""
from decimal import Decimal

import pytest

from app.accounting import handover
from app.extensions import db
from app.models import Account, AppSetting, RealAccount, TaxRate, User
from tests.conftest import _ensure_role

MODAL = {"X-From-Modal": "1", "HX-Request": "true"}
CARD_TITLE = "Übergabe an die Steuerberatung"


@pytest.fixture
def logged_in(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


@pytest.fixture
def test_format(app):
    handover.register_format(app, "test", "Testformat",
                             tax_key_defaults=lambda rate: ("3", "9") if rate == 19 else (None, None),
                             account_hint="z. B. 8400")
    yield
    app.extensions.get(handover.FORMATS_EXTENSION, {}).pop("test", None)


@pytest.fixture
def switched_on(test_format):
    AppSetting.set(handover.SETTING_KEY, "test")
    db.session.commit()


def _settings_form(**extra):
    data = {"org_type": "cooperative", "invoice_document_format": "pdf", "invoice_design": "classic"}
    data.update(extra)
    return data


class TestWithoutFormat:
    def test_no_card_no_fields(self, logged_in):
        assert CARD_TITLE not in logged_in.get("/einstellungen/").get_data(as_text=True)
        body = logged_in.get("/accounting/accounts/new", headers=MODAL).get_data(as_text=True)
        assert 'name="ledger_account"' not in body
        body = logged_in.get("/accounting/real-accounts/new", headers=MODAL).get_data(as_text=True)
        assert 'name="ledger_account"' not in body

    def test_posted_value_is_ignored(self, logged_in):
        logged_in.post("/accounting/accounts/new", headers=MODAL,
                       data={"name": "Erlöse", "code": "", "ledger_fields": "1", "ledger_account": "8400"})
        assert Account.query.one().ledger_account is None


class TestSwitchedOff:
    def test_card_shown_but_no_fields(self, logged_in, test_format):
        html = logged_in.get("/einstellungen/").get_data(as_text=True)
        assert CARD_TITLE in html
        assert 'value="test"' in html
        body = logged_in.get("/accounting/accounts/new", headers=MODAL).get_data(as_text=True)
        assert 'name="ledger_account"' not in body

    def test_switch_on_via_settings(self, logged_in, test_format):
        logged_in.post("/einstellungen/", data=_settings_form(accounting_handover_format="test"))
        assert AppSetting.get(handover.SETTING_KEY) == "test"

    def test_unknown_value_is_not_saved(self, logged_in, test_format):
        logged_in.post("/einstellungen/", data=_settings_form(accounting_handover_format="evil"))
        assert AppSetting.get(handover.SETTING_KEY) is None

    def test_settings_without_field_keep_value(self, logged_in, switched_on):
        logged_in.post("/einstellungen/", data=_settings_form())
        assert AppSetting.get(handover.SETTING_KEY) == "test"


class TestSwitchedOn:
    def test_account_fields(self, logged_in, switched_on):
        body = logged_in.get("/accounting/accounts/new", headers=MODAL).get_data(as_text=True)
        assert 'name="ledger_account"' in body and "z. B. 8400" in body
        r = logged_in.post("/accounting/accounts/new", headers=MODAL, data={
            "name": "Erlöse 19 %", "code": "", "ledger_fields": "1",
            "ledger_account": "8400", "ledger_auto_tax": "1"})
        assert r.status_code == 204
        a = Account.query.one()
        assert (a.ledger_account, a.ledger_auto_tax) == ("8400", True)
        html = logged_in.get("/accounting/accounts").get_data(as_text=True)
        assert "<code>8400</code>" in html and "Automatik" in html

    def test_invalid_account_number(self, logged_in, switched_on):
        r = logged_in.post("/accounting/accounts/new", headers=MODAL, data={
            "name": "Erlöse", "code": "", "ledger_fields": "1", "ledger_account": "84A0"})
        assert r.status_code == 200
        assert "nur aus Ziffern" in r.get_data(as_text=True)
        assert Account.query.count() == 0

    def test_real_account_field(self, logged_in, switched_on):
        r = logged_in.post("/accounting/real-accounts/new", headers=MODAL, data={
            "name": "Giro", "account_type": "bank", "opening_balance": "0",
            "ledger_fields": "1", "ledger_account": "1200"})
        assert r.status_code == 204
        assert RealAccount.query.one().ledger_account == "1200"

    def test_tax_rate_keys(self, logged_in, switched_on):
        row = TaxRate(rate=Decimal("19"), active=True)
        db.session.add(row)
        db.session.commit()
        body = logged_in.get(f"/einstellungen/tax-rates/{row.id}", headers=MODAL).get_data(as_text=True)
        assert 'name="ledger_tax_key_output"' in body
        assert 'placeholder="3"' in body and 'placeholder="9"' in body   # Länder-Default
        r = logged_in.post(f"/einstellungen/tax-rates/{row.id}", headers=MODAL, data={
            "label": "", "active": "1", "ledger_fields": "1",
            "ledger_tax_key_output": "1003", "ledger_tax_key_input": ""})
        assert r.status_code == 204
        db.session.refresh(row)
        assert (row.ledger_tax_key_output, row.ledger_tax_key_input) == ("1003", None)

    def test_switching_off_keeps_values(self, logged_in, switched_on):
        a = Account(name="Erlöse", ledger_account="8400")
        db.session.add(a)
        db.session.commit()
        logged_in.post("/einstellungen/", data=_settings_form(accounting_handover_format=""))
        assert AppSetting.get(handover.SETTING_KEY) is None
        # Formular ohne Felder (Format aus) darf den Wert nicht löschen.
        logged_in.post(f"/accounting/accounts/{a.id}/edit", headers=MODAL,
                       data={"name": "Erlöse", "code": "", "active": "1"})
        db.session.refresh(a)
        assert a.ledger_account == "8400"
        assert "<code>8400</code>" not in logged_in.get("/accounting/accounts").get_data(as_text=True)
