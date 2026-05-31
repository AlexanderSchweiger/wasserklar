"""Integration-Tests fuer Abrechnungsperioden und die datumsbasierte
Verbrauchsberechnung (oss-v1.3.0).

Deckt ab: ``BillingPeriod.current``/``activate`` (Eine-Periode-aktiv-
Invariante), ``save_reading`` (Anlegen/Aktualisieren je Periode,
Verbrauch gegen die Vorablesung nach Datum), ``recompute_meter_chain``.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.meters.services import recompute_meter_chain, save_reading
from app.models import BillingPeriod, MeterReading, Property, WaterMeter


def _period(name, start, end, active=False):
    p = BillingPeriod(name=name, start_date=start, end_date=end, active=active)
    db.session.add(p)
    db.session.flush()
    return p


def _meter(initial_value=None):
    prop = Property(object_number="P", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    m = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main",
                   initial_value=initial_value)
    db.session.add(m)
    db.session.flush()
    return m


# ---------------------------------------------------------------------------
# BillingPeriod-Model
# ---------------------------------------------------------------------------

class TestBillingPeriodModel:
    def test_current_returns_active(self, app):
        _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=False)
        p2 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        db.session.commit()
        assert BillingPeriod.current().id == p2.id

    def test_current_none_when_no_active(self, app):
        _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=False)
        db.session.commit()
        assert BillingPeriod.current() is None

    def test_activate_deactivates_others(self, app):
        p1 = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        p2 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=False)
        db.session.commit()
        p2.activate()
        db.session.commit()
        assert BillingPeriod.current().id == p2.id
        assert db.session.get(BillingPeriod, p1.id).active is False
        assert BillingPeriod.query.filter_by(active=True).count() == 1


# ---------------------------------------------------------------------------
# save_reading
# ---------------------------------------------------------------------------

class TestSaveReading:
    def test_creates_reading(self, app):
        period = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        m = _meter()
        db.session.commit()
        r = save_reading(m, period, Decimal("120"), reading_date=date(2024, 6, 1))
        db.session.commit()
        assert r.billing_period_id == period.id
        assert r.value == Decimal("120")
        assert r.reading_date == date(2024, 6, 1)

    def test_update_existing_same_period(self, app):
        period = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        m = _meter()
        db.session.commit()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        save_reading(m, period, Decimal("200"))
        db.session.commit()
        # Genau eine Ablesung pro (Zaehler, Periode).
        assert MeterReading.query.filter_by(meter_id=m.id).count() == 1
        assert MeterReading.query.filter_by(meter_id=m.id).one().value == Decimal("200")

    def test_consumption_against_previous_period(self, app):
        p23 = _period("2023", date(2023, 1, 1), date(2023, 12, 31))
        p24 = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        m = _meter()
        db.session.commit()
        save_reading(m, p23, Decimal("100"), reading_date=date(2023, 12, 31))
        db.session.commit()
        r = save_reading(m, p24, Decimal("175"), reading_date=date(2024, 12, 31))
        db.session.commit()
        assert r.consumption == Decimal("75")

    def test_consumption_against_initial_value(self, app):
        period = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        m = _meter(initial_value=Decimal("10"))
        db.session.commit()
        r = save_reading(m, period, Decimal("100"))
        db.session.commit()
        assert r.consumption == Decimal("90")

    def test_editing_old_reading_recomputes_following(self, app):
        # Korrektur des 2023-Werts muss den 2024-Verbrauch nachziehen.
        p23 = _period("2023", date(2023, 1, 1), date(2023, 12, 31))
        p24 = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        m = _meter()
        db.session.commit()
        save_reading(m, p23, Decimal("100"), reading_date=date(2023, 12, 31))
        save_reading(m, p24, Decimal("175"), reading_date=date(2024, 12, 31))
        db.session.commit()
        # 2023-Ablesung auf 50 korrigieren.
        save_reading(m, p23, Decimal("50"), reading_date=date(2023, 12, 31))
        db.session.commit()
        r24 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p24.id).one()
        assert r24.consumption == Decimal("125")  # 175 - 50


# ---------------------------------------------------------------------------
# recompute_meter_chain
# ---------------------------------------------------------------------------

class TestRecomputeMeterChain:
    def test_out_of_order_insert(self, app):
        # 2024 zuerst, dann 2023 dazwischen einfuegen.
        p23 = _period("2023", date(2023, 1, 1), date(2023, 12, 31))
        p24 = _period("2024", date(2024, 1, 1), date(2024, 12, 31))
        m = _meter(initial_value=Decimal("0"))
        db.session.commit()
        db.session.add(MeterReading(
            meter_id=m.id, billing_period_id=p24.id,
            value=Decimal("200"), reading_date=date(2024, 12, 31)))
        db.session.add(MeterReading(
            meter_id=m.id, billing_period_id=p23.id,
            value=Decimal("80"), reading_date=date(2023, 12, 31)))
        db.session.flush()
        recompute_meter_chain(m)
        db.session.commit()
        r23 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p23.id).one()
        r24 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p24.id).one()
        assert r23.consumption == Decimal("80")    # 80 - 0
        assert r24.consumption == Decimal("120")   # 200 - 80

    def test_delete_middle_reading_bridges_following(self, app):
        # Mittleren Stand loeschen -> Folge-Ablesung ueberbrueckt die Luecke.
        p23 = _period("2023", date(2023, 1, 1), date(2023, 12, 31))
        p24 = _period("2024", date(2024, 1, 1), date(2024, 12, 31))
        p25 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        m = _meter(initial_value=Decimal("0"))
        db.session.commit()
        save_reading(m, p23, Decimal("100"), reading_date=date(2023, 12, 31))
        save_reading(m, p24, Decimal("175"), reading_date=date(2024, 12, 31))
        save_reading(m, p25, Decimal("230"), reading_date=date(2025, 12, 31))
        db.session.commit()
        # 2024 entfernen und Kette neu rechnen.
        r24 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p24.id).one()
        db.session.delete(r24)
        db.session.flush()
        recompute_meter_chain(m)
        db.session.commit()
        r25 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p25.id).one()
        assert r25.consumption == Decimal("130")   # 230 - 100 (statt 230 - 175)

    def test_delete_first_reading_falls_back_to_initial(self, app):
        # Ersten Stand loeschen -> naechster rechnet gegen initial_value.
        p23 = _period("2023", date(2023, 1, 1), date(2023, 12, 31))
        p24 = _period("2024", date(2024, 1, 1), date(2024, 12, 31), active=True)
        m = _meter(initial_value=Decimal("10"))
        db.session.commit()
        save_reading(m, p23, Decimal("100"), reading_date=date(2023, 12, 31))
        save_reading(m, p24, Decimal("175"), reading_date=date(2024, 12, 31))
        db.session.commit()
        r23 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p23.id).one()
        db.session.delete(r23)
        db.session.flush()
        recompute_meter_chain(m)
        db.session.commit()
        r24 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p24.id).one()
        assert r24.consumption == Decimal("165")   # 175 - 10 (initial_value)
