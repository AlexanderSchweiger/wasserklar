"""Sidebar-Kopf: der Mandantenname (wg.name) steht voll im HTML.

Gekuerzt wird nur noch per CSS (app.css „Sidebar-Kopf"); frueher schnitt
``truncate(22)`` jeden realistischen Namen ab und die zentrierte Brand lief
trotzdem ueber die Leiste hinaus.
"""
import re

import pytest

from app.extensions import db
from app.models import AppSetting, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _brand(client):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    body = client.get("/").get_data(as_text=True)
    m = re.search(r'<a [^>]*class="[^"]*wk-sidebar-brand[^"]*"[^>]*>.*?</a>', body, re.S)
    assert m, "Sidebar-Brand fehlt"
    return m.group(0)


def test_long_name_is_rendered_in_full(client, admin):
    AppSetting.set("wg.name", "Demo-Wassergenossenschaft Hagenberg")
    db.session.commit()

    brand = _brand(client)

    assert 'title="Demo-Wassergenossenschaft Hagenberg"' in brand
    assert '<span class="wk-sidebar-brand-name is-long">Demo-Wassergenossenschaft Hagenberg</span>' in brand
    assert "…" not in brand


def test_short_name_keeps_the_large_font(client, admin):
    AppSetting.set("wg.name", "WG Alm")
    db.session.commit()

    assert '<span class="wk-sidebar-brand-name">WG Alm</span>' in _brand(client)
