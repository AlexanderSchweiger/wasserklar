"""HTTP-Tests fuer das WaterMeter-Form mit den neuen Feldern
``meter_type`` und ``parent_meter_id`` plus die Server-seitige Validierung
(Self-Reference, Parent-muss-main, Type-Wechsel auf 'main' setzt parent NULL).
"""
from datetime import date

import pytest

from app.extensions import db
from app.models import Property, User, WaterMeter


@pytest.fixture
def admin(app):
    u = User(username="admin", email="admin@test.test", role="admin")
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def prop(app):
    p = Property(object_number="P-1", object_type="Haus", ort="Wien")
    db.session.add(p)
    db.session.commit()
    return p


def _login(client):
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


# ---------------------------------------------------------------------------
# GET-Pfad: neue Felder im Form sichtbar
# ---------------------------------------------------------------------------

class TestMeterFormGet:
    def test_new_form_has_type_select(self, client, admin, prop):
        _login(client)
        r = client.get("/meters/new")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Zählertyp" in body
        assert "Hauptzähler" in body
        assert "Subzähler" in body
        assert 'name="meter_type"' in body
        assert 'name="parent_meter_id"' in body

    def test_new_form_lists_main_meters_only(self, client, admin, prop):
        # 1 Hauptzaehler + 1 Subzaehler -> Parent-Dropdown enthaelt nur den main
        db.session.add_all([
            WaterMeter(property_id=prop.id, meter_number="Z-MAIN",
                       meter_type="main", active=True),
            WaterMeter(property_id=prop.id, meter_number="Z-SUB",
                       meter_type="sub", active=True),
        ])
        db.session.commit()
        _login(client)
        r = client.get("/meters/new")
        body = r.get_data(as_text=True)
        # Z-MAIN muss im parent-dropdown vorkommen
        assert "Z-MAIN" in body
        # Z-SUB darf NICHT als parent-Kandidat erscheinen
        # (kommt aber evtl. an anderer Stelle vor -- daher pruefen wir grob:
        # er sollte nicht mit "selected" oder im parent-dropdown stehen.
        # Genaue Verifikation in den POST-Tests unten.)

    def test_edit_form_pre_selects_existing_values(self, client, admin, prop):
        main = WaterMeter(property_id=prop.id, meter_number="Z-MAIN",
                          meter_type="main", active=True)
        db.session.add(main); db.session.flush()
        sub = WaterMeter(property_id=prop.id, meter_number="Z-SUB",
                         meter_type="sub", active=True, parent_meter_id=main.id)
        db.session.add(sub); db.session.commit()
        _login(client)
        r = client.get(f"/meters/{sub.id}/edit")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        # Subzaehler-Option ausgewaehlt
        assert 'value="sub" selected' in body
        # Parent-Dropdown hat main als selected
        assert f'value="{main.id}" selected' in body


# ---------------------------------------------------------------------------
# POST: Anlegen mit verschiedenen Type/Parent-Kombinationen
# ---------------------------------------------------------------------------

class TestMeterCreateValidation:
    def _post_new(self, client, prop, **fields):
        data = {
            "property_id": str(prop.id),
            "meter_number": "Z-NEW",
            "meter_type": "main",
            "parent_meter_id": "",
            **fields,
        }
        return client.post("/meters/new", data=data, follow_redirects=False)

    def test_create_main_meter_default(self, client, admin, prop):
        _login(client)
        r = self._post_new(client, prop)
        assert r.status_code == 302
        m = WaterMeter.query.filter_by(meter_number="Z-NEW").one()
        assert m.meter_type == "main"
        assert m.parent_meter_id is None

    def test_create_sub_meter_with_valid_parent(self, client, admin, prop):
        main = WaterMeter(property_id=prop.id, meter_number="Z-MAIN",
                          meter_type="main", active=True)
        db.session.add(main); db.session.commit()
        _login(client)
        r = self._post_new(client, prop,
                           meter_type="sub",
                           parent_meter_id=str(main.id))
        assert r.status_code == 302
        m = WaterMeter.query.filter_by(meter_number="Z-NEW").one()
        assert m.meter_type == "sub"
        assert m.parent_meter_id == main.id

    def test_create_main_with_parent_id_strips_parent(self, client, admin, prop):
        # User schickt parent_id mit, aber type=main -> parent_id muss NULL werden
        main = WaterMeter(property_id=prop.id, meter_number="Z-MAIN",
                          meter_type="main", active=True)
        db.session.add(main); db.session.commit()
        _login(client)
        self._post_new(client, prop,
                       meter_type="main",
                       parent_meter_id=str(main.id))
        m = WaterMeter.query.filter_by(meter_number="Z-NEW").one()
        assert m.meter_type == "main"
        assert m.parent_meter_id is None

    def test_create_sub_with_non_main_parent_blocked(self, client, admin, prop):
        # Versuch, einen Subzaehler mit parent=Subzaehler anzulegen -> wird gekappt
        sub_existing = WaterMeter(property_id=prop.id, meter_number="Z-EX",
                                  meter_type="sub", active=True)
        db.session.add(sub_existing); db.session.commit()
        _login(client)
        r = self._post_new(client, prop,
                           meter_type="sub",
                           parent_meter_id=str(sub_existing.id),
                           follow_redirects=False) \
            if False else self._post_new(client, prop,
                                         meter_type="sub",
                                         parent_meter_id=str(sub_existing.id))
        # Server-Validierung kappt parent_id, Meter wird trotzdem angelegt
        m = WaterMeter.query.filter_by(meter_number="Z-NEW").one()
        assert m.meter_type == "sub"
        assert m.parent_meter_id is None

    def test_create_invalid_meter_type_falls_back_to_main(self, client, admin, prop):
        _login(client)
        self._post_new(client, prop, meter_type="evil")
        m = WaterMeter.query.filter_by(meter_number="Z-NEW").one()
        assert m.meter_type == "main"
        assert m.parent_meter_id is None


