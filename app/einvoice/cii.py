"""EN-16931-Modell → UN/CEFACT Cross Industry Invoice (CII, D16B).

Profil „EN 16931" von ZUGFeRD 2.x/Factur-X. Die Reihenfolge der Elemente folgt
dem XSD (Sequenzen) — beim Ergaenzen eines Elements immer an der Stelle
einfuegen, die das Schema vorgibt, sonst ist die Datei ein Formatfehler und
damit keine E-Rechnung. Die Ausgabe ist deterministisch (keine Zeitstempel):
dieselbe Rechnung ergibt Byte fuer Byte dasselbe XML.
"""
from decimal import Decimal

from lxml import etree

GUIDELINE_EN16931 = "urn:cen.eu:en16931:2017"

NS_RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
NS_RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
NS_QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
NS_UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
_NSMAP = {"rsm": NS_RSM, "ram": NS_RAM, "qdt": NS_QDT, "udt": NS_UDT}


def _amount(value):
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def _price(value):
    return format(Decimal(value).quantize(Decimal("0.0001")), "f")


def _quantity(value):
    return format(Decimal(value).quantize(Decimal("0.001")), "f")


def _rate(value):
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def _el(parent, ns, tag, text=None, **attrib):
    node = etree.SubElement(parent, f"{{{ns}}}{tag}", attrib)
    if text is not None:
        node.text = text
    return node


def _ram(parent, tag, text=None, **attrib):
    return _el(parent, NS_RAM, tag, text, **attrib)


def _date(parent, tag, value, ns=NS_UDT):
    wrapper = _ram(parent, tag)
    _el(wrapper, ns, "DateTimeString", value.strftime("%Y%m%d"), format="102")


def _address(parent, address):
    node = _ram(parent, "PostalTradeAddress")
    if address.postcode:
        _ram(node, "PostcodeCode", address.postcode)
    if address.line1:
        _ram(node, "LineOne", address.line1)
    if address.city:
        _ram(node, "CityName", address.city)
    _ram(node, "CountryID", address.country_code or "")


def _party(parent, tag, party):
    node = _ram(parent, tag)
    if party.identifier:
        _ram(node, "ID", party.identifier)
    _ram(node, "Name", party.name)
    if party.legal_id:
        legal = _ram(node, "SpecifiedLegalOrganization")
        _ram(legal, "ID", party.legal_id)
    if party.contact_name or party.contact_phone:
        contact = _ram(node, "DefinedTradeContact")
        if party.contact_name:
            _ram(contact, "PersonName", party.contact_name)
        if party.contact_phone:
            phone = _ram(contact, "TelephoneUniversalCommunication")
            _ram(phone, "CompleteNumber", party.contact_phone)
        if party.contact_email:
            mail = _ram(contact, "EmailURIUniversalCommunication")
            _ram(mail, "URIID", party.contact_email)
    _address(node, party.address)
    if party.email:
        uri = _ram(node, "URIUniversalCommunication")
        _ram(uri, "URIID", party.email, schemeID="EM")
    if party.vat_id:
        reg = _ram(node, "SpecifiedTaxRegistration")
        _ram(reg, "ID", party.vat_id, schemeID="VA")
    if party.tax_number:
        reg = _ram(node, "SpecifiedTaxRegistration")
        _ram(reg, "ID", party.tax_number, schemeID="FC")
    return node


def _line(parent, line):
    item = _ram(parent, "IncludedSupplyChainTradeLineItem")
    doc = _ram(item, "AssociatedDocumentLineDocument")
    _ram(doc, "LineID", line.line_id)
    product = _ram(item, "SpecifiedTradeProduct")
    _ram(product, "Name", line.name)
    agreement = _ram(item, "SpecifiedLineTradeAgreement")
    price = _ram(agreement, "NetPriceProductTradePrice")
    _ram(price, "ChargeAmount", _price(line.net_price))
    delivery = _ram(item, "SpecifiedLineTradeDelivery")
    _ram(delivery, "BilledQuantity", _quantity(line.quantity), unitCode=line.unit_code)
    settlement = _ram(item, "SpecifiedLineTradeSettlement")
    tax = _ram(settlement, "ApplicableTradeTax")
    _ram(tax, "TypeCode", "VAT")
    _ram(tax, "CategoryCode", line.vat_category)
    _ram(tax, "RateApplicablePercent", _rate(line.vat_rate))
    summation = _ram(settlement, "SpecifiedTradeSettlementLineMonetarySummation")
    _ram(summation, "LineTotalAmount", _amount(line.net_amount))


