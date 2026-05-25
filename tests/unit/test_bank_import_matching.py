"""Unit-Tests fuer den Invoice-Number-Regex in bank_import/matching.py.

Wir testen primaer das Regex-Pattern, das die Rechnungsnummer im
Verwendungszweck findet — die DB-touched Pfade brauchen einen vollen
Setup und sind in einer separaten Integration-Test-Datei besser
aufgehoben.
"""

from __future__ import annotations

import pytest

from app.bank_import.matching import INVOICE_NR_RE


class TestInvoiceNumberRegex:
    @pytest.mark.parametrize("text,expected", [
        ("Rg 2026-00042", "2026-00042"),
        ("Zahlung fuer Rechnung 2024-12345 vielen Dank", "2024-12345"),
        ("RE 2025-99999", "2025-99999"),
        ("2023-00001", "2023-00001"),
    ])
    def test_finds_valid_number(self, text, expected):
        m = INVOICE_NR_RE.search(text)
        assert m is not None
        assert m.group(1) == expected

    @pytest.mark.parametrize("text", [
        "keine Nummer hier",
        "abc 12-345",         # zu wenige Stellen
        "abc 2024-1234",      # 4-stellige Nummer (sollte 5 sein)
        "abc 2024-123456",    # 6-stellige Nummer (sollte 5 sein)
        "abc 24-00001",       # 2-stelliges Jahr
        "",
    ])
    def test_does_not_match_invalid(self, text):
        m = INVOICE_NR_RE.search(text)
        assert m is None

    def test_finds_first_when_multiple(self):
        text = "2024-00001 oder 2024-00002"
        m = INVOICE_NR_RE.search(text)
        assert m.group(1) == "2024-00001"
