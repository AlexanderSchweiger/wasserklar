from datetime import date
from decimal import Decimal

import mt940

from app.bank_import.parsers.types import ParsedLine, ParsedStatement


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return None


def _as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    amount = getattr(value, "amount", None)
    if amount is not None:
        return Decimal(str(amount))
    try:
        return Decimal(str(value))
    except Exception:
        return None


def parse(content: bytes, format: str) -> ParsedStatement:
    text = content.decode("utf-8", errors="replace")
    transactions = mt940.parse(text)
    data = dict(transactions.data or {})

    account_identification = data.get("account_identification")
    account_iban = account_identification.strip() if account_identification else None

    statement_reference = None
    seq = data.get("sequence_number")
    stmt_no = data.get("statement_number")
    if stmt_no or seq:
        statement_reference = "/".join(filter(None, [str(stmt_no or ""), str(seq or "")])) or None

    final_opening = data.get("final_opening_balance") or data.get("intermediate_opening_balance")
    final_closing = data.get("final_closing_balance") or data.get("intermediate_closing_balance")

    currency = None
    if final_opening is not None:
        amt = getattr(final_opening, "amount", None)
        currency = getattr(amt, "currency", None) if amt else None

    opening_balance = _as_decimal(getattr(final_opening, "amount", None)) if final_opening else None
    closing_balance = _as_decimal(getattr(final_closing, "amount", None)) if final_closing else None

    lines: list[ParsedLine] = []
    all_dates: list[date] = []

    for tx in transactions:
        tx_data = dict(tx.data or {})
        booking_date = _as_date(tx_data.get("date") or tx_data.get("entry_date"))
        value_date = _as_date(tx_data.get("date"))
        if booking_date is None:
            booking_date = value_date
        if booking_date is None:
            continue
        all_dates.append(booking_date)

        amount = _as_decimal(tx_data.get("amount"))
        if amount is None:
            continue
        line_currency = getattr(tx_data.get("amount"), "currency", None) or currency or "EUR"

        counterparty_name = (
            tx_data.get("applicant_name")
            or tx_data.get("customer_reference")
            or None
        )
        counterparty_iban = tx_data.get("applicant_iban") or tx_data.get("applicant_bin") or None

        purpose_parts = []
        for key in (
            "purpose",
            "transaction_details",
            "extra_details",
            "additional_purpose",
        ):
            val = tx_data.get(key)
            if val:
                purpose_parts.append(str(val).strip())
        purpose = " ".join(purpose_parts) if purpose_parts else None

        end_to_end_id = tx_data.get("end_to_end_reference") or tx_data.get("mandate_reference")
        tx_id = tx_data.get("bank_reference") or tx_data.get("customer_reference")

        lines.append(
            ParsedLine(
                booking_date=booking_date,
                value_date=value_date,
                amount=amount,
                currency=line_currency,
                counterparty_name=counterparty_name.strip() if counterparty_name else None,
                counterparty_iban=counterparty_iban.strip() if counterparty_iban else None,
                purpose=purpose,
                end_to_end_id=end_to_end_id.strip() if end_to_end_id else None,
                tx_id=tx_id.strip() if tx_id else None,
            )
        )

    return ParsedStatement(
        format=format,
        account_iban=account_iban,
        statement_reference=statement_reference,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        currency=currency or "EUR",
        booking_date_from=min(all_dates) if all_dates else None,
        booking_date_to=max(all_dates) if all_dates else None,
        lines=lines,
    )
