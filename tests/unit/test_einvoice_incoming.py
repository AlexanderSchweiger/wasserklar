"""Parser für empfangene E-Rechnungen (app/einvoice/incoming.py).

Drei Quellen: (1) die offizielle KoSIT-Testsuite als Fremdwerk-Gegenprobe (UBL und CII
derselben Rechnung müssen dasselbe ergeben), (2) eigene XMLs aus unseren Serialisierern
(Hin- und Rückweg), (3) präparierte Dateien — kaputt, zu exotisch, bösartig.
"""
import base64
import io
import os
import re
from decimal import Decimal

import pytest
from pypdf import PdfWriter

from app.einvoice import incoming
from app.einvoice.incoming import IncomingError, ParsedInvoice, parse

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "einvoice")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def _pdf_with(xml, name="factur-x.xml"):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    if xml is not None:
        writer.add_attachment(name, xml)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestOfficialSamples:
    @pytest.mark.parametrize("case", ["01.01a", "01.13a"])
    def test_ubl_and_cii_of_the_same_invoice_agree(self, case):
        ubl = parse(_fixture(f"kosit-{case}-INVOICE_ubl.xml")).invoice
        cii = parse(_fixture(f"kosit-{case}-INVOICE_uncefact.xml")).invoice
        assert (ubl.syntax, cii.syntax) == ("ubl", "cii")
        for attr in ("number", "issue_date", "type_code", "currency", "line_total", "tax_basis_total",
                     "tax_total", "grand_total", "payable", "buyer_reference"):
            assert getattr(ubl, attr) == getattr(cii, attr), attr
        assert len(ubl.lines) == len(cii.lines) and len(ubl.vat) == len(cii.vat)
        assert [(v.rate, v.taxable, v.tax) for v in ubl.vat] == [(v.rate, v.taxable, v.tax) for v in cii.vat]
        for party in ("seller", "buyer"):
            a, b = getattr(ubl, party), getattr(cii, party)
            assert (a.name, a.vat_id, a.postcode, a.city, a.country) == (b.name, b.vat_id, b.postcode, b.city, b.country)
        assert ubl.iban == cii.iban

    def test_known_values_of_the_first_sample(self):
        inv = parse(_fixture("kosit-01.01a-INVOICE_uncefact.xml")).invoice
        assert inv.number == "123456XX"
        assert inv.issue_date.isoformat() == "2016-04-04"
        assert inv.type_code == "380" and inv.currency == "EUR"
        assert inv.seller.vat_id == "DE123456789"
        assert inv.grand_total == Decimal("336.90")
        assert len(inv.lines) == 2
        assert "xrechnung_3.0" in inv.guideline
        assert not inv.is_credit_note and inv.warnings == []

    def test_many_lines(self):
        inv = parse(_fixture("kosit-01.13a-INVOICE_ubl.xml")).invoice
        assert len(inv.lines) == 11
        assert sum((line.net_amount for line in inv.lines), Decimal("0")) == inv.line_total


