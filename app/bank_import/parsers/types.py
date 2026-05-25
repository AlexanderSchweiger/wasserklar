from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class ParsedLine:
    booking_date: date
    amount: Decimal
    currency: str = "EUR"
    value_date: date | None = None
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    purpose: str | None = None
    end_to_end_id: str | None = None
    tx_id: str | None = None


@dataclass
class ParsedStatement:
    format: str
    account_iban: str | None = None
    statement_reference: str | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    currency: str = "EUR"
    booking_date_from: date | None = None
    booking_date_to: date | None = None
    lines: list[ParsedLine] = field(default_factory=list)
