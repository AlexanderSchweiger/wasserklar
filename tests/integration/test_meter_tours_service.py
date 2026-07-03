"""Integration-Tests fuer die Zaehlertausch-Touren-Services (DB-beruehrend)."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.meter_tours import services as svc
from app.models import (
    AppSetting, BillingPeriod, Customer, Invoice, MeterReplacement, MeterTour,
    MeterTourStop, Property, PropertyOwnership, WaterMeter,
)


def _property(object_number="P1", lat=48.001, lng=16.000):
    p = Property(
        object_number=object_number, object_type="Haus",
        strasse="Teststraße", hausnummer="1", plz="1234", ort="Testdorf",
        lat=lat, lng=lng,
    )
    db.session.add(p)
    db.session.flush()
    return p


def _meter(prop, number, eichjahr=2019, active=True):
    m = WaterMeter(meter_number=number, property_id=prop.id,
                   eichjahr=eichjahr, active=active)
    db.session.add(m)
    db.session.flush()
    return m


def _period():
    bp = BillingPeriod.query.filter_by(name="2026").first()
    if bp is None:
        bp = BillingPeriod(name="2026", start_date=date(2026, 1, 1),
                           end_date=date(2026, 12, 31), active=True)
        db.session.add(bp)
        db.session.flush()
    return bp


class TestDueMeters:
    def test_threshold_includes_and_excludes(self, app):
        prop = _property()
        _meter(prop, "M-2021", eichjahr=2021)   # 2021+5=2026 -> faellig 2026
        _meter(prop, "M-2022", eichjahr=2022)   # 2027 -> nicht faellig 2026
        db.session.commit()
        rows = svc.due_meters(due_until_year=2026)
        numbers = [r["meter"].meter_number for r in rows]
        assert "M-2021" in numbers
        assert "M-2022" not in numbers

    def test_inactive_and_null_eichjahr_excluded(self, app):
        prop = _property()
        _meter(prop, "M-old", eichjahr=2000, active=False)
        _meter(prop, "M-none", eichjahr=None)
        db.session.commit()
        assert svc.due_meters(due_until_year=2026) == []

    def test_interval_from_app_setting(self, app):
        prop = _property()
        _meter(prop, "M-2020", eichjahr=2020)
        AppSetting.set(svc.SETTING_INTERVAL, "6")
        db.session.commit()
        # Intervall 6: 2020+6=2026 -> faellig bis 2026, aber nicht bis 2025.
        assert len(svc.due_meters(due_until_year=2026)) == 1
        assert svc.due_meters(due_until_year=2025) == []

    def test_due_year_and_owner_loading(self, app):
        prop = _property()
        m = _meter(prop, "M-1", eichjahr=2019)
        c1 = Customer(name="Huber Anna")
        c2 = Customer(name="Huber Bernd")
        db.session.add_all([c1, c2])
        db.session.flush()
        # Zwei parallele aktive Eigentuemer sind erlaubt.
        db.session.add_all([
            PropertyOwnership(property_id=prop.id, customer_id=c1.id,
                              valid_from=date(2020, 1, 1)),
            PropertyOwnership(property_id=prop.id, customer_id=c2.id,
                              valid_from=date(2020, 1, 1)),
        ])
        db.session.commit()
        rows = svc.due_meters(due_until_year=2026)
        assert rows[0]["due_year"] == 2024
        assert {c.name for c in rows[0]["owners"]} == {"Huber Anna", "Huber Bernd"}

    def test_freitext_filter(self, app):
        prop = _property(object_number="OBJ-7")
        _meter(prop, "M-77", eichjahr=2019)
        db.session.commit()
        assert len(svc.due_meters(due_until_year=2026, q="obj-7")) == 1
        assert svc.due_meters(due_until_year=2026, q="gibtsnicht") == []

    def test_meters_in_open_tours_excluded(self, app, user):
        prop = _property()
        m = _meter(prop, "M-1", eichjahr=2019)
        db.session.commit()
        tour = svc.create_tour(name="T1", meter_ids=[m.id],
                               created_by_id=user.id)
        db.session.commit()
        assert svc.due_meters(due_until_year=2026) == []
        assert len(svc.due_meters(due_until_year=2026, include_toured=True)) == 1
        # Nach Abschluss der Tour taucht der offene Stop wieder auf.
        tour.status = MeterTour.STATUS_DONE
        db.session.commit()
        assert len(svc.due_meters(due_until_year=2026)) == 1


class TestCreateTour:
    def test_positions_follow_route(self, app, user):
        p1 = _property("P1", lat=48.001, lng=16.0)
        p2 = _property("P2", lat=48.010, lng=16.0)
        p3 = _property("P3", lat=48.005, lng=16.0)
        p4 = _property("P4", lat=None, lng=None)   # nicht geocodet
        m1 = _meter(p1, "M1")
        m2 = _meter(p2, "M2")
        m3 = _meter(p3, "M3")
        m4 = _meter(p4, "M4")
        db.session.commit()
        tour = svc.create_tour(
            name="Tour", meter_ids=[m2.id, m4.id, m1.id, m3.id],
            start_lat=48.000, start_lng=16.000, created_by_id=user.id)
        db.session.commit()
        by_pos = {s.position: s.meter_id for s in tour.stops}
        assert by_pos == {1: m1.id, 2: m3.id, 3: m2.id, 4: m4.id}

    def test_rejects_meter_in_open_tour(self, app, user):
        prop = _property()
        m = _meter(prop, "M1")
        db.session.commit()
        svc.create_tour(name="T1", meter_ids=[m.id], created_by_id=user.id)
        db.session.commit()
        with pytest.raises(svc.TourError):
            svc.create_tour(name="T2", meter_ids=[m.id], created_by_id=user.id)

    def test_rejects_inactive_meter(self, app, user):
        prop = _property()
        m = _meter(prop, "M1", active=False)
        db.session.commit()
        with pytest.raises(svc.TourError):
            svc.create_tour(name="T", meter_ids=[m.id], created_by_id=user.id)


class TestCompletionSync:
    def _tour_with_stop(self, user):
        prop = _property()
        m = _meter(prop, "ALT-1")
        db.session.commit()
        tour = svc.create_tour(name="T", meter_ids=[m.id], created_by_id=user.id)
        db.session.commit()
        return tour, tour.stops[0], m, prop

    def _replace(self, prop, old):
        new = _meter(prop, "NEU-" + old.meter_number)
        repl = MeterReplacement(
            property_id=prop.id, old_meter_id=old.id, new_meter_id=new.id,
            billing_period_id=_period().id, replacement_date=date.today())
        db.session.add(repl)
        db.session.flush()
        return repl

    def test_complete_links_replacement(self, app, user):
        tour, stop, m, prop = self._tour_with_stop(user)
        assert svc.complete_stop_from_replacement(stop) is False
        repl = self._replace(prop, m)
        db.session.commit()
        assert svc.complete_stop_from_replacement(stop) is True
        assert stop.replacement_id == repl.id
        assert stop.status == MeterTourStop.STATUS_DONE
        assert stop.completed_at is not None
        # Idempotent.
        assert svc.complete_stop_from_replacement(stop) is True

    def test_sync_heals_out_of_band_replacement(self, app, user):
        tour, stop, m, prop = self._tour_with_stop(user)
        self._replace(prop, m)
        db.session.commit()
        assert svc.sync_tour_completions(tour) == 1
        assert stop.status == MeterTourStop.STATUS_DONE
        assert svc.sync_tour_completions(tour) == 0

    def test_reorder_keeps_done_after_pending(self, app, user):
        p1 = _property("P1", lat=48.001, lng=16.0)
        p2 = _property("P2", lat=48.010, lng=16.0)
        m1 = _meter(p1, "M1")
        m2 = _meter(p2, "M2")
        db.session.commit()
        tour = svc.create_tour(name="T", meter_ids=[m1.id, m2.id],
                               start_lat=48.0, start_lng=16.0,
                               created_by_id=user.id)
        db.session.commit()
        # Ersten Stop erledigen, dann ab einem Punkt NAHE m2 neu sortieren.
        first = next(s for s in tour.stops if s.meter_id == m1.id)
        first.status = MeterTourStop.STATUS_DONE
        db.session.commit()
        svc.reorder_pending_stops(tour, 48.011, 16.0)
        db.session.commit()
        pending = next(s for s in tour.stops if s.meter_id == m2.id)
        assert pending.position == 1
        assert first.position == 2   # erledigte hinten, Relativfolge stabil


class TestCreateFeeInvoice:
    def test_gross_total_with_tax(self, app, user, customer):
        prop = _property()
        db.session.commit()
        from app.invoices.services import create_fee_invoice
        inv = create_fee_invoice(
            customer=customer, property=prop,
            description="Zählertausch-Pauschale", amount=Decimal("60.00"),
            tax_rate=Decimal("10"), created_by_id=user.id)
        db.session.commit()
        assert inv.status == Invoice.STATUS_DRAFT
        assert inv.customer_id == customer.id
        assert inv.property_id == prop.id
        assert len(inv.items) == 1
        assert inv.total_amount == Decimal("66.00")

    def test_no_tax(self, app, user, customer):
        prop = _property()
        db.session.commit()
        from app.invoices.services import create_fee_invoice
        inv = create_fee_invoice(
            customer=customer, property=prop, description="Pauschale",
            amount=Decimal("50"), tax_rate=None, created_by_id=user.id)
        db.session.commit()
        assert inv.total_amount == Decimal("50")
