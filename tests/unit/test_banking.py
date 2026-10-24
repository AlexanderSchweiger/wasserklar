"""Bankkennungen (app/banking.py) und TARGET-Kalender des Lastschrift-Einzugs."""
from datetime import date

import pytest

from app.banking import (
    bic_valid, creditor_id_valid, format_iban, iban_is_sepa, iban_valid, mask_iban,
    normalize_iban, sepa_id_valid, sepa_text,
)
from app.direct_debit.services import (
    add_target_days, earliest_collection_date, is_target_day, next_collection_day,
    target_holidays,
)


class TestIban:
    @pytest.mark.parametrize("value", [
        "AT61 1904 3002 3457 3201",
        "at611904300234573201",
        "DE89 3704 0044 0532 0130 00",
        "CH93 0076 2011 6238 5295 7",
    ])
    def test_valid(self, value):
        assert iban_valid(value)

    @pytest.mark.parametrize("value", [
        "AT61 1904 3002 3457 3202",        # Pruefziffer
        "AT61 1904 3002 3457 320",         # Laenge AT = 20
        "DE89 3704 0044 0532 0130 0000",   # Laenge DE = 22
        "1234", "", None,
    ])
    def test_invalid(self, value):
        assert not iban_valid(value)

    def test_format_and_mask(self):
        assert normalize_iban(" at61 1904 3002 3457 3201 ") == "AT611904300234573201"
        assert format_iban("AT611904300234573201") == "AT61 1904 3002 3457 3201"
        assert mask_iban("AT611904300234573201") == "AT61 •••• •••• •••• 3201"
        assert mask_iban("DE89370400440532013000").endswith("30 00")
        assert format_iban(None) == "" and mask_iban("") == ""

    def test_sepa_area(self):
        assert iban_is_sepa("AT611904300234573201")
        assert iban_is_sepa("CH9300762011623852957")
        assert not iban_is_sepa("US64SVBKUS6S3300958879")


class TestBicAndCreditorId:
    def test_bic(self):
        assert bic_valid("RZOOAT2L") and bic_valid("rzooat2lxxx")
        assert not bic_valid("RZOOAT2") and not bic_valid("RZOOAT2LXX")

    @pytest.mark.parametrize("value", [
        "DE98ZZZ09999999999",              # Beispiel der Deutschen Bundesbank
        "AT61ZZZ01234567890",
        "de98 zzz0 9999 9999 99",
    ])
    def test_creditor_id_valid(self, value):
        assert creditor_id_valid(value)

    @pytest.mark.parametrize("value", [
        "DE97ZZZ09999999999",              # Pruefziffer
        "AT61ZZZ0123456789",               # Laenge AT = 18
        "AT12ZZZ00000000001", "", "XYZ",
    ])
    def test_creditor_id_invalid(self, value):
        assert not creditor_id_valid(value)

    def test_business_code_does_not_count(self):
        # Die Geschaeftsbereichskennung (Stellen 5–7) geht nicht in die Pruefziffer ein.
        assert creditor_id_valid("DE98ABC09999999999")


class TestSepaText:
    def test_umlauts_and_specials(self):
        assert sepa_text("Müller & Söhne – Straße") == "Mueller + Soehne - Strasse"
        assert sepa_text("Café „Zur Quelle“") == "Cafe 'Zur Quelle'"

    def test_length(self):
        assert len(sepa_text("x" * 200, 70)) == 70

    def test_ids(self):
        assert sepa_id_valid("WG-10023") and sepa_id_valid("M/2019/7")
        assert not sepa_id_valid("WG 10023")          # Leerzeichen
        assert not sepa_id_valid("/WG1") and not sepa_id_valid("WG//1")
        assert not sepa_id_valid("x" * 36) and not sepa_id_valid("")


class TestTargetCalendar:
    def test_holidays_2026(self):
        assert target_holidays(2026) == {
            date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6),
            date(2026, 5, 1), date(2026, 12, 25), date(2026, 12, 26),
        }

    def test_weekend_and_holiday(self):
        assert not is_target_day(date(2026, 10, 24))      # Samstag
        assert not is_target_day(date(2026, 4, 3))        # Karfreitag
        assert is_target_day(date(2026, 10, 23))

    def test_lead_time(self):
        # Freitag + 2 Bankarbeitstage = Dienstag
        assert add_target_days(date(2026, 10, 23), 2) == date(2026, 10, 27)
        assert earliest_collection_date(date(2026, 10, 23)) == date(2026, 10, 27)
        # Gruendonnerstag + 2 → Karfreitag + Ostermontag fallen aus
        assert add_target_days(date(2026, 4, 2), 2) == date(2026, 4, 8)

    def test_next_collection_day(self):
        assert next_collection_day(date(2026, 10, 24)) == date(2026, 10, 26)
        assert next_collection_day(date(2026, 10, 26)) == date(2026, 10, 26)