class TestRoundTripWithOurOwnFiles:
    """Was wir ausstellen, muss sich auch wieder einlesen lassen."""

    def _invoice(self, app, customer_kwargs=None):
        from datetime import date

        from app.einvoice.mapper import build_einvoice
        from app.extensions import db
        from app.models import Customer
        from tests.integration.test_einvoice import AT_SELLER, _invoice, _item, _seller

        _seller(AT_SELLER)
        customer = Customer(name="Leitner Monika", customer_number=31, plz="4233", ort="Katsdorf",
                            strasse="Lindenweg", hausnummer="1", land="Österreich",
                            **(customer_kwargs or {}))
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch 2024 (179 m³)", "179", "m³", "1.40", "10"),
                        _item("Zählermiete", "1", "Pauschal", "85", "20")], customer=customer)
        return inv, build_einvoice

    def test_cii_roundtrip(self, app):
        from app.einvoice import cii
        inv, build = self._invoice(app)
        with app.test_request_context():
            xml = cii.serialize(build(inv))
        parsed = parse(xml).invoice
        assert parsed.syntax == "cii" and parsed.number == "2026-00042"
        assert parsed.grand_total == inv.total_amount and parsed.payable == inv.total_amount
        assert [v.rate for v in parsed.vat] == [Decimal("10.00"), Decimal("20.00")]
        assert parsed.seller.vat_id == "ATU12345678" and parsed.iban == "AT942070604500050440"
        assert parsed.lines[0].unit == "MTQ" and parsed.lines[0].quantity == Decimal("179")
        assert parsed.buyer.name == "Monika Leitner" or parsed.buyer.name == "Leitner Monika"

    def test_ubl_roundtrip_with_attachment(self, app):
        from app.einvoice import ubl
        from app.einvoice.model import Attachment, PROFILE_PEPPOL_UBL
        inv, build = self._invoice(app, dict(email="b@bmt.gv.at", einvoice_format="peppol_ubl",
                                             order_reference="BBG-1", supplier_number="L-1",
                                             peppol_id="9915:b"))
        with app.test_request_context():
            e = build(inv, PROFILE_PEPPOL_UBL)
            e.attachment = Attachment("2026-00042.pdf", "application/pdf", b"%PDF-1.7 test", "Ansicht")
            xml = ubl.serialize(e)
        doc = parse(xml)
        parsed = doc.invoice
        assert parsed.syntax == "ubl" and parsed.order_reference == "BBG-1"
        assert parsed.seller.identifier == "L-1" and parsed.grand_total == inv.total_amount
        assert [(a.filename, a.mime, a.size) for a in parsed.attachments] == \
            [("2026-00042.pdf", "application/pdf", len(b"%PDF-1.7 test"))]
        assert incoming.attachment_bytes(doc.xml, 0) == ("2026-00042.pdf", "application/pdf", b"%PDF-1.7 test")
        assert incoming.attachment_bytes(doc.xml, 1) is None

    def test_credit_note_roundtrip(self, app):
        from app.einvoice import ubl
        from app.einvoice.model import PROFILE_PEPPOL_UBL
        from app.extensions import db
        from app.models import Customer, Invoice, InvoiceItem
        from tests.integration.test_einvoice import AT_SELLER, _invoice, _item, _seller

        _seller(AT_SELLER)
        customer = Customer(name="Bund", customer_number=5, plz="1010", ort="Wien", land="Österreich",
                            email="a@b.gv.at", einvoice_format="peppol_ubl", order_reference="X",
                            supplier_number="Y", peppol_id="9915:b")
        db.session.add(customer)
        original = _invoice([_item("Wasser", "40", "m³", "2.50", "10")], number="2026-00041", customer=customer)
        mirrored = [InvoiceItem(description=i.description, quantity=-i.quantity, unit=i.unit,
                                unit_price=i.unit_price, amount=-i.amount, tax_rate=i.tax_rate)
                    for i in original.items]
        credit = _invoice(mirrored, number="2026-00050", kind=Invoice.KIND_CREDIT_NOTE,
                          customer=customer, cancels=original)
        from app.einvoice.mapper import build_einvoice
        with app.test_request_context():
            xml = ubl.serialize(build_einvoice(credit, PROFILE_PEPPOL_UBL))
        parsed = parse(xml).invoice
        assert parsed.is_credit_note and parsed.type_code == "381"
        assert parsed.preceding_number == "2026-00041" and parsed.grand_total == Decimal("110.00")


