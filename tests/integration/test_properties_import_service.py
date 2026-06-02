"""Integration-Tests für den Objekte-Import-Service.

Deckt ``build_preview_rows`` (Status-Erkennung, object_type-Ableitung,
Adress-Duplikat-Warnung, Owner-Integritäts-Warnung) und ``commit``
(Anlegen, Aktualisieren, Skip-Modus, Owner-Zuordnung) ab.

Stolperer: Property-Fixtures immer mit ``object_type`` anlegen
(NOT NULL ohne DB-Default)!
"""
import pandas as pd
import pytest
from datetime import date

from app.extensions import db
from app.properties.import_service import (
    PropertyImportConfig,
    build_preview_rows,
    apply_edits,
    commit,
    suggest_config,
    _resolve_object_type,
)
from app.imports.common import (
    ROW_NEW,
    ROW_UPDATE,
    ROW_EXISTS,
    ROW_ERROR,
)
from app.models import Customer, Property, PropertyOwnership


# ---------------------------------------------------------------------------
# Helper: build a minimal DataFrame
# ---------------------------------------------------------------------------

def _df(*rows, columns=("Objekt-Nr.", "Typ", "Straße", "Ort")):
    """Build a DataFrame with the given column names and rows."""
    return pd.DataFrame(list(rows), columns=list(columns))


def _cfg(**kwargs) -> PropertyImportConfig:
    """Build a PropertyImportConfig with common column names."""
    defaults = {
        "col_object_number": "Objekt-Nr.",
        "col_object_type": "Typ",
        "col_strasse": "Straße",
        "col_ort": "Ort",
    }
    defaults.update(kwargs)
    return PropertyImportConfig(**defaults)


# ---------------------------------------------------------------------------
# _resolve_object_type
# ---------------------------------------------------------------------------

class TestResolveObjectType:
    def test_empty_gives_haus(self):
        assert _resolve_object_type("") == "Haus"

    def test_whitespace_only_gives_haus(self):
        assert _resolve_object_type("   ") == "Haus"

    def test_haus_case_insensitive(self):
        assert _resolve_object_type("haus") == "Haus"
        assert _resolve_object_type("HAUS") == "Haus"

    def test_garten_is_valid_type(self):
        assert _resolve_object_type("Garten") == "Garten"
        assert _resolve_object_type("garten") == "Garten"

    def test_sonstiges_is_valid_type(self):
        assert _resolve_object_type("Sonstiges") == "Sonstiges"
        assert _resolve_object_type("sonstiges") == "Sonstiges"

    def test_unknown_value_gives_sonstiges(self):
        assert _resolve_object_type("Stall") == "Sonstiges"
        assert _resolve_object_type("Scheune") == "Sonstiges"
        assert _resolve_object_type("Garage") == "Sonstiges"


# ---------------------------------------------------------------------------
# build_preview_rows — Status-Erkennung
# ---------------------------------------------------------------------------

class TestBuildPreviewRowsStatus:
    def test_new_object_number_is_row_new(self, app):
        df = _df(("99", "Haus", "Bergstraße", "Wien"))
        rows = build_preview_rows(df, _cfg())
        assert len(rows) == 1
        assert rows[0].status == ROW_NEW
        assert rows[0].fields["object_number"] == "99"
        # Raw value stored; resolved to "Haus" at commit time
        assert rows[0].fields["object_type"] == "Haus"

    def test_existing_number_update_mode(self, app):
        prop = Property(object_number="A1", object_type="Haus")
        db.session.add(prop)
        db.session.commit()

        df = _df(("A1", "Garten", "Andere Straße", "Linz"))
        cfg = _cfg(duplicate_mode="update")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_UPDATE

    def test_existing_number_skip_mode(self, app):
        prop = Property(object_number="B2", object_type="Haus")
        db.session.add(prop)
        db.session.commit()

        df = _df(("B2", "Haus", "Straße", "Wien"))
        cfg = _cfg(duplicate_mode="skip")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_EXISTS

    def test_empty_row_is_error(self, app):
        df = _df(("", "", "", ""))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR
        assert "Leere Zeile" in rows[0].message

    def test_object_type_empty_raw_in_fields(self, app):
        """Empty object_type cell is stored as empty string in fields (resolved at commit)."""
        df = _df(("X1", "", "Straße", "Wien"))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].fields["object_type"] == ""

    def test_object_type_garten_preserved_in_fields(self, app):
        df = _df(("X2", "Garten", "Gartenweg", "Wien"))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].fields["object_type"] == "Garten"

    def test_object_type_stall_in_fields_raw(self, app):
        """Unrecognised types are stored as-is in fields; commit resolves them."""
        df = _df(("X3", "Stall", "Stallweg", "Wien"))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].fields["object_type"] == "Stall"

    def test_no_number_is_new(self, app):
        """Rows without object_number are always ROW_NEW."""
        df = _df(("", "Haus", "Bergstraße", "Graz"))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_NEW


# ---------------------------------------------------------------------------
# build_preview_rows — Adress-Duplikat-Warnung
# ---------------------------------------------------------------------------

