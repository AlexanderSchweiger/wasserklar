"""Liegenschafts-Formular: Eingaben, die in der Datenbank scheitern wuerden, werden
vorab abgewiesen (Prod 2026-11-07: 19 x HTTP 500 beim Speichern, vermutlich eine
schon vergebene Objektnummer)."""
import pytest

from app.extensions import db
from app.models import Property, User
from tests.conftest import _ensure_role

MODAL = {"X-From-Modal": "1", "HX-Request": "true"}


@pytest.fixture
def admin(app):
    u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def logged_in(client, admin):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


def _prop(number, active=True):
    p = Property(object_number=number, object_type="Haus", strasse="Dorfstraße", hausnummer="1",
                 plz="4232", ort="Hagenberg", active=active)
    db.session.add(p)
    db.session.commit()
    return p


def _form(**over):
    data = {"object_number": "", "object_type": "Haus", "strasse": "Dorfstraße", "hausnummer": "2",
            "plz": "4232", "ort": "Hagenberg", "land": "Österreich", "notes": ""}
    data.update(over)
    return data


class TestObjectNumber:
    def test_edit_with_number_in_use_shows_message(self, logged_in):
        _prop("A-1")
        other = _prop("A-2")
        resp = logged_in.post(f"/properties/{other.id}/edit", headers=MODAL, data=_form(object_number="A-1"))
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "A-1" in body and "bereits" in body
        db.session.expire_all()
        assert db.session.get(Property, other.id).object_number == "A-2"

    def test_number_of_archived_property_is_also_taken(self, logged_in):
        """Die Eindeutigkeit gilt auch gegen archivierte Liegenschaften."""
        _prop("A-1", active=False)
        other = _prop("A-2")
        resp = logged_in.post(f"/properties/{other.id}/edit", headers=MODAL, data=_form(object_number="A-1"))
        assert resp.status_code == 200 and "archiviert" in resp.get_data(as_text=True)

    def test_own_number_and_empty_number_still_save(self, logged_in):
        own = _prop("A-1")
        assert logged_in.post(f"/properties/{own.id}/edit", headers=MODAL,
                              data=_form(object_number="A-1", ort="Linz")).status_code == 204
        assert logged_in.post(f"/properties/{own.id}/edit", headers=MODAL,
                              data=_form(object_number="")).status_code == 204
        db.session.expire_all()
        assert db.session.get(Property, own.id).object_number is None

    def test_new_with_number_in_use_shows_message(self, logged_in):
        _prop("A-1")
        resp = logged_in.post("/properties/new", headers=MODAL, data=_form(object_number="A-1"))
        assert resp.status_code == 200 and "bereits" in resp.get_data(as_text=True)
        assert Property.query.count() == 1

    def test_full_page_form_shows_message(self, logged_in):
        _prop("A-1")
        other = _prop("A-2")
        resp = logged_in.post(f"/properties/{other.id}/edit", data=_form(object_number="A-1"))
        assert resp.status_code == 200 and "bereits" in resp.get_data(as_text=True)


class TestFieldLengths:
    """SQLite prueft die Laenge nicht, Postgres/MariaDB schon (dort waere es ein 500)."""

    @pytest.mark.parametrize("field,limit,label", [
        ("object_number", 50, "Objektnummer"), ("hausnummer", 20, "Hausnummer"),
        ("plz", 10, "PLZ"), ("ort", 100, "Ort"), ("strasse", 200, "Straße"),
    ])
    def test_too_long_is_rejected(self, logged_in, field, limit, label):
        p = _prop("A-1")
        resp = logged_in.post(f"/properties/{p.id}/edit", headers=MODAL, data=_form(**{field: "x" * (limit + 1)}))
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert label in body and str(limit) in body

    def test_exact_limit_is_fine(self, logged_in):
        p = _prop("A-1")
        assert logged_in.post(f"/properties/{p.id}/edit", headers=MODAL,
                              data=_form(hausnummer="x" * 20)).status_code == 204
