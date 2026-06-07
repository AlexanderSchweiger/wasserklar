"""Unit-Tests fuer den gemeinsamen Namens-/Typ-Resolver der Import-Wizards.

Pure Funktionen ohne DB: ``resolve_contact_name``, ``parse_is_company``,
``normalize_salutation`` aus ``app.imports.common``.
"""
from app.imports.common import (
    resolve_contact_name,
    parse_is_company,
    normalize_salutation,
    salutation_is_company,
)


class TestParseIsCompany:
    def test_empty_is_none(self):
        assert parse_is_company("") is None
        assert parse_is_company("   ") is None

    def test_person_words(self):
        for v in ("Person", "privat", "Privatperson", "nein", "N", "0"):
            assert parse_is_company(v) is False

    def test_company_words(self):
        for v in ("Firma", "Fa.", "Unternehmen", "ja", "X", "1"):
            assert parse_is_company(v) is True

    def test_legal_form_token(self):
        assert parse_is_company("Wasser GmbH") is True
        assert parse_is_company("Muster OG") is True

    def test_unknown_is_none(self):
        assert parse_is_company("Mustermann") is None


class TestNormalizeSalutation:
    def test_known(self):
        assert normalize_salutation("Herr") == "Herr"
        assert normalize_salutation("frau") == "Frau"
        assert normalize_salutation("Familie") == "Familie"
        assert normalize_salutation("Fam.") == "Familie"

    def test_fa_is_not_familie(self):
        # "Fa." ist Firma, nicht Familie.
        assert normalize_salutation("Fa.") == ""
        assert salutation_is_company("Fa.") is True

    def test_unknown_empty(self):
        assert normalize_salutation("Dr.") == ""
        assert normalize_salutation("") == ""


class TestResolveContactName:
    def test_split_person(self):
        r = resolve_contact_name(last="Mustermann", first="Max", salutation="Herr")
        assert r == {
            "is_company": False, "name": "Mustermann Max",
            "salutation": "Herr", "first_name": "Max", "last_name": "Mustermann",
        }

    def test_combined_only_to_last(self):
        r = resolve_contact_name(combined="Nur Kombiniert")
        assert r["is_company"] is False
        assert r["last_name"] == "Nur Kombiniert"
        assert r["first_name"] == ""
        assert r["name"] == "Nur Kombiniert"

    def test_company_clears_person_fields(self):
        r = resolve_contact_name(combined="Wasser GmbH", company="Firma")
        assert r["is_company"] is True
        assert r["name"] == "Wasser GmbH"
        assert r["first_name"] == "" and r["last_name"] == "" and r["salutation"] == ""

    def test_anrede_firma_implies_company(self):
        r = resolve_contact_name(combined="Wasser GmbH", salutation="Firma")
        assert r["is_company"] is True

    def test_familie_has_no_first_name(self):
        r = resolve_contact_name(last="Mustermann", first="Max", salutation="Familie")
        assert r["salutation"] == "Familie"
        assert r["first_name"] == ""
        assert r["name"] == "Mustermann"

    def test_company_name_from_split_columns(self):
        # Firmenname in einer Nachname-Spalte, Typ separat.
        r = resolve_contact_name(last="Wasser GmbH", company="Firma")
        assert r["is_company"] is True
        assert r["name"] == "Wasser GmbH"

    def test_explicit_person_overrides_legal_token(self):
        # Eigene Typ-Spalte "Person" schlaegt die Rechtsform-Heuristik im Namen.
        r = resolve_contact_name(last="Mustermann", company="Person")
        assert r["is_company"] is False
        assert r["last_name"] == "Mustermann"
