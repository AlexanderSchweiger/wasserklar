"""HTTP-Tests fuer das Notiz-Modul (Pin-Notizen / Notizzettel).

Deckt Login-Gate, CRUD ueber die HTMX-Endpoints (Panel-Fragment + notes:changed-
Trigger), Scope-/Body-Validierung, die Uebersichtsseite sowie die Integration in
Kontakt-Liste (Zeilen-Pin) und Dashboard (Tenant-Panel) ab.
"""
import pytest

from app.extensions import db
from app.models import Customer, Note, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def customer(app):
    c = Customer(name="Mustermann Max", is_customer=True)
    db.session.add(c)
    db.session.commit()
    return c


def _login(client):
    # Werkzeug-3 teilt den CookieJar zwischen test_client-Instanzen → vorher abmelden.
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


class TestLoginRequired:
    def test_index_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/notes/", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


class TestCrud:
    def test_create_note_on_customer(self, client, admin, customer):
        _login(client)
        r = client.post("/notes/", data={
            "entity_type": "customer", "entity_id": customer.id,
            "body": "Zahlt immer bar!", "color": "pink",
        })
        assert r.status_code == 200
        assert "notes:changed" in r.headers.get("HX-Trigger", "")
        n = Note.query.filter_by(entity_type="customer", entity_id=customer.id).one()
        assert n.body == "Zahlt immer bar!"
        assert n.color == "pink"
        assert n.pinned is True
        assert n.created_by_id == admin.id
        # Panel-Fragment enthaelt die Notiz
        assert b"Zahlt immer bar!" in r.data

    def test_create_tenant_note(self, client, admin):
        _login(client)
        r = client.post("/notes/", data={"entity_type": "tenant", "body": "GV am 30.6."})
        assert r.status_code == 200
        n = Note.query.filter_by(entity_type="tenant").one()
        assert n.entity_id is None
        assert n.color == "yellow"           # Default

    def test_create_rejects_empty_body(self, client, admin, customer):
        _login(client)
        r = client.post("/notes/", data={
            "entity_type": "customer", "entity_id": customer.id, "body": "   ",
        })
        assert r.status_code == 200          # Formular mit Fehler, kein 4xx
        assert Note.query.count() == 0
        assert "HX-Trigger" not in r.headers  # keine Mutation -> kein Trigger

    def test_create_rejects_unknown_scope(self, client, admin):
        _login(client)
        r = client.post("/notes/", data={"entity_type": "bogus", "body": "x"})
        assert r.status_code == 400
        assert Note.query.count() == 0

    def test_create_rejects_missing_entity(self, client, admin):
        _login(client)
        r = client.post("/notes/", data={
            "entity_type": "customer", "entity_id": 999999, "body": "x",
        })
        assert r.status_code == 400

    def test_update_note(self, client, admin, customer):
        _login(client)
        n = Note(entity_type="customer", entity_id=customer.id, body="alt", color="yellow")
        db.session.add(n)
        db.session.commit()
        r = client.post(f"/notes/{n.id}", data={"body": "neu", "color": "azure"})
        assert r.status_code == 200
        db.session.refresh(n)
        assert n.body == "neu"
        assert n.color == "azure"

    def test_delete_note(self, client, admin, customer):
        _login(client)
        n = Note(entity_type="customer", entity_id=customer.id, body="weg")
        db.session.add(n)
        db.session.commit()
        nid = n.id
        r = client.post(f"/notes/{nid}/delete")
        assert r.status_code == 200
        assert db.session.get(Note, nid) is None

    def test_toggle_pin(self, client, admin, customer):
        _login(client)
        n = Note(entity_type="customer", entity_id=customer.id, body="x", pinned=True)
        db.session.add(n)
        db.session.commit()
        client.post(f"/notes/{n.id}/pin")
        db.session.refresh(n)
        assert n.pinned is False
        client.post(f"/notes/{n.id}/pin")
        db.session.refresh(n)
        assert n.pinned is True


class TestFragments:
    def test_panel_renders_notes(self, client, admin, customer):
        _login(client)
        db.session.add(Note(entity_type="customer", entity_id=customer.id, body="Panel-Notiz"))
        db.session.commit()
        r = client.get(f"/notes/panel?entity_type=customer&entity_id={customer.id}")
        assert r.status_code == 200
        assert b"Panel-Notiz" in r.data

    def test_pin_badge_shows_count(self, client, admin, customer):
        _login(client)
        db.session.add(Note(entity_type="customer", entity_id=customer.id, body="a"))
        db.session.add(Note(entity_type="customer", entity_id=customer.id, body="b"))
        db.session.commit()
        r = client.get(f"/notes/pin?entity_type=customer&entity_id={customer.id}")
        assert r.status_code == 200
        assert f"note-pin-customer-{customer.id}".encode() in r.data
        assert b">2<" in r.data            # Zaehl-Badge bei >1


class TestOverviewAndIntegration:
    def test_overview_lists_note(self, client, admin, customer):
        _login(client)
        db.session.add(Note(entity_type="customer", entity_id=customer.id, body="Sichtbar in Übersicht"))
        db.session.commit()
        r = client.get("/notes/")
        assert r.status_code == 200
        assert b"Sichtbar in \xc3\x9cbersicht" in r.data

    def test_customer_list_shows_pin(self, client, admin, customer):
        _login(client)
        db.session.add(Note(entity_type="customer", entity_id=customer.id, body="x"))
        db.session.commit()
        r = client.get("/customers/")
        assert r.status_code == 200
        assert f"note-pin-customer-{customer.id}".encode() in r.data

    def test_dashboard_shows_tenant_panel(self, client, admin):
        _login(client)
        r = client.get("/")
        assert r.status_code == 200
        assert b"note-panel-tenant-0" in r.data