class TestAddressDuplicateWarning:
    def test_same_address_warns_but_stays_new(self, app):
        prop = Property(
            object_type="Haus",
            strasse="Hauptstraße", hausnummer="5", plz="1010", ort="Wien",
        )
        db.session.add(prop)
        db.session.commit()

        df = pd.DataFrame([{
            "Straße": "Hauptstraße", "Hausnummer": "5",
            "PLZ": "1010", "Ort": "Wien",
        }])
        cfg = PropertyImportConfig(
            col_strasse="Straße", col_hausnummer="Hausnummer",
            col_plz="PLZ", col_ort="Ort",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW
        assert any("Duplikat" in w for w in rows[0].warnings)

    def test_different_address_no_warning(self, app):
        prop = Property(
            object_type="Haus",
            strasse="Andere Straße", hausnummer="1", plz="1010", ort="Wien",
        )
        db.session.add(prop)
        db.session.commit()

        df = pd.DataFrame([{
            "Straße": "Bergstraße", "Hausnummer": "9",
            "PLZ": "1010", "Ort": "Wien",
        }])
        cfg = PropertyImportConfig(
            col_strasse="Straße", col_hausnummer="Hausnummer",
            col_plz="PLZ", col_ort="Ort",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW
        assert not rows[0].warnings


# ---------------------------------------------------------------------------
# build_preview_rows — Owner-Integritäts-Warnungen
# ---------------------------------------------------------------------------

class TestOwnerWarnings:
    def test_unknown_customer_number_warns(self, app):
        df = pd.DataFrame([{
            "Objekt-Nr.": "Z1", "Besitzer": "9999",
        }])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW
        assert any("nicht gefunden" in w for w in rows[0].warnings)

    def test_known_customer_no_conflict_warning(self, app):
        c = Customer(name="Besitzer A", customer_number=101)
        db.session.add(c)
        db.session.commit()

        df = pd.DataFrame([{"Objekt-Nr.": "Z2", "Besitzer": "101"}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
        )
        rows = build_preview_rows(df, cfg)
        # No owner conflict (no existing owner)
        assert not any("mehrere aktive Eigentümer" in w for w in rows[0].warnings)

    def test_second_owner_for_existing_property_warns(self, app):
        c1 = Customer(name="Besitzer Eins", customer_number=201)
        c2 = Customer(name="Besitzer Zwei", customer_number=202)
        prop = Property(object_number="W1", object_type="Haus")
        db.session.add_all([c1, c2, prop])
        db.session.flush()
        own = PropertyOwnership(
            property_id=prop.id, customer_id=c1.id,
            valid_from=date(2020, 1, 1), valid_to=None,
        )
        db.session.add(own)
        db.session.commit()

        df = pd.DataFrame([{"Objekt-Nr.": "W1", "Besitzer": "202"}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
            duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        assert any("mehrere aktive Eigentümer" in w for w in rows[0].warnings)

    def test_same_owner_no_warning(self, app):
        c1 = Customer(name="Gleicher Besitzer", customer_number=203)
        prop = Property(object_number="W2", object_type="Haus")
        db.session.add_all([c1, prop])
        db.session.flush()
        own = PropertyOwnership(
            property_id=prop.id, customer_id=c1.id,
            valid_from=date(2020, 1, 1), valid_to=None,
        )
        db.session.add(own)
        db.session.commit()

        df = pd.DataFrame([{"Objekt-Nr.": "W2", "Besitzer": "203"}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
            duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        # No conflict — same customer
        assert not any("mehrere aktive Eigentümer" in w for w in rows[0].warnings)


# ---------------------------------------------------------------------------
# commit — Anlegen und Aktualisieren
# ---------------------------------------------------------------------------

class TestCommit:
    def test_new_property_created(self, app):
        df = _df(("P1", "Haus", "Teststraße", "Wien"))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW

        stats = commit(rows, cfg)
        assert stats.created == 1
        prop = Property.query.filter_by(object_number="P1").first()
        assert prop is not None
        assert prop.object_type == "Haus"
        assert prop.active is True

    def test_new_property_without_number(self, app):
        df = _df(("", "Garten", "Gartenstraße", "Linz"))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        stats = commit(rows, cfg)
        assert stats.created == 1
        prop = Property.query.filter_by(
            strasse="Gartenstraße", ort="Linz"
        ).first()
        assert prop is not None
        assert prop.object_number is None
        assert prop.object_type == "Garten"

    def test_update_overwrites_fields(self, app):
        prop = Property(
            object_number="U1", object_type="Haus",
            strasse="Alte Straße", ort="Wien",
        )
        db.session.add(prop)
        db.session.commit()

        df = _df(("U1", "Garten", "Neue Straße", "Graz"))
        cfg = _cfg(duplicate_mode="update")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_UPDATE

        stats = commit(rows, cfg)
        assert stats.updated == 1
        db.session.refresh(prop)
        assert prop.strasse == "Neue Straße"
        assert prop.ort == "Graz"
        assert prop.object_type == "Garten"

    def test_skip_mode_leaves_property_unchanged(self, app):
        prop = Property(object_number="S1", object_type="Haus", ort="Wien")
        db.session.add(prop)
        db.session.commit()

        df = _df(("S1", "Garten", "Neue Straße", "Graz"))
        cfg = _cfg(duplicate_mode="skip")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_EXISTS

        stats = commit(rows, cfg)
        assert stats.skipped == 1
        assert stats.updated == 0
        db.session.refresh(prop)
        assert prop.object_type == "Haus"

    def test_error_row_skipped(self, app):
        df = _df(("", "", "", ""))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_ERROR
        stats = commit(rows, cfg)
        assert stats.skipped_error == 1
        assert stats.created == 0

    def test_skip_checkbox_skips_row(self, app):
        df = _df(("", "Haus", "Straße", "Wien"))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        rows[0].skip = True
        stats = commit(rows, cfg)
        assert stats.skipped == 1
        assert stats.created == 0

    def test_object_type_not_set_to_null_on_update_with_empty_column(self, app):
        """object_type is NOT NULL — update must never clear it."""
        prop = Property(object_number="T1", object_type="Garten")
        db.session.add(prop)
        db.session.commit()

        # col_object_type mapped but empty cell → should keep existing value
        df = pd.DataFrame([{"Objekt-Nr.": "T1", "Typ": ""}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_object_type="Typ",
            duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        commit(rows, cfg)
        db.session.refresh(prop)
        assert prop.object_type == "Garten"


# ---------------------------------------------------------------------------
# commit — Owner-Zuordnung
# ---------------------------------------------------------------------------

class TestCommitOwner:
    def test_owner_assigned_on_new_property(self, app):
        c = Customer(name="Eigentümer Neu", customer_number=301)
        db.session.add(c)
        db.session.commit()

        df = pd.DataFrame([{"Objekt-Nr.": "O1", "Besitzer": "301"}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
        )
        rows = build_preview_rows(df, cfg)
        stats = commit(rows, cfg)
        assert stats.created == 1
        prop = Property.query.filter_by(object_number="O1").first()
        own = PropertyOwnership.query.filter_by(
            property_id=prop.id, customer_id=c.id, valid_to=None
        ).first()
        assert own is not None

    def test_two_owners_after_import_non_blocking(self, app):
        """Second owner warning is non-blocking — property ends up with 2 active owners."""
        c1 = Customer(name="Eigentümer Eins", customer_number=401)
        c2 = Customer(name="Eigentümer Zwei", customer_number=402)
        prop = Property(object_number="O2", object_type="Haus")
        db.session.add_all([c1, c2, prop])
        db.session.flush()
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=c1.id,
            valid_from=date(2020, 1, 1), valid_to=None,
        ))
        db.session.commit()

        df = pd.DataFrame([{"Objekt-Nr.": "O2", "Besitzer": "402"}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
            duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        # There should be a warning about multiple owners
        assert any("mehrere aktive Eigentümer" in w for w in rows[0].warnings)

        stats = commit(rows, cfg)
        # Import still succeeds
        assert stats.updated == 1 or stats.created == 0
        # Both ownerships now active
        active = PropertyOwnership.query.filter_by(
            property_id=prop.id, valid_to=None
        ).all()
        assert len(active) == 2

    def test_same_owner_no_duplicate_ownership(self, app):
        """Importing the same owner again must not create a duplicate ownership row."""
        c = Customer(name="Gleicher Eigentümer", customer_number=501)
        prop = Property(object_number="O3", object_type="Haus")
        db.session.add_all([c, prop])
        db.session.flush()
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=c.id,
            valid_from=date(2020, 1, 1), valid_to=None,
        ))
        db.session.commit()

        df = pd.DataFrame([{"Objekt-Nr.": "O3", "Besitzer": "501"}])
        cfg = PropertyImportConfig(
            col_object_number="Objekt-Nr.",
            col_owner_customer_number="Besitzer",
            duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        # No conflict warning for same owner
        assert not any("mehrere aktive Eigentümer" in w for w in rows[0].warnings)

        commit(rows, cfg)
        active = PropertyOwnership.query.filter_by(
            property_id=prop.id, customer_id=c.id, valid_to=None
        ).all()
        assert len(active) == 1  # still just one, no duplicate


# ---------------------------------------------------------------------------
# suggest_config
# ---------------------------------------------------------------------------

class TestSuggestConfig:
    def test_suggests_object_number_column(self, app):
        cfg = suggest_config(["Objektnummer", "Straße", "Ort"])
        assert cfg.col_object_number == "Objektnummer"

    def test_suggests_ort_column(self, app):
        cfg = suggest_config(["Objekt-Nr.", "Straße", "Ort"])
        assert cfg.col_ort == "Ort"

    def test_no_match_returns_empty(self, app):
        cfg = suggest_config(["Spalte1", "Spalte2"])
        assert cfg.col_object_number == ""
        assert cfg.col_ort == ""
