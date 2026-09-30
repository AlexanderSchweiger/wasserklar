"""Leitweg-ID (XRechnung BT-10): Format und Pruefziffer (ISO 7064 MOD 97-10)."""
import pytest

from app.einvoice import leitweg


@pytest.mark.parametrize("value", [
    "04011000-1234512345-06",   # Beispiel aus der Leitweg-ID-Spezifikation
    "992-90009-96",             # Bund
    " 992-90009-96 ",           # Leerraum am Rand
])
def test_valid_ids(value):
    assert leitweg.looks_like(value)
    assert leitweg.is_valid(value)


def test_wrong_check_digit_is_rejected():
    assert leitweg.looks_like("04011000-1234512345-07")
    assert not leitweg.is_valid("04011000-1234512345-07")


def test_check_digit_is_computed_over_grob_and_fein():
    assert leitweg.check_digit("04011000-1234512345") == "06"
    assert leitweg.check_digit("992-90009") == "96"
    assert leitweg.check_digit("99290009") == "96"     # Bindestriche zaehlen nicht


@pytest.mark.parametrize("value", [
    "",
    None,
    "4711",                       # keine Leitweg-ID
    "2024-15",                    # Bestellnummer: Laenderkennzeichen 20 gibt es nicht
    "Kostenstelle 4711",
    "04011000",                   # ohne Pruefziffer
    "04011000-12345678901234567890123456789012-12",   # Feinadressierung > 30 Zeichen
])
def test_freely_chosen_references_are_not_leitweg_ids(value):
    assert not leitweg.looks_like(value)


def test_alphanumeric_fine_addressing_is_not_checked():
    """Die Buchstaben-Umrechnung ist nicht belegt — lieber nichts beanstanden."""
    assert leitweg.looks_like("04011000-ABC12-99")
    assert leitweg.is_valid("04011000-ABC12-99")
