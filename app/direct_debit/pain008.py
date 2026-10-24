"""Einzugslauf → SEPA-Lastschrift-Sammelauftrag pain.008.001.08 (ISO 20022, Version 2019).

Die Datei, die der Kassier im Online-Banking (ELBA, George Business, Sparkasse,
VR-Banking …) hochlaedt. Basislastschrift (CORE), Folge-Lastschrift (RCUR — seit
dem EPC-Regelwerk 2016 auch fuer den ersten Einzug zulaessig), ein Sammler
(``PmtInf``) je Einzugsdatum.

Gebaut wird nur aus den Snapshots des Laufs (``DirectDebitRun``/``Item``) — die
Ausgabe ist deterministisch: derselbe Lauf ergibt Byte fuer Byte dieselbe Datei.
Die Reihenfolge der Elemente folgt dem XSD (``tests/fixtures/sepa/``); beim
Ergaenzen an der Stelle einfuegen, die das Schema vorgibt.
"""
from decimal import Decimal

from lxml import etree

from app.banking import iban_bank_code, normalize_iban, sepa_text

NS = "urn:iso:std:iso:20022:tech:xsd:pain.008.001.08"
NOT_PROVIDED = "NOTPROVIDED"
# SEPA-Laender ausserhalb des EWR: dort verlangt das Regelwerk die Anschrift des Zahlers.
NON_EEA_SEPA = {"CH", "GB", "MC", "SM", "VA", "AD", "GI"}


def _amount(value):
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def _el(parent, tag, text=None, **attrib):
    node = etree.SubElement(parent, f"{{{NS}}}{tag}", attrib)
    if text is not None:
        node.text = text
    return node


def _agent(parent, tag, bic):
    fin = _el(_el(parent, tag), "FinInstnId")
    if bic:
        _el(fin, "BICFI", bic)
    else:
        _el(_el(fin, "Othr"), "Id", NOT_PROVIDED)


def _account(parent, tag, iban):
    _el(_el(_el(parent, tag), "Id"), "IBAN", normalize_iban(iban))


def pmt_inf_id(run, day):
    from app.direct_debit.services import _pmt_inf_id
    return _pmt_inf_id(run, day)


def _debtor_address(party, item):
    country = normalize_iban(item.iban)[:2]
    if country not in NON_EEA_SEPA:
        return
    customer = item.customer
    adr = _el(party, "PstlAdr")
    if customer is not None:
        if customer.strasse:
            _el(adr, "StrtNm", sepa_text(customer.strasse, 70))
        if customer.hausnummer:
            _el(adr, "BldgNb", sepa_text(customer.hausnummer, 16))
        if customer.plz:
            _el(adr, "PstCd", sepa_text(customer.plz, 16))
        if customer.ort:
            _el(adr, "TwnNm", sepa_text(customer.ort, 35))
    _el(adr, "Ctry", country)


def _mandate(parent, item):
    info = _el(parent, "MndtRltdInf")
    _el(info, "MndtId", item.mandate_reference)
    _el(info, "DtOfSgntr", item.mandate_signed_on.isoformat())
    amended = bool(item.amended_from_mandate or item.amended_from_iban)
    _el(info, "AmdmntInd", "true" if amended else "false")
    if not amended:
        return
    details = _el(info, "AmdmntInfDtls")
    if item.amended_from_mandate:
        _el(details, "OrgnlMndtId", item.amended_from_mandate)
    if item.amended_from_iban:
        if iban_bank_code(item.amended_from_iban) == iban_bank_code(item.iban):
            # Kontowechsel innerhalb derselben Bank: alte IBAN angeben.
            _account(details, "OrgnlDbtrAcct", item.amended_from_iban)
        else:
            # Wechsel zu einer anderen Bank: „SMNDA“ (same mandate, new debtor agent).
            _el(_el(_el(_el(details, "OrgnlDbtrAgt"), "FinInstnId"), "Othr"), "Id", "SMNDA")


def build(run):
    """Bytes der pain.008-Datei eines Einzugslaufs (UTF-8 mit XML-Deklaration)."""
    items = [i for i in run.items]
    by_date = {}
    for item in items:
        by_date.setdefault(item.collection_date, []).append(item)

    root = etree.Element(f"{{{NS}}}Document", nsmap={None: NS})
    initn = _el(root, "CstmrDrctDbtInitn")

    hdr = _el(initn, "GrpHdr")
    _el(hdr, "MsgId", run.message_id)
    _el(hdr, "CreDtTm", run.created_at.replace(microsecond=0).isoformat())
    _el(hdr, "NbOfTxs", str(len(items)))
    _el(hdr, "CtrlSum", _amount(sum((Decimal(str(i.amount)) for i in items), Decimal("0"))))
    _el(_el(hdr, "InitgPty"), "Nm", sepa_text(run.creditor_name, 70))

    for day in sorted(by_date):
        group = by_date[day]
        pmt = _el(initn, "PmtInf")
        _el(pmt, "PmtInfId", pmt_inf_id(run, day))
        _el(pmt, "PmtMtd", "DD")
        _el(pmt, "BtchBookg", "true")
        _el(pmt, "NbOfTxs", str(len(group)))
        _el(pmt, "CtrlSum", _amount(sum((Decimal(str(i.amount)) for i in group), Decimal("0"))))
        tp = _el(pmt, "PmtTpInf")
        _el(_el(tp, "SvcLvl"), "Cd", "SEPA")
        _el(_el(tp, "LclInstrm"), "Cd", "CORE")
        _el(tp, "SeqTp", "RCUR")
        _el(pmt, "ReqdColltnDt", day.isoformat())
        _el(_el(pmt, "Cdtr"), "Nm", sepa_text(run.creditor_name, 70))
        _account(pmt, "CdtrAcct", run.creditor_iban)
        _agent(pmt, "CdtrAgt", run.creditor_bic)
        _el(pmt, "ChrgBr", "SLEV")
        scheme = _el(pmt, "CdtrSchmeId")
        othr = _el(_el(_el(scheme, "Id"), "PrvtId"), "Othr")
        _el(othr, "Id", run.creditor_id)
        _el(_el(othr, "SchmeNm"), "Prtry", "SEPA")

        for item in group:
            tx = _el(pmt, "DrctDbtTxInf")
            _el(_el(tx, "PmtId"), "EndToEndId", item.end_to_end_id)
            _el(tx, "InstdAmt", _amount(item.amount), Ccy="EUR")
            _mandate(_el(tx, "DrctDbtTx"), item)
            _agent(tx, "DbtrAgt", item.bic)
            debtor = _el(tx, "Dbtr")
            _el(debtor, "Nm", sepa_text(item.debtor_name, 70) or NOT_PROVIDED)
            _debtor_address(debtor, item)
            _account(tx, "DbtrAcct", item.iban)
            if item.remittance:
                _el(_el(tx, "RmtInf"), "Ustrd", sepa_text(item.remittance, 140))

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def filename(run):
    return f"Lastschrift_{run.message_id}.xml"
