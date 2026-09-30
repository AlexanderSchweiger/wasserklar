"""EN-16931-Modell → UBL 2.1 (Invoice bzw. CreditNote), Peppol BIS Billing 3.0.

Fuer Rechnungen an Bundesdienststellen in Oesterreich: e-Rechnung.gv.at nimmt UBL 2.1
gleichwertig zu ebInterface an (E_RECHNUNG_PLAN.md § 1.2). Dasselbe Format bedient
die Peppol-faehigen Behoerdenportale. Die Reihenfolge der Elemente folgt den
UBL-2.1-Schemas (xsd:sequence) — beim Ergaenzen eines Elements immer an der Stelle
einfuegen, die das Schema vorgibt, sonst ist die Datei ein Formatfehler.

Die Ausgabe ist deterministisch (keine Zeitstempel, keine UUIDs).
"""
import base64
from decimal import Decimal

from lxml import etree

CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

TYPE_CREDIT_NOTE = "381"


def _amount(value):
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def _price(value):
    return format(Decimal(value).quantize(Decimal("0.0001")), "f")


def _quantity(value):
    return format(Decimal(value).quantize(Decimal("0.001")), "f")


def _rate(value):
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def _iso(value):
    return value.strftime("%Y-%m-%d")


def _sub(parent, ns, tag, text=None, **attrib):
    node = etree.SubElement(parent, f"{{{ns}}}{tag}", attrib)
    if text is not None:
        node.text = text
    return node


def _cbc(parent, tag, text=None, **attrib):
    return _sub(parent, NS_CBC, tag, text, **attrib)


def _cac(parent, tag):
    return _sub(parent, NS_CAC, tag)


def _money(parent, tag, value, currency):
    return _cbc(parent, tag, _amount(value), currencyID=currency)


def _address(parent, tag, address):
    node = _cac(parent, tag)
    if address.line1:
        _cbc(node, "StreetName", address.line1)
    if address.city:
        _cbc(node, "CityName", address.city)
    if address.postcode:
        _cbc(node, "PostalZone", address.postcode)
    country = _cac(node, "Country")
    _cbc(country, "IdentificationCode", address.country_code or "")


def _party(parent, tag, party):
    wrapper = _cac(parent, tag)
    node = _cac(wrapper, "Party")
    if party.endpoint:
        _cbc(node, "EndpointID", party.endpoint, schemeID=party.endpoint_scheme)
    if party.identifier:
        ident = _cac(node, "PartyIdentification")
        _cbc(ident, "ID", party.identifier)
    name = _cac(node, "PartyName")
    _cbc(name, "Name", party.name)
    _address(node, "PostalAddress", party.address)
    if party.vat_id:
        scheme = _cac(node, "PartyTaxScheme")
        _cbc(scheme, "CompanyID", party.vat_id)
        _cbc(_cac(scheme, "TaxScheme"), "ID", "VAT")
    if party.tax_number:
        scheme = _cac(node, "PartyTaxScheme")
        _cbc(scheme, "CompanyID", party.tax_number)
        _cbc(_cac(scheme, "TaxScheme"), "ID", "FC")
    legal = _cac(node, "PartyLegalEntity")
    _cbc(legal, "RegistrationName", party.name)
    if party.legal_id:
        _cbc(legal, "CompanyID", party.legal_id)
    if party.contact_name or party.contact_phone or party.contact_email:
        contact = _cac(node, "Contact")
        if party.contact_name:
            _cbc(contact, "Name", party.contact_name)
        if party.contact_phone:
            _cbc(contact, "Telephone", party.contact_phone)
        if party.contact_email:
            _cbc(contact, "ElectronicMail", party.contact_email)


def _tax_category(parent, tag, category, rate, reason=None, *, with_reason=True):
    node = _cac(parent, tag)
    _cbc(node, "ID", category)
    _cbc(node, "Percent", _rate(rate))
    if reason and with_reason:
        _cbc(node, "TaxExemptionReason", reason)
    _cbc(_cac(node, "TaxScheme"), "ID", "VAT")


def _line(parent, line, *, credit, currency):
    node = _cac(parent, "CreditNoteLine" if credit else "InvoiceLine")
    _cbc(node, "ID", line.line_id)
    _cbc(node, "CreditedQuantity" if credit else "InvoicedQuantity", _quantity(line.quantity),
         unitCode=line.unit_code)
    _money(node, "LineExtensionAmount", line.net_amount, currency)
    item = _cac(node, "Item")
    _cbc(item, "Name", line.name)
    _tax_category(item, "ClassifiedTaxCategory", line.vat_category, line.vat_rate)
    price = _cac(node, "Price")
    _cbc(price, "PriceAmount", _price(line.net_price), currencyID=currency)


