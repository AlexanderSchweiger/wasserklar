"""Seiten-Skripte muessen re-execution-sicher sein (hx-boost).

``base.html`` setzt ``hx-boost="true"``: htmx tauscht bei jeder Navigation den
``<body>`` aus und fuehrt die enthaltenen Inline-Skripte **erneut aus** — im
selben globalen Scope, denn die JS-Realm wird nie neu geladen. Ein top-level
``const``/``let`` wirft beim zweiten Lauf

    SyntaxError: Identifier 'X' has already been declared

Das ist ein *Parse*-Fehler: das GESAMTE Skript wird verworfen, nicht nur die
betroffene Zeile. Real passiert ist genau das auf ``/dunning/notices`` — dort
und in ``/invoices/`` hiess dieselbe Konstante ``BULK_PRINT_MAX``, sodass schon
der Wechsel Rechnungen -> Mahnungen die komplette Seitenlogik abschoss:
Mehrfachauswahl, Sammeldruck und Massenmail taten nichts mehr, bis man hart neu
lud (Strg+F5).

Zulaessig sind ``var`` (darf redeklariert werden), ``function``-Deklarationen
und alles in einer IIFE.
"""
import re

import pytest

from app.extensions import db
from app.models import User
from tests.conftest import _ensure_role


# Seiten mit nennenswerter Inline-Logik (Auswahl/Sammelaktionen/Versand).
PAGES = [
    "/invoices/",
    "/dunning/notices",
    "/dunning/",
    # /dunning/run bewusst nicht: die Route redirected ohne geseedete
    # Standard-Mahnvorlage (302) und hat ohnehin kein Inline-Skript.
    "/customers/",
    "/meters/",
]

_TOPLEVEL_DECL = re.compile(r"^(?:const|let)\s+(\w+)", re.MULTILINE)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.mark.parametrize("path", PAGES)
def test_no_toplevel_const_or_let(client, admin, path):
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    html = resp.get_data(as_text=True)

    offenders = []
    for body in _INLINE_SCRIPT.findall(html):
        offenders += _TOPLEVEL_DECL.findall(body)
    assert not offenders, (
        f"{path}: top-level {offenders} im Inline-Skript. Beim hx-boost-"
        f"Reexecute wirft das SyntaxError und verwirft das ganze Skript. "
        f"Fix: in eine IIFE wickeln oder `var` verwenden."
    )
