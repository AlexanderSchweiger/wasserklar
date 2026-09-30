"""XRechnung 3.0 (CII) fuer Behoerdenkunden: Mapper, Regeln, Serializer, Einfrieren.

Ergaenzt ``test_einvoice.py`` (ZUGFeRD). Die XMLs gehen mit
``EINVOICE_DUMP_DIR=…`` in ein Verzeichnis und werden mit dem Mustang-Validator
gegen das XRechnung-Schematron geprueft:

    EINVOICE_DUMP_DIR=/tmp/einvoice pytest tests/integration/test_einvoice_xrechnung.py
    java -jar Mustang-CLI.jar --action validate --source /tmp/einvoice/<name>.xml
"""
import base64
import os
from decimal import Decimal

import pytest
from lxml import etree

from app.einvoice import cii
from app.einvoice.mapper import build_einvoice
from app.einvoice.model import Attachment, PROFILE_EN16931, PROFILE_XRECHNUNG
from app.einvoice.rules import check
from app.einvoice.service import (
    EInvoiceUnavailable, einvoice_xml, invoice_status, profile_of, xrechnung_attachment,
)
from app.extensions import db
from app.models import AppSetting, Customer, Invoice, InvoiceItem, Property
from tests.integration.test_einvoice import (
    DE_SELLER, NS, _all, _invoice, _item, _seller, _text,
)

LEITWEG_ID = "04011000-1234512345-06"
XR_SELLER = {**DE_SELLER, "wg.contact_name": "Erika Kassier"}
FAKE_PDF = b"%PDF-1.7\n% Sichtkopie\n"


def _government(**extra):
    """Gemeinde als Wasserkunde: keine E-Mail, dafuer Leitweg-ID."""
    customer = Customer(name="Gemeinde Musterdorf", is_company=True, customer_number=12,
                        strasse="Rathausplatz", hausnummer="1", plz="91234", ort="Musterdorf",
                        einvoice_format="xrechnung", buyer_reference=LEITWEG_ID, **extra)
    db.session.add(customer)
    return customer


def _xrechnung(invoice, name, pdf=None):
    """XRechnung-XML (+ Dump fuer Mustang) → geparster Baum."""
    e = build_einvoice(invoice, PROFILE_XRECHNUNG)
    assert check(e) == [], check(e)
    if pdf is not None:
        e.attachment = Attachment(filename=f"{invoice.invoice_number}.pdf",
                                  mime_code="application/pdf", data=pdf,
                                  name=f"Rechnung {invoice.invoice_number} (PDF-Ansicht)")
    data = cii.serialize(e)
    dump_dir = os.environ.get("EINVOICE_DUMP_DIR")
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        with open(os.path.join(dump_dir, f"{name}.xml"), "wb") as fh:
            fh.write(data)
    return e, etree.fromstring(data)


