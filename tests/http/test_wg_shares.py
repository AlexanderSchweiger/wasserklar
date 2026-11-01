"""HTTP-Tests: Anteile am Mitglied (Anteilsbuch-Modal, Kontaktseite, Listen),
Umschalt-Assistent, Mitgliederliste und Liegenschafts-Formular je Modus."""
import json
from datetime import date

import pytest

from app import wg_shares
from app.extensions import db
from app.models import (AppSetting, Customer, CustomerWgProfile, Property,
                        PropertyOwnership, PropertyWgProfile, User, WgShareEntry)
from tests.conftest import _ensure_role

D1 = date(2026, 1, 15)
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


def _mode(binding="person", org="cooperative"):
    AppSetting.set("org.type", org)
    AppSetting.set("org.share_binding", binding)
    db.session.commit()


def _member(name, status="member"):
    c = Customer(name=name, is_customer=True, active=True)
    db.session.add(c)
    db.session.flush()
    c.wg_profile = CustomerWgProfile(status=status)
    db.session.commit()
    return c


def _prop(nr, shares, owner=None):
    """Liegenschaft mit Anteilen — gebucht ueber ihr Anteilsbuch."""
    from app import property_shares
    p = Property(object_number=nr, object_type="Haus")
    db.session.add(p)
    db.session.flush()
    property_shares.record_opening(p, shares, date(2020, 1, 1), area=140)
    if owner:
        db.session.add(PropertyOwnership(property_id=p.id, customer_id=owner.id,
                                         valid_from=date(2020, 1, 1)))
    db.session.commit()
    return p


class TestPropertyFormByMode:
    def test_person_mode_edit_keeps_property_shares(self, logged_in):
        """Regression: das ausgeblendete Anteile-Feld darf die Anteile der
        Liegenschaft nicht auf 0 setzen."""
        _mode("person")
        p = _prop("7", 3)
        body = logged_in.get(f"/properties/{p.id}/edit", headers={"X-From-Modal": "1"})
        assert body.status_code == 200
        assert b'name="wg_shares"' not in body.data
        assert b'name="area_m2"' in body.data
        resp = logged_in.post(f"/properties/{p.id}/edit", headers={"X-From-Modal": "1"},
                              data={"object_number": "7", "object_type": "Haus",
                                    "area_m2": "150"})
        assert resp.status_code in (200, 204, 302)
        profile = db.session.get(PropertyWgProfile, p.id)
        assert profile.shares == 3 and profile.area_m2 == 150

    def test_person_mode_hides_shares_column_and_ignores_stale_filter(self, logged_in):
        _mode("person")
        _prop("1", 0)
        _prop("2", 4)
        resp = logged_in.get("/properties/?shares=with&sort=shares")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert 'name="shares"' not in html          # kein Filter-Select
        assert html.count('id="property-row-') == 2  # alter Filter greift nicht

    def test_property_mode_edit_is_read_only_new_has_fields(self, logged_in):
        _mode("property")
        p = _prop("7", 3)
        body = logged_in.get(f"/properties/{p.id}/edit", headers={"X-From-Modal": "1"})
        assert b'name="wg_shares"' not in body.data      # Änderung nur übers Anteilsbuch
        assert b"#anteilsbuch" in body.data
        new = logged_in.get("/properties/new", headers={"X-From-Modal": "1"})
        assert b'name="wg_shares"' in new.data and b'name="area_m2"' in new.data


class TestShareEntryModal:
    def test_get_fragment_and_acquire(self, logged_in):
        _mode("person")
        c = _member("Neu Käufer", status="prospect")
        url = f"/members/{c.id}/shares/new?action=acquisition"
        frag = logged_in.get(url, headers=MODAL)
        assert frag.status_code == 200
        assert b'name="quantity"' in frag.data
        resp = logged_in.post(url, headers=MODAL, data={
            "action": "acquisition", "quantity": "1", "entry_date": "2026-03-01",
            "note": "Einkauf", "adjust_membership": "1"})
        assert resp.status_code == 204
        assert "shareEntrySaved" in json.loads(resp.headers["HX-Trigger"])
        c = db.session.get(Customer, c.id)
        assert wg_shares.holding(c.id) == 1
        assert c.wg_status == "member" and c.member_since == date(2026, 3, 1)

    def test_form_action_wins_over_url(self, logged_in):
        _mode("person")
        a, b = _member("Anna"), _member("Bert")
        wg_shares.record_acquisition(a, 2, D1)
        db.session.commit()
        resp = logged_in.post(f"/members/{a.id}/shares/new?action=acquisition",
                              headers=MODAL, data={
                                  "action": "transfer", "quantity": "2",
                                  "entry_date": "2026-03-01", "to_customer_id": str(b.id)})
        assert resp.status_code == 204
        assert wg_shares.holdings_map([a.id, b.id]) == {a.id: 0, b.id: 2}

    def test_error_stays_in_modal(self, logged_in):
        _mode("person")
        c = _member("Anna")
        resp = logged_in.post(f"/members/{c.id}/shares/new", headers=MODAL, data={
            "action": "return", "quantity": "1", "entry_date": "2026-03-01"})
        assert resp.status_code == 200
        assert "negativ" in resp.get_data(as_text=True)
        assert WgShareEntry.query.count() == 0

    def test_property_mode_redirects(self, logged_in):
        _mode("property")
        c = _member("Anna")
        resp = logged_in.get(f"/members/{c.id}/shares/new", headers=MODAL)
        assert resp.status_code == 302

    def test_delete_transfer_pair(self, logged_in):
        _mode("person")
        a, b = _member("Anna"), _member("Bert")
        wg_shares.record_acquisition(a, 2, D1)
        out, _ = wg_shares.record_transfer(a, b, 1, date(2026, 2, 1))
        db.session.commit()
        resp = logged_in.post(f"/members/shares/{out.id}/delete")
        assert resp.status_code == 302
        assert WgShareEntry.query.count() == 1


