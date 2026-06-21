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


def _book_against_op(op, amount, line, stmt, user_id, account_override=None):
    """Verbucht ``amount`` gegen den offenen Posten ``op`` und aktualisiert dessen
    Status. Gemeinsamer Kern fuer die 1:1-Zuordnung und die Aufteilung.

    Gibt ``(booking_group_id, booking_id)`` der ersten erzeugten Buchung zurueck.
    """
    amount = _as_decimal(amount)
    invoice = op.invoice

    # override_account hat Vorrang vor op.account_id
    effective_account_id = account_override or op.account_id

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
        group_id = group.id if group else None
        booking_id = children[0].id if (not group and children) else None
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
        group_id, booking_id = None, booking.id

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

    return group_id, booking_id


def _commit_with_op(line: BankStatementLine, stmt: BankStatement, user_id: int) -> None:
    op = OpenItem.query.get(line.matched_open_item_id)
    if op is None:
        raise ValueError(f"Offener Posten #{line.matched_open_item_id} nicht gefunden")
    group_id, booking_id = _book_against_op(
        op, line.amount, line, stmt, user_id, account_override=line.override_account_id
    )
    line.booking_group_id = group_id
    line.booking_id = booking_id


def _commit_split(line: BankStatementLine, stmt: BankStatement, user_id: int) -> None:
    """Verbucht eine auf mehrere offene Posten (und/oder Konten) aufgeteilte Zeile.

    Pro Allocation entsteht eine eigene Buchung; die Summe muss exakt dem
    Buchungsbetrag der Zeile entsprechen (sonst stimmt die Bankkonto-Bewegung
    nicht). ``line.booking_id`` bleibt leer — die Verknuepfung laeuft pro
    Buchung ueber ``Booking.open_item_id``.
    """
    allocs = list(line.allocations)
    if not allocs:
        raise ValueError("Keine Aufteilung vorhanden.")

    total = sum((_as_decimal(a.amount) for a in allocs), Decimal("0"))
    if total != _as_decimal(line.amount):
        raise ValueError(
            f"Summe der Aufteilung ({total} €) entspricht nicht dem "
            f"Buchungsbetrag ({_as_decimal(line.amount)} €)."
        )

    for a in allocs:
        amt = _as_decimal(a.amount)
        if amt <= 0:
            raise ValueError("Teilbeträge müssen größer als 0 sein.")
        if a.open_item_id:
            op = OpenItem.query.get(a.open_item_id)
            if op is None:
                raise ValueError(f"Offener Posten #{a.open_item_id} nicht gefunden")
            _book_against_op(op, amt, line, stmt, user_id)
        elif a.account_id:
            description = (line.purpose or line.counterparty_name or "Bankauszug-Import").strip()[:500]
            booking = Booking(
                date=line.booking_date,
                account_id=a.account_id,
                amount=amt,
                description=description,
                reference=line.end_to_end_id or None,
                real_account_id=stmt.real_account_id,
                customer_id=line.matched_customer_id,
                created_by_id=user_id,
            )
            db.session.add(booking)
            db.session.flush()
        else:
            raise ValueError("Aufteilungs-Position ohne Ziel (weder Posten noch Konto).")


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
            if line.is_split:
                _commit_split(line, stmt, user_id)
            elif line.matched_open_item_id:
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
