"""Eingangs-E-Rechnungen lesen: CII (ZUGFeRD/Factur-X/XRechnung) und UBL (XRechnung/Peppol).

Flask-frei. Liest das Original nur — es wird nirgends veraendert (§ 14b UStG: der
strukturierte Teil ist unveraendert aufzubewahren). ``parse`` nimmt die Bytes einer
XML-Datei oder eines ZUGFeRD/Factur-X-PDFs und liefert eine ``ParsedInvoice`` mit
den Daten fuer Ansicht und Buchungsvorschlag.

Fremddateien sind nicht vertrauenswuerdig: der XML-Parser loest keine Entitaeten auf,
laedt nichts nach und lehnt jede DTD ab (XXE, Billion-Laughs).
"""
import base64
import io
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from lxml import etree

CREDIT_NOTE_CODES = ("381", "261")          # Gutschrift: Beträge zugunsten des Empfaengers
SYNTAX_CII = "cii"
SYNTAX_UBL = "ubl"
SOURCE_XML = "xml"
SOURCE_PDF = "pdf"

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "crn": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
}
_CII_ROOT = "{%s}CrossIndustryInvoice" % NS["rsm"]
_UBL_ROOTS = ("{%s}Invoice" % NS["inv"], "{%s}CreditNote" % NS["crn"])
# Dateinamen eingebetteter Rechnungs-XML in Hybrid-PDFs, in dieser Reihenfolge.
_EMBEDDED_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml", "ubl-invoice.xml")


class IncomingError(Exception):
    """Die Datei ist keine lesbare E-Rechnung; die Meldung ist fuer den Nutzer gedacht."""


class NotAnEInvoice(IncomingError):
    """Ein PDF ohne eingebettete E-Rechnungsdaten — kein Fehler der Datei, sondern ein
    ganz normales Dokument (die Belegablage legt es als solches ab)."""


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------

@dataclass
class InParty:
    name: str = ""
    vat_id: Optional[str] = None        # BT-31 / BT-48
    tax_number: Optional[str] = None    # BT-32
    legal_id: Optional[str] = None      # BT-30
    identifier: Optional[str] = None    # BT-29 / BT-46
    street: str = ""
    postcode: str = ""
    city: str = ""
    country: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    contact: Optional[str] = None


@dataclass
class InLine:
    line_id: str = ""
    name: str = ""
    quantity: Optional[Decimal] = None
    unit: str = ""
    net_price: Optional[Decimal] = None
    net_amount: Decimal = Decimal("0")
    vat_category: str = ""
    vat_rate: Decimal = Decimal("0")


@dataclass
class InVat:
    category: str = ""
    rate: Decimal = Decimal("0")
    taxable: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    exemption_reason: Optional[str] = None


@dataclass
class InAttachment:
    filename: str = ""
    mime: str = ""
    description: str = ""
    size: int = 0


@dataclass
class ParsedInvoice:
    syntax: str = SYNTAX_CII
    guideline: str = ""
    number: str = ""
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    type_code: str = "380"
    currency: str = "EUR"
    seller: InParty = field(default_factory=InParty)
    buyer: InParty = field(default_factory=InParty)
    lines: list = field(default_factory=list)
    vat: list = field(default_factory=list)
    line_total: Decimal = Decimal("0")
    tax_basis_total: Decimal = Decimal("0")
    tax_total: Decimal = Decimal("0")
    grand_total: Decimal = Decimal("0")
    prepaid: Decimal = Decimal("0")
    payable: Decimal = Decimal("0")
    payment_terms: Optional[str] = None
    payment_means_code: Optional[str] = None
    iban: Optional[str] = None
    bic: Optional[str] = None
    payment_reference: Optional[str] = None
    notes: list = field(default_factory=list)
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    buyer_reference: Optional[str] = None
    order_reference: Optional[str] = None
    preceding_number: Optional[str] = None
    attachments: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def is_credit_note(self):
        return self.type_code in CREDIT_NOTE_CODES

    def to_json(self):
        return json.dumps(asdict(self), default=_json_default, ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        return _from_dict(json.loads(text))


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value))


def _d(value):
    return Decimal(value) if value not in (None, "") else None


def _date(value):
    return date.fromisoformat(value) if value else None


