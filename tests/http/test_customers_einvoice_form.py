"""Kundenformular: Felder fuer die E-Rechnung (Format, Leitweg-ID, USt-IdNr.)."""
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
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _company(**extra):
    return {"is_company": "1", "company_name": "Gemeinde Musterdorf",
            "is_customer": "1", "force": "1", **extra}


class TestEInvoiceFields:
    def test_government_customer_is_saved(self, client, admin):
        _login(client)
        client.post("/customers/new", data=_company(
            einvoice_format="xrechnung", buyer_reference=" 04011000-1234512345-06 ",
            vat_id="de 123 456 789"))
        c = Customer.query.filter_by(name="Gemeinde Musterdorf").one()
        assert c.einvoice_format == "xrechnung"
        assert c.buyer_reference == "04011000-1234512345-06"
        assert c.vat_id == "DE123456789"          # ohne Leerzeichen, gross

    def test_defaults_stay_empty(self, client, admin):
        _login(client)
        client.post("/customers/new", data=_company())
        c = Customer.query.filter_by(name="Gemeinde Musterdorf").one()
        assert c.einvoice_format is None
        assert c.buyer_reference is None and c.vat_id is None

    def test_unknown_format_is_ignored(self, client, admin):
        _login(client)
        client.post("/customers/new", data=_company(einvoice_format="ebinterface"))
        assert Customer.query.filter_by(name="Gemeinde Musterdorf").one().einvoice_format is None

    def test_wrong_leitweg_check_digit_is_rejected(self, client, admin):
        _login(client)
        r = client.post("/customers/new", data=_company(
            einvoice_format="xrechnung", buyer_reference="04011000-1234512345-07"))
        assert "ungültige Prüfziffer" in r.get_data(as_text=True)
        assert Customer.query.filter_by(name="Gemeinde Musterdorf").first() is None

    def test_free_reference_of_a_company_customer_is_accepted(self, client, admin):
        _login(client)
        client.post("/customers/new", data=_company(
            einvoice_format="xrechnung", buyer_reference="Bestellung 2024-15"))
        assert Customer.query.filter_by(name="Gemeinde Musterdorf").one().buyer_reference \
            == "Bestellung 2024-15"

    def test_vat_id_needs_a_country_prefix(self, client, admin):
        _login(client)
        r = client.post("/customers/new", data=_company(vat_id="123456789"))
        assert "Länderkürzel" in r.get_data(as_text=True)
        assert Customer.query.filter_by(name="Gemeinde Musterdorf").first() is None

    def test_edit_can_switch_back_to_zugferd(self, client, admin):
        c = Customer(name="Gemeinde Musterdorf", is_company=True, is_customer=True,
                     einvoice_format="xrechnung", buyer_reference="992-90009-96")
        db.session.add(c)
        db.session.commit()
        _login(client)
        client.post(f"/customers/{c.id}/edit", data={
            "is_company": "1", "company_name": "Gemeinde Musterdorf", "is_customer": "1",
            "einvoice_format": "", "buyer_reference": "992-90009-96"})
        c = db.session.get(Customer, c.id)
        assert c.einvoice_format is None
        assert c.buyer_reference == "992-90009-96"    # bleibt stehen

    def test_form_shows_the_current_values(self, client, admin):
        c = Customer(name="Gemeinde Musterdorf", is_company=True, is_customer=True,
                     einvoice_format="xrechnung", buyer_reference="992-90009-96",
                     vat_id="DE123456789")
        db.session.add(c)
        db.session.commit()
        _login(client)
        html = client.get(f"/customers/{c.id}/edit").get_data(as_text=True)
        assert '<option value="xrechnung" selected>' in html
        assert 'value="992-90009-96"' in html
        assert 'value="DE123456789"' in html


class TestPeppolFields:
    def test_federal_customer_is_saved(self, client, admin):
        _login(client)
        client.post("/customers/new", data=_company(
            einvoice_format="peppol_ubl", order_reference=" BBG-4711 ",
            supplier_number="L-123456", peppol_id=" 9915:b "))
        c = Customer.query.filter_by(name="Gemeinde Musterdorf").one()
        assert c.einvoice_format == "peppol_ubl"
        assert (c.order_reference, c.supplier_number, c.peppol_id) == ("BBG-4711", "L-123456", "9915:b")

    def test_a_peppol_id_needs_scheme_and_identifier(self, client, admin):
        _login(client)
        r = client.post("/customers/new", data=_company(einvoice_format="peppol_ubl",
                                                        peppol_id="9915b"))
        assert "Schema:Kennung" in r.get_data(as_text=True)
        assert Customer.query.filter_by(name="Gemeinde Musterdorf").first() is None

    def test_the_form_shows_the_ubl_option_and_the_values(self, client, admin):
        c = Customer(name="Gemeinde Musterdorf", is_company=True, is_customer=True,
                     einvoice_format="peppol_ubl", order_reference="BBG-4711",
                     supplier_number="L-123456", peppol_id="9915:b")
        db.session.add(c)
        db.session.commit()
        _login(client)
        html = client.get(f"/customers/{c.id}/edit").get_data(as_text=True)
        assert '<option value="peppol_ubl" selected>' in html
        for value in ('value="BBG-4711"', 'value="L-123456"', 'value="9915:b"'):
            assert value in html