class TestXRechnung:
    def test_government_invoice_with_leitweg_id(self, app):
        """G6: Behoerde mit Leitweg-ID — BR-DE-*, BT-10, BT-49 (0204), BG-24-PDF."""
        _seller(XR_SELLER)
        inv = _invoice([
            _item("Wasserverbrauch 2025/26 (120 m³)", "120", "m³", "1.85", "7",
                  charge_key="water"),
            _item("Grundgebühr", "1", "Pauschal", "60", "7"),
        ], customer=_government())
        with app.test_request_context():
            e, tree = _xrechnung(inv, "g6_de_xrechnung_government", pdf=FAKE_PDF)

        assert _text(tree, "//ram:GuidelineSpecifiedDocumentContextParameter/ram:ID") == \
            "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
        assert _text(tree, "//ram:BusinessProcessSpecifiedDocumentContextParameter/ram:ID") == \
            "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
        assert _text(tree, "//ram:ApplicableHeaderTradeAgreement/ram:BuyerReference") == LEITWEG_ID
        endpoint = _all(tree, "//ram:BuyerTradeParty/ram:URIUniversalCommunication/ram:URIID")[0]
        assert endpoint.text == LEITWEG_ID and endpoint.get("schemeID") == "0204"
        # BR-DE-2/5/6/7: Ansprechpartner des Verkaeufers komplett
        assert _text(tree, "//ram:SellerTradeParty//ram:PersonName") == "Erika Kassier"
        assert _text(tree, "//ram:SellerTradeParty//ram:TelephoneUniversalCommunication"
                           "/ram:CompleteNumber") == "+49 9123 4567"
        assert _text(tree, "//ram:SellerTradeParty//ram:EmailURIUniversalCommunication"
                           "/ram:URIID") == "info@wv-musterdorf.example"
        assert _text(tree, "//ram:SpecifiedTradeSettlementPaymentMeans/ram:TypeCode") == "58"
        # BG-24: die PDF-Ansicht steckt als Base64 im XML
        doc = _all(tree, "//ram:AdditionalReferencedDocument")[0]
        assert _text(doc, "ram:TypeCode") == "916"
        binary = doc.xpath("ram:AttachmentBinaryObject", namespaces=NS)[0]
        assert binary.get("mimeCode") == "application/pdf"
        assert binary.get("filename") == "2026-00042.pdf"
        assert base64.b64decode(binary.text) == FAKE_PDF
        assert Decimal(_text(tree, "//ram:GrandTotalAmount")) == inv.total_amount

    def test_buyer_reference_falls_back_to_the_customer_number(self, app):
        """BR-DE-15: ohne Leitweg-ID gilt die Kundennummer; eine E-Mail ist die Adresse."""
        _seller(XR_SELLER)
        customer = Customer(name="Müller Bau GmbH", is_company=True, customer_number=77,
                            strasse="Werkstr.", hausnummer="3", plz="91234", ort="Musterdorf",
                            email="buchhaltung@mueller-bau.example", vat_id="DE987654321",
                            einvoice_format="xrechnung")
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            e, tree = _xrechnung(inv, "g6b_de_xrechnung_company")
        assert _text(tree, "//ram:BuyerReference") == "77"
        endpoint = _all(tree, "//ram:BuyerTradeParty/ram:URIUniversalCommunication/ram:URIID")[0]
        assert endpoint.text == "buchhaltung@mueller-bau.example"
        assert endpoint.get("schemeID") == "EM"
        assert _text(tree, "//ram:BuyerTradeParty/ram:SpecifiedTaxRegistration"
                           "/ram:ID[@schemeID='VA']") == "DE987654321"

    def test_xrechnung_reports_everything_the_portal_would_reject(self, app):
        _seller({**DE_SELLER, "wg.phone": "", "wg.email": "", "wg.iban": ""})
        customer = Customer(name="Gemeinde Musterdorf", is_company=True, customer_number=12,
                            strasse="Rathausplatz", einvoice_format="xrechnung")
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            issues = " | ".join(check(build_einvoice(inv, PROFILE_XRECHNUNG)))
            zugferd = " | ".join(check(build_einvoice(inv)))
        for expected in ("Zahlungsanweisung", "Ansprechpartner", "Telefonnummer",
                         "E-Mail-Adresse der Genossenschaft",
                         "PLZ und Ort in der Anschrift des Kunden",
                         "elektronische Adresse des Kunden"):
            assert expected in issues, expected
        # Dieselbe Rechnung als ZUGFeRD braucht davon nichts.
        assert "Ansprechpartner" not in zugferd and "Zahlungsanweisung" not in zugferd

    def test_wrong_leitweg_check_digit_blocks_the_xml(self, app):
        _seller(XR_SELLER)
        customer = _government()
        customer.buyer_reference = "04011000-1234512345-07"
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            issues = check(build_einvoice(inv, PROFILE_XRECHNUNG))
        assert any("ungültige Prüfziffer" in issue for issue in issues)

    def test_invalid_iban_is_reported(self, app):
        _seller({**XR_SELLER, "wg.iban": "DE02120300000000202052"})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                       customer=_government())
        with app.test_request_context():
            assert any("IBAN" in issue
                       for issue in check(build_einvoice(inv, PROFILE_XRECHNUNG)))

    def test_credit_note_carries_a_payment_instruction(self, app):
        """BR-DE-1 gilt auch fuer Storno-Belege; das ZUGFeRD-Profil laesst sie weg."""
        _seller(XR_SELLER)
        customer = _government()
        original = _invoice([_item("Wasserverbrauch", "40", "m³", "2.50", "7")],
                            number="2026-00041", customer=customer)
        mirrored = [InvoiceItem(description=i.description, quantity=-i.quantity, unit=i.unit,
                                unit_price=i.unit_price, amount=-i.amount, tax_rate=i.tax_rate)
                    for i in original.items]
        credit = _invoice(mirrored, number="2026-00050", kind=Invoice.KIND_CREDIT_NOTE,
                          customer=customer, cancels=original)
        with app.test_request_context():
            e, tree = _xrechnung(credit, "g6c_de_xrechnung_credit_note")
            assert _text(tree, "//rsm:ExchangedDocument/ram:TypeCode") == "381"
            assert _all(tree, "//ram:SpecifiedTradeSettlementPaymentMeans")
            assert build_einvoice(credit).payment is None       # ZUGFeRD: keine

    def test_delivery_address_without_postcode_is_left_out(self, app):
        """BR-DE-10/11: eine Lieferanschrift braucht PLZ und Ort, sonst entfaellt sie."""
        _seller(XR_SELLER)
        prop = Property(object_number="12", object_type="Haus", strasse="Hauptstraße",
                        hausnummer="5")
        db.session.add(prop)
        db.session.flush()
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                       customer=_government(), prop=prop)
        with app.test_request_context():
            e, tree = _xrechnung(inv, "g6d_de_xrechnung_no_delivery_address")
            assert not _all(tree, "//ram:ShipToTradeParty")
            assert _text(tree, "//ram:ActualDeliverySupplyChainEvent//udt:DateTimeString")
            assert build_einvoice(inv).delivery.address is not None    # ZUGFeRD: bleibt

    def test_zugferd_keeps_the_mail_gate_for_the_buyer_address(self, app):
        """ZUGFeRD nennt die E-Mail nur, wenn der Kunde Post per E-Mail bekommt."""
        _seller(DE_SELLER)
        customer = Customer(name="Beispiel Anna", first_name="Anna", last_name="Beispiel",
                            customer_number=31, plz="91234", ort="Musterdorf",
                            email="anna@example.test", rechnung_per_email=False)
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            assert build_einvoice(inv).buyer.endpoint is None
            customer.rechnung_per_email = True
            assert build_einvoice(inv).buyer.endpoint == "anna@example.test"

    def test_a_buyer_reference_also_reaches_a_zugferd_invoice(self, app):
        """Hat ein Firmenkunde eine Bestellreferenz, steht sie auch im ZUGFeRD (BT-10)."""
        _seller(DE_SELLER)
        customer = Customer(name="Müller Bau GmbH", is_company=True, customer_number=77,
                            plz="91234", ort="Musterdorf", buyer_reference="PO-4711")
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            assert build_einvoice(inv).buyer_reference == "PO-4711"
            assert build_einvoice(inv).profile == PROFILE_EN16931


