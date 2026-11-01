"""HTTP-Tests: Anteilsbuch der Liegenschaft (Detailkarte, Modal, Löschen),
Regel "Fläche je Anteil" in den Einstellungen, bezahlte Anteile am Kontakt
und der Liegenschafts-Bezug im Mitglieder-Modal."""
import json
from datetime import date

import pytest

from app import property_shares as ps
from app.extensions import db
from app.models import (AppSetting, Customer, CustomerWgProfile, Property,
                        PropertyOwnership, PropertyShareEntry, User, WgShareEntry)
from tests.conftest import _ensure_role

MODAL = {"X-From-Modal": "1", "HX-Request": "true"}
D1 = date(2024, 2, 1)


@pytest.fixture
def logged_in(client, app):
    u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


def _mode(binding="property", org="cooperative"):
    AppSetting.set("org.type", org)
    AppSetting.set("org.share_binding", binding)
    db.session.commit()


def _prop(nr="H-1", shares=2, area=180, owner=None):
    p = Property(object_number=nr, object_type="Haus", active=True)
    db.session.add(p)
    db.session.flush()
    ps.record_opening(p, shares, D1, area=area)
    if owner:
        db.session.add(PropertyOwnership(property_id=p.id, customer_id=owner.id,
                                         valid_from=date(2020, 1, 1)))
    db.session.commit()
    return p


def _customer(name):
    c = Customer(name=name, is_customer=True, active=True)
    db.session.add(c)
    db.session.flush()
    c.wg_profile = CustomerWgProfile(status="member")
    db.session.commit()
    return c


class TestDetailCard:
    def test_card_only_in_property_mode(self, logged_in):
        _mode("property")
        p = _prop()
        html = logged_in.get(f"/properties/{p.id}").get_data(as_text=True)
        assert 'id="anteilsbuch"' in html and 'id="propertyShareModal"' in html
        assert "Anfangsbestand" in html
        _mode("person")
        html = logged_in.get(f"/properties/{p.id}").get_data(as_text=True)
        assert 'id="anteilsbuch"' not in html
        _mode("property", org="utility")
        assert 'id="anteilsbuch"' not in logged_in.get(f"/properties/{p.id}").get_data(as_text=True)


class TestModal:
    def test_fragment_prefills_owner_and_rule(self, logged_in):
        _mode("property")
        AppSetting.set("org.share_area_per_share", "100")
        db.session.commit()
        owner = _customer("Eigentümer Eva")
        p = _prop(owner=owner)
        frag = logged_in.get(f"/properties/{p.id}/shares/new?action=extension", headers=MODAL)
        html = frag.get_data(as_text=True)
        assert frag.status_code == 200
        assert 'data-per-share="100"' in html
        assert f'value="{owner.id}" selected' in html

    def test_extension_with_area_and_payer(self, logged_in):
        _mode("property")
        owner = _customer("Eva")
        p = _prop(owner=owner)
        resp = logged_in.post(f"/properties/{p.id}/shares/new", headers=MODAL, data={
            "action": "extension", "entry_date": date.today().isoformat(),
            "area_m2": "260", "quantity": "1", "payer_customer_id": str(owner.id),
            "note": "Zubau"})
        assert resp.status_code == 204
        assert "propertyShareSaved" in json.loads(resp.headers["HX-Trigger"])
        p = db.session.get(Property, p.id)
        assert (p.wg_shares, p.wg_area_m2) == (3, 260)
        entry = PropertyShareEntry.query.filter_by(kind="change").one()
        assert entry.payer_customer_id == owner.id

    def test_error_stays_in_modal(self, logged_in):
        _mode("property")
        p = _prop(shares=1)
        resp = logged_in.post(f"/properties/{p.id}/shares/new", headers=MODAL, data={
            "action": "decommission", "entry_date": date.today().isoformat(),
            "quantity": "-5"})
        assert resp.status_code == 200
        assert "negativ" in resp.get_data(as_text=True)
        assert PropertyShareEntry.query.count() == 1

    def test_transfer_and_delete_pair(self, logged_in):
        _mode("property")
        a, b = _prop("A", shares=3), _prop("B", shares=0, area=90)
        resp = logged_in.post(f"/properties/{a.id}/shares/new", headers=MODAL, data={
            "action": "transfer", "entry_date": date.today().isoformat(), "quantity": "1",
            "to_property_id": str(b.id), "transfer_reason": "split"})
        assert resp.status_code == 204
        out = PropertyShareEntry.query.filter_by(kind="transfer_out").one()
        logged_in.post(f"/properties/shares/{out.id}/delete")
        a, b = db.session.get(Property, a.id), db.session.get(Property, b.id)
        assert (a.wg_shares, b.wg_shares) == (3, 0)

    def test_person_mode_redirects(self, logged_in):
        _mode("person")
        p = _prop()
        assert logged_in.get(f"/properties/{p.id}/shares/new", headers=MODAL).status_code == 302


class TestSettingsRule:
    def test_rule_saved_with_marker_only(self, logged_in):
        _mode("property")
        html = logged_in.get("/einstellungen/").get_data(as_text=True)
        assert 'name="share_area_per_share"' in html
        logged_in.post("/einstellungen/", data={"org_type": "cooperative",
                                                "share_rule_fields": "1",
                                                "share_area_per_share": "120,5",
                                                "share_area_rounding": "down"})
        assert AppSetting.get("org.share_area_per_share") == "120.5"
        assert AppSetting.get("org.share_area_rounding") == "down"
        logged_in.post("/einstellungen/", data={"org_type": "cooperative"})   # ohne Marker
        assert AppSetting.get("org.share_area_per_share") == "120.5"

    def test_rule_hidden_in_person_mode(self, logged_in):
        _mode("person")
        html = logged_in.get("/einstellungen/").get_data(as_text=True)
        assert 'name="share_area_per_share"' not in html


class TestCustomerAndMemberRefs:
    def test_customer_page_lists_paid_shares(self, logged_in):
        _mode("property")
        payer = _customer("Zahler Zoe")
        p = _prop("Z-1")
        ps.record_change(p, 1, date.today(), ps.REASON_EXTENSION, payer=payer)
        db.session.commit()
        html = logged_in.get(f"/customers/{payer.id}").get_data(as_text=True)
        assert "Bezahlte Anteile" in html and "Z-1" in html

    def test_member_entry_with_property_reference(self, logged_in):
        _mode("person")
        c = _customer("Mitglied Max")
        p = _prop("REF-1")
        resp = logged_in.post(f"/members/{c.id}/shares/new", headers=MODAL, data={
            "action": "acquisition", "quantity": "1",
            "entry_date": date.today().isoformat(), "property_id": str(p.id)})
        assert resp.status_code == 204
        assert WgShareEntry.query.one().property_id == p.id
        html = logged_in.get(f"/properties/{p.id}").get_data(as_text=True)
        assert "Anteile mit Bezug" in html and "Mitglied Max" in html
