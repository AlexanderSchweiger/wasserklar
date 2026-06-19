"""Integration-Tests fuer geschaetzte Zaehlerstaende + Korrekturposten
(``app/meters/estimation.py``).

Deckt ab:
- ``estimate_meter_value``: letzter Stand + Ø-Verbrauch.
- Abgleich in ``save_reading``: ein echter Stand, der eine *abgerechnete*
  Schaetzung ersetzt, erzeugt eine ``ReadingCorrection`` (Nachforderung/
  Gutschrift); ohne ausgestellte Rechnung KEINEN Posten.
- ``apply_corrections_to_invoice``: Nachforderung voll, Gutschrift nie unter 0
  (Rest bleibt offen, Status ``Teilverrechnet``).
"""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.meters.services import save_reading
from app.meters import estimation
from app.models import (
    BillingPeriod, Property, WaterMeter, Customer, PropertyOwnership,
    Invoice, InvoiceItem, ReadingCorrection,
)


# --- Helpers ----------------------------------------------------------------

def _period(name, y, *, active=False):
    p = BillingPeriod(name=name, start_date=date(y, 1, 1),
                      end_date=date(y, 12, 31), active=active)
    db.session.add(p)
    db.session.flush()
    return p


def _property(num):
    prop = Property(object_number=num, object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    return prop


def _meter(prop, num, *, initial="0"):
    m = WaterMeter(property_id=prop.id, meter_number=num,
                   initial_value=(Decimal(initial) if initial is not None else None),
                   active=True, meter_type="main")
    db.session.add(m)
    db.session.flush()
    return m


def _customer(name="Kunde A"):
    c = Customer(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _own(prop, cust):
    db.session.add(PropertyOwnership(
        property_id=prop.id, customer_id=cust.id,
        valid_from=date(2000, 1, 1), valid_to=None))
    db.session.flush()


def _issued_invoice(cust, prop, period, *, m3, price, number, tax=None):
    """Ausgestellte Rechnung (Versendet) mit einer Verbrauchsposition."""
    inv = Invoice(
        invoice_number=number, customer_id=cust.id, property_id=prop.id,
        billing_period_id=period.id, date=date.today(),
        status=Invoice.STATUS_SENT)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description=f"Wasserverbrauch {period.name}",
        quantity=Decimal(str(m3)), unit="m³",
        unit_price=Decimal(str(price)),
        amount=(Decimal(str(m3)) * Decimal(str(price))).quantize(Decimal("0.01")),
        tax_rate=(Decimal(str(tax)) if tax else None), is_estimated=True))
    db.session.flush()
    inv.recalculate_total()
    db.session.flush()
    return inv


# --- estimate_meter_value ---------------------------------------------------

class TestEstimateValue:
    def test_estimate_is_last_value_plus_average(self, app):
        prop = _property("P-1")
        meter = _meter(prop, "Z1", initial="100")
        p22 = _period("2022", 2022)
        p23 = _period("2023", 2023)
        p24 = _period("2024", 2024)
        # Verbraeuche 20/30/40 -> Ø 30; letzter Stand 190.
        save_reading(meter, p22, Decimal("120"), reading_date=date(2022, 12, 1))
        save_reading(meter, p23, Decimal("150"), reading_date=date(2023, 12, 1))
        save_reading(meter, p24, Decimal("190"), reading_date=date(2024, 12, 1))
        db.session.commit()

        p25 = _period("2025", 2025, active=True)
        db.session.commit()
        est = estimation.estimate_meter_value(meter, p25)
        assert est is not None
        assert est["avg_consumption"] == 30
        assert int(est["base_value"]) == 190
        assert int(est["value"]) == 220  # 190 + 30

    def test_no_basis_returns_none(self, app):
        prop = _property("P-2")
        meter = _meter(prop, "Z2", initial=None)  # kein Vorstand, keine Historie
        p25 = _period("2025", 2025, active=True)
        db.session.commit()
        assert estimation.estimate_meter_value(meter, p25) is None


# --- Abgleich (echter Stand ersetzt abgerechnete Schaetzung) ----------------

