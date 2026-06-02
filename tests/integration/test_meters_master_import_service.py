"""Integration-Tests für den Zähler-Stammdaten-Import-Service.

Deckt build_preview_rows (ROW_NEW/ROW_UPDATE/ROW_EXISTS/ROW_ERROR),
Meter↔Objekt-Warnungen (Datei-intern und gegen Bestand) und
commit (anlegen, aktualisieren, überspringen, Error-Skip) ab.
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.extensions import db
from app.meters import meter_import_service as svc
from app.imports.common import ROW_NEW, ROW_UPDATE, ROW_EXISTS, ROW_ERROR
from app.models import Customer, Property, PropertyOwnership, User, WaterMeter
from tests.conftest import _ensure_role


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="testadmin_mmi", email="mmi@t.test", role_id=role.id)
    u.set_password("x")
    db.session.add(u)
    db.session.commit()
    return u


def _make_property(object_number, object_type="Haus"):
    p = Property(object_number=object_number, object_type=object_type, ort="Testort")
    db.session.add(p)
    db.session.flush()
    return p


def _make_meter(prop, meter_number, meter_type="main", active=True):
    m = WaterMeter(
        property_id=prop.id,
        meter_number=meter_number,
        meter_type=meter_type,
        active=active,
    )
    db.session.add(m)
    db.session.flush()
    return m


def _df(*rows):
    """Helper: create a DataFrame from dict rows."""
    return pd.DataFrame(list(rows))


def _cfg(**kwargs):
    defaults = dict(
        col_meter_number="zaehlernummer",
        col_object_number="objekt",
        col_location="",
        col_eichjahr="",
        col_installed_from="",
        col_initial_value="",
        col_meter_type="",
        col_notes="",
        duplicate_mode="skip",
    )
    defaults.update(kwargs)
    return svc.MeterImportConfig(**defaults)


# ---------------------------------------------------------------------------
# build_preview_rows — Status-Erkennung
# ---------------------------------------------------------------------------

class TestBuildPreviewRows:

    def test_new_meter_gives_row_new(self, app):
        p = _make_property("OBJ-10")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-100", "objekt": "OBJ-10"})
        rows = svc.build_preview_rows(df, _cfg())
        assert len(rows) == 1
        assert rows[0].status == ROW_NEW

    def test_existing_meter_skip_mode_gives_row_exists(self, app):
        p = _make_property("OBJ-20")
        _make_meter(p, "Z-200")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-200", "objekt": "OBJ-20"})
        rows = svc.build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_EXISTS

    def test_existing_meter_update_mode_gives_row_update(self, app):
        p = _make_property("OBJ-30")
        _make_meter(p, "Z-300")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-300", "objekt": "OBJ-30"})
        rows = svc.build_preview_rows(df, _cfg(duplicate_mode="update"))
        assert rows[0].status == ROW_UPDATE

    def test_missing_meter_number_gives_error(self, app):
        _make_property("OBJ-40")
        db.session.commit()
        df = _df({"zaehlernummer": "", "objekt": "OBJ-40"})
        rows = svc.build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR
        assert "Zählernummer" in rows[0].message

    def test_missing_object_number_gives_error(self, app):
        df = _df({"zaehlernummer": "Z-500", "objekt": ""})
        rows = svc.build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR
        assert "Objekt-Nr." in rows[0].message

    def test_unknown_object_number_gives_error(self, app):
        df = _df({"zaehlernummer": "Z-600", "objekt": "UNKNOWN-999"})
        rows = svc.build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR
        assert "nicht gefunden" in rows[0].message

    def test_unmapped_columns_give_empty_fields(self, app):
        p = _make_property("OBJ-50")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-50", "objekt": "OBJ-50"})
        rows = svc.build_preview_rows(df, _cfg())
        assert rows[0].fields["location"] == ""
        assert rows[0].fields["eichjahr"] == ""


# ---------------------------------------------------------------------------
# build_preview_rows — Meter↔Objekt-Warnungen
# ---------------------------------------------------------------------------

class TestMeterObjectWarnings:

    def test_same_meter_two_rows_different_objects_gives_warning(self, app):
        p1 = _make_property("OBJ-A")
        p2 = _make_property("OBJ-B")
        db.session.commit()
        df = _df(
            {"zaehlernummer": "Z-SHARED", "objekt": "OBJ-A"},
            {"zaehlernummer": "Z-SHARED", "objekt": "OBJ-B"},
        )
        rows = svc.build_preview_rows(df, _cfg())
        # First row: no warning (first registration)
        assert not rows[0].warnings
        # Second row: warning because same meter_number → different object
        assert rows[1].warnings
        assert "Z-SHARED" in rows[1].warnings[0]

    def test_existing_meter_different_object_in_file_gives_warning(self, app):
        p1 = _make_property("OBJ-C")
        p2 = _make_property("OBJ-D")
        # Existing meter is on OBJ-C
        _make_meter(p1, "Z-MOVED")
        db.session.commit()
        # File says Z-MOVED belongs to OBJ-D
        df = _df({"zaehlernummer": "Z-MOVED", "objekt": "OBJ-D"})
        rows = svc.build_preview_rows(df, _cfg(duplicate_mode="update"))
        assert rows[0].warnings
        assert "Z-MOVED" in rows[0].warnings[0]

    def test_existing_meter_same_object_no_warning(self, app):
        p = _make_property("OBJ-E")
        _make_meter(p, "Z-SAME")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-SAME", "objekt": "OBJ-E"})
        rows = svc.build_preview_rows(df, _cfg(duplicate_mode="update"))
        assert not rows[0].warnings


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

class TestCommit:

    def _make_commit_cfg(self, duplicate_mode="skip", col_location="standort",
                          col_meter_type="", col_notes=""):
        return svc.MeterImportConfig(
            col_meter_number="zaehlernummer",
            col_object_number="objekt",
            col_location=col_location,
            col_eichjahr="",
            col_installed_from="",
            col_initial_value="",
            col_meter_type=col_meter_type,
            col_notes=col_notes,
            duplicate_mode=duplicate_mode,
        )

    def test_commit_creates_new_meter(self, app):
        p = _make_property("OBJ-NEW1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-N1", "objekt": "OBJ-NEW1", "standort": "Keller"})
        cfg = self._make_commit_cfg()
        rows = svc.build_preview_rows(df, cfg)
        stats = svc.commit(rows, cfg)
        assert stats.created == 1
        assert stats.updated == 0
        assert stats.skipped == 0
        m = WaterMeter.query.filter_by(meter_number="Z-N1").first()
        assert m is not None
        assert m.property_id == p.id
        assert m.location == "Keller"
        assert m.meter_type == "main"
        assert m.active is True

    def test_commit_skip_existing_meter(self, app):
        p = _make_property("OBJ-SKIP1")
        _make_meter(p, "Z-S1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-S1", "objekt": "OBJ-SKIP1", "standort": "Dach"})
        cfg = self._make_commit_cfg(duplicate_mode="skip")
        rows = svc.build_preview_rows(df, cfg)
        stats = svc.commit(rows, cfg)
        assert stats.skipped == 1
        assert stats.created == 0
        # location should NOT be updated
        m = WaterMeter.query.filter_by(meter_number="Z-S1").first()
        assert m.location is None or m.location != "Dach"

    def test_commit_update_existing_meter(self, app):
        p = _make_property("OBJ-UPD1")
        _make_meter(p, "Z-U1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-U1", "objekt": "OBJ-UPD1", "standort": "Dachboden"})
        cfg = self._make_commit_cfg(duplicate_mode="update")
        rows = svc.build_preview_rows(df, cfg)
        stats = svc.commit(rows, cfg)
        assert stats.updated == 1
        m = WaterMeter.query.filter_by(meter_number="Z-U1").first()
        assert m.location == "Dachboden"

    def test_commit_error_row_gives_skipped_error(self, app):
        df = _df({"zaehlernummer": "", "objekt": "OBJ-X"})
        cfg = self._make_commit_cfg()
        rows = svc.build_preview_rows(df, cfg)
        stats = svc.commit(rows, cfg)
        assert stats.skipped_error == 1
        assert stats.created == 0

    def test_commit_skip_checkbox_skips_row(self, app):
        p = _make_property("OBJ-CHK1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-CHK1", "objekt": "OBJ-CHK1", "standort": ""})
        cfg = self._make_commit_cfg()
        rows = svc.build_preview_rows(df, cfg)
        rows[0].skip = True
        stats = svc.commit(rows, cfg)
        assert stats.skipped == 1
        assert WaterMeter.query.filter_by(meter_number="Z-CHK1").first() is None

    def test_commit_update_moves_meter_to_new_object(self, app):
        """In update mode a meter can be re-assigned to a different object."""
        p1 = _make_property("OBJ-MOV1")
        p2 = _make_property("OBJ-MOV2")
        _make_meter(p1, "Z-MOV1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-MOV1", "objekt": "OBJ-MOV2", "standort": ""})
        cfg = self._make_commit_cfg(duplicate_mode="update")
        rows = svc.build_preview_rows(df, cfg)
        # There should be a warning
        assert rows[0].warnings
        stats = svc.commit(rows, cfg)
        assert stats.updated == 1
        m = WaterMeter.query.filter_by(meter_number="Z-MOV1").first()
        assert m.property_id == p2.id

    def test_commit_skip_mode_meter_stays_at_original_object(self, app):
        """In skip mode a meter that would change object is simply skipped."""
        p1 = _make_property("OBJ-SKM1")
        p2 = _make_property("OBJ-SKM2")
        _make_meter(p1, "Z-SKM1")
        db.session.commit()
        # File says different object, but duplicate_mode=skip → skipped
        df = _df({"zaehlernummer": "Z-SKM1", "objekt": "OBJ-SKM2", "standort": ""})
        cfg = self._make_commit_cfg(duplicate_mode="skip")
        rows = svc.build_preview_rows(df, cfg)
        stats = svc.commit(rows, cfg)
        assert stats.skipped == 1
        m = WaterMeter.query.filter_by(meter_number="Z-SKM1").first()
        assert m.property_id == p1.id  # unchanged

    def test_commit_meter_type_sub(self, app):
        p = _make_property("OBJ-SUB1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-SUB1", "objekt": "OBJ-SUB1", "typ": "sub"})
        cfg = svc.MeterImportConfig(
            col_meter_number="zaehlernummer",
            col_object_number="objekt",
            col_meter_type="typ",
            duplicate_mode="skip",
        )
        rows = svc.build_preview_rows(df, cfg)
        svc.commit(rows, cfg)
        m = WaterMeter.query.filter_by(meter_number="Z-SUB1").first()
        assert m.meter_type == "sub"

    def test_commit_warnings_counted(self, app):
        p1 = _make_property("OBJ-WRN1")
        p2 = _make_property("OBJ-WRN2")
        # Existing meter on p1 — file says p2 → warning in update mode
        _make_meter(p1, "Z-WRN1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-WRN1", "objekt": "OBJ-WRN2", "standort": ""})
        cfg = self._make_commit_cfg(duplicate_mode="update")
        rows = svc.build_preview_rows(df, cfg)
        stats = svc.commit(rows, cfg)
        assert stats.warnings >= 1

    def test_commit_de_decimal_initial_value_parsed_correctly(self, app):
        """Regression: build_preview_rows stores initial_value as DE string
        (e.g. '1.234,567' via format_value_de).  The old commit() code did
        initial_value_raw.replace(',', '.') before calling parse_number, which
        turned '1234,567' → '1234.567' and then parse_number('1234.567', 'auto')
        treated the dot as a DE thousands separator → Decimal('1234567').
        The fix: pass the DE string directly to parse_number, which handles it
        natively."""
        p = _make_property("OBJ-DECI1")
        db.session.commit()
        df = _df({"zaehlernummer": "Z-DECI1", "objekt": "OBJ-DECI1", "anfangsstand": "1234,567"})
        cfg = svc.MeterImportConfig(
            col_meter_number="zaehlernummer",
            col_object_number="objekt",
            col_initial_value="anfangsstand",
            duplicate_mode="skip",
        )
        rows = svc.build_preview_rows(df, cfg)
        assert len(rows) == 1
        assert rows[0].status == ROW_NEW
        stats = svc.commit(rows, cfg)
        assert stats.created == 1
        m = WaterMeter.query.filter_by(meter_number="Z-DECI1").first()
        assert m is not None
        assert m.initial_value == Decimal("1234.567"), (
            f"Expected Decimal('1234.567'), got {m.initial_value!r} — "
            "DE-decimal parse bug may have regressed"
        )


# ---------------------------------------------------------------------------
# _resolve_meter_type helper
# ---------------------------------------------------------------------------

class TestResolveMeterType:

    def test_empty_gives_main(self):
        assert svc._resolve_meter_type("") == "main"

    def test_none_like_gives_main(self):
        assert svc._resolve_meter_type("  ") == "main"

    def test_main_literal(self):
        assert svc._resolve_meter_type("main") == "main"

    def test_sub_literal(self):
        assert svc._resolve_meter_type("sub") == "sub"

    def test_subzaehler_gives_sub(self):
        assert svc._resolve_meter_type("Subzähler") == "sub"

    def test_subzaehler_ascii(self):
        assert svc._resolve_meter_type("subzaehler") == "sub"

    def test_unknown_gives_main(self):
        assert svc._resolve_meter_type("Hauptzähler") == "main"
        assert svc._resolve_meter_type("Wasserzähler") == "main"
