"""E-Rechnung (EN 16931, ZUGFeRD/Factur-X): Mapper, Regeln, CII-XML, Einfrieren.

Die Faelle entsprechen den Golden-Fixtures aus E_RECHNUNG_PLAN.md § 7. Das
erzeugte XML wird hier strukturell geprueft; die volle Schematron-Validierung
(EN 16931 + Factur-X) macht der Mustang-Validator — dafuer schreiben die Tests
jedes XML nach ``$EINVOICE_DUMP_DIR``, wenn die Variable gesetzt ist:

    EINVOICE_DUMP_DIR=/tmp/einvoice pytest tests/integration/test_einvoice.py
    java -jar Mustang-CLI.jar --action validate --source /tmp/einvoice/<name>.xml
"""
import os
from datetime import date
from decimal import Decimal

import pytest
from lxml import etree

from app.einvoice import cii
from app.einvoice.mapper import (
    build_einvoice, country_code_for, parse_address,
)
from app.einvoice.rules import check
from app.einvoice.service import einvoice_xml, invoice_status
from app.einvoice.units import unit_code
from app.extensions import db
from app.models import (
    AppSetting, BillingPeriod, Customer, FiscalYear, Invoice, InvoiceItem, Property,
)

NS = {
    "rsm": cii.NS_RSM, "ram": cii.NS_RAM, "udt": cii.NS_UDT, "qdt": cii.NS_QDT,
}

DE_SELLER = {
    "org.country": "DE",
    "wg.name": "Wasserversorgung Musterdorf eG",
    "wg.address": "Brunnenweg 1\n91234 Musterdorf",
    "wg.vat_id": "DE 123 456 789",
    "wg.tax_number": "212/345/67890",
    "wg.email": "info@wv-musterdorf.example",
    "wg.phone": "+49 9123 4567",
    "wg.iban": "DE02 1203 0000 0000 2020 51",
    "wg.bic": "BYLADEM1001",
}
AT_SELLER = {
    "org.country": "AT",
    "wg.name": "Wassergenossenschaft Treffling",
    "wg.address": "Treffling 3\n9871 Seeboden",
    "wg.vat_id": "ATU12345678",
    "wg.email": "wg@treffling.example",
    "wg.iban": "AT94 2070 6045 0005 0440",
    "wg.bic": "KSPKAT2KXXX",
}


def _seller(values, *, vat_liable=True):
    for key, value in values.items():
        AppSetting.set(key, value)
    db.session.add(FiscalYear(year=2026, start_date=date(2026, 1, 1),
                              end_date=date(2026, 12, 31), is_vat_liable=vat_liable))
    db.session.commit()


def _item(description, quantity, unit, price, rate=None, **extra):
    quantity, price = Decimal(quantity), Decimal(price)
    return InvoiceItem(description=description, quantity=quantity, unit=unit,
                       unit_price=price, amount=(quantity * price).quantize(Decimal("0.01")),
                       tax_rate=Decimal(rate) if rate else None, **extra)


def _invoice(items, *, number="2026-00042", kind=Invoice.KIND_STANDARD,
             status=Invoice.STATUS_SENT, customer=None, cancels=None, prop=None,
             notes=None):
    period = BillingPeriod.query.first()
    if period is None:
        period = BillingPeriod(name="2025/26", start_date=date(2025, 10, 1),
                               end_date=date(2026, 9, 30), active=True)
        db.session.add(period)
    if customer is None:
        customer = Customer(name="Beispiel Anna", first_name="Anna", last_name="Beispiel",
                            customer_number=31, strasse="Hauptstraße", hausnummer="5",
                            plz="91234", ort="Musterdorf")
        db.session.add(customer)
    db.session.flush()
    inv = Invoice(invoice_number=number, customer_id=customer.id,
                  billing_period_id=period.id, property_id=prop.id if prop else None,
                  date=date(2026, 9, 30), due_date=date(2026, 10, 14), status=status,
                  invoice_kind=kind, cancels_invoice_id=cancels.id if cancels else None,
                  notes=notes)
    db.session.add(inv)
    db.session.flush()
    for item in items:
        inv.items.append(item)
    db.session.flush()
    inv.recalculate_total()
    db.session.commit()
    return inv


def _xml(invoice, name):
    """EInvoice → XML (+ Dump fuer Mustang) → geparster Baum."""
    e = build_einvoice(invoice)
    assert check(e) == [], check(e)
    data = cii.serialize(e)
    dump_dir = os.environ.get("EINVOICE_DUMP_DIR")
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        with open(os.path.join(dump_dir, f"{name}.xml"), "wb") as fh:
            fh.write(data)
    return e, etree.fromstring(data)


def _text(tree, path):
    return tree.xpath(f"string({path})", namespaces=NS)


def _all(tree, path):
    return tree.xpath(path, namespaces=NS)


