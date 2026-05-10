"""Unit-Tests fuer die reinen Funktionen im Ablesungs-Import-Service.

Reine Funktionen ohne DB-Zugriff: Format-Detection, Number/Date-Parser,
MappingConfig-Validation. Schnell, isoliert, deterministisch.
"""
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.meters.import_service import (
    CONSUMPTION_TOLERANCE,
    MappingConfig,
    _check_mismatch,
    detect_date_format,
    detect_number_format,
    format_value_de,
    parse_date,
    parse_number,
    parse_year,
    status_badge,
    status_row_class,
)


# ---------------------------------------------------------------------------
# detect_number_format
# ---------------------------------------------------------------------------

class TestDetectNumberFormat:
    def test_at_de_with_thousand_separator(self):
        s = pd.Series(["1.234,56", "987,00", "1.000.000,50"])
        assert detect_number_format(s) == "at_de"

    def test_at_de_decimal_only(self):
        assert detect_number_format(pd.Series(["12,5", "8,0", "100,99"])) == "at_de"

    def test_us_with_thousand_separator(self):
        assert detect_number_format(pd.Series(["1,234.56", "987.00"])) == "us"

    def test_plain_integers(self):
        assert detect_number_format(pd.Series(["1234", "987", "5500"])) == "plain"

    def test_plain_decimal_with_dot(self):
        assert detect_number_format(pd.Series(["12.5", "8.0", "100.99"])) == "plain"

    def test_thousand_dot_only(self):
        # 1.234 ohne Dezimalstellen => Tausenderpunkt-Heuristik => 'at_de'
        assert detect_number_format(pd.Series(["1.234", "5.678"])) == "at_de"

    def test_empty_series(self):
        assert detect_number_format(pd.Series([])) == "unknown"

    def test_only_nan_none(self):
        assert detect_number_format(pd.Series([None, "nan", ""])) == "unknown"

    def test_mixed_at_de_majority(self):
        # 2x at_de, 1x us -> at_de gewinnt
        s = pd.Series(["1.234,56", "987,00", "1,000.50"])
        assert detect_number_format(s) == "at_de"


# ---------------------------------------------------------------------------
# detect_date_format
# ---------------------------------------------------------------------------

class TestDetectDateFormat:
    def test_iso(self):
        assert detect_date_format(pd.Series(["2024-12-31", "2024-01-15"])) == "iso"

    def test_de_dot(self):
        assert detect_date_format(pd.Series(["31.12.2024", "15.01.2024"])) == "de"

    def test_de_slash(self):
        # 31/12/2024 ist eindeutig DE (zweite Komp <=12, erste >12)
        assert detect_date_format(pd.Series(["31/12/2024"])) == "de"

    def test_us_unambiguous(self):
        # 12/31/2024 ist eindeutig US (zweite Komp >12)
        assert detect_date_format(pd.Series(["12/31/2024"])) == "us"

    def test_ambiguous_defaults_to_de(self):
        # 01/02/2024 koennte beides sein -> Default DE (AT-Lokal)
        assert detect_date_format(pd.Series(["01/02/2024", "03/04/2024"])) == "de"

    def test_excel_timestamp(self):
        assert detect_date_format(pd.Series([pd.Timestamp("2024-12-31")])) == "excel_ts"

    def test_python_date_object(self):
        assert detect_date_format(pd.Series([date(2024, 12, 31)])) == "excel_ts"

    def test_empty_series(self):
        assert detect_date_format(pd.Series([])) == "unknown"

    def test_unknown_format(self):
        assert detect_date_format(pd.Series(["foo", "bar"])) == "unknown"

    def test_us_majority_wins(self):
        # 2x eindeutig US, 1x ambig -> US
        s = pd.Series(["12/31/2024", "12/15/2024", "01/02/2024"])
        assert detect_date_format(s) == "us"


# ---------------------------------------------------------------------------
# parse_number
# ---------------------------------------------------------------------------

