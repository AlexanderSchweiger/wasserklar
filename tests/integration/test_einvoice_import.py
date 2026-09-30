"""E-Rechnung-Spalten in den beiden Kunden-Importen (Kunden-Wizard + Stammdaten-Import).

USt-IdNr., Leitweg-ID/Käuferreferenz, Rechnungsformat (XRechnung) und das
Unternehmer-Kennzeichen folgen den Regeln des Kundenformulars; ungültige Werte
entfallen mit Warnung, der Rest der Zeile wird importiert. Nicht gemappte Spalten
lassen vorhandene Werte unberührt.
"""
import pandas as pd
import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.customers.import_service import (
    CustomerImportConfig, apply_edits, build_preview_rows, commit, suggest_config,
)
from app.extensions import db
from app.import_csv.routes import _build_suggestions, _run_import
from app.imports.common import (
    check_buyer_reference, normalize_vat_id, parse_bool, parse_einvoice_format, suggest_column,
)
from app.models import AppSetting, Customer, CustomerEmailConsentLog

LEITWEG = "04011000-1234512345-06"


# ---------------------------------------------------------------------------
# Gemeinsame Helfer
# ---------------------------------------------------------------------------

class TestParsers:
    @pytest.mark.parametrize("raw, expected", [
        ("ja", True), ("Ja", True), ("x", True), ("1", True), ("Unternehmer", True),
        ("nein", False), ("0", False), ("Privat", False),
        ("", None), ("vielleicht", None), (None, None),
    ])
    def test_parse_bool(self, raw, expected):
        assert parse_bool(raw) is expected

    @pytest.mark.parametrize("raw, value, has_warning", [
        ("de 123 456 789", "DE123456789", False),
        ("ATU12345678", "ATU12345678", False),
        ("", "", False),
        ("123456789", "", True),            # ohne Länderkürzel unbrauchbar
    ])
    def test_normalize_vat_id(self, raw, value, has_warning):
        result, warning = normalize_vat_id(raw)
        assert result == value and bool(warning) is has_warning

    @pytest.mark.parametrize("raw, expected", [
        ("XRechnung", "xrechnung"), ("X-Rechnung", "xrechnung"), ("x rechnung", "xrechnung"),
        ("ja, XRECHNUNG", "xrechnung"), ("ZUGFeRD", ""), ("PDF", ""), ("", ""),
        ("UBL", "peppol_ubl"), ("ubl 2.1", "peppol_ubl"), ("Peppol", "peppol_ubl"),
        ("Publikum", ""),
    ])
    def test_parse_einvoice_format(self, raw, expected):
        assert parse_einvoice_format(raw) == expected

    def test_buyer_reference(self):
        assert check_buyer_reference(f" {LEITWEG} ") == (LEITWEG, "")
        value, warning = check_buyer_reference("04011000-1234512345-07")
        assert value == "" and "ungültige Prüfziffer" in warning
        assert check_buyer_reference("Bestellung 2024-15") == ("Bestellung 2024-15", "")

    def test_exact_hints_do_not_match_inside_words(self):
        """„=uid“ trifft nur die Spalte UID — nicht jede GUID-Spalte."""
        hints = ["ust-id", "=uid", "=vat"]
        assert suggest_column(["Name", "Kontakt-GUID", "Privatkunde"], hints) == ""
        assert suggest_column(["Name", "UID"], hints) == "UID"
        assert suggest_column(["Name", "USt-IdNr."], hints) == "USt-IdNr."


# ---------------------------------------------------------------------------
# Kunden-Wizard (app/customers/import_service.py)
# ---------------------------------------------------------------------------

COLUMNS = ("Kunden-Nr.", "Name", "E-Mail", "USt-IdNr.", "Leitweg-ID", "Rechnungsformat", "Unternehmer")


def _df(*rows):
    return pd.DataFrame(list(rows), columns=list(COLUMNS))


def _cfg(**kw):
    base = dict(col_customer_number="Kunden-Nr.", col_name="Name", col_email="E-Mail",
                col_vat_id="USt-IdNr.", col_buyer_reference="Leitweg-ID",
                col_einvoice_format="Rechnungsformat", col_is_business="Unternehmer")
    base.update(kw)
    return CustomerImportConfig(**base)


def _set_country(app, code):
    with app.test_request_context():
        AppSetting.set("org.country", code)
        db.session.commit()


