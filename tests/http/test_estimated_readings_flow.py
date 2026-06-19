"""HTTP-Tests fuer geschaetzte Zaehlerstaende — End-to-End ueber die Routen.

Deckt ab:
- ``/meters/readings/estimate-missing`` schaetzt fehlende Staende (Bulk).
- ``/meters/<id>/read`` mit ``is_estimated`` markiert die Schaetzung.
- Lebenszyklus: Schaetzung abrechnen -> echter Stand erzeugt Korrekturposten ->
  naechster Rechnungslauf zieht ihn als eigene Position ein.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    BillingPeriod, FiscalYear, Customer, Property, PropertyOwnership,
    WaterMeter, WaterTariff, MeterReading, Invoice, InvoiceItem,
    ReadingCorrection, BillingRun, User,
)
from app.meters.services import recompute_meter_chain
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post("/auth/login",
                       data={"username": "admin", "password": "secret"})


def _period(name, y, *, active=False):
    p = BillingPeriod(name=name, start_date=date(y, 1, 1),
                      end_date=date(y, 12, 31), active=active)
    db.session.add(p)
    db.session.flush()
    return p


class TestEstimateMissingRoute:
    def test_bulk_estimate_creates_estimated_reading(self, client, admin):
        _login(client)
        prop = Property(object_number="P-1", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                           meter_type="main", initial_value=Decimal("0"))
        db.session.add(meter)
        db.session.flush()
        p22 = _period("2022", 2022)
        p23 = _period("2023", 2023)
        p24 = _period("2024", 2024)
        for per, val, d in [(p22, "20", date(2022, 12, 1)),
                            (p23, "50", date(2023, 12, 1)),
                            (p24, "90", date(2024, 12, 1))]:
            db.session.add(MeterReading(
                meter_id=meter.id, billing_period_id=per.id,
                value=Decimal(val), reading_date=d))
        # Verbrauchskette einfrieren
        from app.meters.services import recompute_meter_chain
        db.session.flush()
        recompute_meter_chain(meter)
        p25 = _period("2025", 2025, active=True)
        db.session.commit()

        r = client.post("/meters/readings/estimate-missing",
                        data={"billing_period_id": str(p25.id)},
                        follow_redirects=False)
        assert r.status_code == 302

        reading = MeterReading.query.filter_by(
            meter_id=meter.id, billing_period_id=p25.id).one()
        assert reading.is_estimated is True
        # letzter Stand 90 + Ø(20,30,40)=30 -> 120
        assert int(reading.value) == 120


class TestEstimatedReadingLifecycle:
    @pytest.fixture
    def setup(self, app):
        today = date.today()
        db.session.add(FiscalYear(
            year=today.year, start_date=date(today.year, 1, 1),
            end_date=date(today.year, 12, 31)))
        tariff = WaterTariff(name="T", valid_from=2024,
                             base_fee=Decimal("30"), price_per_m3=Decimal("2"))
        db.session.add(tariff)
        cust = Customer(name="Kunde", customer_number=1)
        db.session.add(cust)
        db.session.flush()
        prop = Property(object_number="P-1", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=cust.id,
            valid_from=date(2000, 1, 1), valid_to=None))
        meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                           meter_type="main", initial_value=Decimal("0"))
        db.session.add(meter)
        db.session.flush()
        p24 = _period("2024", 2024, active=True)
        db.session.commit()
        return {"tariff": tariff, "cust": cust, "prop": prop,
                "meter": meter, "p24": p24}

    def test_full_cycle_estimate_bill_real_correction(self, client, admin, setup):
        _login(client)
        meter, p24, tariff = setup["meter"], setup["p24"], setup["tariff"]

        # 1. Schaetzung 2024 ueber die Route erfassen (is_estimated=1, Stand 50)
        client.post(f"/meters/{meter.id}/read", data={
            "billing_period_id": str(p24.id), "value": "50",
            "reading_date": "2024-12-31", "is_estimated": "1",
        }, follow_redirects=False)
        reading = MeterReading.query.filter_by(
            meter_id=meter.id, billing_period_id=p24.id).one()
        assert reading.is_estimated is True
        assert reading.consumption == Decimal("50.000")

        # 2. Rechnungslauf 2024 -> Verbrauchsposition als "geschätzt" markiert
        client.post("/invoices/generate", data={
            "billing_period_id": str(p24.id), "tariff_id": str(tariff.id),
            "due_days": "30",
        }, follow_redirects=False)
        inv24 = Invoice.query.filter_by(billing_period_id=p24.id).one()
        cons = [i for i in inv24.items if i.unit == "m³"][0]
        assert cons.is_estimated is True
        # Rechnung ausstellen (sonst entsteht keine Korrektur)
        inv24.status = Invoice.STATUS_SENT
        db.session.commit()

        # 3. Echter Stand 60 nachreichen (keine Schaetzung) -> Nachforderung +20
        client.post(f"/meters/{meter.id}/read", data={
            "billing_period_id": str(p24.id), "value": "60",
            "reading_date": "2025-01-15",
        }, follow_redirects=False)
        reading = MeterReading.query.filter_by(
            meter_id=meter.id, billing_period_id=p24.id).one()
        assert reading.is_estimated is False
        corr = ReadingCorrection.query.one()
        assert corr.amount == Decimal("20.00")
        assert corr.status == ReadingCorrection.STATUS_OPEN

        # 4. Folgeperiode 2025 + Stand -> Rechnungslauf zieht die Nachforderung ein
        p25 = _period("2025", 2025, active=True)
        db.session.flush()
        client.post(f"/meters/{meter.id}/read", data={
            "billing_period_id": str(p25.id), "value": "110",
            "reading_date": "2025-12-31",
        }, follow_redirects=False)

        client.post("/invoices/generate", data={
            "billing_period_id": str(p25.id), "tariff_id": str(tariff.id),
            "due_days": "30",
        }, follow_redirects=False)

        inv25 = Invoice.query.filter_by(billing_period_id=p25.id).one()
        # Verbrauch 50*2=100 + Grundgebuehr 30 + Nachverrechnung 20 = 150
        assert inv25.total_amount == Decimal("150.00")
        corr_items = [i for i in inv25.items if "Nachverrechnung" in i.description]
        assert len(corr_items) == 1
        assert corr_items[0].amount == Decimal("20.00")

        corr = ReadingCorrection.query.one()
        assert corr.status == ReadingCorrection.STATUS_APPLIED
        assert corr.applied_invoice_id == inv25.id
        assert corr.remaining_amount == Decimal("0.00")


class TestNegativeInvoiceCapped:
    """Negativer Verbrauch (zu hohe Vorperioden-Schätzung) darf keine
    Minus-Rechnung erzeugen — auf 0 kappen, Rest als Gutschrift vertagen."""

    @pytest.fixture
    def setup(self, app):
        today = date.today()
        db.session.add(FiscalYear(
            year=today.year, start_date=date(today.year, 1, 1),
            end_date=date(today.year, 12, 31)))
        # Kein Grund-/Zusatzgebühr -> Verbrauch ist die einzige Position.
        tariff = WaterTariff(name="T", valid_from=2026, price_per_m3=Decimal("1.5"))
        db.session.add(tariff)
        cust = Customer(name="Kunde", customer_number=1)
        db.session.add(cust)
        db.session.flush()
        prop = Property(object_number="P-1", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=cust.id,
            valid_from=date(2000, 1, 1), valid_to=None))
        meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                           meter_type="main", initial_value=Decimal("0"))
        db.session.add(meter)
        db.session.flush()
        return {"tariff": tariff, "cust": cust, "meter": meter}

    def test_negative_consumption_capped_then_carried(self, client, admin, setup):
        _login(client)
        meter, tariff = setup["meter"], setup["tariff"]
        p26 = _period("2026", 2026, active=True)
        db.session.flush()
        # echter Stand 100 in 2026
        db.session.add(MeterReading(meter_id=meter.id, billing_period_id=p26.id,
                                    value=Decimal("100"), reading_date=date(2026, 12, 1)))
        # niedrigerer Stand 94 in 2027 -> Verbrauch -6
        p27 = _period("2027", 2027, active=False)
        db.session.flush()
        from app.meters.services import recompute_meter_chain
        db.session.add(MeterReading(meter_id=meter.id, billing_period_id=p27.id,
                                    value=Decimal("94"), reading_date=date(2027, 12, 1)))
        db.session.flush()
        recompute_meter_chain(meter)
        db.session.commit()

        # Rechnungslauf 2027 -> Verbrauch -6 * 1,5 = -9,00, auf 0 gekappt
        client.post("/invoices/generate", data={
            "billing_period_id": str(p27.id), "tariff_id": str(tariff.id),
            "due_days": "30",
        }, follow_redirects=False)
        inv27 = Invoice.query.filter_by(billing_period_id=p27.id).one()
        assert inv27.total_amount == Decimal("0.00")  # NIE negativ
        assert any("Guthaben aus Vorperiode" in i.description for i in inv27.items)
        corr = ReadingCorrection.query.one()
        assert corr.amount == Decimal("-9.00")
        assert corr.status == ReadingCorrection.STATUS_OPEN

        # 2028 Stand 130 -> Verbrauch 36 * 1,5 = 54, minus Gutschrift 9 = 45
        p28 = _period("2028", 2028, active=False)
        db.session.flush()
        db.session.add(MeterReading(meter_id=meter.id, billing_period_id=p28.id,
                                    value=Decimal("130"), reading_date=date(2028, 12, 1)))
        db.session.flush()
        recompute_meter_chain(meter)
        db.session.commit()
        client.post("/invoices/generate", data={
            "billing_period_id": str(p28.id), "tariff_id": str(tariff.id),
            "due_days": "30",
        }, follow_redirects=False)
        inv28 = Invoice.query.filter_by(billing_period_id=p28.id).one()
        assert inv28.total_amount == Decimal("45.00")
        corr = ReadingCorrection.query.one()
        assert corr.status == ReadingCorrection.STATUS_APPLIED
        assert corr.applied_invoice_id == inv28.id


class TestCorrectionReversalOnDelete:
    """Löschen eines Rechnungslaufs muss den Korrektur-Ledger sauber rückabwickeln
    — sonst geht beim Löschen+Neu-Lauf ein teilverrechneter Gutschrift-Betrag
    verloren (genau der in test7 beobachtete -6/-9-Drift)."""

    @pytest.fixture
    def setup(self, app):
        today = date.today()
        db.session.add(FiscalYear(
            year=today.year, start_date=date(today.year, 1, 1),
            end_date=date(today.year, 12, 31)))
        tariff = WaterTariff(name="T", valid_from=2026, price_per_m3=Decimal("1.5"))
        db.session.add(tariff)
        cust = Customer(name="Kunde", customer_number=1)
        db.session.add(cust)
        db.session.flush()
        prop = Property(object_number="P-1", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=cust.id,
            valid_from=date(2000, 1, 1), valid_to=None))
        meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                           meter_type="main", initial_value=Decimal("0"))
        db.session.add(meter)
        db.session.flush()
        return {"tariff": tariff, "cust": cust, "meter": meter}

    def _reading(self, meter, period, value, d):
        db.session.add(MeterReading(
            meter_id=meter.id, billing_period_id=period.id,
            value=Decimal(str(value)), reading_date=d))

    def _run(self, client, period, tariff):
        client.post("/invoices/generate", data={
            "billing_period_id": str(period.id), "tariff_id": str(tariff.id),
            "due_days": "30",
        }, follow_redirects=False)

    def _make_credit_and_partial_apply(self, client, setup):
        meter, tariff = setup["meter"], setup["tariff"]
        p26 = _period("2026", 2026, active=True)
        db.session.flush()
        self._reading(meter, p26, 100, date(2026, 12, 1))
        p27 = _period("2027", 2027)
        db.session.flush()
        self._reading(meter, p27, 94, date(2027, 12, 1))   # -6 -> gekappt, Gutschrift -9
        db.session.flush(); recompute_meter_chain(meter); db.session.commit()
        self._run(client, p27, tariff)
        p28 = _period("2028", 2028)
        db.session.flush()
        self._reading(meter, p28, 96, date(2028, 12, 1))   # +2 m³ -> 3,00 -> Gutschrift teils
        db.session.flush(); recompute_meter_chain(meter); db.session.commit()
        self._run(client, p28, tariff)
        return p27, p28

    def test_delete_restores_partially_applied_credit(self, client, admin, setup):
        _login(client)
        p27, p28 = self._make_credit_and_partial_apply(client, setup)

        corr = ReadingCorrection.query.one()
        assert corr.remaining_amount == Decimal("-6.00")
        assert corr.status == ReadingCorrection.STATUS_PARTIAL
        assert Invoice.query.filter_by(billing_period_id=p28.id).one().total_amount == Decimal("0.00")

        # Lauf 2028 löschen -> Gutschrift wieder voll offen (KEIN Verlust)
        run28 = BillingRun.query.filter_by(billing_period_id=p28.id).one()
        client.post(f"/invoices/billing-runs/{run28.id}/delete", data={},
                    follow_redirects=False)
        assert Invoice.query.filter_by(billing_period_id=p28.id).count() == 0
        corr = ReadingCorrection.query.one()
        assert corr.remaining_amount == Decimal("-9.00")
        assert corr.status == ReadingCorrection.STATUS_OPEN
        assert corr.applied_invoice_id is None

        # Re-Run -> wieder genau -3.00 verrechnet, remaining -6.00 (kein Drift)
        self._run(client, p28, setup["tariff"])
        corr = ReadingCorrection.query.one()
        assert corr.remaining_amount == Decimal("-6.00")

    def test_cannot_delete_source_run_while_credit_consumed(self, client, admin, setup):
        _login(client)
        p27, p28 = self._make_credit_and_partial_apply(client, setup)

        # Quell-Lauf (2027) löschen, während die Gutschrift in 2028 teils
        # verrechnet ist -> abgelehnt, nichts gelöscht.
        run27 = BillingRun.query.filter_by(billing_period_id=p27.id).one()
        client.post(f"/invoices/billing-runs/{run27.id}/delete", data={},
                    follow_redirects=True)
        assert BillingRun.query.filter_by(billing_period_id=p27.id).count() == 1
        assert Invoice.query.filter_by(billing_period_id=p27.id).count() == 1
        assert ReadingCorrection.query.one().remaining_amount == Decimal("-6.00")


class TestCustomerCreditVisibility:
    """Offenes Guthaben/Nachforderung eines Kunden ist auf der Kundendetailseite
    sichtbar und die Korrekturen-Seite lässt sich pro Kunde filtern."""

    def _customer_with_credit(self, amount):
        cust = Customer(name="Guthaben Kunde", customer_number=7)
        db.session.add(cust)
        db.session.flush()
        prop = Property(object_number="P-9", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        meter = WaterMeter(property_id=prop.id, meter_number="Z-9", meter_type="main")
        db.session.add(meter)
        db.session.flush()
        period = _period("2027", 2027)
        db.session.flush()
        db.session.add(ReadingCorrection(
            customer_id=cust.id, meter_id=meter.id, billing_period_id=period.id,
            unit_price=Decimal("0"), tax_rate=None,
            amount=Decimal(str(amount)), remaining_amount=Decimal(str(amount)),
            status=ReadingCorrection.STATUS_OPEN))
        db.session.commit()
        return cust

    def test_customer_detail_shows_open_credit(self, client, admin):
        _login(client)
        cust = self._customer_with_credit("-12.00")
        html = client.get(f"/customers/{cust.id}").get_data(as_text=True)
        assert "Offenes Guthaben" in html
        assert "12,00" in html

    def test_customer_detail_shows_open_surcharge(self, client, admin):
        _login(client)
        cust = self._customer_with_credit("8.50")
        html = client.get(f"/customers/{cust.id}").get_data(as_text=True)
        assert "Offene Nachforderung" in html
        assert "8,50" in html

    def test_corrections_filtered_by_customer(self, client, admin):
        _login(client)
        cust = self._customer_with_credit("-12.00")
        r = client.get(f"/invoices/corrections?customer_id={cust.id}&status=all")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Guthaben Kunde" in body
        assert "Filter entfernen" in body
