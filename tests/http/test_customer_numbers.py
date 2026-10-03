"""Nummernkreise am Kontakt: Kunden-/Mitgliedsnummer und Lieferantennummer.

Regeln (app/customers/numbers.py): je Rolle ein eigener Kreis, Vergabe automatisch bei
leerem Feld (auch beim Bearbeiten), eine verwendete Nummer ist geschuetzt, ein grosser
Sprung bei Handeingabe muss bestaetigt werden, reine Lieferanten sind nie Mitglieder.
"""
import io
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import (
    Account, AppSetting, Booking, Customer, CustomerWgProfile, Invoice, SupplierCounter, User,
)
from app.utils import SUPPLIER_START_DEFAULT
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
def logged_in(client, admin):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


def _new(client, last_name, **extra):
    data = {"is_company": "0", "last_name": last_name, "force": "1"}
    data.update(extra)
    return client.post("/customers/new", data=data)


def _edit(client, c, **extra):
    data = {"is_company": "0", "last_name": c.last_name or c.name}
    if c.is_customer:
        data["is_customer"] = "1"
    if c.is_supplier:
        data["is_supplier"] = "1"
    data.update(extra)
    return client.post(f"/customers/{c.id}/edit", data=data)


def _get(last_name):
    db.session.expire_all()
    return Customer.query.filter_by(last_name=last_name).one()


def _invoice_for(customer):
    db.session.add(Invoice(invoice_number="2026-00001", customer_id=customer.id,
                           date=date(2026, 1, 15), status=Invoice.STATUS_DRAFT,
                           total_amount=Decimal("10")))
    db.session.commit()


class TestAutomaticAssignment:
    def test_customer_gets_next_number(self, logged_in):
        _new(logged_in, "Erster", is_customer="1")
        _new(logged_in, "Zweiter", is_customer="1")
        assert _get("Erster").customer_number == 1
        assert _get("Zweiter").customer_number == 2
        assert _get("Erster").creditor_number is None

    def test_supplier_gets_own_range(self, logged_in):
        _new(logged_in, "Baufirma", is_supplier="1")
        c = _get("Baufirma")
        assert c.customer_number is None
        assert c.creditor_number == SUPPLIER_START_DEFAULT

    def test_dual_role_gets_both_numbers(self, logged_in):
        _new(logged_in, "Beides", is_customer="1", is_supplier="1")
        c = _get("Beides")
        assert c.customer_number == 1
        assert c.creditor_number == SUPPLIER_START_DEFAULT

    def test_supplier_becoming_customer_gets_number_on_edit(self, logged_in):
        _new(logged_in, "Wechsel", is_supplier="1")
        c = _get("Wechsel")
        _edit(logged_in, c, is_customer="1")
        c = _get("Wechsel")
        assert c.customer_number == 1
        assert c.creditor_number == SUPPLIER_START_DEFAULT

    def test_configured_supplier_start(self, logged_in):
        AppSetting.set("numbers.supplier_start", "500")
        db.session.commit()
        _new(logged_in, "Startwert", is_supplier="1")
        assert _get("Startwert").creditor_number == 500

    def test_quick_create_assigns_supplier_number(self, logged_in):
        r = logged_in.post("/customers/quick-create", data={
            "name": "Schnell GmbH", "is_supplier": "1", "force": "1"})
        assert r.status_code == 200, r.get_data(as_text=True)
        c = Customer.query.filter_by(name="Schnell GmbH").one()
        assert c.creditor_number == SUPPLIER_START_DEFAULT
        assert c.customer_number is None


class TestManualEntry:
    def test_manual_number_is_taken(self, logged_in):
        _new(logged_in, "Hand", is_customer="1", customer_number="42")
        assert _get("Hand").customer_number == 42
        _new(logged_in, "Danach", is_customer="1")
        assert _get("Danach").customer_number == 43

    def test_duplicate_number_rejected(self, logged_in):
        _new(logged_in, "Eins", is_customer="1", customer_number="7")
        r = _new(logged_in, "Zwei", is_customer="1", customer_number="7")
        assert "bereits vergeben" in r.get_data(as_text=True)
        assert Customer.query.filter_by(last_name="Zwei").first() is None

    def test_big_jump_needs_confirmation(self, logged_in):
        r = _new(logged_in, "Tippfehler", is_customer="1", customer_number="10000")
        assert "bestätigen" in r.get_data(as_text=True)
        assert 'name="confirm_number_jump"' in r.get_data(as_text=True)
        assert Customer.query.filter_by(last_name="Tippfehler").first() is None
        _new(logged_in, "Tippfehler", is_customer="1", customer_number="10000",
             confirm_number_jump="1")
        assert _get("Tippfehler").customer_number == 10000