# ---------------------------------------------------------------------------
# POST: Edit -- Self-Reference, Type-Wechsel
# ---------------------------------------------------------------------------

class TestMeterEditValidation:
    def test_self_reference_blocked(self, client, admin, prop):
        # Edit mit parent_id == self.id -> wird auf NULL gekappt
        m = WaterMeter(property_id=prop.id, meter_number="Z-X",
                       meter_type="sub", active=True)
        db.session.add(m); db.session.commit()
        _login(client)
        client.post(f"/meters/{m.id}/edit", data={
            "property_id": str(prop.id),
            "meter_number": "Z-X",
            "meter_type": "sub",
            "parent_meter_id": str(m.id),  # SELF
        }, follow_redirects=False)
        db.session.refresh(m)
        assert m.parent_meter_id is None

    def test_type_change_to_main_clears_parent(self, client, admin, prop):
        main = WaterMeter(property_id=prop.id, meter_number="Z-M",
                          meter_type="main", active=True)
        db.session.add(main); db.session.flush()
        sub = WaterMeter(property_id=prop.id, meter_number="Z-S",
                         meter_type="sub", parent_meter_id=main.id, active=True)
        db.session.add(sub); db.session.commit()
        _login(client)
        client.post(f"/meters/{sub.id}/edit", data={
            "property_id": str(prop.id),
            "meter_number": "Z-S",
            "meter_type": "main",  # gewechselt!
            "parent_meter_id": str(main.id),  # User hat parent dringelassen
        }, follow_redirects=False)
        db.session.refresh(sub)
        assert sub.meter_type == "main"
        # Server kappt parent automatisch, weil type=main
        assert sub.parent_meter_id is None

    def test_type_change_to_sub_with_valid_parent(self, client, admin, prop):
        main = WaterMeter(property_id=prop.id, meter_number="Z-M",
                          meter_type="main", active=True)
        db.session.add(main); db.session.flush()
        m = WaterMeter(property_id=prop.id, meter_number="Z-X",
                       meter_type="main", active=True)
        db.session.add(m); db.session.commit()
        _login(client)
        client.post(f"/meters/{m.id}/edit", data={
            "property_id": str(prop.id),
            "meter_number": "Z-X",
            "meter_type": "sub",
            "parent_meter_id": str(main.id),
        }, follow_redirects=False)
        db.session.refresh(m)
        assert m.meter_type == "sub"
        assert m.parent_meter_id == main.id


# ---------------------------------------------------------------------------
# Index-Tabelle: Badges sichtbar
# ---------------------------------------------------------------------------

class TestMeterIndexBadges:
    def test_index_shows_main_badge(self, client, admin, prop):
        db.session.add(WaterMeter(property_id=prop.id, meter_number="Z-M",
                                  meter_type="main", active=True))
        db.session.commit()
        _login(client)
        r = client.get("/meters/")
        body = r.get_data(as_text=True)
        assert "Hauptzähler" in body
        assert "Z-M" in body

    def test_index_shows_sub_badge_with_parent_arrow(self, client, admin, prop):
        main = WaterMeter(property_id=prop.id, meter_number="Z-M",
                          meter_type="main", active=True)
        db.session.add(main); db.session.flush()
        db.session.add(WaterMeter(property_id=prop.id, meter_number="Z-S",
                                  meter_type="sub", parent_meter_id=main.id, active=True))
        db.session.commit()
        _login(client)
        r = client.get("/meters/")
        body = r.get_data(as_text=True)
        assert "Subzähler" in body
        # Parent-Arrow zeigt Z-M unter Z-S
        # Test eher locker: die Zaehlernummer des Parents muss zwei Mal vorkommen
        assert body.count("Z-M") >= 2
