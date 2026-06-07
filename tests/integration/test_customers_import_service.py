"""Integration-Tests für den Kunden-Import-Service.

Deckt ``build_preview_rows`` (Status-Erkennung), ``apply_edits`` und
``commit`` (Anlegen, Aktualisieren, Skip-Modus) ab.

Nutzt die Fixtures aus ``tests/integration/conftest.py``.
"""
import pandas as pd
import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.extensions import db
from app.customers.import_service import (
    CustomerImportConfig,
    build_preview_rows,
    apply_edits,
    commit,
    suggest_config,
)
from app.imports.common import (
    ROW_NEW,
    ROW_UPDATE,
    ROW_EXISTS,
    ROW_ERROR,
)
from app.models import Customer
from tests.conftest import _ensure_role


# ---------------------------------------------------------------------------
# Helper: build a minimal DataFrame
# ---------------------------------------------------------------------------

def _df(*rows, columns=("Kunden-Nr.", "Name", "Ort", "E-Mail")):
    """Build a DataFrame with the given column names and rows."""
    return pd.DataFrame(list(rows), columns=list(columns))


def _cfg(**kwargs) -> CustomerImportConfig:
    """Build a CustomerImportConfig with the most common column names."""
    defaults = {
        "col_customer_number": "Kunden-Nr.",
        "col_name": "Name",
        "col_ort": "Ort",
        "col_email": "E-Mail",
    }
    defaults.update(kwargs)
    return CustomerImportConfig(**defaults)


# ---------------------------------------------------------------------------
# build_preview_rows
# ---------------------------------------------------------------------------