def _from_dict(data):
    def party(raw):
        return InParty(**(raw or {}))

    def dec(raw):
        return Decimal(raw) if raw is not None else None

    lines = [InLine(line_id=x["line_id"], name=x["name"], quantity=dec(x["quantity"]), unit=x["unit"],
                    net_price=dec(x["net_price"]), net_amount=Decimal(x["net_amount"]),
                    vat_category=x["vat_category"], vat_rate=Decimal(x["vat_rate"]))
             for x in data.get("lines", [])]
    vat = [InVat(category=x["category"], rate=Decimal(x["rate"]), taxable=Decimal(x["taxable"]),
                 tax=Decimal(x["tax"]), exemption_reason=x.get("exemption_reason"))
           for x in data.get("vat", [])]
    invoice = ParsedInvoice(
        syntax=data["syntax"], guideline=data.get("guideline", ""), number=data.get("number", ""),
        issue_date=_date(data.get("issue_date")), due_date=_date(data.get("due_date")),
        type_code=data.get("type_code", "380"), currency=data.get("currency", "EUR"),
        seller=party(data.get("seller")), buyer=party(data.get("buyer")), lines=lines, vat=vat,
        line_total=Decimal(data["line_total"]), tax_basis_total=Decimal(data["tax_basis_total"]),
        tax_total=Decimal(data["tax_total"]), grand_total=Decimal(data["grand_total"]),
        prepaid=Decimal(data["prepaid"]), payable=Decimal(data["payable"]),
        payment_terms=data.get("payment_terms"), payment_means_code=data.get("payment_means_code"),
        iban=data.get("iban"), bic=data.get("bic"), payment_reference=data.get("payment_reference"),
        notes=data.get("notes", []), period_start=_date(data.get("period_start")),
        period_end=_date(data.get("period_end")), buyer_reference=data.get("buyer_reference"),
        order_reference=data.get("order_reference"), preceding_number=data.get("preceding_number"),
        attachments=[InAttachment(**a) for a in data.get("attachments", [])],
        warnings=data.get("warnings", []))
    return invoice


# ---------------------------------------------------------------------------
# Einlesen
# ---------------------------------------------------------------------------

@dataclass
class ParsedDocument:
    invoice: ParsedInvoice
    source_kind: str        # xml | pdf
    xml: bytes              # das gelesene Rechnungs-XML (bei PDF: der eingebettete Anhang)


def parse(data, filename=""):
    """Liest eine E-Rechnung aus den Bytes einer XML-Datei oder eines Hybrid-PDFs."""
    if not data:
        raise IncomingError("Die Datei ist leer.")
    if data.lstrip()[:5] == b"%PDF-":
        xml = _xml_from_pdf(data)
        return ParsedDocument(_parse_xml(xml), SOURCE_PDF, xml)
    return ParsedDocument(_parse_xml(data), SOURCE_XML, data)


def _xml_from_pdf(data):
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise IncomingError("Das PDF ist verschlüsselt und lässt sich nicht lesen.")
        attachments = reader.attachments
    except IncomingError:
        raise
    except Exception as exc:  # noqa: BLE001 — defekte PDFs sind ein Nutzerfehler, kein Systemfehler
        raise IncomingError(f"Das PDF lässt sich nicht lesen ({type(exc).__name__}).") from exc
    by_name = {name.lower(): blobs for name, blobs in attachments.items() if blobs}
    for wanted in _EMBEDDED_NAMES:
        if wanted in by_name:
            return by_name[wanted][0]
    for name, blobs in by_name.items():
        if name.endswith(".xml") and _root_kind(blobs[0]):
            return blobs[0]
    raise NotAnEInvoice(
        "Das PDF enthält keine E-Rechnungsdaten (kein ZUGFeRD/Factur-X-Anhang). Ein PDF ohne "
        "strukturierten Anteil ist keine E-Rechnung — es lässt sich als Beleg ablegen und von "
        "Hand buchen.")


def _safe_parse(data):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             dtd_validation=False, huge_tree=False, remove_comments=True)
    try:
        tree = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise IncomingError(f"Die Datei ist kein gültiges XML ({exc.msg}).") from exc
    if tree.getroottree().docinfo.doctype:
        raise IncomingError("Die Datei enthält eine DTD und wird aus Sicherheitsgründen abgelehnt.")
    return tree