class TestPdf:
    def test_reads_the_embedded_xml(self):
        xml = _fixture("kosit-01.01a-INVOICE_uncefact.xml")
        doc = parse(_pdf_with(xml))
        assert doc.source_kind == "pdf" and doc.xml == xml
        assert doc.invoice.number == "123456XX"

    def test_finds_an_xml_with_another_name(self):
        xml = _fixture("kosit-01.01a-INVOICE_ubl.xml")
        assert parse(_pdf_with(xml, "rechnung-2026.xml")).invoice.syntax == "ubl"

    def test_a_plain_pdf_is_not_an_einvoice(self):
        with pytest.raises(IncomingError, match="keine E-Rechnungsdaten"):
            parse(_pdf_with(None))

    def test_an_unrelated_xml_attachment_does_not_count(self):
        with pytest.raises(IncomingError, match="keine E-Rechnungsdaten"):
            parse(_pdf_with(b"<notiz>hallo</notiz>", "notiz.xml"))

    def test_a_broken_pdf(self):
        with pytest.raises(IncomingError, match="lässt sich nicht lesen"):
            parse(b"%PDF-1.7\nkein echtes PDF")


class TestRejected:
    def test_empty(self):
        with pytest.raises(IncomingError, match="leer"):
            parse(b"")

    def test_not_xml(self):
        with pytest.raises(IncomingError, match="kein gültiges XML"):
            parse(b"das ist keine Rechnung")

    def test_another_xml_format(self):
        with pytest.raises(IncomingError, match="Kein bekanntes E-Rechnungs-Format"):
            parse(b"<?xml version='1.0'?><html><body>x</body></html>")

    def test_zugferd_1_is_named(self):
        with pytest.raises(IncomingError, match="ZUGFeRD 1.x"):
            parse(b"<?xml version='1.0'?><rsm:CrossIndustryDocument "
                  b"xmlns:rsm='urn:ferd:CrossIndustryDocument:invoice:1p0'/>")

    def test_a_dtd_is_refused(self):
        """XXE / Billion-Laughs: keine DTD, keine Entitäten."""
        evil = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                b'<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:'
                b'CrossIndustryInvoice:100">&x;</rsm:CrossIndustryInvoice>')
        with pytest.raises(IncomingError, match="DTD"):
            parse(evil)

    def test_entities_are_never_expanded(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE b [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;">]>'
                b'<root>&b;</root>')
        with pytest.raises(IncomingError):
            parse(bomb)

    def test_cii_without_trade_data(self):
        with pytest.raises(IncomingError, match="keine Handelsdaten"):
            parse(b'<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:'
                  b'CrossIndustryInvoice:100"/>')


class TestWarningsAndRobustness:
    def test_inconsistent_totals_warn_but_parse(self):
        xml = re.sub(rb"(<cbc:TaxInclusiveAmount[^>]*>)[^<]+", rb"\g<1>999.00",
                     _fixture("kosit-01.01a-INVOICE_ubl.xml"))
        inv = parse(xml).invoice
        assert inv.grand_total == Decimal("999.00")
        assert any("passt nicht zum Gesamtbetrag" in w for w in inv.warnings)

    def test_foreign_currency_warns(self):
        xml = _fixture("kosit-01.01a-INVOICE_ubl.xml").replace(
            b"<cbc:DocumentCurrencyCode>EUR", b"<cbc:DocumentCurrencyCode>CHF")
        assert any("Fremdwährung CHF" in w for w in parse(xml).invoice.warnings)

    def test_missing_optional_parts_do_not_break_it(self):
        minimal = (b'<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:'
                   b'CrossIndustryInvoice:100" xmlns:ram="urn:un:unece:uncefact:data:standard:'
                   b'ReusableAggregateBusinessInformationEntity:100"><rsm:SupplyChainTradeTransaction/>'
                   b'</rsm:CrossIndustryInvoice>')
        inv = parse(minimal).invoice
        assert inv.number == "" and inv.grand_total == Decimal("0") and inv.lines == []
        assert inv.warnings          # „weder Positionen noch Summen“, fehlendes Datum


class TestJson:
    def test_roundtrip_keeps_everything(self):
        inv = parse(_fixture("kosit-01.13a-INVOICE_uncefact.xml")).invoice
        again = ParsedInvoice.from_json(inv.to_json())
        assert again == inv

    def test_decimals_stay_exact(self):
        inv = parse(_fixture("kosit-01.01a-INVOICE_ubl.xml")).invoice
        assert ParsedInvoice.from_json(inv.to_json()).grand_total == Decimal("336.90")
