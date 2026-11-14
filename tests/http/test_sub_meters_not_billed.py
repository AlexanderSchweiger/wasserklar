"""Subzaehler werden aufgezeichnet, aber nie verrechnet (``WaterMeter.is_billable``).

Ein Subzaehler sitzt hinter dem Hauptzaehler (z. B. Gartenwasser) — dessen
Stand misst den Verbrauch schon mit. Frueher addierten Rechnungslauf,
Verbrauchsnachweis, Teilbetragsplanung und Jahressummen alle Zaehler eines
Objekts und zaehlten den Subzaehler damit doppelt. Hier wird jede dieser
Stellen gegen dieselbe Ausgangslage geprueft: Hauptzaehler 100 → 150 (50 m³),
Subzaehler 10 → 30 (20 m³) — abgerechnet werden 50 m³.
"""
from datetime import date
from decimal import Decimal

import pytest

from app import consumption
from app.extensions import db
from app.installments import plans
from app.invoices import meter_statement as ms
from app.meters.services import save_reading
from app.models import (
    Account, AppSetting, BillingPeriod, ConsumptionYear, Customer, FiscalYear, Invoice,
    InvoiceMeterReading, OwnerChangeMeterValue, Property, PropertyOwnership,
    ReadingCorrection, User, WaterMeter,
)
from app.owner_change import services as oc_svc
from tests.conftest import _ensure_role

TODAY = date.today()
YEAR = TODAY.year


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def setup(app, admin):
    """Vorperiode + laufende Periode, Tarif 2 €/m³ + 30 € Grundgebuehr, ein
    Objekt mit Hauptzaehler (Vorstand 100) und Garten-Subzaehler (Vorstand 10)."""
    db.session.add(FiscalYear(year=YEAR, start_date=date(YEAR, 1, 1),
                              end_date=date(YEAR, 12, 31)))
    prev = BillingPeriod(name=str(YEAR - 1), start_date=date(YEAR - 1, 1, 1),
                         end_date=date(YEAR - 1, 12, 31), active=False)
    period = BillingPeriod(name=str(YEAR), start_date=date(YEAR, 1, 1),
                           end_date=date(YEAR, 12, 31), active=True)
    db.session.add_all([prev, period])
    from app.invoices.charges import build_tariff
    tariff = build_tariff(name="T", valid_from=YEAR - 1, base_fee=Decimal("30"),
                          water_price=Decimal("2"))
    db.session.add(tariff)
    db.session.add(Account(name="Wasser"))
    cust = Customer(name="Huber Maria", customer_number=1)
    new_owner = Customer(name="Neu", customer_number=2)
    db.session.add_all([cust, new_owner])
    db.session.flush()
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                     valid_from=date(YEAR - 5, 1, 1), valid_to=None))
    main = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main",
                      initial_value=Decimal("0"), installed_from=date(YEAR - 5, 1, 1))
    db.session.add(main)
    db.session.flush()
    sub = WaterMeter(property_id=prop.id, meter_number="Z-G", meter_type="sub",
                     parent_meter_id=main.id, location="Garten",
                     initial_value=Decimal("0"), installed_from=date(YEAR - 5, 1, 1))
    db.session.add(sub)
    db.session.flush()
    save_reading(main, prev, Decimal("100"), reading_date=date(YEAR - 1, 12, 31))
    save_reading(sub, prev, Decimal("10"), reading_date=date(YEAR - 1, 12, 31))
    db.session.commit()
    return {"prev": prev, "period": period, "tariff": tariff, "cust": cust,
            "new_owner": new_owner, "prop": prop, "main": main, "sub": sub,
            "admin": admin}


def _read_year_end(s, *, main="150", sub="30", sub_estimated=False):
    if main is not None:
        save_reading(s["main"], s["period"], Decimal(main), reading_date=date(YEAR, 12, 31))
    if sub is not None:
        save_reading(s["sub"], s["period"], Decimal(sub), reading_date=date(YEAR, 12, 31),
                     is_estimated=sub_estimated)
    db.session.commit()