def _root_kind(data):
    try:
        root = _safe_parse(data)
    except IncomingError:
        return None
    if root.tag == _CII_ROOT:
        return SYNTAX_CII
    if root.tag in _UBL_ROOTS:
        return SYNTAX_UBL
    return None


def _parse_xml(data):
    root = _safe_parse(data)
    if root.tag == _CII_ROOT:
        return _parse_cii(root)
    if root.tag in _UBL_ROOTS:
        return _parse_ubl(root)
    local = etree.QName(root).localname
    if local == "CrossIndustryDocument":
        raise IncomingError("ZUGFeRD 1.x wird nicht unterstützt — bitte eine Rechnung im aktuellen "
                            "ZUGFeRD/Factur-X-Format (ab 2.0) anfordern.")
    raise IncomingError(f"Kein bekanntes E-Rechnungs-Format (Wurzelelement „{local}“). Unterstützt: "
                        "ZUGFeRD/Factur-X/XRechnung (CII) und XRechnung/Peppol (UBL).")


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def _first(node, path):
    found = node.xpath(path, namespaces=NS)
    return found[0] if found else None


def _text(node, path):
    found = _first(node, path)
    if found is None:
        return None
    value = found if isinstance(found, str) else (found.text or "")
    return " ".join(value.split()) or None


def _texts(node, path):
    out = []
    for found in node.xpath(path, namespaces=NS):
        value = " ".join((found if isinstance(found, str) else (found.text or "")).split())
        if value:
            out.append(value)
    return out


def _num(node, path, default=None):
    raw = _text(node, path)
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation:
        return default


def _cii_date(node, path):
    raw = _text(node, path)
    if not raw:
        return None
    digits = raw.replace("-", "")
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _iso_date(node, path):
    raw = _text(node, path)
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def _check_totals(invoice):
    """Hinweise, wenn die Summen der Datei nicht zueinander passen (kein Abbruch)."""
    if invoice.vat:
        taxable = sum((v.taxable for v in invoice.vat), Decimal("0"))
        tax = sum((v.tax for v in invoice.vat), Decimal("0"))
        if abs(taxable + tax - invoice.grand_total) > Decimal("0.05"):
            invoice.warnings.append(
                f"Die USt-Aufstellung ({taxable + tax} €) passt nicht zum Gesamtbetrag "
                f"({invoice.grand_total} €).")
    if invoice.grand_total == 0 and not invoice.vat and not invoice.lines:
        invoice.warnings.append("Die Datei enthält weder Positionen noch Summen.")
    if invoice.currency != "EUR":
        invoice.warnings.append(f"Fremdwährung {invoice.currency}: Beträge bitte vor dem Buchen umrechnen.")
    if not invoice.issue_date:
        invoice.warnings.append("Das Rechnungsdatum fehlt in der Datei.")


# ---------------------------------------------------------------------------
# CII
# ---------------------------------------------------------------------------

def _cii_party(node):
    if node is None:
        return InParty()
    vat = _first(node, "ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']")
    fc = _first(node, "ram:SpecifiedTaxRegistration/ram:ID[@schemeID='FC']")
    return InParty(
        name=_text(node, "ram:Name") or "",
        vat_id=(vat.text or "").replace(" ", "").upper() or None if vat is not None else None,
        tax_number=(fc.text or "").strip() or None if fc is not None else None,
        legal_id=_text(node, "ram:SpecifiedLegalOrganization/ram:ID"),
        identifier=_text(node, "ram:ID"),
        street=" ".join(_texts(node, "ram:PostalTradeAddress/ram:LineOne | ram:PostalTradeAddress/ram:LineTwo")),
        postcode=_text(node, "ram:PostalTradeAddress/ram:PostcodeCode") or "",
        city=_text(node, "ram:PostalTradeAddress/ram:CityName") or "",
        country=_text(node, "ram:PostalTradeAddress/ram:CountryID") or "",
        email=_text(node, "ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID")
        or _text(node, "ram:URIUniversalCommunication/ram:URIID[@schemeID='EM']"),
        phone=_text(node, "ram:DefinedTradeContact/ram:TelephoneUniversalCommunication/ram:CompleteNumber"),
        contact=_text(node, "ram:DefinedTradeContact/ram:PersonName"),
    )


