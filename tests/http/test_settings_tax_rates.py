"""HTTP: Land + Steuersaetze in den Einstellungen (Tab „Mandant"/„Steuern")."""
import json
from decimal import Decimal

import pytest

from app import country, tax_service
from app.extensions import db
from app.models import AppSetting, TaxRate, User
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
def logged_in(client, admin):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


@pytest.fixture
def at_rates(app):
    from cli import seed_default_tax_rates
    seed_default_tax_rates(db, country_code="AT")


def _form(**extra):
    data = {"org_type": "cooperative", "invoice_document_format": "pdf",
            "invoice_design": "classic"}
    data.update(extra)
    return data


MODAL = {"X-From-Modal": "1", "HX-Request": "true"}


def test_settings_page_shows_country_and_tax_tab(logged_in, at_rates):
    resp = logged_in.get("/einstellungen/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="org_country"' in html
    assert 'id="pane-tax"' in html
    assert 'id="taxRatesCard"' in html
    assert "Standard Wasser" in html  # Badge am 10-%-Satz (AT-Default)


def test_create_tax_rate_via_modal(logged_in, at_rates):
    resp = logged_in.post("/einstellungen/tax-rates/new",
                          data={"rate": "5,5", "label": "Sonder", "active": "1"},
                          headers=MODAL)
    assert resp.status_code == 204
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "taxRateSaved" in trigger
    row = TaxRate.query.filter_by(rate=Decimal("5.5")).one()
    assert row.label == "Sonder" and row.active is True


def test_duplicate_rate_rejected(logged_in, at_rates):
    resp = logged_in.post("/einstellungen/tax-rates/new",
                          data={"rate": "20", "active": "1"}, headers=MODAL)
    assert resp.status_code == 200
    assert "gibt es bereits" in resp.get_data(as_text=True)


def test_cannot_deactivate_water_default(logged_in, at_rates):
    row = TaxRate.query.filter_by(rate=Decimal("10")).one()
    resp = logged_in.post(f"/einstellungen/tax-rates/{row.id}",
                          data={"label": "10 %"}, headers=MODAL)  # active fehlt = aus
    assert resp.status_code == 200
    assert "Standard-USt für Wasser" in resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(TaxRate, row.id).active is True


def test_deactivate_other_rate(logged_in, at_rates):
    row = TaxRate.query.filter_by(rate=Decimal("13")).one()
    resp = logged_in.post(f"/einstellungen/tax-rates/{row.id}",
                          data={"label": "13 %"}, headers=MODAL)
    assert resp.status_code == 204
    db.session.expire_all()
    assert db.session.get(TaxRate, row.id).active is False
    assert Decimal("13") not in tax_service.tax_rate_values()


def test_card_fragment(logged_in, at_rates):
    resp = logged_in.get("/einstellungen/tax-rates/card", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert 'id="taxRatesCard"' in resp.get_data(as_text=True)


def test_switch_country_to_germany(logged_in, at_rates):
    resp = logged_in.post("/einstellungen/", data=_form(org_country="DE", tax_water_rate="10"),
                          follow_redirects=True)
    assert resp.status_code == 200
    db.session.expire_all()
    assert AppSetting.get(country.SETTING_KEY) == "DE"
    assert tax_service.water_tax_rate() == Decimal("7")  # geposteter AT-Wert ignoriert
    assert {Decimal("7"), Decimal("19")} <= set(tax_service.tax_rate_values())
    assert "Land auf Deutschland umgestellt" in resp.get_data(as_text=True)


def test_water_rate_saved(logged_in, at_rates):
    logged_in.post("/einstellungen/", data=_form(org_country="AT", tax_water_rate="20"))
    db.session.expire_all()
    assert tax_service.water_tax_rate() == Decimal("20")


def test_apply_country_defaults_route(logged_in, at_rates):
    AppSetting.set(country.SETTING_KEY, "DE")
    db.session.commit()
    resp = logged_in.post("/einstellungen/country-defaults",
                          data={"deactivate_foreign": "1"})
    assert resp.status_code == 302
    db.session.expire_all()
    assert tax_service.tax_rate_values() == [Decimal("0"), Decimal("7"), Decimal("19")]
    assert TaxRate.query.filter_by(rate=Decimal("20")).one().active is False