class TestHelpers:
    def test_units(self):
        assert unit_code("m³") == "MTQ"
        assert unit_code("Pauschal") == "LS"
        assert unit_code("Stk") == "H87"
        assert unit_code("Irgendwas") == "C62"

    @pytest.mark.parametrize("text, expected", [
        ("Musterstraße 1\n1234 Musterdorf", ("Musterstraße 1", "1234", "Musterdorf")),
        ("Musterstraße 1\\n1234 Musterdorf", ("Musterstraße 1", "1234", "Musterdorf")),
        ("Brunnenweg 1, 91234 Musterdorf", ("Brunnenweg 1", "91234", "Musterdorf")),
        ("Postfach 12\nTreffling 3\nA-9871 Seeboden\nÖsterreich",
         ("Postfach 12, Treffling 3", "9871", "Seeboden")),
        ("Nur ein Ort", ("Nur ein Ort", "", "")),
    ])
    def test_parse_address(self, text, expected):
        assert parse_address(text) == expected

    def test_country_codes(self, app):
        with app.test_request_context():
            AppSetting.set("org.country", "AT")
            assert country_code_for("") == "AT"
            assert country_code_for("Deutschland") == "DE"
            assert country_code_for("Österreich") == "AT"
            assert country_code_for("Italien") == "IT"
            assert country_code_for("CH") == "CH"
            assert country_code_for("Narnia") is None


