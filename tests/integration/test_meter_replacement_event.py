"""Integration-Tests fuer das explizite Zaehlertausch-Event
(``MeterReplacement``) — Quelle der Tausch-Erkennung statt der frueheren
Datums-Heuristik.

Fokus:
- ``commit_swap_import`` legt bei einem Tausch genau ein Event an (mit korrekter
  alt->neu-Paarung + Snapshot), bei einer reinen Neuanlage KEINS.
- ``_build_replacement_map`` liest aus der Event-Tabelle und ordnet auch zwei am
  selben Tag am selben Objekt getauschte Zaehler eindeutig zu (der Fall, der mit
  der alten Heuristik nicht aufloesbar war).
"""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.meters import swap_import_service as S
from app.meters.routes import _build_replacement_map
from app.models import (
    BillingPeriod, MeterReplacement, Property, WaterMeter,
)


def _period(name="2025", active=True):
    p = BillingPeriod(name=name, start_date=date(2025, 1, 1),
                      end_date=date(2025, 12, 31), active=active)
    db.session.add(p)
    db.session.flush()
    return p


def _property(num):
    prop = Property(object_number=num, object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    return prop


def _meter(prop, num):
    m = WaterMeter(property_id=prop.id, meter_number=num,
                   initial_value=Decimal("0"), active=True)
    db.session.add(m)
    db.session.flush()
    return m


def _swap_row(old_meter, *, new_num, dismount, swap_date):
    r = S.SwapRow(
        idx=0,
        old_meter_number=old_meter.meter_number,
        new_meter_number=new_num,
        dismount_value=Decimal(str(dismount)),
        new_initial_value=Decimal("0"),
        new_eichjahr=None,
        swap_date=swap_date,
        object_number_raw="",
    )
    r.old_meter = old_meter
    S._classify(r)
    return r


class TestSwapImportCreatesEvent:
    def test_tausch_creates_replacement_event(self, app, user):
        p25 = _period()
        prop = _property("P-1")
        old = _meter(prop, "Z1")
        db.session.commit()

        row = _swap_row(old, new_num="Z2", dismount=480, swap_date=date(2025, 6, 1))
        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        assert stats.swapped == 1

        new = WaterMeter.query.filter_by(meter_number="Z2").one()
        ev = MeterReplacement.query.one()
        assert ev.old_meter_id == old.id
        assert ev.new_meter_id == new.id
        assert ev.property_id == prop.id
        assert ev.billing_period_id == p25.id
        assert ev.replacement_date == date(2025, 6, 1)
        assert ev.final_value == Decimal("480")
        assert ev.created_by_id == user.id

    def test_neuanlage_creates_no_event(self, app, user):
        p25 = _period()
        prop = _property("P-1")
        db.session.commit()

        # Reine Neuanlage (kein Vorgaenger) -> Zaehler entsteht, aber KEIN Event.
        row = S.SwapRow(
            idx=0, old_meter_number="", new_meter_number="NEW-1",
            dismount_value=None, new_initial_value=Decimal("0"),
            new_eichjahr=None, swap_date=date(2025, 6, 1), object_number_raw="P-1",
        )
        row.status = S.STATUS_NEUANLAGE
        row.property_id = prop.id

        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        assert stats.created == 1
        assert WaterMeter.query.filter_by(meter_number="NEW-1").count() == 1
        assert MeterReplacement.query.count() == 0


class TestBuildReplacementMapSameDay:
    """Zwei Zaehler am selben Objekt, beide am selben Tag getauscht — mit der
    alten Datums-Heuristik nicht eindeutig zuordenbar, mit dem Event schon."""

    def test_same_day_same_property_resolves_uniquely(self, app, user):
        p25 = _period()
        prop = _property("P-1")
        old_a = _meter(prop, "A-OLD")
        old_b = _meter(prop, "B-OLD")
        db.session.commit()

        swap_date = date(2025, 6, 1)
        rows = [
            _swap_row(old_a, new_num="A-NEW", dismount=100, swap_date=swap_date),
            _swap_row(old_b, new_num="B-NEW", dismount=200, swap_date=swap_date),
        ]
        stats = S.commit_swap_import(rows, user_id=user.id, billing_period=p25)
        assert stats.swapped == 2
        assert MeterReplacement.query.count() == 2

        new_a = WaterMeter.query.filter_by(meter_number="A-NEW").one()
        new_b = WaterMeter.query.filter_by(meter_number="B-NEW").one()

        repl_map = _build_replacement_map([new_a, new_b], p25)
        # Jeder neue Zaehler zeigt auf SEINEN korrekten Vorgaenger.
        assert repl_map[new_a.id]["old_meter"].id == old_a.id
        assert repl_map[new_b.id]["old_meter"].id == old_b.id

    def test_map_empty_for_unrelated_period(self, app, user):
        p25 = _period("2025", active=True)
        p24 = _period("2024", active=False)
        prop = _property("P-1")
        old = _meter(prop, "Z1")
        db.session.commit()

        row = _swap_row(old, new_num="Z2", dismount=50, swap_date=date(2025, 6, 1))
        S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        new = WaterMeter.query.filter_by(meter_number="Z2").one()

        # Event haengt an p25 -> in p24 keine Treffer.
        assert _build_replacement_map([new], p24) == {}
        assert new.id in _build_replacement_map([new], p25)
