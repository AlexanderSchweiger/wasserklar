"""Integration-Tests fuer die Jahres-Verbrauchssummen (``app/consumption.py``).

Deckt beide Rollen der Tabelle ab: Cache der gemessenen Summen (inkl.
Invalidierung durch die Schreibpfade) und manuelle Erfassung historischer
Jahre — samt der Regel „Messung schlaegt Eingabe".
"""
from datetime import date
from decimal import Decimal

import pytest

from app import consumption
from app.extensions import db
from app.meters.services import save_reading
from app.models import (
    BillingPeriod, ConsumptionYear, MeterReading, Property, WaterMeter,
)


def _period(name="2024", year=2024, active=False):
    p = BillingPeriod(name=name, start_date=date(year, 1, 1),
                      end_date=date(year, 12, 31), active=active)
    db.session.add(p)
    db.session.commit()
    return p


def _meter(number="M1", initial=Decimal("0"), object_number="P1"):
    prop = Property(object_number=object_number, object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    m = WaterMeter(meter_number=number, property_id=prop.id,
                   initial_value=initial)
    db.session.add(m)
    db.session.commit()
    return m


class TestRefresh:

    def test_sums_readings_of_a_period(self, app):
        period = _period()
        m1 = _meter("M1", Decimal("0"), "P1")
        m2 = _meter("M2", Decimal("100"), "P2")
        save_reading(m1, period, Decimal("120"))
        save_reading(m2, period, Decimal("250"))
        db.session.commit()

        consumption.refresh_all(force=True)
        db.session.commit()

        row = consumption.for_year(2024)
        assert row is not None
        assert row.total_m3 == Decimal("270.000")   # 120 + 150
        assert row.source == ConsumptionYear.SOURCE_MEASURED
        assert row.reading_count == 2
        assert row.unit_count == 2
        assert row.billing_period_id == period.id
        assert row.stale is False
        assert row.computed_at is not None

    def test_year_comes_from_period_start_date(self, app):
        """Eine Juni–Juni-Periode "2024/25" zaehlt zum Startjahr — das ist die
        Lesart des Periodennamens."""
        p = BillingPeriod(name="2024/25", start_date=date(2024, 6, 1),
                          end_date=date(2025, 5, 31))
        db.session.add(p)
        db.session.commit()
        m = _meter()
        save_reading(m, p, Decimal("80"))
        db.session.commit()

        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(2024).total_m3 == Decimal("80.000")
        assert consumption.for_year(2025) is None

    def test_two_periods_in_one_year_are_summed(self, app):
        a = BillingPeriod(name="2024-H1", start_date=date(2024, 1, 1),
                          end_date=date(2024, 6, 30))
        b = BillingPeriod(name="2024-H2", start_date=date(2024, 7, 1),
                          end_date=date(2024, 12, 31))
        db.session.add_all([a, b])
        db.session.commit()
        m = _meter()
        save_reading(m, a, Decimal("50"))
        save_reading(m, b, Decimal("130"))
        db.session.commit()

        consumption.refresh_all(force=True)
        db.session.commit()
        row = consumption.for_year(2024)
        assert row.total_m3 == Decimal("130.000")   # 50 + 80
        # Mehrdeutig -> kein einzelner Perioden-Link.
        assert row.billing_period_id is None

    def test_period_without_readings_creates_no_row(self, app):
        _period()
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(2024) is None

    def test_orphaned_measured_row_is_removed(self, app):
        """Verschwinden alle Ablesungen eines Jahres, verschwindet auch die
        berechnete Zeile — sonst zeigte die Historie eine Geistersumme."""
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("90"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(2024) is not None

        MeterReading.query.delete()
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(2024) is None


class TestStaleness:

    def test_saving_a_reading_marks_stale(self, app):
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(2024).stale is False

        save_reading(m, period, Decimal("150"))
        db.session.commit()
        assert consumption.for_year(2024).stale is True
        assert consumption.has_stale() is True

    def test_refresh_clears_stale_and_updates_total(self, app):
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        save_reading(m, period, Decimal("150"))
        db.session.commit()
        consumption.ensure_fresh()
        db.session.commit()

        row = consumption.for_year(2024)
        assert row.stale is False
        assert row.total_m3 == Decimal("150.000")

    def test_deleting_a_reading_invalidates(self, app):
        """``reading_delete`` laeuft ueber ``recompute_meter_chain`` — dort
        haengt die Invalidierung."""
        from app.meters.services import recompute_meter_chain
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        MeterReading.query.filter_by(meter_id=m.id).delete()
        db.session.flush()
        recompute_meter_chain(m)
        db.session.commit()
        assert consumption.for_year(2024).stale is True

    def test_manual_rows_are_marked_stale_too(self, app):
        """Auch uebersteuerte Zeilen werden markiert — ihr Vergleichswert
        (``measured_m3``) muss mitlaufen, nur der wirksame Wert bleibt fix."""
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()
        assert consumption.for_year(2024).stale is False

        save_reading(m, period, Decimal("250"))
        db.session.commit()
        assert consumption.for_year(2024).stale is True

    def test_ensure_fresh_leaves_untouched_rows_alone(self, app):
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()
        before = consumption.for_year(2024).computed_at

        consumption.ensure_fresh()
        db.session.commit()
        assert consumption.for_year(2024).computed_at == before


class TestManualEntry:

    def test_manual_year_without_any_period(self, app):
        """Der Hauptfall: Jahre vor der App-Einfuehrung, fuer die es weder
        Periode noch Ablesungen gibt."""
        consumption.set_manual(2019, Decimal("42500"),
                               notes="aus der Excel-Liste", unit_count=180)
        db.session.commit()

        row = consumption.for_year(2019)
        assert row.total_m3 == Decimal("42500")
        assert row.source == ConsumptionYear.SOURCE_MANUAL
        assert row.is_manual is True
        assert row.billing_period_id is None
        assert row.reading_count is None
        assert row.unit_count == 180
        assert row.notes == "aus der Excel-Liste"
        assert row.label == "2019"

    def test_manual_entry_is_updatable(self, app):
        consumption.set_manual(2019, Decimal("42500"))
        db.session.commit()
        consumption.set_manual(2019, Decimal("43000"))
        db.session.commit()
        assert consumption.for_year(2019).total_m3 == Decimal("43000")
        assert ConsumptionYear.query.count() == 1

    def test_manual_overrides_an_existing_measurement(self, app):
        """Die Eingabe gewinnt — z.B. bei unvollstaendig erfasstem Jahr oder
        bekanntem Zaehlerdefekt."""
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        consumption.set_manual(2024, Decimal("99999"), notes="Zähler defekt")
        db.session.commit()

        row = consumption.for_year(2024)
        assert row.total_m3 == Decimal("99999")
        assert row.source == ConsumptionYear.SOURCE_MANUAL
        # Die Messung bleibt zum Vergleich erhalten.
        assert row.measured_m3 == Decimal("100.000")
        assert row.is_override is True
        assert row.override_delta == Decimal("99899.000")
        assert row.reading_count == 1

    def test_override_survives_a_forced_refresh(self, app):
        """Kernzusage: keine Neuberechnung ueberschreibt die Eingabe."""
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()

        consumption.refresh_all(force=True)
        db.session.commit()
        row = consumption.for_year(2024)
        assert row.total_m3 == Decimal("99999")
        assert row.source == ConsumptionYear.SOURCE_MANUAL

    def test_new_readings_update_the_comparison_value_only(self, app):
        """Kommen nach der Uebersteuerung Ablesungen dazu, waechst
        ``measured_m3`` mit — der wirksame Wert bleibt die Eingabe."""
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()

        save_reading(m, period, Decimal("400"))
        db.session.commit()
        consumption.ensure_fresh()
        db.session.commit()

        row = consumption.for_year(2024)
        assert row.total_m3 == Decimal("99999")
        assert row.measured_m3 == Decimal("400.000")

    def test_measured_data_does_not_overwrite_a_manual_year(self, app):
        """Ein manuell erfasstes Jahr, das nachtraeglich Ablesungen bekommt,
        bleibt uebersteuert — der Nutzer hat den Wert bewusst gesetzt."""
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()

        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        row = consumption.for_year(2024)
        assert row.source == ConsumptionYear.SOURCE_MANUAL
        assert row.total_m3 == Decimal("99999")
        assert row.measured_m3 == Decimal("100.000")

    def test_manual_year_survives_a_refresh(self, app):
        consumption.set_manual(2019, Decimal("42500"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(2019).total_m3 == Decimal("42500")

    def test_revert_removes_a_row_without_measurement(self, app):
        consumption.set_manual(2019, Decimal("42500"))
        db.session.commit()
        assert consumption.revert_to_measured(2019) is True
        db.session.commit()
        assert consumption.for_year(2019) is None

    def test_revert_restores_the_measured_value(self, app):
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()

        assert consumption.revert_to_measured(2024) is True
        db.session.commit()
        row = consumption.for_year(2024)
        assert row.source == ConsumptionYear.SOURCE_MEASURED
        assert row.total_m3 == Decimal("100.000")
        assert row.is_override is False

    def test_revert_on_a_measured_row_is_refused(self, app):
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        with pytest.raises(consumption.ConsumptionError):
            consumption.revert_to_measured(2024)

    def test_override_loses_its_comparison_when_readings_vanish(self, app):
        """Verschwinden die Ablesungen, ist die Eingabe wieder eine reine
        manuelle Angabe ohne Vergleichswert — und nicht mehr loeschgeschuetzt."""
        period = _period()
        m = _meter()
        save_reading(m, period, Decimal("100"))
        db.session.commit()
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()

        MeterReading.query.delete()
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        row = consumption.for_year(2024)
        assert row.total_m3 == Decimal("99999")
        assert row.measured_m3 is None
        assert row.is_override is False


class TestAverage:

    def test_average_mixes_measured_and_manual(self, app):
        consumption.set_manual(2021, Decimal("30000"))
        consumption.set_manual(2022, Decimal("40000"))
        consumption.set_manual(2023, Decimal("50000"))
        db.session.commit()
        assert consumption.average(3) == Decimal("40000.000")

    def test_average_uses_only_the_last_n_years(self, app):
        for year, value in [(2019, "10000"), (2020, "10000"),
                            (2021, "30000"), (2022, "40000"),
                            (2023, "50000")]:
            consumption.set_manual(year, Decimal(value))
        db.session.commit()
        assert consumption.average(3) == Decimal("40000.000")

    def test_average_excludes_the_running_period_year(self, app):
        """Eine erst halb abgelesene laufende Periode wuerde den Schnitt nach
        unten ziehen — genau die Zahl, auf der die Tarifplanung fusst."""
        consumption.set_manual(2022, Decimal("40000"))
        consumption.set_manual(2023, Decimal("40000"))
        db.session.commit()

        period = _period(name="2024", year=2024, active=True)
        m = _meter()
        save_reading(m, period, Decimal("500"))     # erst ein Zaehler abgelesen
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()

        assert consumption.average(3) == Decimal("40000.000")

    def test_average_falls_back_to_the_running_year_if_alone(self, app):
        """Gibt es nur das laufende Jahr, ist eine grobe Basis besser als
        keine."""
        period = _period(name="2024", year=2024, active=True)
        m = _meter()
        save_reading(m, period, Decimal("500"))
        db.session.commit()
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.average(3) == Decimal("500.000")

    def test_average_without_data_is_zero(self, app):
        assert consumption.average(3) == Decimal("0")

    def test_average_basis_reports_the_years_used(self, app):
        consumption.set_manual(2021, Decimal("30000"))
        consumption.set_manual(2022, Decimal("40000"))
        db.session.commit()
        avg, basis = consumption.average_basis(3)
        assert avg == Decimal("35000.000")
        assert [r.year for r in basis] == [2021, 2022]   # chronologisch


class TestHistory:

    def test_history_is_chronological_and_tagged(self, app):
        consumption.set_manual(2021, Decimal("30000"))
        consumption.set_manual(2022, Decimal("40000"))
        db.session.commit()
        rows = consumption.history()
        assert [r["year"] for r in rows] == [2021, 2022]
        assert all(r["is_manual"] for r in rows)
        assert rows[0]["value"] == 30000.0

    def test_history_limit_keeps_the_latest(self, app):
        for year in range(2018, 2024):
            consumption.set_manual(year, Decimal("1000"))
        db.session.commit()
        rows = consumption.history(limit=3)
        assert [r["year"] for r in rows] == [2021, 2022, 2023]
