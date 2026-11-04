"""``parse_customer_number``: only whole numbers in INTEGER range pass.

Regression for the prod 500 on ``/properties/import/preview`` (Postgres:
``invalid input syntax for type integer``) — a name in the customer-number
column must parse to ``None`` so it never reaches a query.
"""
import pytest

from app.imports.common import INT_COLUMN_MAX, parse_customer_number


@pytest.mark.parametrize("raw, expected", [
    ("123", 123),
    (" 123 ", 123),
    ("123.0", 123),
    ("123,0", 123),
    ("1", 1),
    (123, 123),
    (123.0, 123),
    (str(INT_COLUMN_MAX), INT_COLUMN_MAX),
])
def test_valid_numbers(raw, expected):
    assert parse_customer_number(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ",
    "Huber Maria", "Müller, Josef", "K-123", "123a",
    "12.7", "12,5", "1.234",      # fractions / thousands separator: never guess
    "inf", "-inf", "nan", "1e12", "1E3",
    "0", "-5", "+5",
    str(INT_COLUMN_MAX + 1), "99999999999",
    "²",                          # str.isdigit() is True, int() fails
])
def test_invalid_values(raw):
    assert parse_customer_number(raw) is None