def _parse_cii(root):
    trans = _first(root, "rsm:SupplyChainTradeTransaction")
    if trans is None:
        raise IncomingError("Die CII-Datei enthält keine Handelsdaten (SupplyChainTradeTransaction).")
    agreement = _first(trans, "ram:ApplicableHeaderTradeAgreement")
    settlement = _first(trans, "ram:ApplicableHeaderTradeSettlement")
    sums = _first(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation") if settlement is not None else None
    inv = ParsedInvoice(syntax=SYNTAX_CII)
    inv.guideline = _text(root, "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID") or ""
    inv.number = _text(root, "rsm:ExchangedDocument/ram:ID") or ""
    inv.type_code = _text(root, "rsm:ExchangedDocument/ram:TypeCode") or "380"
    inv.issue_date = _cii_date(root, "rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString")
    inv.notes = _texts(root, "rsm:ExchangedDocument/ram:IncludedNote/ram:Content")
    if agreement is not None:
        inv.seller = _cii_party(_first(agreement, "ram:SellerTradeParty"))
        inv.buyer = _cii_party(_first(agreement, "ram:BuyerTradeParty"))
        inv.buyer_reference = _text(agreement, "ram:BuyerReference")
        inv.order_reference = _text(agreement, "ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID")
        for doc in agreement.xpath("ram:AdditionalReferencedDocument[ram:AttachmentBinaryObject]", namespaces=NS):
            blob = _first(doc, "ram:AttachmentBinaryObject")
            inv.attachments.append(InAttachment(
                filename=blob.get("filename") or _text(doc, "ram:IssuerAssignedID") or "Anhang",
                mime=blob.get("mimeCode") or "", description=_text(doc, "ram:Name") or "",
                size=_b64_size(blob.text)))
    for item in trans.xpath("ram:IncludedSupplyChainTradeLineItem", namespaces=NS):
        inv.lines.append(InLine(
            line_id=_text(item, "ram:AssociatedDocumentLineDocument/ram:LineID") or "",
            name=_text(item, "ram:SpecifiedTradeProduct/ram:Name") or "",
            quantity=_num(item, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity"),
            unit=(_first(item, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity").get("unitCode") or "")
            if _first(item, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity") is not None else "",
            net_price=_num(item, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount"),
            net_amount=_num(item, "ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation"
                                  "/ram:LineTotalAmount", Decimal("0")),
            vat_category=_text(item, "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax/ram:CategoryCode") or "",
            vat_rate=_num(item, "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax/ram:RateApplicablePercent",
                          Decimal("0"))))
    if settlement is not None:
        inv.currency = _text(settlement, "ram:InvoiceCurrencyCode") or "EUR"
        inv.payment_reference = _text(settlement, "ram:PaymentReference")
        inv.payment_means_code = _text(settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:TypeCode")
        inv.iban = _text(settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID")
        inv.bic = _text(settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeeSpecifiedCreditorFinancialInstitution/ram:BICID")
        inv.payment_terms = _text(settlement, "ram:SpecifiedTradePaymentTerms/ram:Description")
        inv.due_date = _cii_date(settlement, "ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString")
        inv.period_start = _cii_date(settlement, "ram:BillingSpecifiedPeriod/ram:StartDateTime/udt:DateTimeString")
        inv.period_end = _cii_date(settlement, "ram:BillingSpecifiedPeriod/ram:EndDateTime/udt:DateTimeString")
        inv.preceding_number = _text(settlement, "ram:InvoiceReferencedDocument/ram:IssuerAssignedID")
        for tax in settlement.xpath("ram:ApplicableTradeTax", namespaces=NS):
            inv.vat.append(InVat(
                category=_text(tax, "ram:CategoryCode") or "",
                rate=_num(tax, "ram:RateApplicablePercent", Decimal("0")),
                taxable=_num(tax, "ram:BasisAmount", Decimal("0")),
                tax=_num(tax, "ram:CalculatedAmount", Decimal("0")),
                exemption_reason=_text(tax, "ram:ExemptionReason")))
    if sums is not None:
        inv.line_total = _num(sums, "ram:LineTotalAmount", Decimal("0"))
        inv.tax_basis_total = _num(sums, "ram:TaxBasisTotalAmount", inv.line_total)
        inv.tax_total = _num(sums, "ram:TaxTotalAmount[@currencyID='%s'] | ram:TaxTotalAmount" % inv.currency,
                             Decimal("0"))
        inv.grand_total = _num(sums, "ram:GrandTotalAmount", inv.tax_basis_total + inv.tax_total)
        inv.prepaid = _num(sums, "ram:TotalPrepaidAmount", Decimal("0"))
        inv.payable = _num(sums, "ram:DuePayableAmount", inv.grand_total - inv.prepaid)
    else:
        inv.grand_total = inv.payable = sum((v.taxable + v.tax for v in inv.vat), Decimal("0"))
    _check_totals(inv)
    return inv


def _b64_size(text):
    try:
        return len(base64.b64decode("".join((text or "").split()), validate=False))
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# UBL
# ---------------------------------------------------------------------------

def _ubl_party(node):
    if node is None:
        return InParty()
    party = _first(node, "cac:Party")
    if party is None:
        return InParty()
    vat = _text(party, "cac:PartyTaxScheme[cac:TaxScheme/cbc:ID='VAT']/cbc:CompanyID")
    fc = _text(party, "cac:PartyTaxScheme[cac:TaxScheme/cbc:ID!='VAT']/cbc:CompanyID")
    endpoint = _first(party, "cbc:EndpointID")
    email = _text(party, "cac:Contact/cbc:ElectronicMail")
    if not email and endpoint is not None and endpoint.get("schemeID") == "EM":
        email = (endpoint.text or "").strip() or None
    return InParty(
        name=_text(party, "cac:PartyLegalEntity/cbc:RegistrationName") or _text(party, "cac:PartyName/cbc:Name") or "",
        vat_id=(vat or "").replace(" ", "").upper() or None,
        tax_number=fc,
        legal_id=_text(party, "cac:PartyLegalEntity/cbc:CompanyID"),
        identifier=_text(party, "cac:PartyIdentification/cbc:ID"),
        street=" ".join(_texts(party, "cac:PostalAddress/cbc:StreetName | cac:PostalAddress/cbc:AdditionalStreetName")),
        postcode=_text(party, "cac:PostalAddress/cbc:PostalZone") or "",
        city=_text(party, "cac:PostalAddress/cbc:CityName") or "",
        country=_text(party, "cac:PostalAddress/cac:Country/cbc:IdentificationCode") or "",
        email=email,
        phone=_text(party, "cac:Contact/cbc:Telephone"),
        contact=_text(party, "cac:Contact/cbc:Name"),
    )


def _parse_ubl(root):
    credit = root.tag == "{%s}CreditNote" % NS["crn"]
    inv = ParsedInvoice(syntax=SYNTAX_UBL)
    inv.guideline = _text(root, "cbc:CustomizationID") or ""
    inv.number = _text(root, "cbc:ID") or ""
    inv.type_code = _text(root, "cbc:CreditNoteTypeCode" if credit else "cbc:InvoiceTypeCode") or ("381" if credit else "380")
    inv.issue_date = _iso_date(root, "cbc:IssueDate")
    inv.due_date = _iso_date(root, "cbc:DueDate") or _iso_date(root, "cac:PaymentMeans/cbc:PaymentDueDate")
    inv.currency = _text(root, "cbc:DocumentCurrencyCode") or "EUR"
    inv.notes = _texts(root, "cbc:Note")
    inv.buyer_reference = _text(root, "cbc:BuyerReference")
    inv.order_reference = _text(root, "cac:OrderReference/cbc:ID")
    inv.period_start = _iso_date(root, "cac:InvoicePeriod/cbc:StartDate")
    inv.period_end = _iso_date(root, "cac:InvoicePeriod/cbc:EndDate")
    inv.preceding_number = _text(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID")
    inv.seller = _ubl_party(_first(root, "cac:AccountingSupplierParty"))
    inv.buyer = _ubl_party(_first(root, "cac:AccountingCustomerParty"))
    inv.payment_means_code = _text(root, "cac:PaymentMeans/cbc:PaymentMeansCode")
    inv.payment_reference = _text(root, "cac:PaymentMeans/cbc:PaymentID")
    inv.iban = _text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID")
    inv.bic = _text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cac:FinancialInstitutionBranch/cbc:ID")
    inv.payment_terms = _text(root, "cac:PaymentTerms/cbc:Note")
    for doc in root.xpath("cac:AdditionalDocumentReference[cac:Attachment/cbc:EmbeddedDocumentBinaryObject]", namespaces=NS):
        blob = _first(doc, "cac:Attachment/cbc:EmbeddedDocumentBinaryObject")
        inv.attachments.append(InAttachment(
            filename=blob.get("filename") or _text(doc, "cbc:ID") or "Anhang",
            mime=blob.get("mimeCode") or "", description=_text(doc, "cbc:DocumentDescription") or "",
            size=_b64_size(blob.text)))
    line_tag = "cac:CreditNoteLine" if credit else "cac:InvoiceLine"
    qty_tag = "cbc:CreditedQuantity" if credit else "cbc:InvoicedQuantity"
    for item in root.xpath(line_tag, namespaces=NS):
        quantity = _first(item, qty_tag)
        inv.lines.append(InLine(
            line_id=_text(item, "cbc:ID") or "",
            name=_text(item, "cac:Item/cbc:Name") or _text(item, "cac:Item/cbc:Description") or "",
            quantity=_num(item, qty_tag),
            unit=(quantity.get("unitCode") or "") if quantity is not None else "",
            net_price=_num(item, "cac:Price/cbc:PriceAmount"),
            net_amount=_num(item, "cbc:LineExtensionAmount", Decimal("0")),
            vat_category=_text(item, "cac:Item/cac:ClassifiedTaxCategory/cbc:ID") or "",
            vat_rate=_num(item, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", Decimal("0"))))
    for total in root.xpath("cac:TaxTotal", namespaces=NS):
        for sub in total.xpath("cac:TaxSubtotal", namespaces=NS):
            inv.vat.append(InVat(
                category=_text(sub, "cac:TaxCategory/cbc:ID") or "",
                rate=_num(sub, "cac:TaxCategory/cbc:Percent", Decimal("0")),
                taxable=_num(sub, "cbc:TaxableAmount", Decimal("0")),
                tax=_num(sub, "cbc:TaxAmount", Decimal("0")),
                exemption_reason=_text(sub, "cac:TaxCategory/cbc:TaxExemptionReason")))
    own = root.xpath("cac:TaxTotal/cbc:TaxAmount[@currencyID='%s']" % inv.currency, namespaces=NS)
    inv.tax_total = Decimal(own[0].text) if own else sum((v.tax for v in inv.vat), Decimal("0"))
    inv.line_total = _num(root, "cac:LegalMonetaryTotal/cbc:LineExtensionAmount", Decimal("0"))
    inv.tax_basis_total = _num(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", inv.line_total)
    inv.grand_total = _num(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", inv.tax_basis_total + inv.tax_total)
    inv.prepaid = _num(root, "cac:LegalMonetaryTotal/cbc:PrepaidAmount", Decimal("0"))
    inv.payable = _num(root, "cac:LegalMonetaryTotal/cbc:PayableAmount", inv.grand_total - inv.prepaid)
    _check_totals(inv)
    return inv


# ---------------------------------------------------------------------------
# Anhaenge
# ---------------------------------------------------------------------------

def attachment_bytes(xml, index):
    """``(Dateiname, MIME-Typ, Bytes)`` des ``index``-ten eingebetteten Anhangs (BG-24) oder ``None``."""
    root = _safe_parse(xml)
    if root.tag == _CII_ROOT:
        blobs = root.xpath("//ram:AdditionalReferencedDocument/ram:AttachmentBinaryObject", namespaces=NS)
    elif root.tag in _UBL_ROOTS:
        blobs = root.xpath("//cac:AdditionalDocumentReference/cac:Attachment/cbc:EmbeddedDocumentBinaryObject",
                           namespaces=NS)
    else:
        return None
    if not 0 <= index < len(blobs):
        return None
    blob = blobs[index]
    try:
        content = base64.b64decode("".join((blob.text or "").split()))
    except Exception:  # noqa: BLE001
        return None
    return blob.get("filename") or f"anhang-{index + 1}", blob.get("mimeCode") or "application/octet-stream", content
