"""HTTP-Tests fuer den periodenbasierten Rechnungslauf (oss-v1.3.0).

Der Rechnungslauf ``/invoices/generate`` baut seit oss-v1.3.0 auf einer
``BillingPeriod`` auf statt auf einer Jahreszahl.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, BillingPeriod, BillingRun, Customer, FiscalYear, Invoice,
    MeterReading, Property, PropertyOwnership, User, WaterMeter, WaterTariff,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def billing_setup(app):
    """Periode, Tarif, Konto, Buchungsjahr, Objekt mit Besitzer + Zaehler +
    Ablesung in der Periode."""
    today = date.today()
    db.session.add(FiscalYear(
        year=today.year, start_date=date(today.year, 1, 1),
        end_date=date(today.year, 12, 31)))
    period = BillingPeriod(
        name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        active=True)
    db.session.add(period)
    tariff = WaterTariff(
        name="T", valid_from=2024, base_fee=Decimal("30"),
        price_per_m3=Decimal("2"))
    db.session.add(tariff)
    account = Account(name="Wasser")
    db.session.add(account)
    cust = Customer(name="Kunde", customer_number=1)
    db.session.add(cust)
    db.session.flush()
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    db.session.add(PropertyOwnership(
        property_id=prop.id, customer_id=cust.id,
        valid_from=date(2020, 1, 1), valid_to=None))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main")
    db.session.add(meter)
    db.session.flush()
    db.session.add(MeterReading(
        meter_id=meter.id, billing_period_id=period.id,
        value=Decimal("150"), consumption=Decimal("50"),
        reading_date=date(2024, 12, 31)))
    db.session.commit()
    return {"period": period, "tariff": tariff, "account": account}


class TestBillingRun:
    def test_generate_creates_invoice_for_period(self, client, admin, billing_setup):
        _login(client)
        r = client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)
        assert r.status_code == 302

        run = BillingRun.query.one()
        assert run.billing_period_id == billing_setup["period"].id

        inv = Invoice.query.one()
        assert inv.billing_period_id == billing_setup["period"].id
        consumption_items = [i for i in inv.items if i.unit == "m³"]
        assert len(consumption_items) == 1
        assert consumption_items[0].quantity == Decimal("50")

    def test_generate_skips_duplicate_period(self, client, admin, billing_setup):
        _login(client)
        data = {
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }
        client.post("/invoices/generate", data=data, follow_redirects=False)
        client.post("/invoices/generate", data=data, follow_redirects=False)
        # Zweiter Lauf legt keine zweite Rechnung fuer dasselbe Objekt+Periode an.
        assert Invoice.query.count() == 1

    def test_generate_without_period_flashes_and_redirects(self, client, admin,
                                                           billing_setup):
        _login(client)
        r = client.post("/invoices/generate", data={
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert Invoice.query.count() == 0


class TestBillingRunDetail:
    def _generate(self, client, billing_setup):
        return client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)

    def test_detail_page_renders(self, client, admin, billing_setup):
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        r = client.get(f"/invoices/billing-runs/{run.id}")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # Neue Bausteine sind da (faengt Template-Runtime-Fehler ab).
        assert "Zusammenfassung" in html
        assert "Versenden" in html
        assert "per Post" in html          # Versand-Aufschlüsselung (Entwurf vorhanden)
        # Neue Finanz-Zusammenfassung: Gesamt / Bezahlt / Offen.
        assert "Gesamtsumme" in html
        assert "Zahlungseingang" in html
        assert "Brutto" in html
        # Kunde ohne Mail-Einwilligung -> Post-Versand vorausgewaehlt.
        assert 'data-versandart="post"' in html

    def test_detail_partitions_mail_vs_post(self, client, admin, billing_setup):
        # Kunde auf E-Mail-Versand umstellen (email + rechnung_per_email).
        cust = Customer.query.one()
        cust.email = "k@k.test"
        cust.rechnung_per_email = True
        db.session.commit()
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        r = client.get(f"/invoices/billing-runs/{run.id}")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # Mailbare Rechnung erscheint im Versenden-Dialog.
        assert "k@k.test" in html
        assert 'data-versandart="mail"' in html

    def test_post_bulk_without_weasyprint_redirects(self, client, admin, billing_setup):
        # Ohne WeasyPrint (requirements-dev) faellt der Post-Bulk sauber auf
        # Flash+Redirect zurueck statt zu crashen; Status bleibt Entwurf.
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        inv = Invoice.query.one()
        r = client.post(
            f"/invoices/billing-runs/{run.id}/post-bulk-merged",
            data={"invoice_ids": str(inv.id)}, follow_redirects=False)
        assert r.status_code == 302
        assert inv.status == Invoice.STATUS_DRAFT