class TestGermanInvoice:
    def test_standard_invoice_two_rates(self, app):
        """G1: Wasser 7 % + Zählermiete 19 %, Liegenschaft als Lieferort."""
        _seller(DE_SELLER)
        prop = Property(object_number="12", object_type="Haus", strasse="Hauptstraße",
                        hausnummer="5", plz="91234", ort="Musterdorf")
        db.session.add(prop)
        db.session.flush()
        inv = _invoice([
            _item("Wasserverbrauch 2025/26 (120 m³ × 1,8500 €/m³)", "120", "m³", "1.85", "7",
                  charge_key="water"),
            _item("Grundgebühr", "1", "Pauschal", "60", "7"),
            _item("Zählermiete", "1", "Pauschal", "12", "19"),
        ], prop=prop, notes="Danke für die rechtzeitige Ablesung.")
        with app.test_request_context():
            e, tree = _xml(inv, "g1_de_standard")

        assert _text(tree, "//rsm:ExchangedDocument/ram:TypeCode") == "380"
        assert _text(tree, "//ram:GuidelineSpecifiedDocumentContextParameter/ram:ID") == \
            "urn:cen.eu:en16931:2017"
        assert [n.get("unitCode") for n in _all(tree, "//ram:BilledQuantity")] == \
            ["MTQ", "LS", "LS"]
        taxes = {_text(t, "ram:RateApplicablePercent"): (
            _text(t, "ram:BasisAmount"), _text(t, "ram:CalculatedAmount"))
            for t in _all(tree, "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax")}
        assert taxes == {"7.00": ("282.00", "19.74"), "19.00": ("12.00", "2.28")}
        assert _text(tree, "//ram:GrandTotalAmount") == "316.02"
        assert Decimal(_text(tree, "//ram:GrandTotalAmount")) == inv.total_amount
        assert _text(tree, "//ram:SellerTradeParty/ram:SpecifiedTaxRegistration"
                           "/ram:ID[@schemeID='VA']") == "DE123456789"
        assert _text(tree, "//ram:SellerTradeParty//ram:PostcodeCode") == "91234"
        assert _text(tree, "//ram:IBANID") == "DE02120300000000202051"
        assert _text(tree, "//ram:PaymentReference") == "2026-00042"
        assert _text(tree, "//ram:BillingSpecifiedPeriod/ram:StartDateTime/udt:DateTimeString") \
            == "20251001"
        assert _text(tree, "//ram:ShipToTradeParty/ram:ID") == "12"
        assert _text(tree, "//ram:ActualDeliverySupplyChainEvent//udt:DateTimeString") == "20260930"
        assert _text(tree, "//ram:BuyerTradeParty/ram:ID") == "31"
        assert _text(tree, "//ram:BuyerTradeParty/ram:Name") == "Anna Beispiel"
        assert _text(tree, "//ram:IncludedNote/ram:Content") == "Danke für die rechtzeitige Ablesung."

    def test_small_business_needs_an_identifier(self, app):
        """G2: Kleinunternehmer ohne USt-IdNr. — Kategorie E, Kennung ueber Registernummer."""
        _seller({**DE_SELLER, "wg.vat_id": ""}, vat_liable=False)
        inv = _invoice([_item("Wasserverbrauch", "80", "m³", "2.10")])
        with app.test_request_context():
            issues = check(build_einvoice(inv))
            assert any("Kennung" in issue for issue in issues)

            AppSetting.set("wg.register_number", "GnR 123")
            e, tree = _xml(inv, "g2_de_small_business")
        tax = _all(tree, "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax")[0]
        assert _text(tax, "ram:CategoryCode") == "E"
        assert _text(tax, "ram:RateApplicablePercent") == "0.00"
        assert "§ 19 UStG" in _text(tax, "ram:ExemptionReason")
        assert _text(tree, "//ram:SpecifiedLegalOrganization/ram:ID") == "GnR 123"
        assert _text(tree, "//ram:TaxTotalAmount") == "0.00"

    def test_credit_note_is_positive_type_381(self, app):
        """G3: Storno-Beleg — Typ 381, alle Werte positiv, Verweis aufs Original."""
        _seller(DE_SELLER)
        original = _invoice([_item("Wasserverbrauch", "40", "m³", "2.50", "7")],
                            number="2026-00041")
        mirrored = [InvoiceItem(description=i.description, quantity=-i.quantity, unit=i.unit,
                                unit_price=i.unit_price, amount=-i.amount, tax_rate=i.tax_rate)
                    for i in original.items]
        credit = _invoice(mirrored, number="2026-00050", kind=Invoice.KIND_CREDIT_NOTE,
                          customer=original.customer, cancels=original)
        with app.test_request_context():
            e, tree = _xml(credit, "g3_de_credit_note")
        assert _text(tree, "//rsm:ExchangedDocument/ram:TypeCode") == "381"
        assert _text(tree, "//ram:BilledQuantity") == "40.000"
        assert _text(tree, "//ram:LineTotalAmount") == "100.00"
        assert _text(tree, "//ram:GrandTotalAmount") == "107.00"
        assert _text(tree, "//ram:InvoiceReferencedDocument/ram:IssuerAssignedID") == "2026-00041"
        assert not _all(tree, "//ram:SpecifiedTradeSettlementPaymentMeans")
        assert "Gutschrift" in _text(tree, "//ram:SpecifiedTradePaymentTerms/ram:Description")

    def test_negative_correction_line_has_positive_price(self, app):
        """G5: Schaetzkorrektur mit negativem Einzelpreis → negative Menge (BR-27)."""
        _seller(DE_SELLER)
        inv = _invoice([
            _item("Wasserverbrauch", "100", "m³", "2.00", "7"),
            _item("Gutschrift geschätzter Wasserverbrauch 2024", "1", "Pauschal", "-35.20", "7"),
        ])
        with app.test_request_context():
            e, tree = _xml(inv, "g5_de_correction")
        second = _all(tree, "//ram:IncludedSupplyChainTradeLineItem")[1]
        assert _text(second, ".//ram:ChargeAmount") == "35.2000"
        assert _text(second, ".//ram:BilledQuantity") == "-1.000"
        assert _text(second, ".//ram:LineTotalAmount") == "-35.20"
        assert Decimal(_text(tree, "//ram:GrandTotalAmount")) == inv.total_amount

    def test_dunning_fee_is_not_part_of_the_invoice(self, app):
        """G11: Mahngebuehr-Positionen gehoeren zur Mahnung."""
        _seller(DE_SELLER)
        inv = _invoice([
            _item("Wasserverbrauch", "10", "m³", "2.00", "7"),
            _item("Mahngebühr", "1", "Pauschal", "5.00", is_dunning_fee=1),
        ])
        with app.test_request_context():
            e, tree = _xml(inv, "g11_de_dunning_fee")
        assert len(_all(tree, "//ram:IncludedSupplyChainTradeLineItem")) == 1
        assert _text(tree, "//ram:GrandTotalAmount") == "21.40"

    def test_zero_rate_line_in_vat_liable_year(self, app):
        """G12: 0-%-Position im steuerpflichtigen Jahr → E mit einstellbarer Begruendung."""
        _seller(DE_SELLER)
        AppSetting.set("einvoice.exempt_reason", "Echter Mitgliedsbeitrag, nicht steuerbar")
        inv = _invoice([
            _item("Wasserverbrauch", "10", "m³", "2.00", "7"),
            _item("Mitgliedsbeitrag", "1", "Pauschal", "20.00"),
        ])
        with app.test_request_context():
            e, tree = _xml(inv, "g12_de_zero_rate")
        exempt = [t for t in _all(tree, "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax")
                  if _text(t, "ram:CategoryCode") == "E"][0]
        assert _text(exempt, "ram:ExemptionReason") == "Echter Mitgliedsbeitrag, nicht steuerbar"
        assert _text(exempt, "ram:BasisAmount") == "20.00"

    def test_rounding_per_line_matches_the_invoice(self, app):
        """G9: USt je Position gerundet (wie recalculate_total) — Summen stimmen exakt."""
        _seller(DE_SELLER)
        inv = _invoice([_item(f"Position {i}", "1", "Stk", "0.15", "7") for i in range(40)])
        with app.test_request_context():
            e, tree = _xml(inv, "g9_de_rounding")
        assert Decimal(_text(tree, "//ram:GrandTotalAmount")) == inv.total_amount
        assert e.mapping_errors == []

    def test_foreign_and_unknown_buyer_country(self, app):
        """G10: Auslandsanschrift → ISO-Code; unbekanntes Land → Meldung."""
        _seller(DE_SELLER)
        customer = Customer(name="Rossi Mario", customer_number=77, strasse="Via Roma",
                            hausnummer="1", plz="39100", ort="Bozen", land="Italien")
        db.session.add(customer)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], customer=customer)
        with app.test_request_context():
            e, tree = _xml(inv, "g10_de_foreign_buyer")
            assert _text(tree, "//ram:BuyerTradeParty//ram:CountryID") == "IT"
            customer.land = "Narnia"
            assert any("Land" in issue for issue in check(build_einvoice(inv)))


