"""HTTP-Tests fuer die plan-uebergreifende Elementliste (/technik/elements).

CSRF ist im Test-Modus aus (TestingConfig). Cookie-Jar-Stolperer (Werkzeug 3):
``_login`` macht vorher ``/auth/logout``.
"""
from datetime import date

import pytest

from app.extensions import db
from app.models import NetworkPlan, Property, PropertyOwnership, Customer, User
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
def plain_user(app):
    role = _ensure_role("NurStammdaten", perms=["stammdaten"])
    u = User(username="hans", email="hans@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture(autouse=True)
def active_plan(app):
    p = NetworkPlan(name="Testplan", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True)
    db.session.add(p)
    db.session.commit()
    return p


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": password})


def _make_point(client, ftype="hydrant", lng=16.37, lat=48.21, **props):
    body = {"geometry": {"type": "Point", "coordinates": [lng, lat]}, "feature_type": ftype}
    body.update(props)
    return client.post("/technik/features", json=body)


def _customer(name):
    c = Customer(name=name)
    db.session.add(c)
    db.session.commit()
    return c


def _property(object_number, otype="Haus"):
    p = Property(object_number=object_number, object_type=otype)
    db.session.add(p)
    db.session.commit()
    return p


def _own(prop, cust):
    o = PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                          valid_from=date(2020, 1, 1), valid_to=None)
    db.session.add(o)
    db.session.commit()
    return o


class TestAccess:
    def test_login_required(self, client, admin):
        client.get("/auth/logout")
        r = client.get("/technik/elements")
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_permission_gate_redirects(self, client, plain_user):
        _login(client, "hans")
        r = client.get("/technik/elements", follow_redirects=True)
        assert r.status_code == 200
        assert "Kein Zugriff" in r.get_data(as_text=True)


