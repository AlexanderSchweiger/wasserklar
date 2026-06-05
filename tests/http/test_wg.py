"""HTTP-Tests fuer die WG-Features: Listen-Filter/-Spalten (Kontakte +
Liegenschaften), Mandant-Typ-Gating und die Edit-Modale (Kontakt, Buchungsjahr,
Abrechnungsperiode)."""
import json

import pytest

from app.extensions import db
from app.models import (
    Customer, Property, CustomerWgProfile, PropertyWgProfile, WgFunction,
    FiscalYear, BillingPeriod, User, AppSetting,
)
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


def _member(name, status="member"):
    c = Customer(name=name, is_customer=True)
    db.session.add(c)
    db.session.flush()
    c.wg_profile = CustomerWgProfile(status=status)
    db.session.commit()
    return c


def _prop(nr, shares):
    p = Property(object_number=nr, object_type="Haus")
    db.session.add(p)
    db.session.flush()
    if shares is not None:
        p.wg_profile = PropertyWgProfile(shares=shares)
    db.session.commit()
    return p


class TestCustomerList:
    def test_wg_columns_and_rename(self, client, admin):
        _login(client)
        _member("Anna Mitglied", "member")
        body = client.get("/customers/").get_data(as_text=True)
        assert "Status" in body and "Funktion" in body
        assert "Mitglieder" in body          # Tab "Kunden" → "Mitglieder"

    def test_status_filter(self, client, admin):
        _login(client)
        _member("Mitglied X", "member")
        _member("Interessent Y", "prospect")
        body = client.get("/customers/?status=member").get_data(as_text=True)
        assert "Mitglied X" in body
        assert "Interessent Y" not in body

    def test_function_filter(self, client, admin):
        _login(client)
        c = _member("Kassier K", "member")
        db.session.add(WgFunction(customer_id=c.id, function="treasurer"))
        _member("Ohne Funktion", "member")
        db.session.commit()
        body = client.get("/customers/?func=treasurer").get_data(as_text=True)
        assert "Kassier K" in body
        assert "Ohne Funktion" not in body

    def test_function_filter_any_and_none(self, client, admin):
        _login(client)
        c = _member("Hat Funktion", "member")
        db.session.add(WgFunction(customer_id=c.id, function="treasurer"))
        _member("Keine Funktion", "member")
        db.session.commit()

        any_body = client.get("/customers/?func=__any__").get_data(as_text=True)
        assert "Hat Funktion" in any_body and "Keine Funktion" not in any_body

        none_body = client.get("/customers/?func=__none__").get_data(as_text=True)
        assert "Hat Funktion" not in none_body and "Keine Funktion" in none_body

    def test_default_status_is_member(self, client, admin):
        _login(client)
        # Kontakt ohne WG-Profil (z.B. Altbestand) gilt standardmaessig als
        # Mitglied, nicht als Interessent.
        c = Customer(name="Ohne Profil", is_customer=True)
        db.session.add(c)
        db.session.commit()
        assert c.wg_status == "member"

        member_body = client.get("/customers/?status=member").get_data(as_text=True)
        assert "Ohne Profil" in member_body
        prospect_body = client.get("/customers/?status=prospect").get_data(as_text=True)
        assert "Ohne Profil" not in prospect_body

    def test_versorger_hides_member_rename(self, client, admin):
        _login(client)
        AppSetting.set("org.type", "utility")
        db.session.commit()
        _member("Irgendwer", "member")
        body = client.get("/customers/").get_data(as_text=True)
        assert "Mitglieder" not in body      # Versorger-Modus: bleibt "Kunden"


