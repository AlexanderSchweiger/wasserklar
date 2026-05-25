from datetime import date, datetime
from decimal import Decimal

from lxml import etree

from app.bank_import.parsers.types import ParsedLine, ParsedStatement


def _ns(root):
    tag = root.tag
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _find(elem, path, ns):
    if not ns:
        return elem.find(path)
    return elem.find("/".join(f"{{{ns}}}{p}" for p in path.split("/")))


def _findall(elem, path, ns):
    if not ns:
        return elem.findall(path)
    return elem.findall("/".join(f"{{{ns}}}{p}" for p in path.split("/")))


def _text(elem, path, ns):
    found = _find(elem, path, ns)
    return found.text.strip() if found is not None and found.text else None


def _parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except Exception:
        return None


def parse(content: bytes) -> ParsedStatement:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(content, parser=parser)
    ns = _ns(root)

    stmt_elem = _find(root, "BkToCstmrStmt/Stmt", ns)
    if stmt_elem is None:
        raise ValueError("camt.053: <Stmt>-Element nicht gefunden")

    account_iban = _text(stmt_elem, "Acct/Id/IBAN", ns)
    statement_reference = _text(stmt_elem, "ElctrncSeqNb", ns) or _text(stmt_elem, "Id", ns)
    currency = None
    acct_ccy = _find(stmt_elem, "Acct/Ccy", ns)
    if acct_ccy is not None and acct_ccy.text:
        currency = acct_ccy.text.strip()

    opening_balance = None
    closing_balance = None
    for bal in _findall(stmt_elem, "Bal", ns):
        code = _text(bal, "Tp/CdOrPrtry/Cd", ns)
        amt_elem = _find(bal, "Amt", ns)
        amount = _decimal(amt_elem.text) if amt_elem is not None else None
        cd_dbt = _text(bal, "CdtDbtInd", ns)
        if amount is not None and cd_dbt == "DBIT":
            amount = -amount
        if not currency and amt_elem is not None:
            currency = amt_elem.get("Ccy") or currency
        if code in ("OPBD", "PRCD") and opening_balance is None:
            opening_balance = amount
        elif code in ("CLBD", "CLAV"):
            closing_balance = amount

    lines: list[ParsedLine] = []
    all_dates: list[date] = []

    for idx, ntry in enumerate(_findall(stmt_elem, "Ntry", ns)):
        amt_elem = _find(ntry, "Amt", ns)
        amount = _decimal(amt_elem.text) if amt_elem is not None else None
        if amount is None:
            continue
        line_currency = amt_elem.get("Ccy") if amt_elem is not None else currency
        cd_dbt = _text(ntry, "CdtDbtInd", ns)
        if cd_dbt == "DBIT":
            amount = -amount

        booking_date = _parse_date(_text(ntry, "BookgDt/Dt", ns) or _text(ntry, "BookgDt/DtTm", ns))
        value_date = _parse_date(_text(ntry, "ValDt/Dt", ns) or _text(ntry, "ValDt/DtTm", ns))
        if booking_date is None and value_date is not None:
            booking_date = value_date
        if booking_date is None:
            continue
        all_dates.append(booking_date)

        tx_details = _find(ntry, "NtryDtls/TxDtls", ns)
        counterparty_name = None
        counterparty_iban = None
        end_to_end_id = None
        tx_id = None
        purpose_parts: list[str] = []

        if tx_details is not None:
            # Gegenpartei: bei Eingang = Debtor, bei Ausgang = Creditor
            if amount > 0:
                counterparty_name = _text(tx_details, "RltdPties/Dbtr/Nm", ns)
                counterparty_iban = _text(tx_details, "RltdPties/DbtrAcct/Id/IBAN", ns)
            else:
                counterparty_name = _text(tx_details, "RltdPties/Cdtr/Nm", ns)
                counterparty_iban = _text(tx_details, "RltdPties/CdtrAcct/Id/IBAN", ns)

            end_to_end_id = _text(tx_details, "Refs/EndToEndId", ns)
            tx_id = _text(tx_details, "Refs/AcctSvcrRef", ns) or _text(tx_details, "Refs/TxId", ns)

            rmt_inf = _find(tx_details, "RmtInf", ns)
            if rmt_inf is not None:
                for ustrd in _findall(rmt_inf, "Ustrd", ns):
                    if ustrd.text:
                        purpose_parts.append(ustrd.text.strip())
                strd_ref = _text(rmt_inf, "Strd/CdtrRefInf/Ref", ns)
                if strd_ref:
                    purpose_parts.append(strd_ref)

        if not purpose_parts:
            addtl = _text(ntry, "AddtlNtryInf", ns)
            if addtl:
                purpose_parts.append(addtl)

        lines.append(
            ParsedLine(
                booking_date=booking_date,
                value_date=value_date,
                amount=amount,
                currency=line_currency or currency or "EUR",
                counterparty_name=counterparty_name,
                counterparty_iban=counterparty_iban,
                purpose=" ".join(purpose_parts) if purpose_parts else None,
                end_to_end_id=end_to_end_id,
                tx_id=tx_id,
            )
        )

    return ParsedStatement(
        format="camt053",
        account_iban=account_iban,
        statement_reference=statement_reference,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        currency=currency or "EUR",
        booking_date_from=min(all_dates) if all_dates else None,
        booking_date_to=max(all_dates) if all_dates else None,
        lines=lines,
    )
