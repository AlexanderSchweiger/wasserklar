"""Integration-Tests fuer die Rundschreiben-Services: E-Mail-Eignung
(Notfall-Bypass + Sperrliste) und die Karten-Empfaengerauflösung."""
from datetime import date

import pytest

from app.extensions import db
from app.models import (
    Circular, CircularRecipient, Customer, Incident, NetworkFeature, NetworkPlan,
    Property, PropertyOwnership, EmailSuppression,
)
from app.email_suppression import suppress
from app.circulars import services


def _customer(name, email=None, consent=False):
    c = Customer(name=name, email=email, rechnung_per_email=consent)
    db.session.add(c)
    db.session.flush()
    return c


def _circular(kind):
    circ = Circular(kind=kind, subject="Test", body="{anrede}", status=Circular.STATUS_DRAFT)
    db.session.add(circ)
    db.session.flush()
    return circ


class TestEmailEligibility:
    def test_general_requires_consent(self, app):
        circ = _circular(Circular.KIND_GENERAL)
        with_consent = _customer("A", "a@test.at", consent=True)
        without = _customer("B", "b@test.at", consent=False)
        assert services.email_eligibility(circ, with_consent).can_email is True
        assert services.email_eligibility(circ, without).can_email is False

    def test_emergency_bypasses_consent(self, app):
        circ = _circular(Circular.KIND_BOIL_WATER)
        c = _customer("B", "b@test.at", consent=False)
        elig = services.email_eligibility(circ, c)
        assert elig.can_email is True
        assert elig.bypass is True

    def test_emergency_needs_email_address(self, app):
        circ = _circular(Circular.KIND_BOIL_WATER)
        c = _customer("NoMail", email=None, consent=False)
        assert services.email_eligibility(circ, c).can_email is False

    def test_suppression_blocks_even_emergency(self, app):
        circ = _circular(Circular.KIND_BOIL_WATER)
        c = _customer("S", "sperr@test.at", consent=True)
        suppress("sperr@test.at", EmailSuppression.REASON_MANUAL)
        db.session.commit()
        elig = services.email_eligibility(circ, c)
        assert elig.can_email is False
        assert elig.suppressed is True

    def test_default_method(self, app):
        circ = _circular(Circular.KIND_GENERAL)
        mail_c = _customer("M", "m@test.at", consent=True)
        post_c = Customer(name="P", strasse="Weg", hausnummer="1", plz="1010", ort="Wien")
        db.session.add(post_c); db.session.flush()
        none_c = Customer(name="N")
        db.session.add(none_c); db.session.flush()
        assert services.default_method(circ, mail_c) == CircularRecipient.METHOD_EMAIL
        assert services.default_method(circ, post_c) == CircularRecipient.METHOD_POST
        assert services.default_method(circ, none_c) == CircularRecipient.METHOD_NONE


class TestMapResolution:
    def test_resolves_and_dedupes_owners(self, app):
        p1 = Property(object_number="P1", object_type="Haus")
        p2 = Property(object_number="P2", object_type="Haus")
        db.session.add_all([p1, p2]); db.session.flush()
        # p1 hat zwei aktive Eigentuemer (Ehepaar), einer besitzt auch p2.
        c1 = _customer("Eins", "1@t.at")
        c2 = _customer("Zwei", "2@t.at")
        for prop, cust in [(p1, c1), (p1, c2), (p2, c1)]:
            db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                             valid_from=date(2020, 1, 1), valid_to=None))
        db.session.flush()
        result = services.resolve_customers_from_properties([p1.id, p2.id])
        assert {c.id for c in result} == {c1.id, c2.id}  # dedupliziert

    def test_ignores_inactive_owner(self, app):
        p = Property(object_number="P9", object_type="Haus")
        db.session.add(p); db.session.flush()
        c = _customer("Inaktiv", "x@t.at")
        c.active = False
        db.session.add(PropertyOwnership(property_id=p.id, customer_id=c.id,
                                         valid_from=date(2020, 1, 1), valid_to=None))
        db.session.flush()
        assert services.resolve_customers_from_properties([p.id]) == []

    def test_add_recipients_idempotent(self, app):
        circ = _circular(Circular.KIND_OUTAGE)
        c = _customer("R", "r@t.at", consent=True)
        assert services.add_recipients(circ, [c]) == 1
        assert services.add_recipients(circ, [c]) == 0  # schon dabei
        assert len(circ.recipients) == 1