class TestParseNumber:
    def test_at_de_with_thousand(self):
        assert parse_number("1.234,56", "at_de") == Decimal("1234.56")

    def test_at_de_decimal_only(self):
        assert parse_number("987,5", "at_de") == Decimal("987.5")

    def test_at_de_integer(self):
        assert parse_number("5500", "at_de") == Decimal("5500")

    def test_us_with_thousand(self):
        assert parse_number("1,234.56", "us") == Decimal("1234.56")

    def test_us_decimal_only(self):
        assert parse_number("987.50", "us") == Decimal("987.50")

    def test_plain_integer(self):
        assert parse_number("1234", "plain") == Decimal("1234")

    def test_plain_decimal(self):
        # plain akzeptiert Punkt UND Komma als Dezimaltrennzeichen
        assert parse_number("12.5", "plain") == Decimal("12.5")
        assert parse_number("12,5", "plain") == Decimal("12.5")

    def test_auto_at_de(self):
        assert parse_number("1.234,56", "auto") == Decimal("1234.56")

    def test_auto_plain(self):
        assert parse_number("1234", "auto") == Decimal("1234")

    def test_garbage_returns_none(self):
        assert parse_number("xxx", "auto") is None
        assert parse_number("foo,bar", "at_de") is None

    def test_empty_returns_none(self):
        assert parse_number("", "auto") is None
        assert parse_number("   ", "auto") is None

    def test_nan_string_returns_none(self):
        assert parse_number("nan", "auto") is None
        assert parse_number("None", "auto") is None

    def test_strips_whitespace_inside(self):
        # 1 234,56 (Leerzeichen als Tausender) sollte at_de funktionieren
        assert parse_number("1 234,56", "at_de") == Decimal("1234.56")


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_iso(self):
        assert parse_date("2024-12-31", "iso") == date(2024, 12, 31)

    def test_de_dot(self):
        assert parse_date("31.12.2024", "de") == date(2024, 12, 31)

    def test_de_slash(self):
        assert parse_date("31/12/2024", "de") == date(2024, 12, 31)

    def test_de_two_digit_year(self):
        assert parse_date("31.12.24", "de") == date(2024, 12, 31)

    def test_us(self):
        assert parse_date("12/31/2024", "us") == date(2024, 12, 31)

    def test_auto_picks_iso(self):
        assert parse_date("2024-12-31", "auto") == date(2024, 12, 31)

    def test_auto_picks_de(self):
        assert parse_date("31.12.2024", "auto") == date(2024, 12, 31)

    def test_pd_timestamp_passthrough(self):
        ts = pd.Timestamp("2024-12-31")
        assert parse_date(ts, "any") == date(2024, 12, 31)

    def test_python_date_passthrough(self):
        d = date(2024, 12, 31)
        assert parse_date(d, "iso") == d

    def test_python_datetime_strips_time(self):
        dt = datetime(2024, 12, 31, 14, 30)
        assert parse_date(dt, "iso") == date(2024, 12, 31)

    def test_garbage_returns_none(self):
        assert parse_date("not a date", "iso") is None
        assert parse_date("32.13.2024", "de") is None

    def test_empty_returns_none(self):
        assert parse_date("", "auto") is None
        assert parse_date(None, "auto") is None
        assert parse_date("   ", "auto") is None


# ---------------------------------------------------------------------------
# parse_year
# ---------------------------------------------------------------------------

class TestParseYear:
    def test_int_string(self):
        assert parse_year("2024", 2025) == 2024

    def test_float_string(self):
        # pandas read_excel mit dtype=str kann z.B. "2024.0" liefern
        assert parse_year("2024.0", 2025) == 2024

    def test_int_value(self):
        assert parse_year(2024, 2025) == 2024

    def test_empty_uses_default(self):
        assert parse_year("", 2025) == 2025
        assert parse_year(None, 2025) == 2025

    def test_garbage_uses_default(self):
        assert parse_year("xxx", 2025) == 2025

    def test_no_default_returns_none(self):
        assert parse_year("", 0) is None


# ---------------------------------------------------------------------------
# MappingConfig
# ---------------------------------------------------------------------------

