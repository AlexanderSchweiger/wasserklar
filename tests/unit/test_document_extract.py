"""Belegablage Stufe 2: Text aus PDFs lesen und Angaben vorschlagen (app/documents/extract.py).

Die Fixtures in ``tests/fixtures/documents`` sind echte PDFs mit Textebene (siehe dortige README);
die Grenzfaelle laufen gegen kurze Texte, damit jede Regel einzeln benannt bleibt.
"""
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.documents import extract

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
TODAY = date(2026, 10, 3)


def fixture(name):
    return (FIXTURES / f"{name}.pdf").read_bytes()


def suggest_from(name, **kw):
    result = extract.extract_pdf_text(fixture(name))
    assert result.status == "ok", result
    return extract.suggest(result.text, today=TODAY, **kw)


def suggest_text(text, **kw):
    return extract.suggest(text, today=TODAY, **kw)


def blank_pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Text lesen
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_a_digital_pdf_yields_its_text(self):
        result = extract.extract_pdf_text(fixture("zeilen"))
        assert result.status == "ok" and result.pages == 1 and not result.truncated
        assert "Rechnungsnummer" in result.text and "Jänner" in result.text      # Umlaute bleiben erhalten

    def test_a_pdf_without_text_layer_is_empty(self):
        result = extract.extract_pdf_text(blank_pdf())
        assert (result.status, result.text) == ("empty", "")

    def test_garbage_is_an_error_not_an_exception(self):
        assert extract.extract_pdf_text(b"%PDF-1.7 kaputt").status == "error"
        assert extract.extract_pdf_text(b"").status == "error"

    def test_a_password_protected_pdf_is_an_error(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("geheim")
        buf = io.BytesIO()
        writer.write(buf)
        assert extract.extract_pdf_text(buf.getvalue()).status == "error"

    def test_only_the_first_pages_are_read(self):
        result = extract.extract_pdf_text(blank_pdf(pages=5), max_pages=2)
        assert result.pages == 2 and result.truncated

    def test_the_text_is_capped(self):
        result = extract.extract_pdf_text(fixture("tabellenkopf"), max_chars=100)
        assert result.status == "ok" and len(result.text) == 100 and result.truncated

    def test_the_time_budget_stops_reading(self):
        result = extract.extract_pdf_text(fixture("tabellenkopf"), budget=-1)
        assert result.pages == 0 and result.truncated


class TestCleanText:
    def test_control_characters_and_surrogates_are_removed(self):
        text = extract.clean_text("a\x00b\x07c \ud800 d\r\ne")
        assert text == "a b c  d\ne"
        text.encode("utf-8")                                   # kein UnicodeEncodeError beim Speichern

    def test_wide_gaps_shrink_but_columns_stay_apart(self):
        assert extract.clean_text("Summe            12,00") == "Summe  12,00"

    def test_blank_lines_collapse(self):
        assert extract.clean_text("a\n\n\n\n\nb\n") == "a\n\nb"


# ---------------------------------------------------------------------------
# Vorschlaege an echten Belegen
# ---------------------------------------------------------------------------

class TestFixtures:
    def test_header_row_layout(self):
        s = suggest_from("tabellenkopf", own_vat_ids=["ATU87654321"])
        assert s.kind == "invoice"
        assert s.number == "2026-0123"                         # Wert steht UNTER der Ueberschrift
        assert s.date == date(2026, 3, 14)                     # nicht Liefer- (10.03.) oder Faelligkeitsdatum
        assert s.amount == Decimal("1234.56")                  # nicht Skonto-Zahlbetrag, nicht Zeile „Gesamt“
        assert s.vat_ids == ["ATU12345678"]                    # eigene UID ist nicht der Lieferant
        assert s.ibans == ["AT109999900000067890"]

    def test_the_own_vat_id_is_only_dropped_when_known(self):
        s = suggest_from("tabellenkopf")
        assert s.vat_ids == ["ATU12345678", "ATU87654321"]

    def test_key_value_layout_and_austrian_month_name(self):
        s = suggest_from("zeilen")
        assert (s.kind, s.number, s.date, s.amount) == ("invoice", "RE-2026-0042", date(2026, 1, 12), Decimal("98.40"))
        assert s.vat_ids == ["ATU23456789"] and s.ibans == ["AT089999900000012345"]

    def test_receipt(self):
        s = suggest_from("kassenbon")
        assert (s.kind, s.number, s.date, s.amount) == ("receipt", "004711", date(2026, 2, 3), Decimal("23.45"))

    def test_credit_note(self):
        s = suggest_from("gutschrift")
        assert (s.kind, s.number, s.date, s.amount) == ("credit_note", "GS-2026-07", date(2026, 4, 20), Decimal("120.00"))
        assert s.vat_ids == ["DE123456789"] and s.ibans == ["DE97999999990000012345"]

    def test_english_invoice_with_english_number_format(self):
        s = suggest_from("invoice-en")
        assert (s.kind, s.number, s.date, s.amount) == ("invoice", "INV-1001", date(2026, 3, 5), Decimal("1234.50"))

    def test_plain_extraction_works_too(self):
        """Faellt der Layout-Modus aus, liefert der Plain-Modus dieselben Angaben."""
        from pypdf import PdfReader
        text = extract.clean_text(PdfReader(io.BytesIO(fixture("zeilen"))).pages[0].extract_text())
        s = suggest_text(text)
        assert (s.number, s.date, s.amount) == ("RE-2026-0042", date(2026, 1, 12), Decimal("98.40"))


# ---------------------------------------------------------------------------
# Betrag
# ---------------------------------------------------------------------------

class TestAmount:
    @pytest.mark.parametrize("raw, expected", [
        ("1.234,56", "1234.56"), ("98,40", "98.40"), ("1,234.50", "1234.50"), ("12.50", "12.50"),
        ("1.234.567,89", "1234567.89"),
    ])
    def test_parse(self, raw, expected):
        assert extract.parse_amount(raw) == Decimal(expected)

    @pytest.mark.parametrize("raw", ["0,00", "abc", "", "100.000.000,00"])
    def test_parse_rejects_zero_and_nonsense(self, raw):
        assert extract.parse_amount(raw) is None

    def test_a_date_is_not_an_amount(self):
        assert suggest_text("Rechnungsdatum 12.03.2026").amount is None

    def test_net_and_tax_lines_do_not_count(self):
        text = "Gesamtbetrag netto 100,00 €\n20 % USt. 20,00 €\nUmsatzsteuerbetrag 20,00 €\nGesamtbetrag brutto 120,00 €"
        assert suggest_text(text).amount == Decimal("120.00")

    def test_discount_and_advance_payment_lines_do_not_count(self):
        text = "Gesamtbetrag 500,00 €\nAnzahlung 200,00 €\nBei Zahlung bis 01.01.2026 2 % Skonto: Zahlbetrag 490,00 €"
        assert suggest_text(text).amount == Decimal("500.00")

    def test_a_strong_keyword_beats_a_bigger_unmarked_amount(self):
        text = "Kontostand 9.999,00 €\nZu zahlen 45,00 €"
        assert suggest_text(text).amount == Decimal("45.00")

    def test_the_value_may_stand_under_the_column_header(self):
        text = "Rechnungsbetrag          Zahlungsziel\n  1.234,56 €             14 Tage"
        assert suggest_text(text).amount == Decimal("1234.56")

    def test_a_weak_keyword_works_for_receipts(self):
        assert suggest_text("Bon\nSumme EUR 12,30\nGegeben 20,00").amount == Decimal("12.30")

    def test_without_a_keyword_the_biggest_euro_amount_is_the_fallback(self):
        assert suggest_text("Pos 1  10,00 €\nPos 2  35,50 €").amount == Decimal("35.50")

    def test_no_amounts_no_suggestion(self):
        assert suggest_text("Lieferschein ohne Preise").amount is None


# ---------------------------------------------------------------------------
# Datum
# ---------------------------------------------------------------------------

class TestDate:
    @pytest.mark.parametrize("text, expected", [
        ("Rechnungsdatum: 12.03.2026", date(2026, 3, 12)),
        ("Rechnungsdatum 5.1.26", date(2026, 1, 5)),                     # zweistelliges Jahr
        ("Datum: 2026-03-05", date(2026, 3, 5)),                          # ISO
        ("Rechnung vom 7. März 2026", date(2026, 3, 7)),
        ("Belegdatum 15 Feber 2026", date(2026, 2, 15)),                  # oesterreichisch
        ("Invoice date: 2026-04-01", date(2026, 4, 1)),
    ])
    def test_formats(self, text, expected):
        assert suggest_text(text).date == expected

    def test_due_and_delivery_dates_are_not_the_document_date(self):
        text = "Lieferdatum: 01.03.2026\nFällig bis: 15.04.2026\nRechnungsdatum: 10.03.2026"
        assert suggest_text(text).date == date(2026, 3, 10)

    def test_the_fallback_skips_lines_with_due_words(self):
        text = "Fällig am 15.04.2026\nBestellt am 02.03.2026"
        assert suggest_text(text).date == date(2026, 3, 2)

    def test_header_row(self):
        text = "Rechnungsnummer    Rechnungsdatum    Fälligkeit\n  R-1             12.03.2026       26.03.2026"
        assert suggest_text(text).date == date(2026, 3, 12)

    @pytest.mark.parametrize("text", [
        "Rechnungsdatum: 12.03.1985",          # vor 2000
        "Rechnungsdatum: 12.03.2031",          # Zukunft
        "Rechnungsdatum: 31.02.2026",          # gibt es nicht
    ])
    def test_implausible_dates_are_dropped(self, text):
        assert suggest_text(text).date is None


# ---------------------------------------------------------------------------
# Nummer, Art
# ---------------------------------------------------------------------------

class TestNumberAndKind:
    @pytest.mark.parametrize("text, expected", [
        ("Rechnungsnummer: 2026/001", "2026/001"),
        ("Rechnungs-Nr. RE-77", "RE-77"),
        ("Rechnung Nr. 12345", "12345"),
        ("Beleg-Nr.: B-5 vom Montag", "B-5"),        # das Leerzeichen beendet den Wert
        ("Rg.-Nr. 889", "889"),
        ("Invoice number INV-9", "INV-9"),
        ("Rechnung 2026-77", "2026-77"),             # Ueberschrift mit Nummer
    ])
    def test_number_formats(self, text, expected):
        assert suggest_text(text).number == expected

    def test_a_date_or_a_label_is_not_a_number(self):
        assert suggest_text("Rechnungsnummer: 12.03.2026").number is None
        assert suggest_text("Rechnungsnummer Rechnungsdatum").number is None

    def test_customer_and_order_numbers_are_not_invoice_numbers(self):
        assert suggest_text("Kundennummer: 4711\nBestellnummer: 99").number is None

    @pytest.mark.parametrize("text, kind", [
        ("Gutschrift Nr. 5", "credit_note"), ("Stornorechnung 7", "credit_note"),
        ("RECHNUNG", "invoice"), ("Invoice", "invoice"), ("Kassenbon\nBon-Nr. 1", "receipt"),
        ("Quittung", "receipt"), ("Kontoauszug Nr. 3", "bank_statement"), ("Lieferschein", None),
    ])
    def test_kind(self, text, kind):
        assert suggest_text(text).kind == kind

    def test_invoice_address_is_not_an_invoice(self):
        assert suggest_text("Rechnungsadresse: Hauptplatz 1").kind is None


# ---------------------------------------------------------------------------
# UID, IBAN
# ---------------------------------------------------------------------------

class TestIdentifiers:
    def test_vat_ids_are_normalised(self):
        assert extract.find_vat_ids("UID: ATU12345678") == ["ATU12345678"]
        assert extract.find_vat_ids("UID ATU 12345678") == ["ATU12345678"]
        assert extract.find_vat_ids("USt-IdNr. DE 123456789") == ["DE123456789"]

    def test_an_iban_is_not_taken_for_a_vat_id(self):
        assert extract.find_vat_ids("IBAN DE97 9999 9999 0000 0123 45") == []
        assert extract.find_vat_ids("DE97999999990000012345") == []

    def test_own_ids_are_skipped_regardless_of_spacing(self):
        assert extract.find_vat_ids("ATU12345678 ATU87654321", own=["ATU 8765 4321"]) == ["ATU12345678"]

    def test_only_valid_ibans_count(self):
        good = "AT08 9999 9000 0001 2345"
        assert extract.find_ibans(f"IBAN {good}") == ["AT089999900000012345"]
        assert extract.find_ibans("IBAN AT09 9999 9000 0001 2345") == []          # Pruefziffer falsch

    def test_a_following_word_does_not_break_the_iban(self):
        assert extract.find_ibans("AT08 9999 9000 0001 2345 BIC RZTIAT22") == ["AT089999900000012345"]
        assert extract.find_ibans("IBAN: AT089999900000012345, BIC: X") == ["AT089999900000012345"]

    def test_own_ibans_are_skipped(self):
        text = "AT08 9999 9000 0001 2345 und AT10 9999 9000 0006 7890"
        assert extract.find_ibans(text, own=["at08 9999 9000 0001 2345"]) == ["AT109999900000067890"]


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------

class TestSearchHelpers:
    def test_snippet_marks_the_hit_with_context(self):
        text = "Wartung des Hochbehälters\n\nSchieber DN 100 wurde ausgetauscht und geprüft"
        before, hit, after = extract.snippet(text, "schieber", width=10)
        assert hit == "Schieber"                               # Schreibweise des Originals, nicht des Suchworts
        assert before == "… behälters " and after == " DN 100 wu …"

    def test_snippet_without_a_hit(self):
        assert extract.snippet("abc", "xyz") is None
        assert extract.snippet("abc", "") is None
        assert extract.snippet(None, "a") is None

    def test_names_compare_without_case_accents_and_punctuation(self):
        assert extract.normalize_name("Müller & Söhne GmbH.") == "muller sohne gmbh"
        assert extract.normalize_name("Straße") == "strasse"