class TestCustomerEditModal:
    def test_get_returns_fragment(self, client, admin):
        _login(client)
        c = _member("Edit Me", "prospect")
        r = client.get(f"/customers/{c.id}/edit", headers={"X-From-Modal": "1"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "<html" not in body.lower()   # nur Fragment
        assert "Mitgliedschaft" in body      # WG-Block

    def test_post_saves_and_triggers(self, client, admin):
        _login(client)
        c = _member("Save Me", "prospect")
        r = client.post(
            f"/customers/{c.id}/edit",
            headers={"X-From-Modal": "1"},
            data={
                "name": "Save Me",
                "is_customer": "1",
                "wg_status": "member",
                "wg_functions": ["treasurer", "secretary"],
                "member_until": "",
            },
        )
        assert r.status_code == 204
        trig = json.loads(r.headers["HX-Trigger"])
        assert "closeCustomerEditModal" in trig and "customerEdited" in trig

        db.session.expire_all()
        saved = db.session.get(Customer, c.id)
        assert saved.wg_status == "member"
        assert saved.function_keys() == {"treasurer", "secretary"}


class TestPropertyList:
    def test_shares_filter_with(self, client, admin):
        _login(client)
        _prop("OBJ-1", 5)
        _prop("OBJ-2", 0)
        body = client.get("/properties/?shares=with").get_data(as_text=True)
        assert "OBJ-1" in body
        assert "OBJ-2" not in body

    def test_shares_filter_without_covers_zero_and_missing(self, client, admin):
        _login(client)
        _prop("OBJ-1", 5)
        _prop("OBJ-2", 0)
        _prop("OBJ-3", None)                 # gar kein Profil
        body = client.get("/properties/?shares=without").get_data(as_text=True)
        assert "OBJ-1" not in body
        assert "OBJ-2" in body
        assert "OBJ-3" in body

    def test_shares_sort_desc(self, client, admin):
        _login(client)
        _prop("LOWNR", 1)
        _prop("HIGHNR", 9)
        body = client.get("/properties/?sort=shares&dir=desc").get_data(as_text=True)
        assert body.index("HIGHNR") < body.index("LOWNR")


class TestPropertyEditModal:
    def test_post_persists_shares_and_area(self, client, admin):
        _login(client)
        p = Property(object_number="P1", object_type="Haus")
        db.session.add(p)
        db.session.commit()
        r = client.post(
            f"/properties/{p.id}/edit",
            headers={"X-From-Modal": "1"},
            data={"object_type": "Haus", "object_number": "P1",
                  "wg_shares": "4", "area_m2": "950"},
        )
        assert r.status_code == 204
        db.session.expire_all()
        saved = db.session.get(Property, p.id)
        assert saved.wg_shares == 4
        assert saved.wg_area_m2 == 950


class TestFiscalYearModal:
    def test_get_fragment_then_post(self, client, admin):
        _login(client)
        r = client.get("/accounting/fiscal-years/new", headers={"X-From-Modal": "1"})
        assert r.status_code == 200
        assert "<html" not in r.get_data(as_text=True).lower()

        r2 = client.post(
            "/accounting/fiscal-years/new",
            headers={"X-From-Modal": "1"},
            data={"year": "2031", "start_date": "2031-01-01", "end_date": "2031-12-31"},
        )
        assert r2.status_code == 204
        assert "closeFiscalYearModal" in json.loads(r2.headers["HX-Trigger"])
        assert db.session.get(FiscalYear, 2031) is not None


class TestPeriodModal:
    def test_get_fragment_then_post(self, client, admin):
        _login(client)
        r = client.get("/perioden/neu", headers={"X-From-Modal": "1"})
        assert r.status_code == 200
        assert "<html" not in r.get_data(as_text=True).lower()

        r2 = client.post(
            "/perioden/neu",
            headers={"X-From-Modal": "1"},
            data={"name": "Modal-Periode", "start_date": "2031-01-01",
                  "end_date": "2031-12-31"},
        )
        assert r2.status_code == 204
        assert "closePeriodModal" in json.loads(r2.headers["HX-Trigger"])
        assert BillingPeriod.query.filter_by(name="Modal-Periode").count() == 1