class TestBuildPreviewRows:
    def test_new_customer_number(self, app):
        df = _df(("99", "Neu Kunde", "Wien", "new@example.com"))
        rows = build_preview_rows(df, _cfg())
        assert len(rows) == 1
        assert rows[0].status == ROW_NEW
        assert rows[0].fields["customer_number"] == "99"
        assert rows[0].fields["name"] == "Neu Kunde"

    def test_existing_number_update_mode(self, app):
        c = Customer(name="Alt Kunde", customer_number=7)
        db.session.add(c)
        db.session.commit()

        df = _df(("7", "Alt Aktuell", "Linz", ""))
        cfg = _cfg(duplicate_mode="update")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_UPDATE

    def test_existing_number_skip_mode(self, app):
        c = Customer(name="Skip Kunde", customer_number=8)
        db.session.add(c)
        db.session.commit()

        df = _df(("8", "Irgendwas", "Graz", ""))
        cfg = _cfg(duplicate_mode="skip")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_EXISTS

    def test_invalid_number_is_error(self, app):
        df = _df(("xyz", "Fehler Kunde", "Wien", ""))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR
        assert "Ungültige Kunden-Nr." in rows[0].message

    def test_empty_row_is_error(self, app):
        df = _df(("", "", "", ""))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR
        assert "Leere Zeile" in rows[0].message

    def test_name_fallback_from_last_first(self, app):
        """When col_name is empty, name should be built from last+first name."""
        df = pd.DataFrame(
            [{"Nachname": "Muster", "Vorname": "Max", "Ort": "Wien"}]
        )
        cfg = CustomerImportConfig(
            col_name_last="Nachname",
            col_name_first="Vorname",
            col_ort="Ort",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW
        assert rows[0].fields["name"] == "Muster Max"

    def test_externe_kennung_match(self, app):
        c = Customer(name="Ext Kunde", externe_kennung="EXT-001")
        db.session.add(c)
        db.session.commit()

        df = pd.DataFrame([{"Ext-Kennung": "EXT-001", "Name": "Ext Kunde"}])
        cfg = CustomerImportConfig(
            col_externe_kennung="Ext-Kennung",
            col_name="Name",
            duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_UPDATE

    def test_no_match_without_key_and_no_name(self, app):
        """Row with no name and no number or ext key → error."""
        df = pd.DataFrame([{"Kunden-Nr.": "", "Name": "", "Ort": "Wien", "E-Mail": ""}])
        rows = build_preview_rows(df, _cfg())
        assert rows[0].status == ROW_ERROR


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

class TestCommit:
    def test_new_customer_gets_number_from_counter(self, app):
        df = _df(("", "Frischer Kunde", "Wien", "f@example.at"))
        cfg = CustomerImportConfig(col_name="Name", col_ort="Ort", col_email="E-Mail")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW

        stats = commit(rows, cfg)
        assert stats.created == 1
        c = Customer.query.filter_by(name="Frischer Kunde").first()
        assert c is not None
        assert c.customer_number is not None
        assert c.is_customer is True

    def test_new_customer_with_explicit_number(self, app):
        df = _df(("500", "Explicit Nr", "Graz", ""))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        stats = commit(rows, cfg)
        assert stats.created == 1
        c = Customer.query.filter_by(customer_number=500).first()
        assert c is not None
        assert c.name == "Explicit Nr"

    def test_update_overwrites_fields(self, app):
        c = Customer(name="Alter Name", customer_number=20, ort="Wien")
        db.session.add(c)
        db.session.commit()

        df = _df(("20", "Neuer Name", "Graz", "new@example.com"))
        cfg = _cfg(duplicate_mode="update")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_UPDATE
        stats = commit(rows, cfg)
        assert stats.updated == 1
        db.session.refresh(c)
        assert c.name == "Neuer Name"
        assert c.ort == "Graz"

    def test_update_clears_field_if_column_mapped_and_empty(self, app):
        c = Customer(name="Mit Ort", customer_number=21, ort="Wien")
        db.session.add(c)
        db.session.commit()

        df = _df(("21", "Mit Ort", "", ""))
        cfg = _cfg(duplicate_mode="update")
        rows = build_preview_rows(df, cfg)
        stats = commit(rows, cfg)
        db.session.refresh(c)
        # col_ort is mapped — empty value should clear the field
        assert c.ort is None

    def test_skip_mode_leaves_customer_unchanged(self, app):
        c = Customer(name="Unveraendert", customer_number=30, ort="Wien")
        db.session.add(c)
        db.session.commit()

        df = _df(("30", "Anderer Name", "Graz", ""))
        cfg = _cfg(duplicate_mode="skip")
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_EXISTS
        stats = commit(rows, cfg)
        assert stats.skipped == 1
        assert stats.updated == 0
        db.session.refresh(c)
        assert c.name == "Unveraendert"

    def test_skip_checkbox_skips_row(self, app):
        df = _df(("", "Zu Skippen", "Wien", ""))
        cfg = CustomerImportConfig(col_name="Name")
        rows = build_preview_rows(df, cfg)
        rows[0].skip = True
        stats = commit(rows, cfg)
        assert stats.skipped == 1
        assert stats.created == 0

    def test_error_row_is_skipped(self, app):
        df = _df(("xyz", "Fehler", "Wien", ""))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_ERROR
        stats = commit(rows, cfg)
        assert stats.skipped_error == 1
        assert stats.created == 0


# ---------------------------------------------------------------------------
# suggest_config
# ---------------------------------------------------------------------------

class TestSuggestConfig:
    def test_suggests_customer_number_column(self, app):
        cfg = suggest_config(["Kundennummer", "Name", "Ort"])
        assert cfg.col_customer_number == "Kundennummer"

    def test_suggests_email_column(self, app):
        cfg = suggest_config(["Name", "E-Mail", "PLZ"])
        assert cfg.col_email == "E-Mail"

    def test_no_match_returns_empty(self, app):
        cfg = suggest_config(["Spalte1", "Spalte2"])
        assert cfg.col_customer_number == ""
        assert cfg.col_email == ""

    def test_suggests_salutation_column(self, app):
        cfg = suggest_config(["Anrede", "Nachname", "Vorname"])
        assert cfg.col_salutation == "Anrede"
        assert cfg.col_name_last == "Nachname"
        assert cfg.col_name_first == "Vorname"

    def test_suggests_company_column(self, app):
        cfg = suggest_config(["Firma", "Name", "Ort"])
        assert cfg.col_is_company == "Firma"


# ---------------------------------------------------------------------------
# Name-Aufspaltung beim Import (oss-v1.21.0)
# ---------------------------------------------------------------------------

class TestNameSplitImport:
    def test_split_columns_persist_and_combine(self, app):
        df = _df(
            ("Mustermann", "Max", "Herr"),
            columns=("Nachname", "Vorname", "Anrede"),
        )
        cfg = CustomerImportConfig(
            col_name_last="Nachname",
            col_name_first="Vorname",
            col_salutation="Anrede",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_NEW
        assert rows[0].fields["name"] == "Mustermann Max"   # kombiniert
        assert rows[0].fields["last_name"] == "Mustermann"
        assert rows[0].fields["first_name"] == "Max"
        assert rows[0].fields["salutation"] == "Herr"

        stats = commit(rows, cfg)
        assert stats.created == 1
        c = Customer.query.filter_by(last_name="Mustermann").first()
        assert c.name == "Mustermann Max"
        assert c.first_name == "Max"
        assert c.salutation == "Herr"
        assert c.letter_name == "Max Mustermann"
        assert c.salutation_line == "Sehr geehrter Herr Mustermann"

    def test_combined_only_goes_to_last_name(self, app):
        """Nur ein kombiniertes Namensfeld → als Privatperson in den Nachnamen."""
        df = _df(("", "Nur Kombiniert", "Wien", ""))
        rows = build_preview_rows(df, _cfg())
        assert rows[0].fields["last_name"] == "Nur Kombiniert"
        assert rows[0].fields["first_name"] == ""
        assert rows[0].fields["is_company"] == ""

        stats = commit(rows, _cfg())
        assert stats.created == 1
        c = Customer.query.filter_by(name="Nur Kombiniert").first()
        assert c.last_name == "Nur Kombiniert"
        assert c.first_name is None
        assert c.is_company is False
        # letter_name = "Vorname Nachname" → nur Nachname vorhanden
        assert c.letter_name == "Nur Kombiniert"


# ---------------------------------------------------------------------------
# Firma / Person beim Import (oss-v1.23.0)
# ---------------------------------------------------------------------------

class TestCompanyImport:
    def test_company_via_type_column(self, app):
        df = _df(
            ("Wasser GmbH", "Firma", "Graz"),
            columns=("Name", "Typ", "Ort"),
        )
        cfg = CustomerImportConfig(
            col_name="Name", col_is_company="Typ", col_ort="Ort",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].fields["is_company"] == "1"
        # Firma räumt die Personen-Felder ab.
        assert rows[0].fields["last_name"] == ""
        assert rows[0].fields["first_name"] == ""

        stats = commit(rows, cfg)
        assert stats.created == 1
        c = Customer.query.filter_by(name="Wasser GmbH").first()
        assert c.is_company is True
        assert c.last_name is None and c.first_name is None and c.salutation is None
        assert c.letter_name == "Wasser GmbH"
        assert c.salutation_line == "Sehr geehrte Damen und Herren"

    def test_person_via_type_column_keeps_split(self, app):
        df = _df(
            ("Mustermann", "Max", "Person"),
            columns=("Nachname", "Vorname", "Typ"),
        )
        cfg = CustomerImportConfig(
            col_name_last="Nachname", col_name_first="Vorname", col_is_company="Typ",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].fields["is_company"] == ""
        stats = commit(rows, cfg)
        assert stats.created == 1
        c = Customer.query.filter_by(last_name="Mustermann").first()
        assert c.is_company is False
        assert c.first_name == "Max"
        assert c.name == "Mustermann Max"

    def test_anrede_firma_implies_company(self, app):
        """Anrede 'Firma' kennzeichnet eine Firma, auch ohne eigene Typ-Spalte."""
        df = _df(
            ("Wasser GmbH", "Firma", "Wien"),
            columns=("Name", "Anrede", "Ort"),
        )
        cfg = CustomerImportConfig(
            col_name="Name", col_salutation="Anrede", col_ort="Ort",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].fields["is_company"] == "1"
        stats = commit(rows, cfg)
        c = Customer.query.filter_by(name="Wasser GmbH").first()
        assert c.is_company is True

    def test_update_sets_company_flag(self, app):
        c = Customer(name="Alt", customer_number=42, is_company=False)
        db.session.add(c)
        db.session.commit()

        df = _df(("42", "Neu GmbH", "Firma"), columns=("Kunden-Nr.", "Name", "Typ"))
        cfg = CustomerImportConfig(
            col_customer_number="Kunden-Nr.", col_name="Name",
            col_is_company="Typ", duplicate_mode="update",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].status == ROW_UPDATE
        commit(rows, cfg)
        db.session.refresh(c)
        assert c.is_company is True
        assert c.last_name is None and c.first_name is None
        assert c.letter_name == "Neu GmbH"


# ---------------------------------------------------------------------------
# Straße / Hausnummer beim Import (oss-v1.23.0)
# ---------------------------------------------------------------------------

class TestAddressSplit:
    def test_combined_street_number_split(self, app):
        df = _df(("100", "Huber", "Hauptstraße 12a"),
                 columns=("Kunden-Nr.", "Name", "Straße"))
        cfg = CustomerImportConfig(
            col_customer_number="Kunden-Nr.", col_name="Name", col_strasse="Straße",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].fields["strasse"] == "Hauptstraße"
        assert rows[0].fields["hausnummer"] == "12a"

        commit(rows, cfg)
        c = Customer.query.filter_by(customer_number=100).first()
        assert c.strasse == "Hauptstraße"
        assert c.hausnummer == "12a"

    def test_separate_house_number_column_wins(self, app):
        df = _df(("101", "Maier", "Hauptstraße 12", "9"),
                 columns=("Kunden-Nr.", "Name", "Straße", "Hausnr"))
        cfg = CustomerImportConfig(
            col_customer_number="Kunden-Nr.", col_name="Name",
            col_strasse="Straße", col_hausnummer="Hausnr",
        )
        rows = build_preview_rows(df, cfg)
        # Eigene Hausnummer-Spalte gefüllt → Straße bleibt unangetastet.
        assert rows[0].fields["strasse"] == "Hauptstraße 12"
        assert rows[0].fields["hausnummer"] == "9"

    def test_street_without_number_kept_whole(self, app):
        df = _df(("102", "Bauer", "Siedlung"),
                 columns=("Kunden-Nr.", "Name", "Straße"))
        cfg = CustomerImportConfig(
            col_customer_number="Kunden-Nr.", col_name="Name", col_strasse="Straße",
        )
        rows = build_preview_rows(df, cfg)
        assert rows[0].fields["strasse"] == "Siedlung"
        assert rows[0].fields["hausnummer"] == ""