def _generate(client, s):
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    client.post("/invoices/generate", data={
        "billing_period_id": str(s["period"].id), "tariff_id": str(s["tariff"].id),
        "due_days": "30"})
    db.session.expire_all()
    return Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).all()


class TestRule:
    def test_billable(self, app, setup):
        assert setup["main"].is_billable() and not setup["sub"].is_billable()
        ids = {m.id for m in WaterMeter.query.filter(WaterMeter.billable_filter())}
        assert ids == {setup["main"].id}


class TestBillingRun:
    @pytest.mark.parametrize("print_swap", [False, True])
    def test_sub_meter_not_added(self, app, client, setup, print_swap):
        # Mit „Zaehlertausch je Zaehler ausweisen“ darf der Subzaehler auch
        # nicht als eigene Zaehlerzeile erscheinen (frueher galt jedes zweite
        # Ablesungs-Paar als Tausch).
        AppSetting.set("invoice.print_meter_swap", "true" if print_swap else "false")
        _read_year_end(setup)
        (inv,) = _generate(client, setup)

        assert inv.consumption == Decimal("50")
        water = [i for i in inv.items if i.is_water_consumption]
        assert len(water) == 1 and water[0].quantity == Decimal("50")
        assert not any("Z-G" in (i.description or "") for i in inv.items)

        # Verbrauchsnachweis: nur der Hauptzaehler, Summe = abgerechnete Menge.
        assert [r.meter_number for r in inv.meter_readings] == ["Z-1"]
        st = ms.for_document(inv)
        assert st.show and st.total == Decimal("50")

    def test_only_sub_meter_read_is_not_billed(self, app, client, setup):
        _read_year_end(setup, main=None)
        assert _generate(client, setup) == []

        from app.invoices.routes import _period_completeness
        comp = _period_completeness(setup["period"])
        assert [e["prop"].id for e in comp["no_reading"]] == [setup["prop"].id]
        assert comp["ready"] == []

    def test_property_with_only_sub_meter_is_not_expected(self, app, setup):
        setup["main"].active = False
        db.session.commit()
        from app.invoices.routes import _period_completeness
        assert _period_completeness(setup["period"])["total_count"] == 0
        assert plans.candidate_properties() == []

    def test_live_rows_of_legacy_draft(self, app, client, setup):
        _read_year_end(setup)
        (inv,) = _generate(client, setup)
        InvoiceMeterReading.query.delete()
        db.session.commit()
        db.session.expire_all()
        st = ms.for_document(db.session.get(Invoice, inv.id))
        assert not st.frozen and [r.meter_number for r in st.rows] == ["Z-1"]
        assert st.show


class TestOwnerChange:
    def _change(self, s):
        return oc_svc.execute_owner_change(
            prop=s["prop"], period=s["period"], stichtag=date(YEAR, 7, 1),
            new_customer_ids=[s["new_owner"].id],
            meter_inputs={s["main"].id: {"value": Decimal("130")},
                          s["sub"].id: {"value": Decimal("22")}},
            create_settlement=True, settlement_recipient_id=s["cust"].id,
            tariff=s["tariff"], due_days=30, fee_mode=oc_svc.FEE_MODE_NEW_OWNER_FULL,
            created_by_id=s["admin"].id)

    def test_sub_meter_recorded_not_billed(self, app, client, setup):
        oc, warnings = self._change(setup)
        assert not any("Z-G" in w for w in warnings)
        values = {mv.meter_id: mv for mv in OwnerChangeMeterValue.query.filter_by(
            owner_change_id=oc.id)}
        assert values[setup["sub"].id].value_at_change == Decimal("22")   # aufgezeichnet
        assert values[setup["sub"].id].consumption_billed is None          # nicht verrechnet
        assert values[setup["main"].id].consumption_billed == Decimal("30")

        settlement = db.session.get(Invoice, oc.settlement_invoice_id)
        assert settlement.consumption == Decimal("30")
        assert [r.meter_number for r in settlement.meter_readings] == ["Z-1"]
        assert ms.for_document(settlement).show

        # Jahresabrechnung des Nachbesitzers: 150 − 130 = 20 m³, ohne Subzaehler.
        _read_year_end(setup)
        (std,) = _generate(client, setup)
        assert std.consumption == Decimal("20")
        assert [r.meter_number for r in std.meter_readings] == ["Z-1"]
        assert ms.for_document(std).show