class TestCustomerWizard:
    def test_columns_are_suggested(self, app):
        cfg = suggest_config(list(COLUMNS))
        assert cfg.col_vat_id == "USt-IdNr."
        assert cfg.col_buyer_reference == "Leitweg-ID"
        assert cfg.col_einvoice_format == "Rechnungsformat"
        assert cfg.col_is_business == "Unternehmer"

    def test_a_guid_or_private_column_is_not_taken_for_a_vat_id(self, app):
        cfg = suggest_config(["Kunden-Nr.", "Name", "Kontakt-GUID", "Privatkunde"])
        assert cfg.col_vat_id == ""

    def test_config_roundtrips_through_the_session(self, app):
        cfg = _cfg()
        restored = CustomerImportConfig.from_dict(cfg.to_dict())
        assert restored == cfg
        form = ImmutableMultiDict({"col_vat_id": "USt-IdNr.", "col_is_business": "Unternehmer"})
        parsed = CustomerImportConfig.from_form(form)
        assert parsed.col_vat_id == "USt-IdNr." and parsed.col_is_business == "Unternehmer"

    def test_preview_normalises_and_warns(self, app):
        df = _df(
            ("1", "Gemeinde A", "", "de 123 456 789", LEITWEG, "X-Rechnung", "ja"),
            ("2", "Firma B", "", "123456789", "04011000-1234512345-07", "ZUGFeRD", "nein"),
        )
        first, second = build_preview_rows(df, _cfg())
        assert first.fields["vat_id"] == "DE123456789"
        assert first.fields["buyer_reference"] == LEITWEG
        assert first.fields["einvoice_format"] == "xrechnung"
        assert first.fields["is_business"] == "1"
        assert first.warnings == []
        # Ungültiges entfällt mit je einer Warnung, der Rest der Zeile bleibt.
        assert second.fields["vat_id"] == "" and second.fields["buyer_reference"] == ""
        assert second.fields["einvoice_format"] == "" and second.fields["is_business"] == ""
        assert len(second.warnings) == 2
        assert second.fields["name"] == "Firma B"

    def test_commit_creates_a_government_customer(self, app):
        df = _df(("101", "Gemeinde Testdorf", "", "", LEITWEG, "XRechnung", ""))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        stats = commit(rows, cfg)
        assert stats.created == 1 and stats.warnings == 0
        customer = Customer.query.filter_by(customer_number=101).one()
        assert customer.einvoice_format == "xrechnung"
        assert customer.buyer_reference == LEITWEG
        assert customer.vat_id is None and customer.is_business is False

    def test_business_switches_on_email_in_germany(self, app):
        _set_country(app, "DE")
        df = _df(("102", "Müller Bau GmbH", "m@mueller.example", "de123456789", "", "", "ja"))
        cfg = _cfg()
        with app.test_request_context():
            stats = commit(build_preview_rows(df, cfg), cfg)
        assert stats.created == 1
        customer = Customer.query.filter_by(customer_number=102).one()
        assert customer.is_business and customer.vat_id == "DE123456789"
        assert customer.wants_email
        (log,) = CustomerEmailConsentLog.query.filter_by(customer_id=customer.id).all()
        assert log.action == CustomerEmailConsentLog.EINVOICE_B2B

    def test_business_does_not_touch_consent_in_austria(self, app):
        _set_country(app, "AT")
        df = _df(("103", "Müller Bau GmbH", "m@mueller.example", "", "", "", "ja"))
        cfg = _cfg()
        with app.test_request_context():
            commit(build_preview_rows(df, cfg), cfg)
        customer = Customer.query.filter_by(customer_number=103).one()
        assert customer.is_business and not customer.rechnung_per_email
        assert CustomerEmailConsentLog.query.count() == 0

    def test_update_only_touches_mapped_columns(self, app):
        existing = Customer(name="Gemeinde Alt", customer_number=104, vat_id="DE111111111",
                            buyer_reference="alt", einvoice_format="xrechnung", is_business=True)
        db.session.add(existing)
        db.session.commit()
        # Nur die Leitweg-Spalte ist gemappt — USt-IdNr., Format und Kennzeichen bleiben stehen.
        df = pd.DataFrame([("104", "Gemeinde Alt", LEITWEG)],
                          columns=["Kunden-Nr.", "Name", "Leitweg-ID"])
        cfg = CustomerImportConfig(col_customer_number="Kunden-Nr.", col_name="Name",
                                   col_buyer_reference="Leitweg-ID", duplicate_mode="update")
        with app.test_request_context():
            commit(build_preview_rows(df, cfg), cfg)
        db.session.refresh(existing)
        assert existing.buyer_reference == LEITWEG
        assert existing.vat_id == "DE111111111"
        assert existing.einvoice_format == "xrechnung" and existing.is_business is True

    def test_a_mapped_empty_cell_clears_the_value(self, app):
        existing = Customer(name="Gemeinde Alt", customer_number=105, vat_id="DE111111111",
                            einvoice_format="xrechnung")
        db.session.add(existing)
        db.session.commit()
        df = _df(("105", "Gemeinde Alt", "", "", "", "", ""))
        cfg = _cfg(duplicate_mode="update")
        with app.test_request_context():
            commit(build_preview_rows(df, cfg), cfg)
        db.session.refresh(existing)
        assert existing.vat_id is None and existing.einvoice_format is None

    def test_an_invalid_value_typed_in_the_preview_is_dropped_with_a_warning(self, app):
        df = _df(("106", "Firma", "", "DE123456789", "", "", ""))
        cfg = _cfg()
        rows = build_preview_rows(df, cfg)
        rows = apply_edits(ImmutableMultiDict({"rows[0][vat_id]": "12345"}), rows)
        stats = commit(rows, cfg)
        assert Customer.query.filter_by(customer_number=106).one().vat_id is None
        assert stats.warnings == 1


