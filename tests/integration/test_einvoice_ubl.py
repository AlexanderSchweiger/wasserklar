"""UBL 2.1 / Peppol BIS 3.0 für Bundesdienststellen in Österreich (e-Rechnung.gv.at).

Golden-Fälle G13 (Bund, 10 % + 20 %) und G14 (Storno als CreditNote). Die XMLs gehen
mit ``EINVOICE_DUMP_DIR=…`` in ein Verzeichnis und werden mit Mustang validiert:

    EINVOICE_DUMP_DIR=/tmp/einvoice pytest tests/integration/test_einvoice_ubl.py
    java -jar Mustang-CLI.jar --action validate --source /tmp/einvoice/<name>.xml
"""
import base64
import os
from datetime import date
from decimal import Decimal

import pytest
from lxml import etree

from app.einvoice import cii, obligation, ubl
from app.einvoice.mapper import build_einvoice
from app.einvoice.model import (
    Attachment, FORMAT_PEPPOL_UBL, PROFILE_EN16931, PROFILE_PEPPOL_UBL,
)
from app.einvoice.rules import check
from app.einvoice.service import (
    EInvoiceUnavailable, customer_profile, einvoice_xml, invoice_status, profile_of,
    xml_only_attachment,
)
from app.extensions import db
from app.models import Customer, Invoice, InvoiceItem, Property
from tests.integration.test_einvoice import AT_SELLER, _invoice, _item, _seller

NS = {
    "ubl": ubl.NS_INVOICE, "cn": ubl.NS_CREDIT_NOTE, "cac": ubl.NS_CAC, "cbc": ubl.NS_CBC,
    "ram": cii.NS_RAM,
}
FAKE_PDF = b"%PDF-1.7\n% Sichtkopie\n"


def _bund(**extra):
    """Bundesdienststelle in Österreich mit Auftragsreferenz und Lieferantennummer."""
    customer = Customer(
        name="Bundesministerium für Testwesen", is_company=True, customer_number=31,
        strasse="Stubenring", hausnummer="1", plz="1010", ort="Wien", land="Österreich",
        email="rechnung@bmt.gv.at", einvoice_format=FORMAT_PEPPOL_UBL,
        order_reference="BBG-4711", supplier_number="L-123456", peppol_id="9915:b", **extra)
    db.session.add(customer)
    return customer


def _xml(invoice, name, pdf=None):
    e = build_einvoice(invoice, PROFILE_PEPPOL_UBL)
    assert check(e) == [], check(e)
    if pdf is not None:
        e.attachment = Attachment(filename=f"{invoice.invoice_number}.pdf",
                                  mime_code="application/pdf", data=pdf,
                                  name=f"Rechnung {invoice.invoice_number} (PDF-Ansicht)")
    data = ubl.serialize(e)
    dump_dir = os.environ.get("EINVOICE_DUMP_DIR")
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        with open(os.path.join(dump_dir, f"{name}.xml"), "wb") as fh:
            fh.write(data)
    return e, etree.fromstring(data)


def _t(tree, path):
    return tree.xpath(f"string({path})", namespaces=NS)


def _all(tree, path):
    return tree.xpath(path, namespaces=NS)