class TestCustomerPages:
    def test_detail_card_only_in_person_mode(self, logged_in):
        c = _member("Anna")
        _mode("property")
        assert b'id="anteilsbuch"' not in logged_in.get(f"/customers/{c.id}").data
        _mode("person")
        wg_shares.record_acquisition(c, 2, D1, note="Gründung")
        db.session.commit()
        html = logged_in.get(f"/customers/{c.id}").get_data(as_text=True)
        assert 'id="anteilsbuch"' in html and "Gründung" in html
        assert 'id="shareEntryModal"' in html

    def test_list_column_and_sort(self, logged_in):
        _mode("person")
        a, b = _member("Anna"), _member("Bert")
        wg_shares.record_acquisition(b, 5, D1)
        db.session.commit()
        html = logged_in.get("/customers/?sort=shares&dir=desc").get_data(as_text=True)
        assert "Anteile" in html
        assert html.index("Bert") < html.index("Anna")
        row = logged_in.get(f"/customers/{b.id}/row").get_data(as_text=True)
        assert "<strong>5</strong>" in row

    def test_shares_sort_ignored_in_property_mode(self, logged_in):
        _mode("property")
        _member("Anna")
        assert logged_in.get("/customers/?sort=shares").status_code == 200

    def test_delete_blocked_by_register(self, logged_in):
        _mode("person")
        a, b = _member("Anna"), _member("Bert")
        wg_shares.record_acquisition(a, 1, D1)
        wg_shares.record_transfer(a, b, 1, date(2026, 2, 1))
        wg_shares.delete_entry(WgShareEntry.query.filter_by(kind="transfer_out").one())
        db.session.commit()
        logged_in.post(f"/customers/{a.id}/delete")
        assert db.session.get(Customer, a.id) is not None   # Erwerb steht noch drin

    def test_new_contact_with_initial_shares(self, logged_in):
        _mode("person")
        resp = logged_in.post("/customers/new", headers={"X-From-Modal": "1"}, data={
            "is_customer": "1", "last_name": "Neumann", "first_name": "Nina",
            "wg_status": "member", "member_since": "2026-02-01", "initial_shares": "1",
            "force": "1"})
        assert resp.status_code == 204, resp.get_data(as_text=True)[:500]
        c = Customer.query.filter_by(last_name="Neumann").one()
        entry = WgShareEntry.query.filter_by(customer_id=c.id).one()
        assert entry.quantity == 1 and entry.entry_date == date(2026, 2, 1)


class TestSettingsAssistant:
    def test_card_only_for_cooperatives(self, logged_in):
        _mode("property")
        assert b'id="anteile"' in logged_in.get("/einstellungen/").data
        _mode("property", org="utility")
        assert b'id="anteile"' not in logged_in.get("/einstellungen/").data

    def test_get_is_read_only_and_post_switches(self, logged_in):
        _mode("property")
        c = _member("Anna")
        p = _prop("1", 2, owner=c)
        assert logged_in.get("/einstellungen/share-binding").status_code == 200
        assert WgShareEntry.query.count() == 0
        resp = logged_in.post("/einstellungen/share-binding", data={
            "target": "person", "on": "2026-03-01", f"alloc_{p.id}_{c.id}": "2"})
        assert resp.status_code == 302
        assert AppSetting.get("org.share_binding") == "person"
        assert wg_shares.holding(c.id, date(2026, 3, 1)) == 2
        # zurueck: Anteilsbuch bleibt
        logged_in.post("/einstellungen/share-binding", data={"target": "property"})
        assert AppSetting.get("org.share_binding") == "property"
        assert WgShareEntry.query.count() == 1

    def test_main_settings_post_never_changes_binding(self, logged_in):
        _mode("person")
        logged_in.post("/einstellungen/", data={"org_type": "cooperative"})
        assert AppSetting.get("org.share_binding") == "person"


class TestMemberListPages:
    def test_page_partial_csv_and_pdf(self, logged_in):
        _mode("person")
        c = _member("Anna")
        wg_shares.record_acquisition(c, 3, D1)
        db.session.commit()
        page = logged_in.get("/members/")
        assert page.status_code == 200 and "Anna" in page.get_data(as_text=True)
        part = logged_in.get("/members/?q=Ann", headers={"HX-Request": "true"})
        assert "<html" not in part.get_data(as_text=True)
        csv = logged_in.get("/members/export.csv").get_data(as_text=True)
        assert csv.startswith("﻿Mitgliedsnummer;")
        assert "Summe;1 Mitglieder" in csv
        pdf = logged_in.get("/members/list.pdf")
        assert pdf.status_code in (200, 302)          # ohne WeasyPrint → Druckansicht
        assert logged_in.get("/members/print").status_code == 200

    def test_property_mode_list(self, logged_in):
        _mode("property")
        c = _member("Anna")
        _prop("H-1", 2, owner=c)
        html = logged_in.get("/members/").get_data(as_text=True)
        assert "H-1" in html and "Liegenschaften" in html

    def test_utility_mode_redirects(self, logged_in):
        _mode("property", org="utility")
        assert logged_in.get("/members/").status_code == 302

    def test_needs_stammdaten_permission(self, client, app):
        role = _ensure_role("Leser")
        u = User(username="leser", email="l@l.test", role_id=role.id)
        u.set_password("secret")
        db.session.add(u)
        db.session.commit()
        _mode("person")
        client.get("/auth/logout")
        client.post("/auth/login", data={"username": "leser", "password": "secret"})
        assert client.get("/members/").status_code == 302