def serialize(e, guideline=GUIDELINE_EN16931):
    """CII-XML der E-Rechnung als UTF-8-Bytes."""
    root = etree.Element(f"{{{NS_RSM}}}CrossIndustryInvoice", nsmap=_NSMAP)

    context = _el(root, NS_RSM, "ExchangedDocumentContext")
    guideline_node = _ram(context, "GuidelineSpecifiedDocumentContextParameter")
    _ram(guideline_node, "ID", guideline)

    document = _el(root, NS_RSM, "ExchangedDocument")
    _ram(document, "ID", e.number)
    _ram(document, "TypeCode", e.type_code)
    _date(document, "IssueDateTime", e.issue_date)
    for note in e.notes:
        included = _ram(document, "IncludedNote")
        _ram(included, "Content", note)

    transaction = _el(root, NS_RSM, "SupplyChainTradeTransaction")
    for line in e.lines:
        _line(transaction, line)

    agreement = _ram(transaction, "ApplicableHeaderTradeAgreement")
    _party(agreement, "SellerTradeParty", e.seller)
    _party(agreement, "BuyerTradeParty", e.buyer)

    delivery = _ram(transaction, "ApplicableHeaderTradeDelivery")
    if e.delivery and e.delivery.address:
        ship_to = _ram(delivery, "ShipToTradeParty")
        if e.delivery.location_id:
            _ram(ship_to, "ID", e.delivery.location_id)
        if e.delivery.name:
            _ram(ship_to, "Name", e.delivery.name)
        _address(ship_to, e.delivery.address)
    if e.delivery and e.delivery.date:
        event = _ram(delivery, "ActualDeliverySupplyChainEvent")
        _date(event, "OccurrenceDateTime", e.delivery.date)

    settlement = _ram(transaction, "ApplicableHeaderTradeSettlement")
    if e.payment and e.payment.reference:
        _ram(settlement, "PaymentReference", e.payment.reference)
    _ram(settlement, "InvoiceCurrencyCode", e.currency)
    if e.payment:
        means = _ram(settlement, "SpecifiedTradeSettlementPaymentMeans")
        _ram(means, "TypeCode", e.payment.means_code)
        account = _ram(means, "PayeePartyCreditorFinancialAccount")
        _ram(account, "IBANID", e.payment.iban)
        if e.payment.account_name:
            _ram(account, "AccountName", e.payment.account_name)
        if e.payment.bic:
            institution = _ram(means, "PayeeSpecifiedCreditorFinancialInstitution")
            _ram(institution, "BICID", e.payment.bic)
    for vat in e.vat:
        tax = _ram(settlement, "ApplicableTradeTax")
        _ram(tax, "CalculatedAmount", _amount(vat.tax_amount))
        _ram(tax, "TypeCode", "VAT")
        if vat.exemption_reason:
            _ram(tax, "ExemptionReason", vat.exemption_reason)
        _ram(tax, "BasisAmount", _amount(vat.taxable_amount))
        _ram(tax, "CategoryCode", vat.category)
        _ram(tax, "RateApplicablePercent", _rate(vat.rate))
    if e.period_start and e.period_end:
        period = _ram(settlement, "BillingSpecifiedPeriod")
        _date(period, "StartDateTime", e.period_start)
        _date(period, "EndDateTime", e.period_end)
    if e.payment_terms or e.due_date:
        terms = _ram(settlement, "SpecifiedTradePaymentTerms")
        if e.payment_terms:
            _ram(terms, "Description", e.payment_terms)
        if e.due_date:
            _date(terms, "DueDateDateTime", e.due_date)
    totals = _ram(settlement, "SpecifiedTradeSettlementHeaderMonetarySummation")
    _ram(totals, "LineTotalAmount", _amount(e.line_total))
    _ram(totals, "TaxBasisTotalAmount", _amount(e.tax_basis_total))
    _ram(totals, "TaxTotalAmount", _amount(e.tax_total), currencyID=e.currency)
    _ram(totals, "GrandTotalAmount", _amount(e.grand_total))
    _ram(totals, "DuePayableAmount", _amount(e.due_payable))
    if e.preceding_number:
        referenced = _ram(settlement, "InvoiceReferencedDocument")
        _ram(referenced, "IssuerAssignedID", e.preceding_number)
        if e.preceding_date:
            _date(referenced, "FormattedIssueDateTime", e.preceding_date, ns=NS_QDT)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