class TestUblInvoice:
    def test_federal_invoice(self, app):
        """G13: Bund, 10 % Wasser + 20 % Zählermiete, Auftragsreferenz + Lieferantennummer."""
        _seller(AT_SELLER)
        prop = Property(object_number="7", object_type="Haus", strasse="Amtsweg", hausnummer="2",
                        plz="1010", ort="Wien")
        db.session.add(prop)
        db.session.flush()
        inv = _invoice([
            _item("Wasserverbrauch 2024 (179 m³)", "179", "m³", "1.40", "10", charge_key="water"),
            _item("Zählermiete", "1", "Pauschal", "85", "20"),
        ], customer=_bund(), prop=prop)
        with app.test_request_context():
            e, tree = _xml(inv, "g13_at_ubl_federal", pdf=FAKE_PDF)

        assert tree.tag == f"{{{ubl.NS_INVOICE}}}Invoice"
        assert _t(tree, "/ubl:Invoice/cbc:CustomizationID") == ubl.CUSTOMIZATION_ID
        assert _t(tree, "/ubl:Invoice/cbc:ProfileID") == ubl.PROFILE_ID
        assert _t(tree, "/ubl:Invoice/cbc:InvoiceTypeCode") == "380"
        assert _t(tree, "/ubl:Invoice/cbc:DueDate") == "2026-10-14"
        assert _t(tree, "/ubl:Invoice/cbc:DocumentCurrencyCode") == "EUR"
        # BT-10 (Kundennummer als Fallback) und BT-13 (Auftragsreferenz der Dienststelle)
        assert _t(tree, "/ubl:Invoice/cbc:BuyerReference") == "31"
        assert _t(tree, "/ubl:Invoice/cac:OrderReference/cbc:ID") == "BBG-4711"
        # BT-29 Lieferantennummer, elektronische Adressen, UID
        supplier = "/ubl:Invoice/cac:AccountingSupplierParty/cac:Party"
        assert _t(tree, f"{supplier}/cac:PartyIdentification/cbc:ID") == "L-123456"
        # Peppol kennt „EM" nicht als Schema (CL008): die Genossenschaft wird über ihre UID adressiert.
        assert _t(tree, f"{supplier}/cbc:EndpointID") == "ATU12345678"
        assert _all(tree, f"{supplier}/cbc:EndpointID")[0].get("schemeID") == "9914"
        assert _t(tree, f"{supplier}/cac:PartyTaxScheme/cbc:CompanyID") == "ATU12345678"
        assert _t(tree, f"{supplier}/cac:PostalAddress/cac:Country/cbc:IdentificationCode") == "AT"
        buyer = "/ubl:Invoice/cac:AccountingCustomerParty/cac:Party"
        assert _t(tree, f"{buyer}/cbc:EndpointID") == "b"
        assert _all(tree, f"{buyer}/cbc:EndpointID")[0].get("schemeID") == "9915"
        assert _t(tree, f"{buyer}/cac:PartyLegalEntity/cbc:RegistrationName") == \
            "Bundesministerium für Testwesen"
        # Summen entsprechen der gespeicherten Rechnung
        total = "/ubl:Invoice/cac:LegalMonetaryTotal"
        assert Decimal(_t(tree, f"{total}/cbc:PayableAmount")) == inv.total_amount
        assert _t(tree, f"{total}/cbc:LineExtensionAmount") == \
            _t(tree, f"{total}/cbc:TaxExclusiveAmount")
        subtotals = {_t(t, "cac:TaxCategory/cbc:Percent"): (_t(t, "cbc:TaxableAmount"), _t(t, "cbc:TaxAmount"))
                     for t in _all(tree, "//cac:TaxTotal/cac:TaxSubtotal")}
        assert subtotals == {"10.00": ("250.60", "25.06"), "20.00": ("85.00", "17.00")}
        # Positionen, Einheit MTQ, Preis
        lines = _all(tree, "/ubl:Invoice/cac:InvoiceLine")
        assert len(lines) == 2
        assert _all(lines[0], "cbc:InvoicedQuantity")[0].get("unitCode") == "MTQ"
        assert _t(lines[0], "cac:Price/cbc:PriceAmount") == "1.4000"
        # Lieferort, Zahlung, Leistungszeitraum
        assert _t(tree, "//cac:Delivery/cac:DeliveryLocation/cbc:ID") == "7"
        assert _t(tree, "//cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID") == "AT942070604500050440"
        assert _t(tree, "/ubl:Invoice/cac:InvoicePeriod/cbc:StartDate") == "2025-10-01"
        # BG-24: PDF-Ansicht als Anhang
        attachment = _all(tree, "//cac:AdditionalDocumentReference//cbc:EmbeddedDocumentBinaryObject")[0]
        assert attachment.get("mimeCode") == "application/pdf"
        assert base64.b64decode(attachment.text) == FAKE_PDF

    def test_output_is_deterministic(self, app):
        _seller(AT_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund())
        with app.test_request_context():
            e = build_einvoice(inv, PROFILE_PEPPOL_UBL)
            assert ubl.serialize(e) == ubl.serialize(e)

    def test_small_business_exemption_reason(self, app):
        """AT-Kleinunternehmer: Kategorie E mit Begründung im TaxCategory."""
        _seller({**AT_SELLER, "wg.tax_number": "12 345/6789"}, vat_liable=False)
        inv = _invoice([_item("Wasserverbrauch", "50", "m³", "1.40")], customer=_bund())
        with app.test_request_context():
            e, tree = _xml(inv, "g13b_at_ubl_small_business")
        category = _all(tree, "//cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory")[0]
        assert _t(category, "cbc:ID") == "E"
        assert "§ 6 Abs. 1 Z 27" in _t(category, "cbc:TaxExemptionReason")
        # Die Begründung steht nur in der Aufstellung, nicht an der Position.
        assert not _all(tree, "//cac:InvoiceLine//cbc:TaxExemptionReason")


