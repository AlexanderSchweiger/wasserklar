"""Integration-Tests fuer ``year_billing_runs`` (Rechnungslaeufe im Jahresbericht).

Der Service liefert die Rechnungslaeufe eines Kalenderjahres (nach ``created_at``)
mit Anzahl und Brutto-Summe der **nicht stornierten** Rechnungen.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.accounting import services as acc_svc
from app.models import (
    BillingPeriod, BillingRun, Customer, Invoice, InvoiceItem,
)


def _invoice(run, cust, number, gross, status):
    inv = Invoice(
        invoice_number=number, customer_id=cust.id,
        billing_run_id=run.id, billing_period_id=run.billing_period_id,
        date=date(2025, 6, 1), status=status,
    )
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Wasser", quantity=Decimal("1"),
        unit="Pauschal", unit_price=Decimal(str(gross)), amount=Decimal(str(gross))))
    db.session.flush()
    inv.recalculate_total()
    return inv


@pytest.fixture
def run_with_invoices(app):
    period = BillingPeriod(
        name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        active=True)
    db.session.add(period)
    cust = Customer(name="Kunde", customer_number=1)
    db.session.add(cust)
    db.session.flush()
    run = BillingRun(
        billing_period_id=period.id, tariff_name="T",
        tariff_price_per_m3=Decimal("2"), invoices_created=2, invoices_skipped=1,
        created_at=datetime(2025, 6, 1, 10, 0, 0))
    db.session.add(run)
    db.session.flush()
    _invoice(run, cust, "2025-00001", "100.00", Invoice.STATUS_SENT)
    _invoice(run, cust, "2025-00002", "40.00", Invoice.STATUS_CANCELLED)
    db.session.commit()
    return {"run": run, "period": period, "cust": cust}


class TestYearBillingRuns:
    def test_returns_run_with_non_cancelled_totals(self, run_with_invoices):
        rows = acc_svc.year_billing_runs(2025)
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == run_with_invoices["run"].id
        assert r["period_name"] == "2025"
        assert r["invoices_skipped"] == 1
        # Stornierte Rechnung zaehlt nicht mit: count==1, Summe nur die SENT-Rechnung.
        assert r["count"] == 1
        assert r["sum_total"] == Decimal("100.00")

    def test_other_year_is_empty(self, run_with_invoices):
        assert acc_svc.year_billing_runs(2024) == []

    def test_run_without_valid_invoices_still_listed(self, app):
        period = BillingPeriod(
            name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
            active=True)
        db.session.add(period)
        db.session.flush()
        run = BillingRun(
            billing_period_id=period.id, tariff_name="T",
            tariff_price_per_m3=Decimal("2"),
            created_at=datetime(2025, 3, 1, 9, 0, 0))
        db.session.add(run)
        db.session.commit()
        rows = acc_svc.year_billing_runs(2025)
        assert len(rows) == 1
        assert rows[0]["count"] == 0
        assert rows[0]["sum_total"] == Decimal("0")
