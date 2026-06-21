"""Unit-Tests fuer den OFX-Parser (bank_import/parsers/ofx.py).

Deckt beide OFX-Spielarten ab, die George (Erste/Sparkasse) anbietet:
2.x (XML, „MS Money Sunset Deluxe") und 1.x (SGML, „MS Money 2000").
Pure Parser-Tests ohne DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.bank_import.parsers import detect_format, parse
from app.bank_import.parsers import ofx


# OFX 2.x (XML) — minifiziert, Struktur wie der echte George-Export:
# eine Eingangsbuchung mit Rechnungsnummer im MEMO, eine Ausgangsbuchung
# (negativer TRNAMT), Schlusssaldo via LEDGERBAL.
OFX_2X = (
    b'<?xml version="1.0" encoding="utf-8" ?>'
    b'<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE" '
    b'OLDFILEUID="NONE" NEWFILEUID="NONE"?>'
    b"<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY>"
    b"</STATUS><LANGUAGE>DEU</LANGUAGE><FI><ORG>Erste Bank</ORG><FID>01234</FID>"
    b"</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS>"
    b"<TRNUID>abc-123</TRNUID><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
    b"<STMTRS><CURDEF>EUR</CURDEF><BANKACCTFROM><BANKID>KSPKAT2KXXX</BANKID>"
    b"<ACCTID>AT942070604500050440</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>"
    b"<BANKTRANLIST><DTSTART>20260501100000.000</DTSTART><DTEND>20260620100000.000</DTEND>"
    b"<STMTTRN><TRNTYPE>OTHER</TRNTYPE><DTPOSTED>20260619100000.000</DTPOSTED>"
    b"<DTUSER>20260619100000.000</DTUSER><TRNAMT>113.75000</TRNAMT>"
    b"<FITID>E2D9EE87675960DC</FITID><NAME>DDipl.-Ing. Axel Thomasch&#252;tz</NAME>"
    b"<BANKACCTTO><BANKID>BKAUATWWXXX</BANKID><ACCTID>AT621200000963514898</ACCTID>"
    b"<ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTTO><MEMO>2026-00200</MEMO></STMTTRN>"
    b"<STMTTRN><TRNTYPE>XFER</TRNTYPE><DTPOSTED>20260609100000.000</DTPOSTED>"
    b"<TRNAMT>-15.50000</TRNAMT><FITID>E2CE4F0E79BC452A</FITID>"
    b"<NAME>Schweiger Alexander und Maritta</NAME>"
    b"<MEMO>Papier und Kuverte</MEMO></STMTTRN></BANKTRANLIST>"
    b"<LEDGERBAL><BALAMT>53007.68</BALAMT><DTASOF>20260620163705.804</DTASOF></LEDGERBAL>"
    b"</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
)

# OFX 1.x (SGML) — KEY:VALUE-Header, Werte-Tags ohne schliessendes Pendant,
# Aggregate (STMTTRN/BANKACCTFROM) MIT schliessendem Tag.
OFX_1X = (
    b"OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:102\r\nSECURITY:NONE\r\n"
    b"ENCODING:UTF-8\r\nCHARSET:NONE\r\nCOMPRESSION:NONE\r\n"
    b"OLDFILEUID:NONE\r\nNEWFILEUID:NONE\r\n\r\n"
    b"<OFX><BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID>"
    b"<STMTRS><CURDEF>EUR</CURDEF><BANKACCTFROM><BANKID>KSPKAT2KXXX</BANKID>"
    b"<ACCTID>AT942070604500050440</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>"
    b"<BANKTRANLIST><DTSTART>20260501<DTEND>20260620"
    b"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>20260619100000<TRNAMT>208.80"
    b"<FITID>E2D9EE6998D900DC<NAME>Gottfried und Br Zlanabitnig"
    b"<MEMO>RENR 2026-00009</STMTTRN></BANKTRANLIST>"
    b"<LEDGERBAL><BALAMT>1234.56<DTASOF>20260620</LEDGERBAL>"
    b"</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
)


class TestDetectFormat:
    def test_detects_ofx_2x(self):
        assert detect_format(OFX_2X) == "ofx"

    def test_detects_ofx_1x(self):
        assert detect_format(OFX_1X) == "ofx"

    def test_camt_still_detected(self):
        camt = b'<?xml version="1.0"?><Document><BkToCstmrStmt></BkToCstmrStmt></Document>'
        assert detect_format(camt) == "camt053"


class TestParseOfx2x:
    def test_statement_metadata(self):
        stmt = parse(OFX_2X, "ofx")
        assert stmt.format == "ofx"
        assert stmt.account_iban == "AT942070604500050440"
        assert stmt.currency == "EUR"
        assert stmt.closing_balance == Decimal("53007.68")
        assert stmt.opening_balance is None
        assert stmt.statement_reference == "abc-123"
        assert stmt.booking_date_from == date(2026, 6, 9)
        assert stmt.booking_date_to == date(2026, 6, 19)

    def test_two_lines_parsed(self):
        stmt = parse(OFX_2X, "ofx")
        assert len(stmt.lines) == 2

    def test_incoming_line(self):
        line = parse(OFX_2X, "ofx").lines[0]
        assert line.booking_date == date(2026, 6, 19)
        assert line.amount == Decimal("113.75000")
        assert line.tx_id == "E2D9EE87675960DC"
        assert line.counterparty_name == "DDipl.-Ing. Axel Thomaschütz"  # &#252; entschluesselt
        assert line.counterparty_iban == "AT621200000963514898"
        assert line.purpose == "2026-00200"

    def test_outgoing_line_keeps_sign(self):
        line = parse(OFX_2X, "ofx").lines[1]
        assert line.amount == Decimal("-15.50000")
        assert line.purpose == "Papier und Kuverte"
        assert line.counterparty_iban is None  # kein BANKACCTTO


class TestParseOfx1x:
    def test_sgml_metadata_and_line(self):
        stmt = parse(OFX_1X, "ofx")
        assert stmt.account_iban == "AT942070604500050440"
        assert stmt.currency == "EUR"
        assert stmt.closing_balance == Decimal("1234.56")
        assert len(stmt.lines) == 1

    def test_sgml_line_fields(self):
        line = parse(OFX_1X, "ofx").lines[0]
        assert line.booking_date == date(2026, 6, 19)
        assert line.amount == Decimal("208.80")
        assert line.counterparty_name == "Gottfried und Br Zlanabitnig"
        assert line.purpose == "RENR 2026-00009"
        assert line.tx_id == "E2D9EE6998D900DC"


class TestEdgeCases:
    def test_empty_statement_raises(self):
        import pytest

        with pytest.raises(ValueError):
            ofx.parse(b"<OFX></OFX>")

    def test_amount_with_comma_decimal(self):
        # Defensiv: manche Banken liefern ',' als Dezimaltrenner.
        assert ofx._decimal("12,50") == Decimal("12.50")
