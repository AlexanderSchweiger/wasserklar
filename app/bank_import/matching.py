import re
from decimal import Decimal

from sqlalchemy import func

from app.models import (
    BankStatementLine,
    Customer,
    Invoice,
    OpenItem,
)


INVOICE_NR_RE = re.compile(r"\b(\d{4}-\d{5})\b")


def _as_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def match_line(line: BankStatementLine) -> None:
    """Automatisches Matching fuer eine Bankauszug-Zeile.

    Reihenfolge:
    1. Rechnungsnummer (Format YYYY-NNNNN) im Verwendungszweck
    2. Name des Absenders -> Kunde -> offene OPs (exakter Betrags-Match bevorzugt)

    Aendert die Felder matched_invoice_id, matched_open_item_id,
    matched_customer_id, match_type und selected am Line-Objekt. Kein commit.
    """
    # Default: alle Zeilen sind vorausgewaehlt — der Nutzer waehlt explizit
    # ab, was er NICHT verbuchen will (Zinsen, Spesen, manuell-zu-pruefende
    # Sonderfaelle). Mehr Zeilen sind "Standardfall verbuchen" als
    # "manuell triagen".
    line.selected = True

    if _as_decimal(line.amount) <= 0:
        return

    # 1) Rechnungsnummer im Verwendungszweck
    if line.purpose:
        m = INVOICE_NR_RE.search(line.purpose)
        if m:
            inv = Invoice.query.filter_by(invoice_number=m.group(1)).first()
            if inv and inv.open_item and inv.open_item.status in (
                OpenItem.STATUS_OPEN,
                OpenItem.STATUS_PARTIAL,
            ):
                line.matched_invoice_id = inv.id
                line.matched_open_item_id = inv.open_item.id
                line.matched_customer_id = inv.customer_id
                line.match_type = BankStatementLine.MATCH_INVOICE_NUMBER
                line.selected = True
                return

    # 2) Name des Absenders (case-insensitive, trim)
    name = (line.counterparty_name or "").strip()
    if not name:
        return

    customers = Customer.query.filter(func.lower(Customer.name) == name.lower()).all()
    if len(customers) != 1:
        return
    cust = customers[0]

    open_ops = (
        OpenItem.query.filter(
            OpenItem.customer_id == cust.id,
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        )
        .all()
    )
    if not open_ops:
        line.matched_customer_id = cust.id
        return

    amount = _as_decimal(line.amount)
    exact = [op for op in open_ops if op.open_balance == amount]
    if len(exact) == 1:
        chosen = exact[0]
    elif len(open_ops) == 1:
        chosen = open_ops[0]
    else:
        line.matched_customer_id = cust.id
        return

    line.matched_open_item_id = chosen.id
    line.matched_invoice_id = chosen.invoice_id
    line.matched_customer_id = cust.id
    line.match_type = BankStatementLine.MATCH_NAME
    line.selected = True
