"""Integration-Tests: eine Bankzeile auf mehrere offene Posten aufteilen.

Verifiziert das Verbuchen einer aufgeteilten Zeile (``_commit_split``):
pro Allocation eine Buchung, jeder OP korrekt geschlossen, Summen-Guard.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.bank_import import services
from app.models import (
    BankStatement,
    BankStatementLine,
    BankStatementLineAllocation,
    Booking,
    FiscalYear,
    OpenItem,
)


@pytest.fixture
def fiscal_year(app):
    fy = FiscalYear(year=2026, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
    db.session.add(fy)
    db.session.commit()
    return fy


def _statement(real_account, user):
    stmt = BankStatement(
        format="ofx", filename="s.ofx", file_hash="h1",
        real_account_id=real_account.id, currency="EUR", uploaded_by_id=user.id,
    )
    db.session.add(stmt)
    db.session.flush()
    return stmt


def _op(customer, account, amount):
    op = OpenItem(
        customer_id=customer.id, description="Wasserrechnung",
        amount=Decimal(str(amount)), status=OpenItem.STATUS_OPEN,
        account_id=account.id,
    )
    db.session.add(op)
    db.session.flush()
    return op


class TestCommitSplit:
    def test_split_settles_two_open_items(self, app, user, real_account, account, customer, fiscal_year):
        op1 = _op(customer, account, "55.00")
        op2 = _op(customer, account, "271.34")
        stmt = _statement(real_account, user)
        line = BankStatementLine(
            statement_id=stmt.id, line_index=0, booking_date=date(2026, 6, 19),
            amount=Decimal("326.34"), counterparty_name="Rothacher Albrecht",
            line_status="pending", selected=True,
        )
        line.allocations.append(BankStatementLineAllocation(open_item_id=op1.id, amount=Decimal("55.00")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=op2.id, amount=Decimal("271.34")))
        db.session.add(line)
        db.session.commit()

        stats = services.commit_statement(stmt.id, user.id)

        assert stats["committed"] == 1
        assert not stats["errors"]
        db.session.refresh(op1)
        db.session.refresh(op2)
        assert op1.status == OpenItem.STATUS_PAID
        assert op2.status == OpenItem.STATUS_PAID
        # Eine Buchung je Allocation, jeweils gegen den richtigen OP.
        b1 = Booking.query.filter_by(open_item_id=op1.id).all()
        b2 = Booking.query.filter_by(open_item_id=op2.id).all()
        assert len(b1) == 1 and b1[0].amount == Decimal("55.00")
        assert len(b2) == 1 and b2[0].amount == Decimal("271.34")
        db.session.refresh(line)
        assert line.line_status == BankStatementLine.STATUS_COMMITTED

    def test_split_sum_mismatch_is_rejected(self, app, user, real_account, account, customer, fiscal_year):
        op1 = _op(customer, account, "55.00")
        op2 = _op(customer, account, "271.34")
        stmt = _statement(real_account, user)
        line = BankStatementLine(
            statement_id=stmt.id, line_index=0, booking_date=date(2026, 6, 19),
            amount=Decimal("326.34"), counterparty_name="X",
            line_status="pending", selected=True,
        )
        # Summe 200 != 326,34
        line.allocations.append(BankStatementLineAllocation(open_item_id=op1.id, amount=Decimal("55.00")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=op2.id, amount=Decimal("145.00")))
        db.session.add(line)
        db.session.commit()

        stats = services.commit_statement(stmt.id, user.id)

        assert stats["committed"] == 0
        assert stats["errors"]
        db.session.refresh(line)
        assert line.line_status == BankStatementLine.STATUS_PENDING
        # Keine Buchungen entstanden.
        assert Booking.query.filter_by(open_item_id=op1.id).count() == 0

    def test_partial_split_marks_op_partial(self, app, user, real_account, account, customer, fiscal_year):
        # Ein OP wird nur teilweise bedient -> bleibt teilbezahlt.
        op1 = _op(customer, account, "100.00")
        op2 = _op(customer, account, "226.34")
        stmt = _statement(real_account, user)
        line = BankStatementLine(
            statement_id=stmt.id, line_index=0, booking_date=date(2026, 6, 19),
            amount=Decimal("326.34"), counterparty_name="X",
            line_status="pending", selected=True,
        )
        line.allocations.append(BankStatementLineAllocation(open_item_id=op1.id, amount=Decimal("60.00")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=op2.id, amount=Decimal("266.34")))
        db.session.add(line)
        db.session.commit()

        stats = services.commit_statement(stmt.id, user.id)
        assert stats["committed"] == 1
        db.session.refresh(op1)
        assert op1.status == OpenItem.STATUS_PARTIAL
        assert op1.open_balance == Decimal("40.00")


class TestSplitAccountFallback:
    """Posten ohne Kontierung: das in der Aufteilung gewaehlte Konto
    (``allocation.account_id``) ist das Ersatzkonto beim Verbuchen."""

    def _line(self, stmt, amount):
        line = BankStatementLine(
            statement_id=stmt.id, line_index=0, booking_date=date(2026, 6, 19),
            amount=Decimal(amount), counterparty_name="X",
            line_status="pending", selected=True,
        )
        db.session.add(line)
        return line

    def test_manual_op_without_account_uses_allocation_account(
            self, app, user, real_account, account, customer, fiscal_year):
        op1 = OpenItem(customer_id=customer.id, description="Ohne Konto",
                       amount=Decimal("50.00"), status=OpenItem.STATUS_OPEN)
        db.session.add(op1)
        db.session.flush()
        op2 = _op(customer, account, "25.00")
        stmt = _statement(real_account, user)
        line = self._line(stmt, "75.00")
        line.allocations.append(BankStatementLineAllocation(
            open_item_id=op1.id, account_id=account.id, amount=Decimal("50.00")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=op2.id, amount=Decimal("25.00")))
        db.session.commit()

        stats = services.commit_statement(stmt.id, user.id)

        assert not stats["errors"]
        assert stats["committed"] == 1
        b = Booking.query.filter_by(open_item_id=op1.id).one()
        assert b.account_id == account.id
        db.session.refresh(op1)
        assert op1.status == OpenItem.STATUS_PAID

    def test_manual_op_without_account_and_no_fallback_fails(
            self, app, user, real_account, account, customer, fiscal_year):
        op1 = OpenItem(customer_id=customer.id, description="Ohne Konto",
                       amount=Decimal("50.00"), status=OpenItem.STATUS_OPEN)
        db.session.add(op1)
        db.session.flush()
        op2 = _op(customer, account, "25.00")
        stmt = _statement(real_account, user)
        line = self._line(stmt, "75.00")
        line.allocations.append(BankStatementLineAllocation(open_item_id=op1.id, amount=Decimal("50.00")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=op2.id, amount=Decimal("25.00")))
        db.session.commit()

        stats = services.commit_statement(stmt.id, user.id)

        assert stats["committed"] == 0
        assert stats["errors"]
        assert Booking.query.filter_by(open_item_id=op2.id).count() == 0

    def test_invoice_op_with_unassigned_items_uses_allocation_account(
            self, app, user, real_account, account, customer, fiscal_year):
        from app.models import Invoice, InvoiceItem

        inv = Invoice(invoice_number="2026-00901", customer_id=customer.id,
                      status=Invoice.STATUS_SENT, date=date(2026, 1, 1),
                      total_amount=Decimal("40.00"))
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(invoice_id=inv.id, description="Wasser",
                                   quantity=Decimal("1"), unit_price=Decimal("40.00"),
                                   amount=Decimal("40.00"), tax_rate=Decimal("0")))
        op1 = OpenItem(customer_id=customer.id, description="Rechnung", invoice_id=inv.id,
                       amount=Decimal("40.00"), status=OpenItem.STATUS_OPEN)
        db.session.add(op1)
        db.session.flush()
        op2 = _op(customer, account, "10.00")
        stmt = _statement(real_account, user)
        line = self._line(stmt, "50.00")
        line.allocations.append(BankStatementLineAllocation(
            open_item_id=op1.id, account_id=account.id, amount=Decimal("40.00")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=op2.id, amount=Decimal("10.00")))
        db.session.commit()

        stats = services.commit_statement(stmt.id, user.id)

        assert not stats["errors"]
        b = Booking.query.filter_by(open_item_id=op1.id).one()
        assert b.account_id == account.id
        db.session.refresh(inv)
        assert inv.status == Invoice.STATUS_PAID
