"""HTTP-Tests fuer das Rundschreiben-Modul (Blueprint ``circulars``).

Deckt ab: Permission-Gate, CRUD nur im Entwurf, Notfall-Bypass beim
E-Mail-Versand vs. normales Einwilligungs-Gate, Testmodus, Karten-Daten-Gate,
Post-Druck ohne WeasyPrint.
"""
from datetime import date

import pytest

from app.extensions import db
from app.models import Circular, CircularRecipient, Customer, Incident, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def viewer(app):
    """User ohne ``circulars``-Recht (nur stammdaten)."""
    role = _ensure_role("Viewer", perms=["stammdaten"])
    u = User(username="viewer", email="v@v.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin"):
    return client.post("/auth/login", data={"username": username, "password": "secret"})


def _make_circular(kind=Circular.KIND_GENERAL, status=Circular.STATUS_DRAFT):
    circ = Circular(kind=kind, subject="Betreff", body="{anrede}\nText", status=status)
    db.session.add(circ)
    db.session.commit()
    return circ


def _customer(email=None, consent=False):
    c = Customer(name="Kunde", email=email, rechnung_per_email=consent)
    db.session.add(c)
    db.session.commit()
    return c


class TestAccess:
    def test_index_requires_login(self, client):
        client.get("/auth/logout")
        resp = client.get("/circulars/")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_index_ok_for_admin(self, client, admin):
        _login(client)
        resp = client.get("/circulars/")
        assert resp.status_code == 200
        assert "Rundschreiben" in resp.get_data(as_text=True)

    def test_permission_gate_redirects(self, client, viewer):
        client.get("/auth/logout")
        _login(client, "viewer")
        resp = client.get("/circulars/")
        assert resp.status_code == 302
        assert "/auth/login" not in resp.headers["Location"]  # -> dashboard

    def test_map_data_permission_gate(self, client, viewer, admin):
        circ = _make_circular()
        client.get("/auth/logout")
        _login(client, "viewer")
        resp = client.get(f"/circulars/{circ.id}/map-data.json")
        assert resp.status_code == 302  # kein circulars-Recht -> Dashboard-Redirect


class TestMapSelect:
    """Kartenauswahl: Seite, Stoerungs-Ebene und deren Rechte-Gate."""

    def test_map_select_page_renders(self, client, admin):
        _login(client)
        circ = _make_circular()
        resp = client.get(f"/circulars/{circ.id}/map-select")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "technik-map.js" in html        # Netz-Symbole + Popup wiederverwendet
        assert 'id="circ-sel-list"' in html    # Liste der gewaehlten Eigentuemer
        assert 'id="circ-show-incidents"' in html

    def test_map_incidents_returns_open_ones(self, client, admin):
        _login(client)
        circ = _make_circular()
        inc = Incident(title="Rohrbruch", detected_at=date(2026, 3, 1),
                       status=Incident.STATUS_OPEN, lat=46.8, lng=13.5)
        db.session.add(inc)
        db.session.commit()
        resp = client.get(f"/circulars/{circ.id}/map-incidents.json")
        assert resp.status_code == 200
        assert [f["id"] for f in resp.get_json()["features"]] == [inc.id]

    def test_map_incidents_empty_without_incident_permission(self, client, app):
        """Rundschreiben ja, Stoerungsjournal nein -> leere Ebene statt 403."""
        role = _ensure_role("Rundschreiber", perms=["circulars"])
        u = User(username="rs", email="rs@t.test", role_id=role.id)
        u.set_password("secret")
        db.session.add(u)
        inc = Incident(title="Rohrbruch", detected_at=date(2026, 3, 1),
                       status=Incident.STATUS_OPEN, lat=46.8, lng=13.5)
        db.session.add(inc)
        db.session.commit()
        circ = _make_circular()
        _login(client, "rs")
        resp = client.get(f"/circulars/{circ.id}/map-incidents.json")
        assert resp.status_code == 200
        assert resp.get_json()["features"] == []
        # ... und der Schalter fehlt auf der Seite.
        page = client.get(f"/circulars/{circ.id}/map-select").get_data(as_text=True)
        assert 'id="circ-show-incidents"' not in page


class TestCrud:
    def test_create_redirects_to_recipients(self, client, admin):
        _login(client)
        resp = client.post("/circulars/new", data={
            "kind": Circular.KIND_GENERAL, "subject": "Hallo", "body": "Text"})
        assert resp.status_code == 302
        assert "/recipients" in resp.headers["Location"]
        assert Circular.query.count() == 1

    def test_create_requires_subject(self, client, admin):
        _login(client)
        resp = client.post("/circulars/new", data={
            "kind": Circular.KIND_GENERAL, "subject": "", "body": "Text"})
        assert resp.status_code == 302
        assert Circular.query.count() == 0

    def test_edit_blocked_after_sent(self, client, admin):
        _login(client)
        circ = _make_circular(status=Circular.STATUS_SENT)
        resp = client.post(f"/circulars/{circ.id}/edit", data={
            "kind": Circular.KIND_GENERAL, "subject": "Neu", "body": "Neu"})
        assert resp.status_code == 302
        db.session.refresh(circ)
        assert circ.subject == "Betreff"  # unveraendert

    def test_delete_blocked_after_sent(self, client, admin):
        _login(client)
        circ = _make_circular(status=Circular.STATUS_SENT)
        client.post(f"/circulars/{circ.id}/delete")
        assert Circular.query.count() == 1  # bleibt erhalten

    def test_delete_draft(self, client, admin):
        _login(client)
        circ = _make_circular()
        client.post(f"/circulars/{circ.id}/delete")
        assert Circular.query.count() == 0


class TestEmailSend:
    def test_general_without_consent_rejected(self, client, admin):
        _login(client)
        circ = _make_circular(kind=Circular.KIND_GENERAL)
        c = _customer(email="x@test.at", consent=False)
        resp = client.post(f"/circulars/{circ.id}/send-email-ajax",
                           data={"customer_id": c.id})
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_emergency_without_consent_ok(self, client, admin):
        _login(client)
        circ = _make_circular(kind=Circular.KIND_BOIL_WATER)
        c = _customer(email="x@test.at", consent=False)
        resp = client.post(f"/circulars/{circ.id}/send-email-ajax",
                           data={"customer_id": c.id})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # Empfaenger-Zeile + Log angelegt, Status auf 'sent'.
        db.session.refresh(circ)
        assert circ.status == Circular.STATUS_SENT
        rec = CircularRecipient.query.filter_by(circular_id=circ.id, customer_id=c.id).first()
        assert rec is not None and rec.email_sent_at is not None

    def test_test_mode_uses_own_address(self, client, admin):
        _login(client)
        circ = _make_circular(kind=Circular.KIND_GENERAL)
        c = _customer(email="x@test.at", consent=False)
        resp = client.post(f"/circulars/{circ.id}/send-email-ajax",
                           data={"customer_id": c.id, "test_mode": "1"})
        assert resp.status_code == 200
        j = resp.get_json()
        assert j["ok"] is True and j["test_mode"] is True
        assert j["email"] == "admin@test.test"
        # Testmodus legt KEINE echte Empfaenger-Zeile an, Status bleibt Entwurf.
        db.session.refresh(circ)
        assert circ.status == Circular.STATUS_DRAFT
        assert CircularRecipient.query.count() == 0


class TestPostPrint:
    def test_print_merged_without_recipients(self, client, admin):
        _login(client)
        circ = _make_circular()
        resp = client.post(f"/circulars/{circ.id}/print-merged")
        assert resp.status_code == 302  # nichts zu drucken -> zurueck zur Versandseite
