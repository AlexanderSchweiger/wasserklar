"""Model-Property-Tests fuer die Namens-Aufspaltung (oss-v1.21.0).

Prueft ``letter_name`` (Anschrift in Briefen/Rechnungen) und
``salutation_line`` (Anrede) ueber Personen (Herr/Frau/Familie/ohne Anrede),
Firmen und den Altbestands-Fallback auf das kombinierte ``name``.
"""
from app.models import Customer


def _c(**kw):
    # is_company-Default greift erst beim INSERT; un-added Instanzen haben None
    # (-> in den Properties falsy = Person), daher nur bei Firmen explizit setzen.
    return Customer(is_customer=True, **kw)


class TestLetterName:
    def test_person_first_last_is_vorname_nachname(self):
        c = _c(name="Mustermann Max", first_name="Max", last_name="Mustermann")
        assert c.letter_name == "Max Mustermann"

    def test_person_last_only(self):
        c = _c(name="Mustermann", last_name="Mustermann")
        assert c.letter_name == "Mustermann"

    def test_family_prefixes_familie(self):
        c = _c(name="Mustermann", salutation="Familie", last_name="Mustermann")
        assert c.letter_name == "Familie Mustermann"

    def test_company_uses_name(self):
        c = _c(name="Wasser GmbH", is_company=True)
        assert c.letter_name == "Wasser GmbH"

    def test_legacy_falls_back_to_combined_name(self):
        # Altbestand/Quick-Create: nur kombinierter Name, keine Einzelfelder.
        c = _c(name="Mustermann Max")
        assert c.letter_name == "Mustermann Max"


class TestSalutationLine:
    def test_herr_surname_only(self):
        c = _c(name="Mustermann Max", salutation="Herr",
               first_name="Max", last_name="Mustermann")
        assert c.salutation_line == "Sehr geehrter Herr Mustermann"

    def test_frau_surname_only(self):
        c = _c(name="Musterfrau Eva", salutation="Frau",
               first_name="Eva", last_name="Musterfrau")
        assert c.salutation_line == "Sehr geehrte Frau Musterfrau"

    def test_familie(self):
        c = _c(name="Mustermann", salutation="Familie", last_name="Mustermann")
        assert c.salutation_line == "Sehr geehrte Familie Mustermann"

    def test_no_salutation_uses_full_name_gender_neutral(self):
        c = _c(name="Mustermann Max", first_name="Max", last_name="Mustermann")
        assert c.salutation_line == "Sehr geehrte/r Max Mustermann"

    def test_company_collective(self):
        c = _c(name="Wasser GmbH", is_company=True)
        assert c.salutation_line == "Sehr geehrte Damen und Herren"

    def test_legacy_falls_back_to_combined_name(self):
        c = _c(name="Mustermann Max")
        assert c.salutation_line == "Sehr geehrte/r Mustermann Max"