# ---------------------------------------------------------------------------
# Stammdaten-Import (app/import_csv)
# ---------------------------------------------------------------------------

COLS = {
    "customer_number": "Kunden-Nr.", "customer_name": "Name", "property_name": "Objekt",
    "meter_number": "Zählernummer", "email": "E-Mail",
}
EI_COLS = {**COLS, "vat_id": "USt-IdNr.", "buyer_reference": "Leitweg-ID",
           "einvoice_format": "Rechnungsformat", "is_business": "Unternehmer"}


def _run(rows, cols, mode="skip"):
    return _run_import(pd.DataFrame(rows).fillna(""), cols, mode, dry_run=False)


class TestMasterImport:
    def test_columns_are_suggested(self, app):
        suggestions = _build_suggestions(["Kunden-Nr.", "Name", "USt-IdNr.", "Leitweg-ID",
                                          "Rechnungsformat", "Unternehmer", "Kontakt-GUID"])
        assert suggestions["vat_id"] == "USt-IdNr."
        assert suggestions["buyer_reference"] == "Leitweg-ID"
        assert suggestions["einvoice_format"] == "Rechnungsformat"
        assert suggestions["is_business"] == "Unternehmer"
        assert _build_suggestions(["Kunden-Nr.", "Kontakt-GUID"])["vat_id"] == ""

    def test_new_customer_gets_the_einvoice_fields(self, app):
        _set_country(app, "DE")
        with app.test_request_context():
            results = _run([{
                "Kunden-Nr.": "200", "Name": "Müller Bau GmbH", "Objekt": "Haus A",
                "Zählernummer": "Z1", "E-Mail": "m@mueller.example", "USt-IdNr.": "de 123 456 789",
                "Leitweg-ID": LEITWEG, "Rechnungsformat": "XRechnung", "Unternehmer": "ja",
            }], EI_COLS)
        assert results["customers_created"] == 1 and results["warnings"] == []
        customer = Customer.query.filter_by(customer_number=200).one()
        assert customer.vat_id == "DE123456789"
        assert customer.buyer_reference == LEITWEG
        assert customer.einvoice_format == "xrechnung"
        assert customer.is_business and customer.wants_email

    def test_invalid_values_are_reported_per_line(self, app):
        with app.test_request_context():
            results = _run([{
                "Kunden-Nr.": "201", "Name": "Firma", "Objekt": "Haus B", "Zählernummer": "Z2",
                "USt-IdNr.": "123", "Leitweg-ID": "04011000-1234512345-07",
            }], EI_COLS)
        assert len(results["warnings"]) == 2
        assert all(w.startswith("Zeile 2:") for w in results["warnings"])
        customer = Customer.query.filter_by(customer_number=201).one()
        assert customer.vat_id is None and customer.buyer_reference is None

    def test_overwrite_without_the_columns_keeps_existing_values(self, app):
        existing = Customer(name="Gemeinde", customer_number=202, vat_id="DE111111111",
                            buyer_reference=LEITWEG, einvoice_format="xrechnung", is_business=True)
        db.session.add(existing)
        db.session.commit()
        with app.test_request_context():
            _run([{"Kunden-Nr.": "202", "Name": "Gemeinde", "Objekt": "Haus C",
                   "Zählernummer": "Z3"}], COLS, mode="overwrite")
        db.session.refresh(existing)
        assert existing.vat_id == "DE111111111" and existing.buyer_reference == LEITWEG
        assert existing.einvoice_format == "xrechnung" and existing.is_business

    def test_overwrite_with_the_columns_updates_them(self, app):
        existing = Customer(name="Gemeinde", customer_number=203, vat_id="DE111111111")
        db.session.add(existing)
        db.session.commit()
        with app.test_request_context():
            _run([{"Kunden-Nr.": "203", "Name": "Gemeinde", "Objekt": "Haus D", "Zählernummer": "Z4",
                   "USt-IdNr.": "DE222222222", "Leitweg-ID": LEITWEG}], EI_COLS, mode="overwrite")
        db.session.refresh(existing)
        assert existing.vat_id == "DE222222222" and existing.buyer_reference == LEITWEG