class TestXRechnungService:
    def test_the_customer_decides_the_profile_and_the_frozen_one_sticks(
            self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
        _seller(XR_SELLER)
        customer = _government()
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            assert profile_of(inv) == PROFILE_XRECHNUNG
            assert invoice_status(inv).is_xrechnung
            xml = einvoice_xml(inv, visual_pdf=FAKE_PDF)
            assert b"xrechnung_3.0" in xml
            assert inv.einvoice_profile == PROFILE_XRECHNUNG
            assert os.path.exists(inv.xml_path)
            # Der Kunde wird spaeter auf ZUGFeRD umgestellt: die Rechnung bleibt XRechnung.
            customer.einvoice_format = None
            assert profile_of(inv) == PROFILE_XRECHNUNG
            assert einvoice_xml(inv) == xml
            status = invoice_status(inv)
            assert status.frozen and status.label == "XRechnung 3.0"

    def test_mail_attachment_is_the_xml_file(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
        _seller(XR_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                       customer=_government(), status=Invoice.STATUS_DRAFT)
        with app.test_request_context():
            name, mime, data = xrechnung_attachment(inv, freeze=True)
            assert (name, mime) == ("2026-00042.xml", "application/xml")
            assert b"CrossIndustryInvoice" in data
            assert inv.xml_path and inv.einvoice_profile == PROFILE_XRECHNUNG

    def test_other_customers_get_no_xrechnung_attachment(self, app):
        _seller(XR_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")])
        with app.test_request_context():
            assert xrechnung_attachment(inv, freeze=False) is None

    def test_incomplete_data_raises_with_the_reasons(self, app):
        _seller({**DE_SELLER, "wg.phone": ""})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                       customer=_government())
        with app.test_request_context():
            with pytest.raises(EInvoiceUnavailable) as exc:
                xrechnung_attachment(inv, freeze=True)
        assert any("Ansprechpartner" in issue for issue in exc.value.issues)
        assert inv.xml_path is None

    def test_switched_off_means_no_xrechnung(self, app):
        _seller(XR_SELLER)
        AppSetting.set("einvoice.enabled", "false")
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                       customer=_government())
        with app.test_request_context():
            assert xrechnung_attachment(inv, freeze=True) is None
