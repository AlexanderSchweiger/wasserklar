"""Demo-Datensatz (``app/seed/demo.py``) für Österreich und Deutschland.

Seedet gegen die SQLite-In-Memory-Test-DB (``PDF_DIR`` zeigt in einen Temp-Ordner),
``now`` bleibt auf dem historischen Anker — der Datensatz ist damit deterministisch.
"""
import hashlib
import math
from decimal import Decimal

import pytest

from app.extensions import db


def _seed(country=None, identity=False):
    from cli import (run_data_migrations, seed_default_billing_period, seed_default_charge_types,
                     seed_default_dunning_policy, seed_default_roles, seed_default_tax_rates)
    from app.seed.demo import fill_demo_identity, seed_demo_data

    seed_default_tax_rates(db)
    seed_default_charge_types(db)
    seed_default_dunning_policy(db)
    seed_default_billing_period(db)
    seed_default_roles(db)
    if identity:
        fill_demo_identity(country)
    counts = seed_demo_data(db, verbose=False, country=country)
    db.session.commit()
    run_data_migrations(db)
    db.session.commit()
    return counts


def _meters(a, b):
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 111320)


class TestAustria:
    def test_counts_unchanged(self, app):
        counts = _seed()
        assert counts["country"] == "AT"
        for key, value in {"customers": 100, "properties": 120, "meters": 150, "invoices": 20,
                           "network_features": 390, "properties_geocoded": 103,
                           "hausanschluss_unassigned": 6, "incidents": 9, "meetings": 9}.items():
            assert counts[key] == value, key
        # Belegablage: 3 Belege an Buchungen, 1 erkannter im Eingang, 1 abgelegt — keine E-Rechnung
        assert (counts["documents"], counts["documents_linked"], counts["documents_inbox"],
                counts["documents_filed"], counts["incoming_einvoices"]) == (5, 3, 1, 1, 0)

    def test_austrian_rates_and_names(self, app):
        from app.models import Booking, InvoiceItem, NetworkFeature, RealAccount
        _seed()
        assert {i.tax_rate for i in InvoiceItem.query if i.tax_rate is not None} == {
            Decimal("10.00"), Decimal("20.00")}
        assert Decimal("19.00") not in {b.tax_rate for b in Booking.query}
        assert NetworkFeature.query.filter_by(name="Hochbehälter Sonnberg").count() == 1
        assert RealAccount.query.filter_by(is_default=True).one().name == "Girokonto Raika"


