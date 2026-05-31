"""Integration-Tests fuer den Zaehlertausch-Import (``swap_import_service``).

Fokus: die Warnungen, die einen stillen Datenverlust verhindern --
ueberschriebener Bestandsstand des alten Zaehlers, Tauschdatum ausserhalb der
gewaehlten Periode und Ausbau-Stand kleiner als der letzte Stand.
"""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.meters import swap_import_service as S
from app.meters.services import save_reading
from app.models import BillingPeriod, MeterReading, Property, WaterMeter


def _period(name, start, end, active=False):
    p = BillingPeriod(name=name, start_date=start, end_date=end, active=active)
    db.session.add(p)
    db.session.flush()
    return p


def _meter(num):
    prop = Property(object_number="P-" + num, object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    m = WaterMeter(property_id=prop.id, meter_number=num,
                   initial_value=Decimal("0"), active=True)
    db.session.add(m)
    db.session.flush()
    return m


def _swap_row(old_meter, *, dismount, swap_date, new_num=None):
    r = S.SwapRow(
        idx=0,
        old_meter_number=old_meter.meter_number,
        new_meter_number=new_num or ("N-" + old_meter.meter_number),
        dismount_value=Decimal(str(dismount)),
        new_initial_value=Decimal("0"),
        new_eichjahr=None,
        swap_date=swap_date,
        object_number_raw="",
    )
    r.old_meter = old_meter
    last = old_meter.last_reading()
    if last is not None:
        r.old_last_value = last.value
        r.old_last_date = last.reading_date
    S._classify(r)
    return r


class TestSwapImportOverwriteWarning:
    def test_overwrite_existing_reading_warns(self, app, user):
        p25 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        m = _meter("Z1")
        save_reading(m, p25, Decimal("500"), reading_date=date(2025, 11, 1))
        db.session.commit()

        row = _swap_row(m, dismount=480, swap_date=date(2025, 6, 1))
        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)

        assert stats.swapped == 1
        assert any("überschrieben" in w and "500" in w and "480" in w
                   for w in stats.warnings)
        # Wert wurde ueberschrieben (Tausch laeuft durch), aber mit Warnung.
        r = MeterReading.query.filter_by(meter_id=m.id, billing_period_id=p25.id).one()
        assert r.value == Decimal("480")

    def test_same_value_no_overwrite_warning(self, app, user):
        p25 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        m = _meter("Z1")
        save_reading(m, p25, Decimal("480"), reading_date=date(2025, 11, 1))
        db.session.commit()

        row = _swap_row(m, dismount=480, swap_date=date(2025, 6, 1))
        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        assert not any("überschrieben" in w for w in stats.warnings)

    def test_no_existing_reading_no_overwrite_warning(self, app, user):
        p25 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        m = _meter("Z1")
        db.session.commit()

        row = _swap_row(m, dismount=120, swap_date=date(2025, 6, 1))
        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        assert stats.warnings == []
        r = MeterReading.query.filter_by(meter_id=m.id, billing_period_id=p25.id).one()
        assert r.value == Decimal("120")


class TestSwapImportDateOutsidePeriod:
    def test_swap_date_outside_period_warns(self, app, user):
        p25 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        m = _meter("Z1")
        db.session.commit()

        row = _swap_row(m, dismount=100, swap_date=date(2024, 6, 1))
        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        assert any("außerhalb der Periode" in w for w in stats.warnings)

    def test_swap_date_inside_period_no_warning(self, app, user):
        p25 = _period("2025", date(2025, 1, 1), date(2025, 12, 31), active=True)
        m = _meter("Z1")
        db.session.commit()

        row = _swap_row(m, dismount=100, swap_date=date(2025, 6, 1))
        stats = S.commit_swap_import([row], user_id=user.id, billing_period=p25)
        assert not any("außerhalb der Periode" in w for w in stats.warnings)


class TestSwapImportDismountBelowLast:
    def test_preview_warns_when_dismount_below_last(self, app, user):
        # Letzter Stand 500 (2024), Ausbau 480 -> Vorschau-Hinweis.
        _period("2024", date(2024, 1, 1), date(2024, 12, 31))
        p24 = BillingPeriod.query.filter_by(name="2024").one()
        m = _meter("Z1")
        save_reading(m, p24, Decimal("500"), reading_date=date(2024, 6, 1))
        db.session.commit()

        row = _swap_row(m, dismount=480, swap_date=date(2025, 6, 1))
        assert row.status == S.STATUS_TAUSCH
        assert any("kleiner als der letzte Stand" in msg for msg in row.messages)

    def test_preview_no_warning_when_dismount_above_last(self, app, user):
        _period("2024", date(2024, 1, 1), date(2024, 12, 31))
        p24 = BillingPeriod.query.filter_by(name="2024").one()
        m = _meter("Z1")
        save_reading(m, p24, Decimal("500"), reading_date=date(2024, 6, 1))
        db.session.commit()

        row = _swap_row(m, dismount=560, swap_date=date(2025, 6, 1))
        assert not any("kleiner als der letzte Stand" in msg for msg in row.messages)