def serialize(e):
    """UBL-XML der E-Rechnung als UTF-8-Bytes (``Invoice``, bei Typ 381 ``CreditNote``)."""
    credit = e.type_code == TYPE_CREDIT_NOTE
    ns_root = NS_CREDIT_NOTE if credit else NS_INVOICE
    root = etree.Element(f"{{{ns_root}}}{'CreditNote' if credit else 'Invoice'}",
                         nsmap={None: ns_root, "cac": NS_CAC, "cbc": NS_CBC})
    currency = e.currency

    _cbc(root, "CustomizationID", CUSTOMIZATION_ID)
    _cbc(root, "ProfileID", PROFILE_ID)
    _cbc(root, "ID", e.number)
    _cbc(root, "IssueDate", _iso(e.issue_date))
    if e.due_date and not credit:
        _cbc(root, "DueDate", _iso(e.due_date))
    _cbc(root, "CreditNoteTypeCode" if credit else "InvoiceTypeCode", e.type_code)
    for note in e.notes:
        _cbc(root, "Note", note)
    _cbc(root, "DocumentCurrencyCode", currency)
    if e.buyer_reference:
        _cbc(root, "BuyerReference", e.buyer_reference)
    if e.period_start and e.period_end:
        period = _cac(root, "InvoicePeriod")
        _cbc(period, "StartDate", _iso(e.period_start))
        _cbc(period, "EndDate", _iso(e.period_end))
    if e.order_reference:
        _cbc(_cac(root, "OrderReference"), "ID", e.order_reference)
    if e.preceding_number:
        billing = _cac(root, "BillingReference")
        invoice_ref = _cac(billing, "InvoiceDocumentReference")
        _cbc(invoice_ref, "ID", e.preceding_number)
        if e.preceding_date:
            _cbc(invoice_ref, "IssueDate", _iso(e.preceding_date))
    if e.attachment:
        doc = _cac(root, "AdditionalDocumentReference")
        _cbc(doc, "ID", e.attachment.filename)
        _cbc(doc, "DocumentDescription", e.attachment.name)
        attachment = _cac(doc, "Attachment")
        _cbc(attachment, "EmbeddedDocumentBinaryObject",
             base64.b64encode(e.attachment.data).decode("ascii"),
             mimeCode=e.attachment.mime_code, filename=e.attachment.filename)

    _party(root, "AccountingSupplierParty", e.seller)
    _party(root, "AccountingCustomerParty", e.buyer)

    if e.delivery and (e.delivery.date or e.delivery.address):
        delivery = _cac(root, "Delivery")
        if e.delivery.date:
            _cbc(delivery, "ActualDeliveryDate", _iso(e.delivery.date))
        if e.delivery.address:
            location = _cac(delivery, "DeliveryLocation")
            if e.delivery.location_id:
                _cbc(location, "ID", e.delivery.location_id)
            _address(location, "Address", e.delivery.address)
            if e.delivery.name:
                party = _cac(_cac(delivery, "DeliveryParty"), "PartyName")
                _cbc(party, "Name", e.delivery.name)

    if e.payment:
        means = _cac(root, "PaymentMeans")
        _cbc(means, "PaymentMeansCode", e.payment.means_code)
        if credit and e.due_date:
            _cbc(means, "PaymentDueDate", _iso(e.due_date))
        if e.payment.reference:
            _cbc(means, "PaymentID", e.payment.reference)
        account = _cac(means, "PayeeFinancialAccount")
        _cbc(account, "ID", e.payment.iban)
        if e.payment.account_name:
            _cbc(account, "Name", e.payment.account_name)
        if e.payment.bic:
            _cbc(_cac(account, "FinancialInstitutionBranch"), "ID", e.payment.bic)
    if e.payment_terms:
        _cbc(_cac(root, "PaymentTerms"), "Note", e.payment_terms)

    tax_total = _cac(root, "TaxTotal")
    _money(tax_total, "TaxAmount", e.tax_total, currency)
    for vat in e.vat:
        subtotal = _cac(tax_total, "TaxSubtotal")
        _money(subtotal, "TaxableAmount", vat.taxable_amount, currency)
        _money(subtotal, "TaxAmount", vat.tax_amount, currency)
        _tax_category(subtotal, "TaxCategory", vat.category, vat.rate, vat.exemption_reason)

    totals = _cac(root, "LegalMonetaryTotal")
    _money(totals, "LineExtensionAmount", e.line_total, currency)
    _money(totals, "TaxExclusiveAmount", e.tax_basis_total, currency)
    _money(totals, "TaxInclusiveAmount", e.grand_total, currency)
    _money(totals, "PayableAmount", e.due_payable, currency)

    for line in e.lines:
        _line(root, line, credit=credit, currency=currency)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