class TestMapTargets:
    """Karten-Ziele: Symbol-/Popup-Daten je Liegenschaft."""

    def _plan(self):
        plan = NetworkPlan(name="Plan", status=NetworkPlan.STATUS_ACTIVE)
        db.session.add(plan)
        db.session.flush()
        return plan

    def _owned_property(self, number, lat=None, lng=None, owner_name="Eigner"):
        prop = Property(object_number=number, object_type="Haus",
                        strasse="Dorfstrasse", hausnummer="1", plz="9800", ort="Spittal",
                        lat=lat, lng=lng)
        db.session.add(prop)
        db.session.flush()
        cust = _customer(owner_name, f"{number}@t.at")
        db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                         valid_from=date(2020, 1, 1), valid_to=None))
        db.session.flush()
        return prop, cust

    def test_hausanschluss_target_carries_feature_props(self, app):
        plan = self._plan()
        prop, cust = self._owned_property("H1")
        db.session.add(NetworkFeature(
            plan_id=plan.id, feature_type="hausanschluss",
            geometry_kind=NetworkFeature.GEOMETRY_POINT,
            geometry='{"type": "Point", "coordinates": [13.5, 46.8]}',
            lat=46.8, lng=13.5, property_id=prop.id, name="HA 1"))
        db.session.flush()

        targets = services.map_targets(plan)
        assert len(targets) == 1
        t = targets[0]
        assert t["source"] == "hausanschluss"
        # Popup-Properties wie im Leitungsplan (Symbol + Besitzer im Popup).
        assert t["props"]["feature_type"] == "hausanschluss"
        assert cust.letter_name in t["props"]["owner_names"]
        assert t["props"]["property_id"] == prop.id

    def test_geocoded_property_without_hausanschluss_gets_neutral_props(self, app):
        plan = self._plan()
        prop, cust = self._owned_property("G1", lat=46.9, lng=13.6)

        targets = services.map_targets(plan)
        assert len(targets) == 1
        t = targets[0]
        assert t["source"] == "property"
        # Kein Netz-Symbol -> feature_type None (neutraler Pin im Karten-JS).
        assert t["props"]["feature_type"] is None
        assert cust.letter_name in t["props"]["owner_names"]


class TestMapIncidents:
    def test_open_incidents_only_plus_own(self, app):
        circ = _circular(Circular.KIND_OUTAGE)
        offen = Incident(title="Rohrbruch", detected_at=date(2026, 1, 2),
                         status=Incident.STATUS_OPEN, lat=46.8, lng=13.5)
        behoben = Incident(title="Alt", detected_at=date(2025, 1, 2),
                           status=Incident.STATUS_RESOLVED, lat=46.7, lng=13.4)
        eigene = Incident(title="Quelle", detected_at=date(2025, 6, 2),
                          status=Incident.STATUS_RESOLVED, lat=46.6, lng=13.3)
        db.session.add_all([offen, behoben, eigene])
        db.session.flush()
        circ.incident_id = eigene.id
        db.session.flush()

        ids = {f["id"] for f in services.map_incidents_geojson(circ)["features"]}
        assert ids == {offen.id, eigene.id}  # behobene bleiben draussen

    def test_without_position_skipped(self, app):
        inc = Incident(title="Ohne Lage", detected_at=date(2026, 2, 2),
                       status=Incident.STATUS_OPEN)
        db.session.add(inc)
        db.session.flush()
        assert services.map_incidents_geojson(None)["features"] == []