class TestReconcile:
    def _setup_estimated_and_billed(self, *, est_m3, price):
        """Schaetzung in 2025 + ausgestellte Rechnung darueber. Gibt
        (meter, period, customer, invoice) zurueck."""
        prop = _property("P-1")
        meter = _meter(prop, "Z1", initial="0")
        cust = _customer()
        _own(prop, cust)
        p25 = _period("2025", 2025, active=True)
        # Schaetzung: Verbrauch == est_m3 (gegen initial 0).
        save_reading(meter, p25, Decimal(str(est_m3)), is_estimated=True,
                     reading_date=date(2025, 12, 1))
        db.session.commit()
        inv = _issued_invoice(cust, prop, p25, m3=est_m3, price=price,
                              number="2025-00001")
        db.session.commit()
        return meter, p25, cust, inv

    def test_real_higher_creates_nachforderung(self, app):
        meter, p25, cust, inv = self._setup_estimated_and_billed(est_m3=50, price=2)
        # Echter Stand 60 -> Verbrauch 60, Schaetzung war 50 -> +10 m³ * 2 = +20.
        save_reading(meter, p25, Decimal("60"), is_estimated=False,
                     reading_date=date(2025, 12, 15))
        db.session.commit()

        corrs = ReadingCorrection.query.filter_by(customer_id=cust.id).all()
        assert len(corrs) == 1
        c = corrs[0]
        assert c.delta_m3 == Decimal("10.000")
        assert c.amount == Decimal("20.00")
        assert c.remaining_amount == Decimal("20.00")
        assert c.status == ReadingCorrection.STATUS_OPEN
        assert not c.is_credit
        # Stand ist jetzt echt:
        from app.models import MeterReading
        r = MeterReading.query.filter_by(
            meter_id=meter.id, billing_period_id=p25.id).first()
        assert r.is_estimated is False

    def test_real_lower_creates_gutschrift(self, app):
        meter, p25, cust, inv = self._setup_estimated_and_billed(est_m3=50, price=2)
        # Echter Stand 40 -> Verbrauch 40, Schaetzung war 50 -> -10 m³ * 2 = -20.
        save_reading(meter, p25, Decimal("40"), is_estimated=False,
                     reading_date=date(2025, 12, 15))
        db.session.commit()
        c = ReadingCorrection.query.filter_by(customer_id=cust.id).one()
        assert c.amount == Decimal("-20.00")
        assert c.is_credit

    def test_no_invoice_no_correction(self, app):
        """Schaetzung wurde nie abgerechnet -> kein Korrekturposten."""
        prop = _property("P-9")
        meter = _meter(prop, "Z9", initial="0")
        cust = _customer()
        _own(prop, cust)
        p25 = _period("2025", 2025, active=True)
        save_reading(meter, p25, Decimal("50"), is_estimated=True)
        db.session.commit()
        # echter Stand, aber keine ausgestellte Rechnung existiert
        save_reading(meter, p25, Decimal("60"), is_estimated=False)
        db.session.commit()
        assert ReadingCorrection.query.count() == 0


# --- Einziehen in die Folgerechnung ----------------------------------------