class TestProtection:
    def test_empty_field_on_edit_keeps_number(self, logged_in):
        _new(logged_in, "Bleibt", is_customer="1")
        c = _get("Bleibt")
        _edit(logged_in, c, customer_number="")
        assert _get("Bleibt").customer_number == 1

    def test_unused_number_can_be_removed_explicitly(self, logged_in):
        _new(logged_in, "Falsch", is_customer="1", is_supplier="1")
        c = _get("Falsch")
        r = _edit(logged_in, c, is_customer="", clear_customer_number="1")
        assert r.status_code in (200, 302)
        c = _get("Falsch")
        assert c.customer_number is None and c.is_customer is False

    def test_used_customer_number_is_locked(self, logged_in):
        _new(logged_in, "Gesperrt", is_customer="1")
        c = _get("Gesperrt")
        _invoice_for(c)
        _edit(logged_in, c, customer_number="99")
        _edit(logged_in, c, clear_customer_number="1")
        assert _get("Gesperrt").customer_number == 1
        page = logged_in.get(f"/customers/{c.id}/edit").get_data(as_text=True)
        assert "nicht mehr änderbar" in page

    def test_used_creditor_number_is_locked(self, logged_in):
        _new(logged_in, "Lieferant", is_supplier="1")
        c = _get("Lieferant")
        acc = Account(name="Aufwand", code="A01")
        db.session.add(acc)
        db.session.flush()
        db.session.add(Booking(date=date.today(), account_id=acc.id, amount=Decimal("-5"),
                               description="Rechnung", customer_id=c.id,
                               status=Booking.STATUS_OFFEN))
        db.session.commit()
        _edit(logged_in, c, creditor_number="70500", clear_creditor_number="1")
        assert _get("Lieferant").creditor_number == SUPPLIER_START_DEFAULT


class TestWgMode:
    @pytest.fixture(autouse=True)
    def cooperative(self, app):
        AppSetting.set("org.type", "cooperative")
        db.session.commit()

    def test_pure_supplier_gets_no_wg_profile(self, logged_in):
        _new(logged_in, "Installateur", is_supplier="1", wg_status="member")
        c = _get("Installateur")
        assert c.wg_profile is None

    def test_member_filter_excludes_suppliers(self, logged_in):
        _new(logged_in, "Mitglied", is_customer="1", wg_status="member")
        _new(logged_in, "Lieferfirma", is_supplier="1")
        member, supplier = _get("Mitglied"), _get("Lieferfirma")
        page = logged_in.get("/customers/?type=all&status=member").get_data(as_text=True)
        assert f'id="customer-row-{member.id}"' in page
        assert f'id="customer-row-{supplier.id}"' not in page

    def test_label_follows_status(self, logged_in):
        _new(logged_in, "Extern", is_customer="1", wg_status="external")
        c = _get("Extern")
        page = logged_in.get(f"/customers/{c.id}").get_data(as_text=True)
        assert "Kundennummer:" in page
        _new(logged_in, "Genosse", is_customer="1", wg_status="member")
        c = _get("Genosse")
        page = logged_in.get(f"/customers/{c.id}").get_data(as_text=True)
        assert "Mitgliedsnummer:" in page


class TestSearchAndSettings:
    def test_search_by_supplier_number(self, logged_in):
        _new(logged_in, "Suchbar", is_supplier="1")
        page = logged_in.get(f"/customers/?type=all&q={SUPPLIER_START_DEFAULT}").get_data(as_text=True)
        assert "Suchbar" in page

    def test_supplier_start_fixed_once_used(self, app):
        from app.settings.routes import _save_supplier_number_start
        from app.utils import next_supplier_number, supplier_number_start
        with app.test_request_context():
            _save_supplier_number_start("200")
            db.session.commit()
            assert supplier_number_start() == 200
            c = Customer(name="L", is_customer=False, is_supplier=True,
                         creditor_number=next_supplier_number())
            db.session.add(c)
            db.session.commit()
            assert c.creditor_number == 200
            _save_supplier_number_start("900")
            db.session.commit()
            assert supplier_number_start() == 200


def test_incoming_einvoice_supplier_gets_number(app):
    from app.accounting.incoming_service import create_supplier
    seller = SimpleNamespace(name="Rohr AG", street="", postcode="", city="", country="AT",
                             email=None, phone=None, vat_id="ATU12345678")
    supplier = create_supplier(SimpleNamespace(seller=seller))
    db.session.commit()
    assert supplier.creditor_number == SUPPLIER_START_DEFAULT
    assert supplier.customer_number is None


def test_supplier_counter_floor_after_manual_numbers(app):
    from app.utils import next_supplier_number
    db.session.add(Customer(name="Alt", is_customer=False, is_supplier=True,
                            creditor_number=SUPPLIER_START_DEFAULT + 5))
    db.session.commit()
    assert next_supplier_number() == SUPPLIER_START_DEFAULT + 6
    assert db.session.get(SupplierCounter, 1).next_seq == SUPPLIER_START_DEFAULT + 7
