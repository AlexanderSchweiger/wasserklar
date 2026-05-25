from datetime import datetime
from decimal import Decimal

from app.accounting.services import (
    booking_group_from_invoice_payment,
    open_fiscal_year_error,
)
from app.extensions import db
from app.models import (
    BankStatement,
    BankStatementLine,
    Booking,
    Invoice,
    OpenItem,
)


def _as_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _commit_with_op(line: BankStatementLine, stmt: BankStatement, user_id: int) -> None:
    op = OpenItem.query.get(line.matched_open_item_id)
    if op is None:
        raise ValueError(f"Offener Posten #{line.matched_open_item_id} nicht gefunden")

    amount = _as_decimal(line.amount)
    invoice = op.invoice

    # override_account_id aus der Vorschau hat Vorrang vor op.account_id
    effective_account_id = line.override_account_id or op.account_id

    if invoice is not None:
        group, children = booking_group_from_invoice_payment(
            invoice=invoice,
            amount=amount,
            payment_date=line.booking_date,
            real_account_id=stmt.real_account_id,
            created_by_id=user_id,
            open_item=op,
            reference=invoice.invoice_number or line.end_to_end_id,
            fallback_account_id=effective_account_id,
        )
        line.booking_group_id = group.id if group else None
        line.booking_id = children[0].id if (not group and children) else None
    else:
        # Manueller OP ohne Rechnung: einfache Einzelbuchung
        if effective_account_id is None:
            raise ValueError(
                f"Offener Posten #{op.id} hat kein Buchungskonto — "
                "bitte in der Vorschau ein Konto zuordnen."
            )
        description = (op.description or line.purpose or line.counterparty_name or "Bankauszug-Import").strip()[:500]
        booking = Booking(
            date=line.booking_date,
            account_id=effective_account_id,
            amount=amount,
            description=description,
            reference=line.end_to_end_id or None,
            real_account_id=stmt.real_account_id,
            customer_id=op.customer_id,
            open_item_id=op.id,
            created_by_id=user_id,
        )
        db.session.add(booking)
        db.session.flush()
        line.booking_id = booking.id

    db.session.flush()

    balance = op.open_balance
    if balance == 0:
        op.status = OpenItem.STATUS_PAID
        if invoice is not None:
            invoice.status = Invoice.STATUS_PAID
    elif balance < 0:
        op.status = OpenItem.STATUS_CREDIT
        if invoice is not None:
            invoice.status = Invoice.STATUS_CREDIT
    else:
        op.status = OpenItem.STATUS_PARTIAL
        # Invoice-Status nicht ueberschreiben (Versendet/Entwurf bleibt)


def _commit_without_op(line: BankStatementLine, stmt: BankStatement, user_id: int) -> None:
    if line.override_account_id is None:
        raise ValueError(
            "Kein Ertrags-/Aufwandskonto gewählt. Bitte in der Vorschau ein Konto zuordnen."
        )

    description = (line.purpose or line.counterparty_name or "Bankauszug-Import").strip()[:500]
    booking = Booking(
        date=line.booking_date,
        account_id=line.override_account_id,
        amount=_as_decimal(line.amount),
        description=description,
        reference=line.end_to_end_id or None,
        real_account_id=stmt.real_account_id,
        customer_id=line.matched_customer_id,
        created_by_id=user_id,
    )
    db.session.add(booking)
    db.session.flush()
    line.booking_id = booking.id


def commit_statement(statement_id: int, user_id: int) -> dict:
    """Verbucht alle ausgewählten, noch nicht verarbeiteten Zeilen eines Auszugs.

    Legt je Zeile eine Buchung (oder Sammelbuchung via
    ``booking_group_from_invoice_payment``) an, schließt / teilbezahlt den
    verknüpften Offenen Posten und setzt den Invoice-Status passend.

    Commit-Verhalten: Die Funktion committet am Ende einmal die gesamte
    Session. Einzelne fehlgeschlagene Zeilen werden in ``stats['errors']``
    gesammelt und per Savepoint-Rollback isoliert.
    """
    stmt = BankStatement.query.get(statement_id)
    if stmt is None:
        raise ValueError(f"Bankauszug #{statement_id} nicht gefunden")

    stats = {"committed": 0, "skipped": 0, "errors": []}

    pending_lines = stmt.lines.filter_by(line_status=BankStatementLine.STATUS_PENDING).all()

    for line in pending_lines:
        if not line.selected:
            line.line_status = BankStatementLine.STATUS_SKIPPED
            stats["skipped"] += 1
            continue

        fy_err = open_fiscal_year_error(line.booking_date)
        if fy_err:
            stats["errors"].append(f"Zeile {line.line_index}: {fy_err}")
            continue

        sp = db.session.begin_nested()
        try:
            if line.matched_open_item_id:
                _commit_with_op(line, stmt, user_id)
            else:
                _commit_without_op(line, stmt, user_id)
            line.line_status = BankStatementLine.STATUS_COMMITTED
            sp.commit()
            stats["committed"] += 1
        except Exception as e:  # noqa: BLE001
            sp.rollback()
            stats["errors"].append(f"Zeile {line.line_index}: {e}")

    stmt.committed_at = datetime.utcnow()
    all_done = all(
        l.line_status != BankStatementLine.STATUS_PENDING
        for l in stmt.lines
    )
    if stats["errors"] or not all_done:
        stmt.status = BankStatement.STATUS_PARTIAL
    else:
        stmt.status = BankStatement.STATUS_COMMITTED

    db.session.commit()
    return stats