class TestUblCreditNote:
    def test_credit_note_is_a_credit_note_document(self, app):
        """G14: Storno — Wurzel CreditNote, Typ 381, CreditedQuantity, BillingReference."""
        _seller(AT_SELLER)
        customer = _bund()
        original = _invoice([_item("Wasserverbrauch", "40", "m³", "2.50", "10")],
                            number="2026-00041", customer=customer)
        mirrored = [InvoiceItem(description=i.description, quantity=-i.quantity, unit=i.unit,
                                unit_price=i.unit_price, amount=-i.amount, tax_rate=i.tax_rate)
                    for i in original.items]
        credit = _invoice(mirrored, number="2026-00050", kind=Invoice.KIND_CREDIT_NOTE,
                          customer=customer, cancels=original)
        with app.test_request_context():
            e, tree = _xml(credit, "g14_at_ubl_credit_note")
        assert tree.tag == f"{{{ubl.NS_CREDIT_NOTE}}}CreditNote"
        assert _t(tree, "/cn:CreditNote/cbc:CreditNoteTypeCode") == "381"
        assert not _all(tree, "/cn:CreditNote/cbc:DueDate")                # im CreditNote nicht erlaubt
        assert _t(tree, "/cn:CreditNote/cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID") \
            == "2026-00041"
        line = _all(tree, "/cn:CreditNote/cac:CreditNoteLine")[0]
        assert _t(line, "cbc:CreditedQuantity") == "40.000"                # positiv
        assert _t(tree, "/cn:CreditNote/cac:LegalMonetaryTotal/cbc:PayableAmount") == "110.00"
        assert _all(tree, "/cn:CreditNote/cac:PaymentMeans")                # BR-DE-1-Logik gilt mit


