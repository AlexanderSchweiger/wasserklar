"""Startwert der E-Rechnung-Einstellung je Land (neue Mandanten: AT aus, DE an)."""
import pytest

from app.einvoice import service
from app.extensions import db
from app.models import AppSetting, Customer, Invoice
from cli import seed_default_einvoice_setting


def _invoice():
    customer = Customer(name="Kunde", is_customer=True)
    db.session.add(customer)
    db.session.flush()
    from datetime import date
    inv = Invoice(invoice_number="2026-00001", customer_id=customer.id, date=date(2026, 1, 1),
                  status=Invoice.STATUS_DRAFT)
    db.session.add(inv)
    db.session.commit()


class TestSeedDefaultEInvoiceSetting:
    @pytest.mark.parametrize("code,expected", [("AT", "false"), ("DE", "true")])
    def test_per_country(self, app, code, expected):
        seed_default_einvoice_setting(db, country_code=code)
        assert AppSetting.get(service.ENABLED_KEY) == expected
        assert service.is_enabled() is (expected == "true")

    def test_uses_the_tenant_country_without_argument(self, app):
        AppSetting.set("org.country", "AT")
        db.session.commit()
        seed_default_einvoice_setting(db)
        assert service.is_enabled() is False

    def test_keeps_a_choice_the_tenant_made(self, app):
        AppSetting.set(service.ENABLED_KEY, "true")
        db.session.commit()
        seed_default_einvoice_setting(db, country_code="AT")
        assert service.is_enabled() is True

    def test_does_not_touch_an_installation_with_invoices(self, app):
        _invoice()
        seed_default_einvoice_setting(db, country_code="AT")
        assert AppSetting.get(service.ENABLED_KEY) is None
        assert service.is_enabled() is True
