"""Integration-Tests fuer den Ablesungs-Import-Service mit Datenbank.

Deckt die DB-beruehrenden Funktionen ab: resolve_meter (alle 3 Modi),
build_resolved_rows (End-to-End-Flow), parse_form_edits (User-Edits-Merging),
commit_import (Insert/Update/Skip-Pfade inkl. consumption-Berechnung).
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.extensions import db
from app.meters import import_service as svc
from app.models import (
    Customer, MeterReading, Property, PropertyOwnership, User, WaterMeter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(app):
    u = User(username="testadmin", email="t@t.test", role="admin")
    u.set_password("x")
    db.session.add(u)
    db.session.commit()
    return u


def _make_customer(number=None, name="Kunde X", active=True):
    c = Customer(name=name, customer_number=number, active=active)
    db.session.add(c)
    db.session.flush()
    return c


def _make_property(object_number, customer=None):
    p = Property(object_number=object_number, object_type="Haus", ort="Testort")
    db.session.add(p)
    db.session.flush()
    if customer:
        po = PropertyOwnership(
            property_id=p.id, customer_id=customer.id,
            valid_from=date(2020, 1, 1), valid_to=None,
        )
        db.session.add(po)
        db.session.flush()
    return p


def _make_meter(prop, number, meter_type="main", parent_id=None, active=True,
                installed_from=None, installed_to=None, initial_value=None):
    m = WaterMeter(
        property_id=prop.id, meter_number=number,
        meter_type=meter_type, parent_meter_id=parent_id, active=active,
        installed_from=installed_from, installed_to=installed_to,
        initial_value=initial_value,
    )
    db.session.add(m)
    db.session.flush()
    return m


def _make_reading(meter, year, value, consumption=None, reading_date=None):
    r = MeterReading(
        meter_id=meter.id, year=year,
        value=Decimal(str(value)),
        consumption=Decimal(str(consumption)) if consumption is not None else None,
        reading_date=reading_date or date(year, 12, 31),
    )
    db.session.add(r)
    db.session.flush()
    return r


# ---------------------------------------------------------------------------
# resolve_meter -- Modus 'meter_number'
# ---------------------------------------------------------------------------

class TestResolveByMeterNumber:
    def test_ok(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        r = svc.resolve_meter("Z-001", "meter_number")
        assert r.status == svc.STATUS_OK
        assert r.chosen.id == m.id
        assert len(r.candidates) == 1

    def test_not_found(self, app):
        r = svc.resolve_meter("Z-999", "meter_number")
        assert r.status == svc.STATUS_NOT_FOUND
        assert r.chosen is None
        assert "Z-999" in r.message

    def test_empty_lookup(self, app):
        r = svc.resolve_meter("", "meter_number")
        assert r.status == svc.STATUS_NOT_FOUND
        assert "leer" in r.message.lower()

    def test_inactive_meter_not_found(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        _make_meter(p, "Z-001", active=False)
        r = svc.resolve_meter("Z-001", "meter_number")
        assert r.status == svc.STATUS_NOT_FOUND


# ---------------------------------------------------------------------------
# resolve_meter -- Modus 'customer_number'
# ---------------------------------------------------------------------------

class TestResolveByCustomerNumber:
    def test_ok_single_meter(self, app):
        c = _make_customer(number=42, name="Mueller")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        r = svc.resolve_meter("42", "customer_number")
        assert r.status == svc.STATUS_OK
        assert r.chosen.id == m.id

    def test_invalid_number_format(self, app):
        r = svc.resolve_meter("abc", "customer_number")
        assert r.status == svc.STATUS_NOT_FOUND
        assert "ungültig" in r.message.lower() or "ungueltig" in r.message.lower()

    def test_customer_not_found(self, app):
        r = svc.resolve_meter("999", "customer_number")
        assert r.status == svc.STATUS_NOT_FOUND
        assert "999" in r.message

    def test_customer_no_meters(self, app):
        c = _make_customer(number=42, name="X")
        # kein Property/Meter
        r = svc.resolve_meter("42", "customer_number")
        assert r.status == svc.STATUS_NOT_FOUND
        assert "keine aktiven Zähler" in r.message

    def test_ok_preferred_main_with_one_main_one_sub(self, app):
        c = _make_customer(number=42, name="X")
        p = _make_property("P-1", c)
        main = _make_meter(p, "Z-MAIN", meter_type="main")
        _make_meter(p, "Z-SUB", meter_type="sub", parent_id=main.id)
        r = svc.resolve_meter("42", "customer_number")
        assert r.status == svc.STATUS_OK_PREFERRED_MAIN
        assert r.chosen.id == main.id
        assert len(r.candidates) == 2

    def test_ambiguous_two_mains(self, app):
        c = _make_customer(number=42, name="X")
        p1 = _make_property("P-1", c)
        p2 = _make_property("P-2", c)
        _make_meter(p1, "Z-1", meter_type="main")
        _make_meter(p2, "Z-2", meter_type="main")
        r = svc.resolve_meter("42", "customer_number")
        assert r.status == svc.STATUS_AMBIGUOUS
        assert r.chosen is None
        assert len(r.candidates) == 2

    def test_ambiguous_only_subs(self, app):
        c = _make_customer(number=42, name="X")
        p = _make_property("P-1", c)
        _make_meter(p, "Z-S1", meter_type="sub")
        _make_meter(p, "Z-S2", meter_type="sub")
        r = svc.resolve_meter("42", "customer_number")
        # 0 mains, 2 meters -> ambiguous
        assert r.status == svc.STATUS_AMBIGUOUS
        assert r.chosen is None

    def test_inactive_customer_not_found(self, app):
        _make_customer(number=42, name="X", active=False)
        r = svc.resolve_meter("42", "customer_number")
        assert r.status == svc.STATUS_NOT_FOUND


# ---------------------------------------------------------------------------
# resolve_meter -- Modus 'customer_name'
# ---------------------------------------------------------------------------

class TestResolveByCustomerName:
    def test_ok_unique(self, app):
        c = _make_customer(name="Mueller Hans")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        r = svc.resolve_meter("Mueller Hans", "customer_name")
        assert r.status == svc.STATUS_OK
        assert r.chosen.id == m.id

    def test_case_insensitive(self, app):
        c = _make_customer(name="Mueller Hans")
        p = _make_property("P-1", c)
        _make_meter(p, "Z-001")
        r = svc.resolve_meter("mueller hans", "customer_name")
        assert r.status == svc.STATUS_OK

    def test_not_found(self, app):
        r = svc.resolve_meter("Niemand", "customer_name")
        assert r.status == svc.STATUS_NOT_FOUND

    def test_two_customers_same_name(self, app):
        c1 = _make_customer(name="Maier")
        c2 = _make_customer(name="Maier")
        p1 = _make_property("P-1", c1)
        p2 = _make_property("P-2", c2)
        _make_meter(p1, "Z-1")
        _make_meter(p2, "Z-2")
        r = svc.resolve_meter("Maier", "customer_name")
        assert r.status == svc.STATUS_AMBIGUOUS
        assert r.chosen is None
        # beide Meter sind in candidates
        assert len(r.candidates) == 2
        assert "2 Kunden" in r.message


# ---------------------------------------------------------------------------
# build_resolved_rows
# ---------------------------------------------------------------------------

class TestBuildResolvedRows:
    def test_empty_df(self, app):
        rows = svc.build_resolved_rows(pd.DataFrame(), svc.MappingConfig())
        assert rows == []

    def test_missing_required_columns(self, app):
        df = pd.DataFrame([{"X": "1"}])
        cfg = svc.MappingConfig(mode="meter_number")  # col_lookup leer
        assert svc.build_resolved_rows(df, cfg) == []

    def test_three_rows_mixed_status(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        _make_meter(p, "Z-001")

        df = pd.DataFrame([
            {"Nr": "Z-001", "Stand": "100,5", "Jahr": "2024"},
            {"Nr": "Z-XXX", "Stand": "200,0", "Jahr": "2024"},  # not_found
            {"Nr": "Z-001", "Stand": "garbage", "Jahr": "2024"},  # parse_error
        ])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert len(rows) == 3
        assert rows[0].status == svc.STATUS_OK
        assert rows[0].value == Decimal("100.5")
        assert rows[1].status == svc.STATUS_NOT_FOUND
        assert rows[2].status == svc.STATUS_PARSE_ERROR

    def test_default_date_31_12_year(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        _make_meter(p, "Z-001")
        df = pd.DataFrame([{"Nr": "Z-001", "Stand": "10", "Jahr": "2024"}])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].reading_date == date(2024, 12, 31)

    def test_explicit_date_column(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        _make_meter(p, "Z-001")
        df = pd.DataFrame([
            {"Nr": "Z-001", "Stand": "10", "Datum": "15.06.2024", "Jahr": "2024"}
        ])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_date="Datum", col_year="Jahr", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].reading_date == date(2024, 6, 15)

    def test_candidate_meter_ids_populated(self, app):
        c = _make_customer(number=42, name="X")
        p = _make_property("P-1", c)
        main = _make_meter(p, "Z-M", meter_type="main")
        _make_meter(p, "Z-S", meter_type="sub", parent_id=main.id)
        df = pd.DataFrame([{"Nr": "42", "Stand": "100"}])
        cfg = svc.MappingConfig(
            mode="customer_number", col_lookup="Nr", col_value="Stand",
            default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].status == svc.STATUS_OK_PREFERRED_MAIN
        assert len(rows[0].candidate_meter_ids) == 2
        assert rows[0].chosen_meter_id == main.id


# ---------------------------------------------------------------------------
# parse_form_edits
# ---------------------------------------------------------------------------

class TestParseFormEdits:
    def _baseline(self, app, value=Decimal("100"), status=svc.STATUS_OK):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        row = svc.ResolvedRow(
            idx=0, raw_data={"Nr": "Z-001", "Stand": "100"},
            lookup_value="Z-001",
            value=value, reading_date=date(2024, 12, 31), year=2024,
            status=status, candidate_meter_ids=[m.id], chosen_meter_id=m.id,
            skip=False, message="",
        )
        return [row], m

    def test_skip_checked(self, app):
        rows, _ = self._baseline(app)
        form = ImmutableMultiDict([("rows[0][skip]", "on")])
        out = svc.parse_form_edits(form, rows)
        assert out[0].skip is True

    def test_skip_unchecked_resets_to_false(self, app):
        # baseline mit skip=True -> Form ohne skip-Key -> skip muss False werden
        rows, _ = self._baseline(app)
        rows[0].skip = True
        form = ImmutableMultiDict([])
        out = svc.parse_form_edits(form, rows)
        assert out[0].skip is False

    def test_value_edit(self, app):
        rows, _ = self._baseline(app)
        form = ImmutableMultiDict([("rows[0][value]", "1234,56")])
        out = svc.parse_form_edits(form, rows)
        assert out[0].value == Decimal("1234.56")

    def test_value_edit_garbage_sets_parse_error(self, app):
        rows, _ = self._baseline(app)
        form = ImmutableMultiDict([("rows[0][value]", "xxx")])
        out = svc.parse_form_edits(form, rows)
        assert out[0].value is None
        assert out[0].status == svc.STATUS_PARSE_ERROR

    def test_value_repair_clears_parse_error(self, app):
        rows, _ = self._baseline(app, value=None, status=svc.STATUS_PARSE_ERROR)
        form = ImmutableMultiDict([("rows[0][value]", "500")])
        out = svc.parse_form_edits(form, rows)
        assert out[0].value == Decimal("500")
        assert out[0].status == svc.STATUS_OK

    def test_date_edit_iso(self, app):
        rows, _ = self._baseline(app)
        form = ImmutableMultiDict([("rows[0][date]", "2024-06-15")])
        out = svc.parse_form_edits(form, rows)
        assert out[0].reading_date == date(2024, 6, 15)

    def test_year_edit(self, app):
        rows, _ = self._baseline(app)
        form = ImmutableMultiDict([("rows[0][year]", "2025")])
        out = svc.parse_form_edits(form, rows)
        assert out[0].year == 2025

    def test_meter_id_edit_resolves_ambiguous(self, app):
        # baseline status=ambiguous, dann User waehlt -> status=ok
        rows, m = self._baseline(app, status=svc.STATUS_AMBIGUOUS)
        rows[0].chosen_meter_id = None
        form = ImmutableMultiDict([("rows[0][meter_id]", str(m.id))])
        out = svc.parse_form_edits(form, rows)
        assert out[0].chosen_meter_id == m.id
        assert out[0].status == svc.STATUS_OK


# ---------------------------------------------------------------------------
# commit_import
# ---------------------------------------------------------------------------

class TestCommitImport:
    def _row(self, m_id, value=Decimal("100"), year=2024, skip=False,
             status=svc.STATUS_OK):
        return svc.ResolvedRow(
            idx=0, raw_data={}, lookup_value="x",
            value=value, reading_date=date(year, 12, 31), year=year,
            status=status, candidate_meter_ids=[m_id], chosen_meter_id=m_id,
            skip=skip, message="",
        )

    def test_create_new_reading(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.commit()
        stats = svc.commit_import([self._row(m.id)], admin.id, "update")
        assert stats.created == 1
        assert stats.updated == 0
        r = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert r.value == Decimal("100")
        assert r.created_by_id == admin.id

    def test_update_existing_reading(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.add(MeterReading(meter_id=m.id, year=2024,
                                    value=Decimal("50"),
                                    reading_date=date(2024, 12, 31)))
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=Decimal("200"))], admin.id, "update",
        )
        assert stats.created == 0
        assert stats.updated == 1
        r = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert r.value == Decimal("200")
        assert r.created_by_id == admin.id

    def test_skip_duplicate_when_mode_skip(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.add(MeterReading(meter_id=m.id, year=2024,
                                    value=Decimal("50"),
                                    reading_date=date(2024, 12, 31)))
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=Decimal("999"))], admin.id, "skip",
        )
        assert stats.skipped_dup == 1
        assert stats.updated == 0
        r = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert r.value == Decimal("50")  # unveraendert

    def test_skip_flag(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.commit()
        stats = svc.commit_import([self._row(m.id, skip=True)], admin.id, "update")
        assert stats.skipped == 1
        assert stats.created == 0
        assert MeterReading.query.count() == 0

    def test_unmapped_skipped(self, app, admin):
        row = svc.ResolvedRow(
            idx=0, raw_data={}, lookup_value="x",
            value=Decimal("100"), reading_date=date(2024, 12, 31), year=2024,
            status=svc.STATUS_NOT_FOUND, candidate_meter_ids=[], chosen_meter_id=None,
            skip=False, message="",
        )
        stats = svc.commit_import([row], admin.id, "update")
        assert stats.skipped_unmapped == 1
        assert stats.created == 0

    def test_value_none_skipped(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=None)], admin.id, "update",
        )
        assert stats.skipped_unmapped == 1

    def test_consumption_with_prev_year(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.add(MeterReading(meter_id=m.id, year=2023,
                                    value=Decimal("50"),
                                    reading_date=date(2023, 12, 31)))
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=Decimal("130"), year=2024)], admin.id, "update",
        )
        assert stats.created == 1
        new = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert new.consumption == Decimal("80")  # 130 - 50

    def test_consumption_none_without_prev_year(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=Decimal("130"), year=2024)], admin.id, "update",
        )
        new = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert new.consumption is None

    def test_consumption_recomputed_on_update(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-001")
        db.session.add(MeterReading(meter_id=m.id, year=2023,
                                    value=Decimal("50"),
                                    reading_date=date(2023, 12, 31)))
        db.session.add(MeterReading(meter_id=m.id, year=2024,
                                    value=Decimal("100"),
                                    consumption=Decimal("50"),
                                    reading_date=date(2024, 12, 31)))
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=Decimal("200"), year=2024)], admin.id, "update",
        )
        assert stats.updated == 1
        r = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert r.value == Decimal("200")
        assert r.consumption == Decimal("150")  # 200 - 50

    def test_multiple_rows_mixed_outcomes(self, app, admin):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m1 = _make_meter(p, "Z-1")
        m2 = _make_meter(p, "Z-2")
        db.session.add(MeterReading(meter_id=m2.id, year=2024,
                                    value=Decimal("50"),
                                    reading_date=date(2024, 12, 31)))
        db.session.commit()

        rows = [
            self._row(m1.id, value=Decimal("100")),  # create
            self._row(m2.id, value=Decimal("200")),  # update
            self._row(m1.id, skip=True),             # skip
        ]
        stats = svc.commit_import(rows, admin.id, "update")
        assert stats.created == 1
        assert stats.updated == 1
        assert stats.skipped == 1

    def test_consumption_via_initial_value_when_no_prev_reading(self, app, admin):
        # Bug-Fix-Test: Ein neuer Meter mit initial_value aber ohne
        # Vorjahres-Reading bekommt jetzt consumption=value-initial (vorher None).
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-NEU", initial_value=Decimal("10"))
        db.session.commit()
        stats = svc.commit_import(
            [self._row(m.id, value=Decimal("100"), year=2024)], admin.id, "update",
        )
        assert stats.created == 1
        r = MeterReading.query.filter_by(meter_id=m.id, year=2024).one()
        assert r.consumption == Decimal("90")


# ---------------------------------------------------------------------------
# compute_prior_and_consumption -- Vorjahresstand + Verbrauch inkl. Wechsel
# ---------------------------------------------------------------------------

class TestComputePriorAndConsumption:
    def test_prev_year_reading_present(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")
        _make_reading(m, 2023, Decimal("50"))
        db.session.commit()
        prior, label, cons, info = svc.compute_prior_and_consumption(
            m, 2024, Decimal("130"),
        )
        assert prior == Decimal("50")
        assert label == "2023"
        assert cons == Decimal("80")
        assert info == ""

    def test_no_prev_reading_falls_back_to_initial(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1", initial_value=Decimal("10"),
                        installed_from=date(2024, 3, 1))
        db.session.commit()
        prior, label, cons, info = svc.compute_prior_and_consumption(
            m, 2024, Decimal("100"),
        )
        assert prior == Decimal("10")
        assert "01.03.2024" in label
        assert cons == Decimal("90")
        assert info == ""

    def test_no_prev_no_initial_returns_none(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")  # kein initial_value
        db.session.commit()
        prior, label, cons, info = svc.compute_prior_and_consumption(
            m, 2024, Decimal("100"),
        )
        assert prior is None
        assert cons is None
        assert label == "—"

    def test_value_none_returns_none(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")
        _make_reading(m, 2023, Decimal("50"))
        db.session.commit()
        prior, label, cons, info = svc.compute_prior_and_consumption(
            m, 2024, None,
        )
        assert prior is None
        assert cons is None

    def test_meter_replacement_in_year_aggregates_predecessor(self, app):
        # Szenario: Alter Meter Z-OLD bis Juni 2024 (Vorjahresende=1200,
        # Abschluss-Ablesung 2024 mit value=1500 consumption=300).
        # Neuer Meter Z-NEW ab Juni 2024 mit initial_value=0.
        # Jahresend-Ablesung Z-NEW = 350.
        # Erwartet: prior=0, cons = 350 + 300 = 650, info enthaelt "Wechsel".
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        old = _make_meter(p, "Z-OLD", active=False,
                          installed_to=date(2024, 6, 15),
                          initial_value=None)
        _make_reading(old, 2023, Decimal("1200"))
        _make_reading(old, 2024, Decimal("1500"), consumption=Decimal("300"),
                      reading_date=date(2024, 6, 15))
        new = _make_meter(p, "Z-NEW", active=True,
                          installed_from=date(2024, 6, 15),
                          initial_value=Decimal("0"))
        db.session.commit()

        prior, label, cons, info = svc.compute_prior_and_consumption(
            new, 2024, Decimal("350"),
        )
        assert prior == Decimal("0")
        assert "Anfang" in label
        assert cons == Decimal("650")
        assert "Wechsel" in info
        assert "Z-OLD" in info
        assert "300" in info  # Vorgaenger-Verbrauch wird genannt

    def test_replacement_without_predecessor_closing_reading(self, app):
        # Wechsel im Jahr, aber Vorgaenger hat keine Abschluss-Ablesung
        # (z.B. Daten unvollstaendig). Erwartung: nur Verbrauch dieses
        # Meters wird gezeigt, info erwaehnt unbekannten Vorgaenger-Anteil.
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        old = _make_meter(p, "Z-OLD", active=False,
                          installed_to=date(2024, 6, 15))
        new = _make_meter(p, "Z-NEW", active=True,
                          installed_from=date(2024, 6, 15),
                          initial_value=Decimal("0"))
        db.session.commit()

        prior, label, cons, info = svc.compute_prior_and_consumption(
            new, 2024, Decimal("350"),
        )
        assert prior == Decimal("0")
        assert cons == Decimal("350")  # ohne Vorgaenger-Anteil
        assert "unbekannt" in info.lower() or "fehlt" in info.lower()


# ---------------------------------------------------------------------------
# build_resolved_rows -- prior/consumption-Felder + Mismatch
# ---------------------------------------------------------------------------

class TestBuildResolvedRowsConsumption:
    def test_prior_and_consumption_filled_with_prev_reading(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")
        _make_reading(m, 2023, Decimal("100"))
        db.session.commit()

        df = pd.DataFrame([{"Nr": "Z-1", "Stand": "150", "Jahr": "2024"}])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].prior_value == Decimal("100")
        assert rows[0].prior_label == "2023"
        assert rows[0].computed_consumption == Decimal("50")
        assert rows[0].imported_consumption is None
        assert rows[0].consumption_mismatch is False
        assert rows[0].replacement_info == ""

    def test_imported_consumption_matches(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")
        _make_reading(m, 2023, Decimal("100"))
        db.session.commit()

        df = pd.DataFrame([{
            "Nr": "Z-1", "Stand": "150", "Jahr": "2024", "Verbrauch": "50,0",
        }])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", col_consumption="Verbrauch", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].computed_consumption == Decimal("50")
        assert rows[0].imported_consumption == Decimal("50.0")
        assert rows[0].consumption_mismatch is False

    def test_imported_consumption_mismatch(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")
        _make_reading(m, 2023, Decimal("100"))
        db.session.commit()

        df = pd.DataFrame([{
            "Nr": "Z-1", "Stand": "150", "Jahr": "2024", "Verbrauch": "75",
        }])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", col_consumption="Verbrauch", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].computed_consumption == Decimal("50")
        assert rows[0].imported_consumption == Decimal("75")
        assert rows[0].consumption_mismatch is True

    def test_replacement_info_visible_on_resolved_row(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        old = _make_meter(p, "Z-OLD", active=False,
                          installed_to=date(2024, 6, 15))
        _make_reading(old, 2023, Decimal("1200"))
        _make_reading(old, 2024, Decimal("1500"),
                      consumption=Decimal("300"),
                      reading_date=date(2024, 6, 15))
        new = _make_meter(p, "Z-NEW", active=True,
                          installed_from=date(2024, 6, 15),
                          initial_value=Decimal("0"))
        db.session.commit()

        df = pd.DataFrame([{"Nr": "Z-NEW", "Stand": "350", "Jahr": "2024"}])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].computed_consumption == Decimal("650")
        assert "Wechsel" in rows[0].replacement_info

    def test_no_consumption_column_skips_imported(self, app):
        c = _make_customer(name="A")
        p = _make_property("P-1", c)
        m = _make_meter(p, "Z-1")
        _make_reading(m, 2023, Decimal("100"))
        db.session.commit()

        df = pd.DataFrame([{"Nr": "Z-1", "Stand": "150", "Jahr": "2024"}])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", default_year=2024,  # col_consumption leer
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].imported_consumption is None
        assert rows[0].consumption_mismatch is False

    def test_unmapped_row_has_no_prior_or_consumption(self, app):
        df = pd.DataFrame([{"Nr": "XXX", "Stand": "150", "Jahr": "2024"}])
        cfg = svc.MappingConfig(
            mode="meter_number", col_lookup="Nr", col_value="Stand",
            col_year="Jahr", default_year=2024,
        )
        rows = svc.build_resolved_rows(df, cfg)
        assert rows[0].prior_value is None
        assert rows[0].computed_consumption is None
        assert rows[0].prior_label == "—"