class TestMappingConfigFromForm:
    def test_minimum_form(self):
        form = ImmutableMultiDict([])
        cfg = MappingConfig.from_form(form, default_year_fallback=2024)
        assert cfg.mode == "meter_number"
        assert cfg.duplicate_mode == "update"
        assert cfg.value_format == "auto"
        assert cfg.date_format == "auto"
        assert cfg.default_year == 2024

    def test_full_form(self):
        form = ImmutableMultiDict([
            ("mode", "customer_name"),
            ("col_lookup", "Name"),
            ("col_value", "Stand"),
            ("col_date", "Datum"),
            ("col_year", "Jahr"),
            ("default_year", "2023"),
            ("duplicate_mode", "skip"),
            ("value_format", "us"),
            ("date_format", "iso"),
        ])
        cfg = MappingConfig.from_form(form, default_year_fallback=2024)
        assert cfg.mode == "customer_name"
        assert cfg.col_lookup == "Name"
        assert cfg.col_value == "Stand"
        assert cfg.default_year == 2023
        assert cfg.duplicate_mode == "skip"
        assert cfg.value_format == "us"
        assert cfg.date_format == "iso"

    def test_invalid_mode_falls_back(self):
        form = ImmutableMultiDict([("mode", "evil")])
        cfg = MappingConfig.from_form(form, default_year_fallback=2024)
        assert cfg.mode == "meter_number"

    def test_invalid_duplicate_mode_falls_back(self):
        form = ImmutableMultiDict([("duplicate_mode", "evil")])
        cfg = MappingConfig.from_form(form, default_year_fallback=2024)
        assert cfg.duplicate_mode == "update"

    def test_invalid_value_format_falls_back(self):
        form = ImmutableMultiDict([("value_format", "evil")])
        cfg = MappingConfig.from_form(form, default_year_fallback=2024)
        assert cfg.value_format == "auto"

    def test_invalid_year_falls_back(self):
        form = ImmutableMultiDict([("default_year", "abc")])
        cfg = MappingConfig.from_form(form, default_year_fallback=2024)
        assert cfg.default_year == 2024

    def test_to_dict_from_dict_roundtrip(self):
        cfg1 = MappingConfig(
            mode="customer_number",
            col_lookup="Kundennr",
            col_value="Stand",
            col_date="Datum",
            col_year="Jahr",
            col_consumption="Verbrauch",
            default_year=2024,
            duplicate_mode="skip",
            value_format="at_de",
            date_format="de",
        )
        cfg2 = MappingConfig.from_dict(cfg1.to_dict())
        assert cfg2 == cfg1

    def test_col_consumption_default_empty(self):
        cfg = MappingConfig.from_form(ImmutableMultiDict([]), default_year_fallback=2024)
        assert cfg.col_consumption == ""

    def test_col_consumption_from_form(self):
        cfg = MappingConfig.from_form(
            ImmutableMultiDict([("col_consumption", "Verbrauch m3")]),
            default_year_fallback=2024,
        )
        assert cfg.col_consumption == "Verbrauch m3"


# ---------------------------------------------------------------------------
# _check_mismatch (Verbrauchs-Vergleich mit Toleranz)
# ---------------------------------------------------------------------------

class TestCheckMismatch:
    def test_both_none_no_mismatch(self):
        assert _check_mismatch(None, None) is False

    def test_only_computed_no_mismatch(self):
        assert _check_mismatch(Decimal("100"), None) is False

    def test_only_imported_no_mismatch(self):
        assert _check_mismatch(None, Decimal("100")) is False

    def test_exact_match(self):
        assert _check_mismatch(Decimal("100"), Decimal("100")) is False

    def test_within_tolerance(self):
        # Toleranz ist 0.5 -- Differenz von 0.4 ist OK
        assert _check_mismatch(Decimal("100.0"), Decimal("100.4")) is False
        assert _check_mismatch(Decimal("100.4"), Decimal("100.0")) is False

    def test_exactly_at_tolerance(self):
        # Diff genau == Toleranz -> kein Mismatch (strikt groesser)
        assert _check_mismatch(Decimal("100.0"),
                               Decimal("100.0") + CONSUMPTION_TOLERANCE) is False

    def test_above_tolerance(self):
        # Differenz von 1.0 ist deutlich -> Mismatch
        assert _check_mismatch(Decimal("100.0"), Decimal("101.0")) is True

    def test_negative_difference(self):
        # Computed kleiner als Imported -> abs() greift
        assert _check_mismatch(Decimal("50"), Decimal("80")) is True


# ---------------------------------------------------------------------------
# Template-Helpers
# ---------------------------------------------------------------------------

class TestStatusHelpers:
    def test_status_row_class_ok(self):
        assert status_row_class("ok") == "table-success"

    def test_status_row_class_ok_preferred_main(self):
        assert status_row_class("ok_preferred_main") == "table-success"

    def test_status_row_class_ambiguous(self):
        assert status_row_class("ambiguous") == "table-warning"

    def test_status_row_class_not_found(self):
        assert status_row_class("not_found") == "table-danger"

    def test_status_row_class_parse_error(self):
        assert status_row_class("parse_error") == "table-danger"

    def test_status_row_class_unknown(self):
        assert status_row_class("xxx") == ""

    def test_status_badge_returns_label_and_class(self):
        label, css = status_badge("ok")
        assert label == "OK"
        assert "bg-success" in css

    def test_status_badge_ambiguous(self):
        label, css = status_badge("ambiguous")
        assert "Mehrdeutig" in label
        assert "bg-warning" in css


class TestFormatValueDe:
    def test_decimal(self):
        assert format_value_de(Decimal("1234.56")) == "1234,56"

    def test_integer_decimal(self):
        assert format_value_de(Decimal("100")) == "100"

    def test_none(self):
        assert format_value_de(None) == ""

    def test_decimal_with_trailing_zeros(self):
        # Decimal("100.000") sollte sauber formatiert werden
        result = format_value_de(Decimal("100.000"))
        # Akzeptiere "100" oder "100,0..." -- normalize() entfernt Trailing-Zeros
        assert result.startswith("100")
