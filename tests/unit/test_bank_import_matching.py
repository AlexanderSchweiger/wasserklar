"""Unit-Tests fuer den Invoice-Number-Regex in bank_import/matching.py.

Wir testen primaer das Regex-Pattern, das die Rechnungsnummer im
Verwendungszweck findet — die DB-touched Pfade brauchen einen vollen
Setup und sind in einer separaten Integration-Test-Datei besser
aufgehoben.
"""

from __future__ import annotations

import pytest

from app.bank_import.matching import (
    INVOICE_NR_RE,
    _find_invoice_number,
    _name_search_tokens,
    _name_tokens_match,
    _normalize_name_tokens,
)


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


class TestFindInvoiceNumber:
    """_find_invoice_number deckt zusaetzlich die von George/OFX zerhackten
    Verwendungszwecke ab (Fixed-Width-Segmente mit Leerzeichen verbunden)."""

    @pytest.mark.parametrize("text,expected", [
        # Sauber -> strikter Match
        ("2026-00200", "2026-00200"),
        ("RENR 2026-00009", "2026-00009"),
        ("Re. 2026-00047", "2026-00047"),
        ("Rechnung 2026-00026 Muskanitzen 16 697m3", "2026-00026"),
        # Zerhackt -> Despace-Fallback
        ("WASSERVERBRAUCH 24/25 RECHNUNGSNR 2 026-00206 7.6.2026", "2026-00206"),
        ("2026 00046", "2026-00046"),
        ("2026-00123 Treffling 67 Seifried El eonora", "2026-00123"),
    ])
    def test_extracts(self, text, expected):
        assert _find_invoice_number(text) == expected

    @pytest.mark.parametrize("text", [
        "Wasserzins",
        "SEPA-Gutschrift Dr. Albrecht Rothacher NOTPROVIDED",
        "& 2026'??DZZS",          # kein vollstaendiges NNNNN
        "Dauerauftrag Wassermeister-Entschaedigung",
    ])
    def test_no_false_positive(self, text):
        assert _find_invoice_number(text) is None


class TestNormalizeNameTokens:
    @pytest.mark.parametrize("name,expected", [
        ("Thomas Petutschnig", {"thomas", "petutschnig"}),
        ("DDipl.-Ing. Axel Thomaschütz", {"axel", "thomaschutz"}),   # Titel weg, Umlaut normalisiert
        ("Dr. Iris Gorgasser", {"iris", "gorgasser"}),
        ("Hans oder Friede Schneeweiss", {"hans", "friede", "schneeweiss"}),  # Verbinder "oder" weg
        ("Ines Strauß", {"ines", "strauss"}),                        # ß -> ss
        ("", set()),
        (None, set()),
    ])
    def test_tokens(self, name, expected):
        assert _normalize_name_tokens(name) == expected

    def test_order_independent_match(self):
        bank = _normalize_name_tokens("Thomas Petutschnig")
        cust = _normalize_name_tokens("Petutschnig Thomas")
        assert _name_tokens_match(bank, cust)

    def test_truncation_tolerated_for_long_names(self):
        # OFX kuerzt auf 32 Zeichen: "Christin" statt "Christine".
        bank = _normalize_name_tokens("Albrecht Rothacher Christin")
        cust = _normalize_name_tokens("Rothacher Albrecht Christine")
        assert _name_tokens_match(bank, cust)

    def test_two_token_names_require_exact(self):
        # Bei zweiteiligen Namen darf NICHT toleriert werden (nur 1 gemeinsames Token).
        assert not _name_tokens_match({"stefan", "egger"}, {"stefan", "maier"})

    def test_single_common_token_no_match(self):
        assert not _name_tokens_match({"thomas", "mueller"}, {"thomas", "schmidt"})


class TestNameSearchTokens:
    def test_keeps_umlauts_for_ilike(self):
        # Akzente bleiben erhalten, damit ILIKE den DB-Namen "Türk" trifft.
        assert "türk" in _name_search_tokens("Thomas Türk")
        assert "schönlieb" in _name_search_tokens("Dr. Thomas Schönlieb")

    def test_drops_short_tokens_titles_connectors(self):
        toks = _name_search_tokens("Dr. Iris u. Max Gorgasser")
        assert "dr" not in toks and "u" not in toks
        assert set(toks) == {"iris", "max", "gorgasser"}
