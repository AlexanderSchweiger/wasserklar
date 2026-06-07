"""Unit-Tests fuer den Straße/Hausnummer-Splitter der Import-Wizards.

Pure Funktion ohne DB: ``split_street_number`` aus ``app.imports.common``.
"""
import pytest

from app.imports.common import split_street_number


class TestSplitStreetNumber:
    @pytest.mark.parametrize("combined, street, number", [
        ("Hauptstraße 12", "Hauptstraße", "12"),
        ("Hauptstraße 12a", "Hauptstraße", "12a"),
        ("Hauptstraße 12 a", "Hauptstraße", "12a"),     # Leerzeichen wird entfernt
        ("Hauptstraße 12/3", "Hauptstraße", "12/3"),
        ("Hauptplatz 1-3", "Hauptplatz", "1-3"),
        ("Am Bach 3", "Am Bach", "3"),
        ("Dr.-Karl-Renner-Straße 5", "Dr.-Karl-Renner-Straße", "5"),
        ("Untere Hauptstraße 1b", "Untere Hauptstraße", "1b"),
        ("Hauptstraße 5/2/14", "Hauptstraße", "5/2/14"),
        ("Hauptstraße 12,", "Hauptstraße", "12"),        # trailing comma
        ("Feldweg 7, ", "Feldweg", "7"),
    ])
    def test_splits_trailing_number(self, combined, street, number):
        assert split_street_number(combined) == (street, number)

    def test_no_number_kept_whole(self):
        assert split_street_number("Siedlung") == ("Siedlung", "")
        assert split_street_number("Am Anger") == ("Am Anger", "")

    def test_existing_house_number_wins(self):
        # Eigene Hausnummer gesetzt → Straße bleibt unangetastet.
        assert split_street_number("Hauptstraße 12", "5") == ("Hauptstraße 12", "5")
        assert split_street_number("Hauptstraße", "12a") == ("Hauptstraße", "12a")

    def test_pure_number_not_split(self):
        # Feld ohne Buchstaben im Straßenteil → nicht trennen.
        assert split_street_number("12") == ("12", "")
        assert split_street_number("3 5") == ("3 5", "")

    def test_empty(self):
        assert split_street_number("") == ("", "")
        assert split_street_number("  ", "") == ("", "")

    def test_whitespace_trimmed(self):
        assert split_street_number("  Hauptstraße 12  ") == ("Hauptstraße", "12")