class TestApplyCorrections:
    def _corr(self, cust, meter, period, *, amount, tax=None):
        c = ReadingCorrection(
            customer_id=cust.id, meter_id=meter.id, billing_period_id=period.id,
            estimated_consumption=Decimal("50"), real_consumption=Decimal("0"),
            delta_m3=Decimal("0"), unit_price=Decimal("2"),
            tax_rate=(Decimal(str(tax)) if tax else None),
            amount=Decimal(str(amount)), remaining_amount=Decimal(str(amount)),
            status=ReadingCorrection.STATUS_OPEN)
        db.session.add(c)
        db.session.flush()
        return c

    def _draft_with_consumption(self, cust, *, gross):
        inv = Invoice(invoice_number="2026-00001", customer_id=cust.id,
                      date=date.today(), status=Invoice.STATUS_DRAFT)
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description="Wasserverbrauch 2026",
            quantity=Decimal("1"), unit="Pauschal",
            unit_price=Decimal(str(gross)), amount=Decimal(str(gross))))
        db.session.flush()
        inv.recalculate_total()
        return inv

    def test_nachforderung_added_full(self, app):
        cust = _customer()
        prop = _property("P-1")
        meter = _meter(prop, "Z1")
        period = _period("2025", 2025)
        self._corr(cust, meter, period, amount="20")
        db.session.commit()
        inv = self._draft_with_consumption(cust, gross="100")
        estimation.apply_corrections_to_invoice(inv, cust.id)
        db.session.commit()
        assert inv.total_amount == Decimal("120.00")
        c = ReadingCorrection.query.filter_by(customer_id=cust.id).one()
        assert c.status == ReadingCorrection.STATUS_APPLIED
        assert c.remaining_amount == Decimal("0.00")
        assert c.applied_invoice_id == inv.id

    def test_gutschrift_full_when_headroom(self, app):
        cust = _customer()
        prop = _property("P-1")
        meter = _meter(prop, "Z1")
        period = _period("2025", 2025)
        self._corr(cust, meter, period, amount="-50")
        db.session.commit()
        inv = self._draft_with_consumption(cust, gross="100")
        estimation.apply_corrections_to_invoice(inv, cust.id)
        db.session.commit()
        assert inv.total_amount == Decimal("50.00")
        c = ReadingCorrection.query.filter_by(customer_id=cust.id).one()
        assert c.status == ReadingCorrection.STATUS_APPLIED
        assert c.remaining_amount == Decimal("0.00")

    def test_gutschrift_never_below_zero_remainder_carries(self, app):
        cust = _customer()
        prop = _property("P-1")
        meter = _meter(prop, "Z1")
        period = _period("2025", 2025)
        # Gutschrift 200 gegen Rechnung 100 -> nur 100 verrechnet, Rest -100 offen.
        self._corr(cust, meter, period, amount="-200")
        db.session.commit()
        inv = self._draft_with_consumption(cust, gross="100")
        estimation.apply_corrections_to_invoice(inv, cust.id)
        db.session.commit()
        assert inv.total_amount == Decimal("0.00")  # nie unter 0
        c = ReadingCorrection.query.filter_by(customer_id=cust.id).one()
        assert c.status == ReadingCorrection.STATUS_PARTIAL
        assert c.remaining_amount == Decimal("-100.00")
        assert c.applied_invoice_id == inv.id

    def _draft_taxed(self, cust, *, net, rate, number="2026-00009"):
        inv = Invoice(invoice_number=number, customer_id=cust.id,
                      date=date.today(), status=Invoice.STATUS_DRAFT)
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description="Wasserverbrauch 2026",
            quantity=Decimal("1"), unit="Pauschal",
            unit_price=Decimal(str(net)), amount=Decimal(str(net)),
            tax_rate=Decimal(str(rate))))
        db.session.flush()
        inv.recalculate_total()
        return inv

    def test_taxed_gutschrift_never_below_zero(self, app):
        """Auch mit USt fällt der Betrag nie unter 0 (rundungssicher)."""
        cust = _customer()
        prop = _property("P-1")
        meter = _meter(prop, "Z1")
        period = _period("2025", 2025)
        self._corr(cust, meter, period, amount="-500", tax="10")
        db.session.commit()
        inv = self._draft_taxed(cust, net="100", rate="10")  # brutto 110
        estimation.apply_corrections_to_invoice(inv, cust.id)
        db.session.commit()
        assert inv.total_amount == Decimal("0.00")
        c = ReadingCorrection.query.one()
        assert c.status == ReadingCorrection.STATUS_PARTIAL
        # 100 netto Gutschrift verrechnet (brutto 110), Rest -400 wandert weiter
        assert c.remaining_amount == Decimal("-400.00")

    def test_negative_invoice_capped_and_credit_carried(self, app):
        """Negativer Verbrauch -> Rechnung wird auf 0 gekappt, Rest als
        Gutschrift übertragen (mit USt, rundungssicher)."""
        cust = _customer()
        prop = _property("P-1")
        meter = _meter(prop, "Z1")
        period = _period("2027", 2027)
        inv = Invoice(invoice_number="2027-00001", customer_id=cust.id,
                      property_id=prop.id, billing_period_id=period.id,
                      date=date.today(), status=Invoice.STATUS_DRAFT)
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description="Wasserverbrauch 2027",
            quantity=Decimal("-6"), unit="m³", unit_price=Decimal("1.5"),
            amount=Decimal("-9.00"), tax_rate=Decimal("10")))
        db.session.flush()
        inv.recalculate_total()
        assert inv.total_amount == Decimal("-9.90")

        corr = estimation.cap_invoice_at_zero(
            inv, customer_id=cust.id, meter_id=meter.id, period_id=period.id,
            tax_rate=Decimal("10"))
        db.session.commit()
        assert inv.total_amount == Decimal("0.00")  # nie negativ
        assert corr is not None
        assert corr.amount == Decimal("-9.00")
        assert corr.remaining_amount == Decimal("-9.00")
        assert corr.status == ReadingCorrection.STATUS_OPEN

    def test_positive_invoice_not_capped(self, app):
        cust = _customer()
        prop = _property("P-2")
        meter = _meter(prop, "Z2")
        period = _period("2027", 2027)
        inv = Invoice(invoice_number="2027-00002", customer_id=cust.id,
                      property_id=prop.id, billing_period_id=period.id,
                      date=date.today(), status=Invoice.STATUS_DRAFT)
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description="W", quantity=Decimal("5"),
            unit="m³", unit_price=Decimal("2"), amount=Decimal("10.00")))
        db.session.flush()
        inv.recalculate_total()
        corr = estimation.cap_invoice_at_zero(
            inv, customer_id=cust.id, meter_id=meter.id, period_id=period.id,
            tax_rate=None)
        assert corr is None
        assert inv.total_amount == Decimal("10.00")

    def test_nachforderung_increases_credit_headroom(self, app):
        """Nachforderung wird vor Gutschrift verrechnet und vergroessert den
        Spielraum -> eine danach passende Gutschrift geht voll auf."""
        cust = _customer()
        prop = _property("P-1")
        meter = _meter(prop, "Z1")
        period = _period("2025", 2025)
        self._corr(cust, meter, period, amount="50")    # Nachforderung
        self._corr(cust, meter, period, amount="-130")  # Gutschrift
        db.session.commit()
        inv = self._draft_with_consumption(cust, gross="100")
        estimation.apply_corrections_to_invoice(inv, cust.id)
        db.session.commit()
        # 100 + 50 - 130 = 20
        assert inv.total_amount == Decimal("20.00")
        for c in ReadingCorrection.query.all():
            assert c.status == ReadingCorrection.STATUS_APPLIED
            assert c.remaining_amount == Decimal("0.00")
