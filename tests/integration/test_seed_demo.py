"""Demo-Datensatz (``app/seed/demo.py``) für Österreich und Deutschland.

Seedet gegen die SQLite-In-Memory-Test-DB (``PDF_DIR`` zeigt in einen Temp-Ordner).
Der Stichtag ist fest (``ANCHOR``) — im Betrieb ist er das heutige Datum, der
Datensatz startet dann immer im laufenden Jahr.
"""
import hashlib
import math
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db


ANCHOR = date(2026, 9, 15)


def _seed(country=None, identity=False, today=ANCHOR, installments=False):
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
    counts = seed_demo_data(db, verbose=False, country=country, today=today,
                            installments=installments)
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


class TestDates:
    """Der Datensatz richtet sich nach dem Stichtag: Vorjahr + laufendes Jahr."""

    def _future(self, today):
        from app.models import (Booking, Invoice, MaintenanceLog, Meeting, MeterReading,
                                MeterReplacement)
        return {
            "readings": MeterReading.query.filter(MeterReading.reading_date > today).count(),
            "swaps": MeterReplacement.query.filter(
                MeterReplacement.replacement_date > today).count(),
            "bookings": Booking.query.filter(Booking.date > today).count(),
            "invoices": Invoice.query.filter(Invoice.date > today).count(),
            "maintenance": MaintenanceLog.query.filter(MaintenanceLog.date > today).count(),
            "held_meetings": Meeting.query.filter(Meeting.meeting_date > today,
                                                  Meeting.status == Meeting.STATUS_HELD).count(),
        }

    def test_periods_follow_the_anchor(self, app):
        from app.models import BillingPeriod, FiscalYear, WaterTariff
        _seed(today=date(2031, 10, 2))
        assert BillingPeriod.current().name == "2031"
        assert [p.name for p in BillingPeriod.query.order_by(BillingPeriod.start_date)] == [
            "2030", "2031"]
        assert [f.year for f in FiscalYear.query.filter_by(closed=False)] == [2031]
        assert {t.name for t in WaterTariff.query} == {"Tarif 2030", "Tarif 2031"}
        assert all(v == 0 for v in self._future(date(2031, 10, 2)).values())

    def test_early_in_the_year_nothing_lies_in_the_future(self, app):
        """Vor dem 15.09. wird das laufende Jahr gestaucht (Zaehlertausch, Buchungen,
        Sitzungen …) — kein Termin nach dem Stichtag, Verbraeuche im Verhaeltnis."""
        from app.models import MeterReading, MeterReplacement
        today = date(2027, 2, 20)
        counts = _seed(today=today)
        assert counts["meter_swaps"] == 20
        assert all(v == 0 for v in self._future(today).values())
        swap = MeterReplacement.query.first()
        assert date(2027, 1, 1) <= swap.replacement_date < today
        current = MeterReading.query.filter(MeterReading.reading_date == today).all()
        assert current and max(r.consumption for r in current) < 60

    def test_bavarian_water_levy_by_year(self):
        """Wassercent: vor 2026 keiner, 2026 ab 01.07. (zeitanteilig), danach ganzjährig."""
        from app.seed.demo import levy_for_year
        from app.seed.demo_locale import locale_for
        de = locale_for("DE")
        assert levy_for_year(de, 2025) == (None, None)
        assert levy_for_year(de, 2026) == (Decimal("0.10"), date(2026, 7, 1))
        assert levy_for_year(de, 2027) == (Decimal("0.10"), None)
        assert levy_for_year(locale_for("AT"), 2027) == (None, None)


class TestInstallments:
    """Testdaten mit Teilrechnungen: Plaene, versendete/bezahlte Teilrechnungen,
    Folgeperiode — und der Jahres-Rechnungslauf verrechnet sie."""

    @pytest.mark.parametrize("country,title", [("AT", "Teilrechnung"),
                                                ("DE", "Abschlagsrechnung")])
    def test_installments_through_the_year(self, app, country, title):
        from app.installments import terms
        from app.models import BillingPeriod, BillingRun, InstallmentPlan, Invoice, OpenItem
        counts = _seed(country, today=date(2026, 11, 14), installments=True)
        assert terms.is_enabled()
        props = 120 if country == "AT" else 140
        # je Objekt mit aktivem Zaehler + Eigentuemer ein Plan, ausser den Ausgenommenen
        assert props - 10 < counts["installment_plans"] <= props - counts["installment_opt_out"]
        assert counts["installment_opt_out"] == 3
        # 15.02., 15.05., 15.08. (meist bezahlt), 15.11. ausgestellt und noch offen
        assert counts["installment_invoices"] == 4 * counts["installment_plans"]
        assert BillingRun.query.filter_by(run_kind=BillingRun.KIND_INSTALLMENT).count() == 4
        insts = Invoice.query.filter_by(invoice_kind=Invoice.KIND_INSTALLMENT).all()
        assert {i.document_title for i in insts} == {title}
        latest = max(i.due_date for i in insts)
        assert all(i.status == Invoice.STATUS_SENT for i in insts if i.due_date == latest)
        paid = [i for i in insts if i.status == Invoice.STATUS_PAID]
        assert len(paid) == counts["installment_invoices_paid"]
        assert len(paid) > 2 * counts["installment_plans"]
        assert OpenItem.query.filter(OpenItem.invoice_id.in_([i.id for i in insts])).count() \
            == len(insts)
        # Folgeperiode mit Plaenen (Vorschau auf der Jahresabrechnung)
        nxt = BillingPeriod.query.filter_by(name="2027").one()
        assert not nxt.active
        assert InstallmentPlan.query.filter_by(billing_period_id=nxt.id).count() \
            == counts["installment_plans_next"] > 0
        if country == "AT":
            first = min(insts, key=lambda i: (i.due_date, i.id))
            assert (first.service_date_from, first.service_date_to) == (
                date(2026, 1, 1), date(2026, 3, 31))
        else:
            assert all(i.service_date_from is None for i in insts)
            assert any(i.tax_breakdown for i in insts)             # 7 % USt

    def test_no_next_period_early_in_the_year(self, app):
        from app.models import BillingPeriod, Invoice
        counts = _seed(today=date(2027, 3, 1), installments=True)
        assert counts["installment_invoices"] == counts["installment_plans"]   # nur 15.02.
        assert "installment_plans_next" not in counts
        assert BillingPeriod.query.count() == 2
        assert not Invoice.query.filter(Invoice.date > date(2027, 3, 1)).count()

    def test_annual_billing_settles_them(self, app, client):
        """Der Rechnungslauf rechnet mit dem echten Datum — der Stichtag ist heute."""
        from app.installments import settlement
        from app.models import BillingPeriod, Invoice, WaterTariff
        _seed(today=date.today(), installments=True)
        client.get("/auth/logout")
        client.post("/auth/login", data={"username": "admin", "password": "demo1234"})
        period = BillingPeriod.current()
        tariff = WaterTariff.query.filter_by(name=f"Tarif {period.name}").one()
        resp = client.post("/invoices/generate", data={
            "billing_period_id": period.id, "tariff_id": tariff.id, "due_days": 30})
        assert resp.status_code == 302
        summaries = [settlement.summary(i) for i in Invoice.query.filter_by(
            invoice_kind=Invoice.KIND_STANDARD, billing_period_id=period.id)]
        summaries = [s for s in summaries if s]
        assert len(summaries) > 50
        assert all(s["deductions"] for s in summaries)
