"""Integration-Tests fuer die Rundschreiben-Services: E-Mail-Eignung
(Notfall-Bypass + Sperrliste) und die Karten-Empfaengerauflösung."""
from datetime import date

import pytest

from app.extensions import db
from app.models import (
    Circular, CircularRecipient, Customer, Property, PropertyOwnership,
    EmailSuppression,
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