class TestGermany:
    def test_switches_country_and_defaults(self, app):
        from app import country, tax_service
        from app.models import AppSetting, ChargeType, FiscalYear, Role, TariffComponent, TaxRate
        counts = _seed("DE")
        assert counts["country"] == "DE"
        assert country.current_code() == "DE"
        active = {t.rate for t in TaxRate.query.filter_by(active=True)}
        assert active == {Decimal("0"), Decimal("7"), Decimal("19")}
        assert tax_service.water_tax_rate() == Decimal("7")
        assert AppSetting.get("einvoice.enabled") == "true"
        assert Role.query.filter_by(name="Kassierer").count() == 1
        assert Role.query.filter_by(name="Kassier").count() == 0
        assert all(fy.is_vat_liable for fy in FiscalYear.query)
        levy = TariffComponent.query.join(ChargeType).filter(ChargeType.is_levy.is_(True)).one()
        assert levy.amount == Decimal("0.1000")
        assert levy.valid_from.isoformat() == "2026-07-01"          # bayerischer Wassercent

    def test_network_and_addresses(self, app):
        from app.models import Customer, NetworkFeature, Property
        from app.seed import demo_geo_de as geo
        counts = _seed("DE")
        assert counts["properties"] == 140
        assert counts["meters"] == 170
        assert counts["hausanschluss_unassigned"] == 6
        props = Property.query.order_by(Property.id).all()
        for prop, (street, number) in zip(props, geo.HOUSE_ADDRESSES):
            assert (prop.strasse, prop.hausnummer, prop.plz, prop.ort) == (street, number, "82392", "Habach")
        for f in NetworkFeature.query.filter(NetworkFeature.feature_type == "hausanschluss",
                                             NetworkFeature.property_id.isnot(None)):
            prop = db.session.get(Property, f.property_id)
            assert _meters((f.lng, f.lat), (prop.lng, prop.lat)) < 5
        # Kunde wohnt in seinem ersten Objekt, Anrede aus Vor-/Nachname
        first = Customer.query.filter_by(customer_number=1).one()
        assert (first.strasse, first.hausnummer) == (props[0].strasse, props[0].hausnummer)
        assert first.letter_name == f"{first.first_name} {first.last_name}"
        assert first.salutation in ("Herr", "Frau")

    def test_einvoice_customers_and_invoices(self, app):
        from app.einvoice import leitweg, mapper, obligation, rules, service
        from app.models import Customer, Invoice
        counts = _seed("DE", identity=True)
        assert counts["business_customers"] == 6 and counts["xrechnung_customers"] == 2
        for c in Customer.query.filter(Customer.is_business.is_(True)):
            assert c.vat_id.startswith("DE") and len(c.vat_id) == 11
            assert c.wants_email                                     # B2B: E-Rechnung per E-Mail
        public = Customer.query.filter(Customer.einvoice_format == "xrechnung").all()
        assert {c.name for c in public} == {"Gemeinde Habach", "Schulverband Habach-Antdorf"}
        assert all(leitweg.is_valid(c.buyer_reference) for c in public)
        invoices = Invoice.query.filter(Invoice.invoice_number.like("RE-%-01%")).all()
        assert len(invoices) == 4
        for inv in invoices:
            assert rules.check(mapper.build_einvoice(inv)) == [], inv.invoice_number
        draft = next(i for i in invoices if i.status == Invoice.STATUS_DRAFT)
        assert service.customer_profile(draft) == "xrechnung-3.0"
        assert obligation.electronic_reason(draft)                    # nur elektronisch zustellbar
        assert obligation.dashboard_summary() is None or obligation.dashboard_summary()["business"] == 6

    def test_documents_and_incoming_einvoices(self, app):
        from app.documents import service as doc_svc, storage
        from app.models import Booking, BookingGroup, Document
        counts = _seed("DE")
        assert counts["incoming_einvoices"] == 3
        docs = Document.query.order_by(Document.id).all()
        assert len(docs) == counts["documents"] == 8
        for doc in docs:
            data = storage.read(doc.storage_key)
            assert data is not None and hashlib.sha256(data).hexdigest() == doc.sha256
        booked = [d for d in docs if doc_svc.is_booked(d)]
        assert len(booked) == counts["documents_linked"] == 5
        # ZUGFeRD-PDF des Gasthofs: Sammelbuchung mit 7 % und 19 %
        zugferd = next(d for d in docs if d.einvoice is not None and d.einvoice.source_kind == "pdf")
        group = zugferd.links[0].booking_group
        assert isinstance(group, BookingGroup)
        assert {b.tax_rate for b in Booking.query.filter_by(group_id=group.id)} == {
            Decimal("7.00"), Decimal("19.00")}
        # XRechnung des Labors liegt noch im Eingang, der Elektro-Beleg ist „erkannt“
        inbox = [d for d in docs if d.status == Document.STATUS_NEW and not doc_svc.is_booked(d)]
        assert len(inbox) == 2
        recognised = next(d for d in inbox if d.einvoice is None)
        assert recognised.meta_auto and recognised.supplier is not None and recognised.amount

    def test_switch_back_to_austria(self, app):
        from app import country
        from app.models import ChargeType, Role, TaxRate
        from app.settings.reset import reset_tenant_data
        _seed("DE")
        reset_tenant_data()
        counts = _seed("AT")
        assert counts["country"] == "AT" and country.current_code() == "AT"
        assert {t.rate for t in TaxRate.query.filter_by(active=True)} == {
            Decimal("0"), Decimal("10"), Decimal("13"), Decimal("20")}
        assert not ChargeType.query.filter_by(key="water_levy").one().active
        # Die Kassier-Rolle nutzt der Demo-Benutzer „kassier“ — benutzt wird sie nie
        # umbenannt, und es entsteht keine zweite.
        assert Role.query.filter(Role.name.in_(("Kassier", "Kassierer"))).count() == 1


@pytest.mark.parametrize("country", ["AT", "DE"])
def test_demo_addresses_are_never_delivered(app, country):
    """Jede E-Mail-Adresse des Demo-Datensatzes (Kunden, Unternehmer, Behörden, Lieferanten,
    eigene Stammdaten) muss eine Platzhalter-Adresse sein — ``send_mail`` stellt sie nie zu.
    ``example.at``/``example.de`` sind echte Domains mit Mailserver; ohne den Filter landeten
    Demo-Rechnungen dort."""
    from app.models import AppSetting, Customer
    from app.settings_service import is_placeholder_address
    _seed(country, identity=True)
    addresses = [c.email for c in Customer.query if c.email] + [AppSetting.get("wg.email")]
    assert len(addresses) > 50
    assert [a for a in addresses if not is_placeholder_address(a)] == []


def test_unknown_country_is_rejected(app):
    from app.seed.demo import switch_country
    with pytest.raises(ValueError):
        switch_country(db, "CH")
