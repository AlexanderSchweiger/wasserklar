"""HTTP-Tests fuer die Bankauszug-Preview: HTMX-Row-Swap statt Full-Reload.

Verifiziert, dass eine Zeilen-Aktion bei HX-Request nur das <tr>-Fragment
zurueckgibt (kein Redirect), ohne HX-Request weiterhin redirected, und dass
die „Keine Zuordnung"-Markierung farblich (badge) gerendert wird.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account,
    BankStatement,
    BankStatementLine,
    BankStatementLineAllocation,
    Customer,
    Invoice,
    OpenItem,
    RealAccount,
    User,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.com", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def statement(app, admin):
    ra = RealAccount(name="Giro", iban="AT99", opening_balance=Decimal("0"))
    db.session.add(ra)
    db.session.add(Account(name="Wassereinnahmen", code="W01"))  # Konto fuers Dropdown
    db.session.flush()
    stmt = BankStatement(
        format="ofx", filename="test.ofx", file_hash="abc",
        real_account_id=ra.id, currency="EUR", uploaded_by_id=admin.id,
    )
    db.session.add(stmt)
    db.session.flush()
    line = BankStatementLine(
        statement_id=stmt.id, line_index=0, booking_date=date(2026, 6, 19),
        amount=Decimal("113.75"), currency="EUR",
        counterparty_name="Unbekannt Zahler", line_status="pending", selected=True,
    )
    db.session.add(line)
    db.session.commit()
    return stmt, line


class TestRowSwap:
    def test_preview_shows_unassigned_badge(self, client, statement):
        stmt, line = statement
        _login(client)
        r = client.get(f"/bank-import/statements/{stmt.id}")
        assert r.status_code == 200
        # Ask #1: „Keine Zuordnung" farblich hervorgehoben (Tabler-Soft-Badge).
        assert b"bg-orange-lt" in r.data
        assert "Keine Zuordnung".encode() in r.data

    def test_update_line_returns_row_fragment_on_hx(self, client, statement):
        stmt, line = statement
        _login(client)
        r = client.post(
            f"/bank-import/statements/{stmt.id}/lines/{line.id}",
            data={"action": "set_account", "account_id": ""},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        # Fragment, kein Redirect: genau die eine Zeile mit stabiler ID.
        assert b"<tr" in r.data
        assert f"bank-line-{line.id}".encode() in r.data
        assert b"<html" not in r.data  # keine Vollseite

    def test_update_line_redirects_without_hx(self, client, statement):
        stmt, line = statement
        _login(client)
        r = client.post(
            f"/bank-import/statements/{stmt.id}/lines/{line.id}",
            data={"action": "set_account", "account_id": ""},
        )
        assert r.status_code == 302  # No-JS-Fallback bleibt erhalten

    def test_preview_renders_customer_recognized_branch(self, client, statement):
        # Zeile mit erkanntem Kunden (aber ohne OP) + offener Posten des Kunden
        # -> rendert den „Kunde erkannt"-Branch (sonst von keinem Test gerendert).
        stmt, line = statement
        cust = Customer(name="Erkannt Kunde", active=True)
        db.session.add(cust)
        db.session.flush()
        db.session.add(OpenItem(
            customer_id=cust.id, description="Offen", amount=Decimal("50.00"),
            status=OpenItem.STATUS_OPEN,
        ))
        line.matched_customer_id = cust.id
        db.session.commit()
        _login(client)
        r = client.get(f"/bank-import/statements/{stmt.id}")
        assert r.status_code == 200
        assert "Kunde erkannt".encode() in r.data
        assert b"bg-blue-lt" in r.data

    def test_set_open_item_swaps_to_matched_branch(self, client, statement):
        stmt, line = statement
        cust = Customer(name="Zahler Unbekannt", active=True)
        db.session.add(cust)
        db.session.flush()
        inv = Invoice(
            invoice_number="2026-00200", customer_id=cust.id,
            status=Invoice.STATUS_SENT, date=date(2026, 1, 1),
            total_amount=Decimal("113.75"),
        )
        db.session.add(inv)
        db.session.flush()
        op = OpenItem(
            customer_id=cust.id, description="Rechnung", amount=Decimal("113.75"),
            status=OpenItem.STATUS_OPEN, invoice_id=inv.id,
        )
        db.session.add(op)
        db.session.commit()

        _login(client)
        r = client.post(
            f"/bank-import/statements/{stmt.id}/lines/{line.id}",
            data={"action": "set_open_item", "open_item_id": str(op.id)},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        # Zuordnungs-Branch: Rechnungs-Link + „OP entfernen".
        assert b"2026-00200" in r.data
        assert "OP entfernen".encode() in r.data


class TestSplit:
    def _two_ops(self, amount_a="13.75", amount_b="100.00"):
        cust = Customer(name="Splitter Kunde", active=True)
        db.session.add(cust)
        db.session.flush()
        ops = []
        for amt in (amount_a, amount_b):
            op = OpenItem(
                customer_id=cust.id, description="Posten", amount=Decimal(amt),
                status=OpenItem.STATUS_OPEN,
            )
            db.session.add(op)
            ops.append(op)
        db.session.flush()
        return cust, ops

    def test_split_form_prefills_recognized_customer(self, client, statement):
        stmt, line = statement
        cust, ops = self._two_ops()           # 13,75 + 100,00 = 113,75 = Zeilenbetrag
        line.matched_customer_id = cust.id
        db.session.commit()
        _login(client)
        r = client.get(f"/bank-import/statements/{stmt.id}/lines/{line.id}/split")
        assert r.status_code == 200
        assert "Aufteilung speichern".encode() in r.data
        # Beide OPs als vorbelegte Beträge (number-input value, dot-decimal).
        assert b'value="13.75"' in r.data
        assert b'value="100.00"' in r.data

    def test_set_split_creates_allocations_and_closes_modal(self, client, statement):
        stmt, line = statement
        cust, ops = self._two_ops()
        db.session.commit()
        _login(client)
        r = client.post(
            f"/bank-import/statements/{stmt.id}/lines/{line.id}",
            data={
                "action": "set_split",
                "alloc_op_id": [str(ops[0].id), str(ops[1].id)],
                "alloc_amount": ["13.75", "100.00"],
            },
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        assert r.headers.get("HX-Trigger") == "bankSplitSaved"
        assert "Aufgeteilt auf 2 Posten".encode() in r.data
        db.session.refresh(line)
        assert len(line.allocations) == 2
        assert line.matched_open_item_id is None
        assert line.match_type == BankStatementLine.MATCH_SPLIT

    def test_set_split_sum_mismatch_rejected(self, client, statement):
        stmt, line = statement
        cust, ops = self._two_ops()
        db.session.commit()
        _login(client)
        r = client.post(
            f"/bank-import/statements/{stmt.id}/lines/{line.id}",
            data={
                "action": "set_split",
                "alloc_op_id": [str(ops[0].id), str(ops[1].id)],
                "alloc_amount": ["13.75", "50.00"],   # Summe 63,75 != 113,75
            },
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 400
        db.session.refresh(line)
        assert len(line.allocations) == 0

    def test_clear_split_removes_allocations(self, client, statement):
        stmt, line = statement
        cust, ops = self._two_ops()
        line.allocations.append(BankStatementLineAllocation(open_item_id=ops[0].id, amount=Decimal("13.75")))
        line.allocations.append(BankStatementLineAllocation(open_item_id=ops[1].id, amount=Decimal("100.00")))
        line.match_type = BankStatementLine.MATCH_SPLIT
        db.session.commit()
        _login(client)
        r = client.post(
            f"/bank-import/statements/{stmt.id}/lines/{line.id}",
            data={"action": "clear_split"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        db.session.refresh(line)
        assert len(line.allocations) == 0
