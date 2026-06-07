"""HTTP-Tests fuer das aufgespaltene Namens-Formular der Kontakt-Anlage.

Verifiziert, dass aus Anrede/Vorname/Nachname bzw. Firmenname serverseitig das
kombinierte ``name`` (Sortier-/Listenfeld, "Nachname Vorname") abgeleitet wird
und die Pflichtfeld-Validierung greift.
"""
import pytest

from app.extensions import db
from app.models import Customer, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


class TestNewCustomerForm:
    def test_create_person_derives_combined_name(self, client, admin):
        _login(client)
        r = client.post("/customers/new", data={
            "is_company": "0", "salutation": "Herr",
            "first_name": "Max", "last_name": "Mustermann",
            "is_customer": "1", "force": "1",
        })
        assert r.status_code in (302, 200)
        c = Customer.query.filter_by(last_name="Mustermann").first()
        assert c is not None
        assert c.name == "Mustermann Max"          # Sortier-/Listenfeld
        assert c.first_name == "Max"
        assert c.salutation == "Herr"
        assert c.is_company is False
        assert c.letter_name == "Max Mustermann"
        assert c.salutation_line == "Sehr geehrter Herr Mustermann"

    def test_create_company(self, client, admin):
        _login(client)
        client.post("/customers/new", data={
            "is_company": "1", "company_name": "Wasser GmbH",
            "is_customer": "1", "force": "1",
        })
        c = Customer.query.filter_by(name="Wasser GmbH").first()
        assert c is not None
        assert c.is_company is True
        assert c.last_name is None and c.first_name is None
        assert c.letter_name == "Wasser GmbH"
        assert c.salutation_line == "Sehr geehrte Damen und Herren"

    def test_missing_lastname_is_rejected(self, client, admin):
        _login(client)
        r = client.post("/customers/new", data={
            "is_company": "0", "first_name": "Max",
            "is_customer": "1", "force": "1",
        })
        assert Customer.query.filter_by(first_name="Max").first() is None
        assert "Nachnamen" in r.get_data(as_text=True)

    def test_family_clears_first_name(self, client, admin):
        _login(client)
        client.post("/customers/new", data={
            "is_company": "0", "salutation": "Familie",
            "first_name": "wird-ignoriert", "last_name": "Mustermann",
            "is_customer": "1", "force": "1",
        })
        c = Customer.query.filter_by(last_name="Mustermann").first()
        assert c.salutation == "Familie"
        assert c.first_name is None                # Familie hat keinen Vornamen
        assert c.name == "Mustermann"
        assert c.letter_name == "Familie Mustermann"

    def test_edit_legacy_name_into_split_fields(self, client, admin):
        _login(client)
        # Altbestand: nur kombinierter Name.
        legacy = Customer(name="Mustermann Max", is_customer=True)
        db.session.add(legacy)
        db.session.commit()
        cid = legacy.id
        client.post(f"/customers/{cid}/edit", data={
            "is_company": "0", "salutation": "Herr",
            "first_name": "Max", "last_name": "Mustermann",
            "is_customer": "1",
        })
        db.session.expire_all()
        c = db.session.get(Customer, cid)
        assert c.first_name == "Max" and c.last_name == "Mustermann"
        assert c.name == "Mustermann Max"
        assert c.salutation_line == "Sehr geehrter Herr Mustermann"