class TestRules:
    def test_a_federal_customer_needs_the_references(self, app):
        _seller(AT_SELLER)
        customer = Customer(name="Bundesstelle", customer_number=40, plz="1010", ort="Wien",
                            land="Österreich", einvoice_format=FORMAT_PEPPOL_UBL)
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context():
            issues = " | ".join(check(build_einvoice(inv, PROFILE_PEPPOL_UBL)))
        for expected in ("Auftragsreferenz", "Lieferantennummer", "elektronische Adresse des Kunden"):
            assert expected in issues, expected

    def test_the_seller_is_addressed_by_its_vat_id(self, app):
        """Ohne UID gibt es keine Peppol-Adresse des Rechnungsstellers."""
        _seller({**AT_SELLER, "wg.vat_id": "", "wg.tax_number": "12 345/6789",
                 "wg.register_number": "FN 12345a"})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund())
        with app.test_request_context():
            assert any("über ihre UID" in i for i in check(build_einvoice(inv, PROFILE_PEPPOL_UBL)))

    @pytest.mark.parametrize("vat_id, scheme", [("ATU12345678", "9914"), ("DE123456789", "9930")])
    def test_seller_scheme_follows_the_country_of_the_vat_id(self, app, vat_id, scheme):
        _seller({**AT_SELLER, "wg.vat_id": vat_id})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund())
        with app.test_request_context():
            party = build_einvoice(inv, PROFILE_PEPPOL_UBL).seller
        assert (party.endpoint, party.endpoint_scheme) == (vat_id, scheme)

    def test_a_vat_id_of_another_country_gives_no_peppol_address(self, app):
        _seller({**AT_SELLER, "wg.vat_id": "IT12345678901"})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund())
        with app.test_request_context():
            assert build_einvoice(inv, PROFILE_PEPPOL_UBL).seller.endpoint is None

    def test_the_buyer_falls_back_to_its_vat_id(self, app):
        _seller(AT_SELLER)
        customer = _bund()
        customer.peppol_id = None
        customer.vat_id = "ATU99999999"
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context():
            party = build_einvoice(inv, PROFILE_PEPPOL_UBL).buyer
        assert (party.endpoint, party.endpoint_scheme) == ("ATU99999999", "9914")

    def test_a_malformed_peppol_id_is_ignored(self, app):
        _seller(AT_SELLER)
        customer = _bund()
        customer.peppol_id = "keinSchema"
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context():
            assert build_einvoice(inv, PROFILE_PEPPOL_UBL).buyer.endpoint is None

    def test_zugferd_needs_none_of_it(self, app):
        _seller(AT_SELLER)
        customer = Customer(name="Bundesstelle", customer_number=40, plz="1010", ort="Wien",
                            land="Österreich")
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context():
            assert check(build_einvoice(inv)) == []

    def test_the_supplier_number_is_only_used_by_ubl(self, app):
        _seller(AT_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund())
        with app.test_request_context():
            assert build_einvoice(inv, PROFILE_PEPPOL_UBL).seller.identifier == "L-123456"
            assert build_einvoice(inv, PROFILE_EN16931).seller.identifier is None


class TestServiceAndShipping:
    def test_profile_follows_the_customer_and_the_xml_is_ubl(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
        _seller(AT_SELLER)
        customer = _bund()
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context():
            assert customer_profile(inv) == PROFILE_PEPPOL_UBL
            status = invoice_status(inv)
            assert status.is_xml_only and not status.is_xrechnung
            assert status.label == "UBL · Peppol BIS 3.0" and status.format_name == "UBL-Rechnung"
            xml = einvoice_xml(inv, visual_pdf=FAKE_PDF)
            assert xml.count(b"<Invoice") == 1 and ubl.NS_INVOICE.encode() in xml
            assert inv.einvoice_profile == PROFILE_PEPPOL_UBL and os.path.exists(inv.xml_path)
            customer.einvoice_format = None                                # später umgestellt …
            assert profile_of(inv) == PROFILE_PEPPOL_UBL                   # … Beleg bleibt UBL
            assert einvoice_xml(inv) == xml

    def test_mail_attachment_is_the_xml(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
        _seller(AT_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund(),
                       status=Invoice.STATUS_DRAFT)
        with app.test_request_context():
            name, mime, data = xml_only_attachment(inv, freeze=True)
        assert (name, mime) == ("2026-00042.xml", "application/xml")
        assert b"CustomizationID" in data

    def test_incomplete_data_raises_with_the_format_name(self, app):
        _seller(AT_SELLER)
        customer = _bund()
        customer.order_reference = None
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context(), pytest.raises(EInvoiceUnavailable) as exc:
            xml_only_attachment(inv, freeze=True)
        assert exc.value.format_name == "UBL-Rechnung"
        assert any("Auftragsreferenz" in issue for issue in exc.value.issues)

    def test_no_post_for_a_federal_customer(self, app):
        _seller(AT_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=_bund(),
                       status=Invoice.STATUS_DRAFT)
        with app.test_request_context():
            assert obligation.electronic_reason(inv) == obligation.REASON_PEPPOL
            printable, blocked = obligation.split_post_invoices([inv])
        assert blocked == [inv] and printable == []


class TestOrderReferenceInCii:
    def test_zugferd_carries_bt13_when_known(self, app):
        _seller(AT_SELLER)
        customer = _bund()
        customer.einvoice_format = None                                    # normales ZUGFeRD
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "1.40", "10")], customer=customer)
        with app.test_request_context():
            e = build_einvoice(inv)
            tree = etree.fromstring(cii.serialize(e))
        assert tree.xpath("string(//ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID)",
                          namespaces=NS) == "BBG-4711"