class TestAustrianInvoice:
    def test_standard_invoice_ten_and_twenty_percent(self, app):
        """G7: AT, 10 % Wasser + 20 % Zählermiete, UID."""
        _seller(AT_SELLER)
        customer = Customer(name="Leitner Monika", first_name="Monika", last_name="Leitner",
                            customer_number=31, strasse="Lindenweg", hausnummer="1",
                            plz="4233", ort="Katsdorf", land="Österreich")
        db.session.add(customer)
        inv = _invoice([
            _item("Wasserverbrauch 2024 (179 m³)", "179", "m³", "1.40", "10"),
            _item("Zählermiete", "1", "Pauschal", "85", "20"),
        ], customer=customer)
        with app.test_request_context():
            e, tree = _xml(inv, "g7_at_standard")
        assert _text(tree, "//ram:SellerTradeParty//ram:CountryID") == "AT"
        assert _text(tree, "//ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID") == \
            "ATU12345678"
        assert Decimal(_text(tree, "//ram:GrandTotalAmount")) == inv.total_amount

    def test_small_business_uses_the_austrian_note(self, app):
        """G8: AT-Kleinunternehmer — Begruendung § 6 Abs. 1 Z 27 UStG."""
        _seller({**AT_SELLER, "wg.tax_number": "12 345/6789"}, vat_liable=False)
        inv = _invoice([_item("Wasserverbrauch", "50", "m³", "1.40")])
        with app.test_request_context():
            e, tree = _xml(inv, "g8_at_small_business")
        assert "§ 6 Abs. 1 Z 27" in _text(tree, "//ram:ExemptionReason")


class TestRulesAndService:
    def test_missing_seller_address_is_reported(self, app):
        _seller({**DE_SELLER, "wg.address": "Brunnenweg 1"})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")])
        with app.test_request_context():
            issues = check(build_einvoice(inv))
        assert any("PLZ und Ort" in issue for issue in issues)

    def test_structured_address_wins_over_free_text(self, app):
        _seller({**DE_SELLER, "wg.street": "Quellweg 9", "wg.postal_code": "91235",
                 "wg.city": "Neudorf"})
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")])
        with app.test_request_context():
            e = build_einvoice(inv)
        assert (e.seller.address.line1, e.seller.address.postcode, e.seller.address.city) == \
            ("Quellweg 9", "91235", "Neudorf")

    def test_xml_is_deterministic(self, app):
        _seller(DE_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")])
        with app.test_request_context():
            assert cii.serialize(build_einvoice(inv)) == cii.serialize(build_einvoice(inv))

    def test_locked_invoice_freezes_its_xml(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
        _seller(DE_SELLER)
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")])
        with app.test_request_context():
            first = einvoice_xml(inv)
            assert inv.xml_path and os.path.exists(inv.xml_path)
            assert inv.einvoice_profile == "en16931"
            # Stammdaten aendern sich — das eingefrorene XML bleibt.
            AppSetting.set("wg.name", "Neuer Name eG")
            assert einvoice_xml(inv) == first
            assert b"Neuer Name eG" not in first
            assert invoice_status(inv).frozen

    def test_draft_is_only_frozen_on_request(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
        _seller(DE_SELLER)
        draft = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                         status=Invoice.STATUS_DRAFT)
        with app.test_request_context():
            assert einvoice_xml(draft) is not None
            assert draft.xml_path is None
            einvoice_xml(draft, freeze=True)
            assert draft.xml_path and os.path.exists(draft.xml_path)

    def test_switched_off(self, app):
        _seller(DE_SELLER)
        AppSetting.set("einvoice.enabled", "false")
        inv = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")])
        with app.test_request_context():
            assert einvoice_xml(inv) is None
            status = invoice_status(inv)
        assert not status.enabled and not status.ok