class TestElementeList:
    def test_lists_features_across_plans(self, client, admin, active_plan):
        _login(client)
        p2 = NetworkPlan(name="Zweitplan", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True)
        db.session.add(p2)
        db.session.commit()
        _make_point(client, name="Hydrant A")                      # -> aktueller Plan (Testplan)
        _make_point(client, name="Hydrant B", plan_id=p2.id)       # -> Zweitplan
        body = client.get("/technik/elements").get_data(as_text=True)
        assert "Hydrant A" in body
        assert "Hydrant B" in body
        assert "Testplan" in body and "Zweitplan" in body

    def test_plan_filter(self, client, admin, active_plan):
        _login(client)
        p2 = NetworkPlan(name="Zweitplan", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True)
        db.session.add(p2)
        db.session.commit()
        _make_point(client, name="Hydrant A")
        _make_point(client, name="Hydrant B", plan_id=p2.id)
        body = client.get(f"/technik/elements?plan={p2.id}").get_data(as_text=True)
        assert "Hydrant B" in body
        assert "Hydrant A" not in body

    def test_search_by_name(self, client, admin):
        _login(client)
        _make_point(client, name="Dorfplatz-Hydrant")
        _make_point(client, name="Bachweg-Schieber", ftype="schieber")
        body = client.get("/technik/elements?q=Dorfplatz").get_data(as_text=True)
        assert "Dorfplatz-Hydrant" in body
        assert "Bachweg-Schieber" not in body

    def test_filter_by_type(self, client, admin):
        _login(client)
        _make_point(client, name="Obj1", ftype="hydrant")
        _make_point(client, name="Obj2", ftype="schieber")
        body = client.get("/technik/elements?type=hydrant").get_data(as_text=True)
        assert "Obj1" in body
        assert "Obj2" not in body

    def test_type_not_in_fulltext_search(self, client, admin):
        """Typ ist aus der Volltextsuche entfernt — dafuer gibt es den Typ-Filter."""
        _login(client)
        _make_point(client, name="Alpha", ftype="hydrant")
        body = client.get("/technik/elements?q=Hydrant").get_data(as_text=True)
        assert "Alpha" not in body

    def test_invalid_type_ignored(self, client, admin):
        _login(client)
        _make_point(client, name="Bravo", ftype="hydrant")
        body = client.get("/technik/elements?type=quatsch").get_data(as_text=True)
        assert "Bravo" in body   # ungültiger Typ-Filter -> ignoriert

    def test_hx_request_returns_fragment(self, client, admin):
        _login(client)
        _make_point(client, name="FragTest")
        full = client.get("/technik/elements").get_data(as_text=True)
        frag = client.get("/technik/elements", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        fb = frag.get_data(as_text=True)
        assert "FragTest" in fb
        assert 'name="q"' in full      # Vollseite hat die Suchleiste
        assert 'name="q"' not in fb    # Fragment nicht

    def test_sort_by_year_built(self, client, admin):
        _login(client)
        z = _make_point(client, name="Zeta").get_json()["id"]
        a = _make_point(client, name="Alpha").get_json()["id"]
        client.post(f"/technik/features/{z}", data={"feature_type": "hydrant", "name": "Zeta", "year_built": "1980"})
        client.post(f"/technik/features/{a}", data={"feature_type": "hydrant", "name": "Alpha", "year_built": "2020"})
        body = client.get("/technik/elements?sort=year_built&dir=asc").get_data(as_text=True)
        assert body.index("Zeta") < body.index("Alpha")
        body2 = client.get("/technik/elements?sort=year_built&dir=desc").get_data(as_text=True)
        assert body2.index("Alpha") < body2.index("Zeta")

    def test_sort_wartung(self, client, admin):
        _login(client)
        early = _make_point(client, name="FruehFaellig").get_json()["id"]
        late = _make_point(client, name="SpaetFaellig").get_json()["id"]
        client.post(f"/technik/features/{early}/maintenance",
                    data={"date": "2025-01-01", "kind": "inspektion", "next_due": "2026-01-01"})
        client.post(f"/technik/features/{late}/maintenance",
                    data={"date": "2025-01-01", "kind": "inspektion", "next_due": "2027-01-01"})
        body = client.get("/technik/elements?sort=wartung&dir=asc").get_data(as_text=True)
        assert body.index("FruehFaellig") < body.index("SpaetFaellig")

    def test_maintenance_disabled_shows_note(self, client, admin):
        _login(client)
        p = NetworkPlan(name="OhneWartung", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=False)
        db.session.add(p)
        db.session.commit()
        _make_point(client, name="ObjOhne", plan_id=p.id)
        body = client.get(f"/technik/elements?plan={p.id}").get_data(as_text=True)
        assert "ObjOhne" in body
        assert "deaktiviert" in body

    def test_no_plan_shows_warning(self, client, admin, active_plan):
        _login(client)
        db.session.delete(active_plan)
        db.session.commit()
        body = client.get("/technik/elements").get_data(as_text=True)
        assert "Plan anlegen" in body

    def test_pagination(self, client, admin):
        _login(client)
        for i in range(12):
            _make_point(client, name=f"Obj{i:02d}")
        hdr = {"HX-Request": "true"}
        p1 = client.get("/technik/elements?per_page=10&page=1", headers=hdr).get_data(as_text=True)
        p2 = client.get("/technik/elements?per_page=10&page=2", headers=hdr).get_data(as_text=True)
        assert p1.count("openFeatureEditModal(") == 10
        assert p2.count("openFeatureEditModal(") == 2
        assert "von <strong>12</strong>" in p1


class TestObjektUndBesitzer:
    def test_shows_property_and_owner(self, client, admin):
        _login(client)
        cust = _customer("Familie Huber")
        prop = _property("OBJ-7")
        _own(prop, cust)
        _make_point(client, name="Hydrant X", property_id=prop.id)
        body = client.get("/technik/elements").get_data(as_text=True)
        assert "OBJ-7" in body            # Objekt-Spalte (label enthält object_number)
        assert "Familie Huber" in body    # Besitzer-Spalte

    def test_filter_by_owner(self, client, admin):
        _login(client)
        huber = _customer("Familie Huber")
        maier = _customer("Familie Maier")
        p1 = _property("OBJ-1"); _own(p1, huber)
        p2 = _property("OBJ-2"); _own(p2, maier)
        _make_point(client, name="Punkt Eins", property_id=p1.id)   # neutrale Namen,
        _make_point(client, name="Punkt Zwei", property_id=p2.id)   # damit nur der Besitzer matcht
        body = client.get("/technik/elements?q=Huber").get_data(as_text=True)
        assert "Punkt Eins" in body
        assert "Punkt Zwei" not in body

    def test_filter_by_object_number(self, client, admin):
        _login(client)
        cust = _customer("Eigentümer A")
        p = _property("PARZ-99"); _own(p, cust)
        _make_point(client, name="Punkt A", property_id=p.id)
        _make_point(client, name="Punkt B")
        body = client.get("/technik/elements?q=PARZ-99").get_data(as_text=True)
        assert "Punkt A" in body
        assert "Punkt B" not in body

    def test_sort_by_objekt(self, client, admin):
        _login(client)
        c = _customer("C")
        pa = _property("AAA-1"); _own(pa, c)
        pz = _property("ZZZ-9"); _own(pz, c)
        _make_point(client, name="ElemZ", property_id=pz.id)
        _make_point(client, name="ElemA", property_id=pa.id)
        body = client.get("/technik/elements?sort=objekt&dir=asc").get_data(as_text=True)
        assert body.index("AAA-1") < body.index("ZZZ-9")

    def test_multiple_owners_joined(self, client, admin):
        _login(client)
        a = _customer("Anna Berg")
        b = _customer("Bert Berg")
        prop = _property("EHE-1")
        _own(prop, a)
        _own(prop, b)   # zweiter paralleler Besitzer (Ehepaar)
        _make_point(client, name="Gemeinsam", property_id=prop.id)
        body = client.get("/technik/elements").get_data(as_text=True)
        assert "Anna Berg" in body
        assert "Bert Berg" in body
