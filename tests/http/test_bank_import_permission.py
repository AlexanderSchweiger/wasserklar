"""Bankauszug-Import nur mit dem Recht ``buchhaltung`` (SECURITY_AUDIT #12).

Vorher trugen die Routen nur ``@login_required`` — jeder angemeldete Nutzer
(z. B. eine reine Zaehler-Rolle) konnte Auszuege sehen, hochladen, verbuchen
und loeschen.
"""

from __future__ import annotations

import io

import pytest

from app.auth.permissions import PERM_BUCHHALTUNG, PERM_ZAEHLER
from app.extensions import db
from app.models import BankStatement, RealAccount, User
from tests.conftest import _ensure_role


def _user(username, role):
    u = User(username=username, email=f"{username}@test.com", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": "secret"})


@pytest.fixture
def statement(app):
    owner = _user("kassier", _ensure_role("Kassa", (PERM_BUCHHALTUNG,)))
    ra = RealAccount(name="Giro", iban="AT99", opening_balance=0)
    db.session.add(ra)
    db.session.flush()
    stmt = BankStatement(format="ofx", filename="test.ofx", file_hash="abc",
                         real_account_id=ra.id, currency="EUR", uploaded_by_id=owner.id)
    db.session.add(stmt)
    db.session.commit()
    return stmt


def _requests(stmt_id):
    return [
        ("get", "/bank-import/", None),
        ("get", "/bank-import/upload", None),
        ("post", "/bank-import/upload", {"file": (io.BytesIO(b"x"), "a.ofx")}),
        ("get", f"/bank-import/statements/{stmt_id}", None),
        ("get", f"/bank-import/statements/{stmt_id}/lines/1/open-items", None),
        ("get", f"/bank-import/statements/{stmt_id}/lines/1/split", None),
        ("post", f"/bank-import/statements/{stmt_id}/lines/1", {}),
        ("post", f"/bank-import/statements/{stmt_id}/commit", {}),
        ("post", f"/bank-import/statements/{stmt_id}/delete", {}),
    ]


class TestBankImportPermission:
    def test_user_without_accounting_permission_is_turned_away(self, client, statement):
        _user("ableser", _ensure_role("NurZaehler", (PERM_ZAEHLER,)))
        _login(client, "ableser")
        for method, url, data in _requests(statement.id):
            r = getattr(client, method)(url, data=data, follow_redirects=False)
            assert r.status_code == 302, (method, url, r.status_code)
            assert r.headers["Location"].rstrip("/") in ("", "http://localhost"), (method, url)
        # Nichts wurde geloescht oder angelegt.
        assert db.session.get(BankStatement, statement.id) is not None
        assert BankStatement.query.count() == 1

    def test_user_with_accounting_permission_gets_in(self, client, statement):
        _login(client, "kassier")
        assert client.get("/bank-import/").status_code == 200
        assert client.get(f"/bank-import/statements/{statement.id}").status_code == 200
