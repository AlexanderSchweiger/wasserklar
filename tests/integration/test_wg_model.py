"""Integration-Tests fuer die WG-Domaene: 1:1-Profile, Funktionen, Regeln,
Status-Vorschlag und Mandant-Typ-Helfer."""
from datetime import date

from app.extensions import db
from app.models import (
    Customer, Property, PropertyOwnership,
    CustomerWgProfile, PropertyWgProfile, WgFunction, AppSetting,
)
from app import wg
from app.settings_service import org_type, is_wassergenossenschaft


class TestProfiles:
    def test_customer_profile_defaults_and_cascade(self, app):
        c = Customer(name="Mit Profil")
        db.session.add(c)
        db.session.commit()
        # Ohne Profil: Read-Through-Default ist 'member' (jeder Kontakt gilt als
        # Mitglied, bis er ausdruecklich anders gesetzt wird).
        assert c.wg_status == "member"
        assert c.wg_member_until is None
        assert c.function_keys() == set()

        prof = c.ensure_wg_profile()
        prof.status = "member"
        prof.member_until = date(2030, 1, 1)
        db.session.add(WgFunction(customer_id=c.id, function="treasurer"))
        db.session.commit()
        assert c.wg_status == "member"
        assert c.function_keys() == {"treasurer"}

        cid = c.id
        db.session.delete(c)
        db.session.commit()
        # delete-orphan-Cascade: Profil + Funktionen verschwinden mit dem Kontakt.
        assert CustomerWgProfile.query.filter_by(customer_id=cid).count() == 0
        assert WgFunction.query.filter_by(customer_id=cid).count() == 0

    def test_property_profile_defaults_and_cascade(self, app):
        p = Property(object_type="Haus")
        db.session.add(p)
        db.session.commit()
        assert p.wg_shares == 0
        assert p.wg_area_m2 is None

        prof = p.ensure_wg_profile()
        prof.shares = 3
        prof.area_m2 = 800
        db.session.commit()
        assert p.wg_shares == 3
        assert p.wg_area_m2 == 800

        pid = p.id
        db.session.delete(p)
        db.session.commit()
        assert PropertyWgProfile.query.filter_by(property_id=pid).count() == 0


class TestSuggestedStatus:
    def test_has_paid_shares_and_suggestion(self, app):
        owner = Customer(name="Eigentuemer")
        prop = Property(object_type="Haus")
        db.session.add_all([owner, prop])
        db.session.commit()
        assert owner.has_paid_shares() is False
        assert wg.suggested_status(owner) == "prospect"

        prop.ensure_wg_profile().shares = 2
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=owner.id, valid_from=date(2020, 1, 1)))
        db.session.commit()
        assert owner.has_paid_shares() is True
        assert wg.suggested_status(owner) == "member"

    def test_zero_shares_is_not_member(self, app):
        owner = Customer(name="Null-Anteile")
        prop = Property(object_type="Haus")
        db.session.add_all([owner, prop])
        db.session.commit()
        prop.ensure_wg_profile().shares = 0
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=owner.id, valid_from=date(2020, 1, 1)))
        db.session.commit()
        assert owner.has_paid_shares() is False

    def test_ended_ownership_does_not_count(self, app):
        owner = Customer(name="Verkauft")
        prop = Property(object_type="Haus")
        db.session.add_all([owner, prop])
        db.session.commit()
        prop.ensure_wg_profile().shares = 5
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=owner.id,
            valid_from=date(2010, 1, 1), valid_to=date(2015, 1, 1)))
        db.session.commit()
        # Nur AKTIVE Ownership (valid_to is None) zaehlt.
        assert owner.has_paid_shares() is False


class TestFunctionWarnings:
    def test_board_function_requires_member(self):
        warnings = wg.function_warnings("prospect", {"chairman"})
        assert any("Mitglied" in m for m in warnings)
        assert wg.function_warnings("member", {"chairman"}) == []

    def test_auditor_not_with_board(self):
        warnings = wg.function_warnings("member", {"auditor", "treasurer"})
        assert any("Rechnungsprüfer" in m for m in warnings)
        # Reiner Rechnungspruefer ist erlaubt.
        assert wg.function_warnings("member", {"auditor"}) == []

    def test_clean_combo_no_warning(self):
        assert wg.function_warnings("member", {"treasurer", "secretary"}) == []


class TestOrgType:
    def test_default_is_cooperative(self, app):
        assert org_type() == "cooperative"
        assert is_wassergenossenschaft() is True

    def test_utility_setting(self, app):
        AppSetting.set("org.type", "utility")
        db.session.commit()
        assert org_type() == "utility"
        assert is_wassergenossenschaft() is False

    def test_unknown_value_falls_back(self, app):
        AppSetting.set("org.type", "garbage")
        db.session.commit()
        assert org_type() == "cooperative"