class TestEstimateCorrection:
    def test_real_value_for_estimated_sub_meter_creates_no_correction(
            self, app, client, setup):
        _read_year_end(setup, sub_estimated=True)
        (inv,) = _generate(client, setup)
        inv.status = Invoice.STATUS_SENT
        db.session.commit()

        save_reading(setup["sub"], setup["period"], Decimal("45"),
                     reading_date=date(YEAR, 12, 31), is_estimated=False)
        db.session.commit()
        assert ReadingCorrection.query.count() == 0

    def test_legacy_invoice_that_billed_the_sub_meter_is_still_corrected(
            self, app, client, setup):
        # Rechnung vor der Regel: der Subzaehler steht im eingefrorenen
        # Verbrauchsnachweis — dann wurde seine Schaetzung abgerechnet und wird
        # weiter korrigiert.
        _read_year_end(setup, sub_estimated=True)
        (inv,) = _generate(client, setup)
        inv.meter_readings.append(InvoiceMeterReading(
            sort_order=1, meter_id=setup["sub"].id, meter_number="Z-G", meter_type="sub",
            consumption=Decimal("20")))
        inv.status = Invoice.STATUS_SENT
        db.session.commit()

        save_reading(setup["sub"], setup["period"], Decimal("45"),
                     reading_date=date(YEAR, 12, 31), is_estimated=False)
        db.session.commit()
        corr = ReadingCorrection.query.one()
        assert corr.meter_id == setup["sub"].id and corr.delta_m3 == Decimal("15")


class TestConsumptionSums:
    def test_installment_basis(self, app, setup):
        _read_year_end(setup)
        assert plans.consumption_by_property(setup["period"].id) == {
            setup["prop"].id: Decimal("50")}

    def test_cost_planning_basis(self, app, setup):
        _read_year_end(setup)
        from app.cost_planning.services import _property_consumptions
        # Grundlage ist die juengste abgeschlossene Periode (Vorjahr):
        # Hauptzaehler 100, Subzaehler 10 zaehlt nicht dazu.
        values, period = _property_consumptions()
        assert period.id == setup["prev"].id
        assert values == {setup["prop"].id: Decimal("100")}

    def test_year_total(self, app, setup):
        _read_year_end(setup)
        consumption.refresh_all(force=True)
        db.session.commit()
        assert consumption.for_year(YEAR).total_m3 == Decimal("50")
        # Vorjahr: nur Hauptzaehler mit Stand 100 ab Anfangsstand 0.
        assert consumption.for_year(YEAR - 1).total_m3 == Decimal("100")

    def test_old_cache_is_recomputed_once(self, app, setup):
        _read_year_end(setup)
        # Cache aus der Zeit vor der Regel: mit Subzaehler gerechnet, nicht stale.
        db.session.add(ConsumptionYear(year=YEAR, total_m3=Decimal("70"),
                                       measured_m3=Decimal("70"), stale=False,
                                       source=ConsumptionYear.SOURCE_MEASURED))
        db.session.commit()
        consumption.ensure_fresh()
        db.session.commit()
        assert consumption.for_year(YEAR).total_m3 == Decimal("50")
        assert AppSetting.get(consumption.RULE_SETTING) == consumption.RULE_VERSION

    def test_dashboard_tile(self, app, client, setup):
        _read_year_end(setup)
        client.post("/auth/login", data={"username": "admin", "password": "secret"})
        from app.main import routes as main_routes
        captured = {}
        orig = main_routes.render_template

        def spy(template, **ctx):
            captured.update(ctx)
            return orig(template, **ctx)

        main_routes.render_template = spy
        try:
            client.get("/")
        finally:
            main_routes.render_template = orig
        assert Decimal(str(captured["period_consumption"])) == Decimal("50")
