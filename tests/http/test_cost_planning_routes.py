"""HTTP-Tests fuer das cost_planning-Blueprint (Permissions, Ziel-CRUD,
Tarifpakete, Verbrauchserfassung, Beschlussvorlage)."""
from datetime import date
from decimal import Decimal

import pytest

from app import consumption
from app.extensions import db
from app.meters.services import save_reading
from app.models import (
    BillingPeriod, ConsumptionYear, FundingGoal, Property, PropertyOwnership,
    Customer, User, WaterMeter, WaterTariff,
)
from tests.conftest import _ensure_role


def _login(client, username, password):
    return client.post("/auth/login",
                       data={"username": username, "password": password})


@pytest.fixture
def admin_user(app):
    role = _ensure_role("Admin")
    u = User(username="cpadmin", email="cpadmin@test.com", role_id=role.id)
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def auswertungen_user(app):
    """Nur ``auswertungen`` — darf planen, aber keinen Tarif anlegen."""
    role = _ensure_role("Revisor", perms=("auswertungen",))
    u = User(username="revisor", email="revisor@test.com", role_id=role.id)
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def zaehler_user(app):
    """Ohne ``auswertungen`` — darf das Modul gar nicht sehen."""
    role = _ensure_role("Wasserwart", perms=("zaehler",))
    u = User(username="ww", email="ww@test.com", role_id=role.id)
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def tariff(app):
    t = WaterTariff(name="Tarif 2025", valid_from=2025,
                    base_fee=Decimal("50.00"),
                    price_per_m3=Decimal("1.5000"))
    db.session.add(t)
    db.session.commit()
    return t


@pytest.fixture
def goal(app):
    g = FundingGoal(name="Netzerneuerung", goal_type=FundingGoal.TYPE_INVESTMENT,
                    target_amount=Decimal("500000"),
                    existing_reserve=Decimal("0"),
                    start_year=2026, target_year=2030)
    db.session.add(g)
    db.session.commit()
    return g


def _seed_billable(count=2, year=2024, consumption_each="100"):
    """``count`` abrechenbare Objekte (aktiver Zaehler + aktueller Eigentuemer)
    mit je einer Ablesung — die Rechenbasis der Tarifpakete."""
    period = BillingPeriod(name=str(year), start_date=date(year, 1, 1),
                           end_date=date(year, 12, 31), active=True)
    db.session.add(period)
    db.session.commit()
    for i in range(count):
        cust = Customer(name=f"Kunde {i}")
        prop = Property(object_number=f"P{i}", object_type="Haus")
        db.session.add_all([cust, prop])
        db.session.flush()
        db.session.add(PropertyOwnership(customer_id=cust.id,
                                         property_id=prop.id,
                                         valid_from=date(2020, 1, 1)))
        m = WaterMeter(meter_number=f"M{i}", property_id=prop.id,
                       initial_value=Decimal("0"))
        db.session.add(m)
        db.session.flush()
        save_reading(m, period, Decimal(consumption_each))
    db.session.commit()
    return period


# ---------------------------------------------------------------------------
# Zugriff
# ---------------------------------------------------------------------------

class TestPermissions:

    def test_anonymous_is_redirected_to_login(self, client, app):
        client.get("/auth/logout")
        resp = client.get("/cost-planning/")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_without_auswertungen_redirects_to_dashboard(self, client,
                                                         zaehler_user):
        client.get("/auth/logout")
        _login(client, "ww", "test")
        resp = client.get("/cost-planning/")
        assert resp.status_code == 302
        assert "/auth/login" not in resp.headers["Location"]

    def test_with_auswertungen_can_open_the_overview(self, client,
                                                     auswertungen_user):
        client.get("/auth/logout")
        _login(client, "revisor", "test")
        assert client.get("/cost-planning/").status_code == 200

    def test_apply_tariff_needs_billing_permission(self, client,
                                                   auswertungen_user, goal,
                                                   tariff):
        """Planen darf die Auswertungs-Rolle, den echten Tarif setzen nicht."""
        client.get("/auth/logout")
        _login(client, "revisor", "test")
        resp = client.post(f"/cost-planning/goals/{goal.id}/apply-tariff",
                           data={"scenario": "base"})
        assert resp.status_code == 302
        assert "/cost-planning/" not in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Ziele
# ---------------------------------------------------------------------------

class TestGoalCrud:

    def test_create_goal_via_modal(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/goals/new", data={
            "name": "Quellsanierung",
            "goal_type": "investment",
            "target_amount": "500.000,00",
            "existing_reserve": "0",
            "start_year": "2026",
            "target_year": "2030",
        }, headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        assert "closeGoalModal" in resp.headers["HX-Trigger"]

        g = FundingGoal.query.filter_by(name="Quellsanierung").one()
        assert g.target_amount == Decimal("500000.00")
        assert g.years == 5

    def test_german_thousand_separator_is_parsed(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.post("/cost-planning/goals/new", data={
            "name": "X", "goal_type": "investment",
            "target_amount": "1.250.000,50",
            "start_year": "2026", "target_year": "2030",
        }, headers={"X-From-Modal": "1"})
        assert FundingGoal.query.one().target_amount == Decimal("1250000.50")

    def test_plain_decimal_point_is_parsed(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.post("/cost-planning/goals/new", data={
            "name": "X", "goal_type": "investment",
            "target_amount": "1250.5",
            "start_year": "2026", "target_year": "2030",
        }, headers={"X-From-Modal": "1"})
        assert FundingGoal.query.one().target_amount == Decimal("1250.50")

    def test_target_year_before_start_is_rejected(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/goals/new", data={
            "name": "X", "goal_type": "investment", "target_amount": "1000",
            "start_year": "2030", "target_year": "2026",
        }, headers={"X-From-Modal": "1"})
        assert resp.status_code == 200        # Modal-Body mit Fehler
        assert FundingGoal.query.count() == 0

    def test_zero_target_amount_is_rejected(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.post("/cost-planning/goals/new", data={
            "name": "X", "goal_type": "investment", "target_amount": "0",
            "start_year": "2026", "target_year": "2030",
        }, headers={"X-From-Modal": "1"})
        assert FundingGoal.query.count() == 0

    def test_loan_ignores_existing_reserve(self, client, admin_user):
        """Beim Kredit IST der Zielbetrag die Restschuld — eine mitgeschickte
        Ruecklage darf den Bedarf nicht kleinrechnen."""
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.post("/cost-planning/goals/new", data={
            "name": "Kredit", "goal_type": "loan",
            "target_amount": "100000", "existing_reserve": "50000",
            "interest_rate": "3,5",
            "start_year": "2026", "target_year": "2031",
        }, headers={"X-From-Modal": "1"})
        g = FundingGoal.query.one()
        assert g.existing_reserve == Decimal("0")
        assert g.interest_rate == Decimal("3.500")

    def test_edit_goal(self, client, admin_user, goal):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post(f"/cost-planning/goals/{goal.id}/edit", data={
            "name": "Umbenannt", "goal_type": "investment",
            "target_amount": "600000", "existing_reserve": "0",
            "start_year": "2026", "target_year": "2031",
            "status": "active",
        }, headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        g = db.session.get(FundingGoal, goal.id)
        assert g.name == "Umbenannt"
        assert g.target_amount == Decimal("600000.00")
        assert g.status == FundingGoal.STATUS_ACTIVE

    def test_delete_goal(self, client, admin_user, goal):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post(f"/cost-planning/goals/{goal.id}/delete")
        assert resp.status_code == 302
        assert FundingGoal.query.count() == 0

    def test_modal_get_returns_form_body(self, client, admin_user, goal):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}/edit",
                          headers={"X-From-Modal": "1"})
        assert resp.status_code == 200
        assert b'name="target_amount"' in resp.data


# ---------------------------------------------------------------------------
# Ergebnisseite
# ---------------------------------------------------------------------------

class TestGoalDetail:

    def test_detail_renders_three_packages(self, client, admin_user, goal,
                                           tariff):
        _seed_billable(count=2)
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Grundgebühr-lastig" in body
        assert "Verbrauchslastig" in body
        assert "Ausgewogen" in body

    def test_detail_works_without_a_tariff(self, client, admin_user, goal):
        """Kein Tarif hinterlegt: die Seite muss trotzdem rendern und den
        reinen Aufschlag zeigen, statt zu krachen."""
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}")
        assert resp.status_code == 200

    def test_detail_works_without_any_consumption(self, client, admin_user,
                                                  goal, tariff):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}")
        assert resp.status_code == 200
        assert "Verbrauchsjahre erfassen" in resp.data.decode()

    def test_rounding_parameter_is_applied(self, client, admin_user, goal,
                                           tariff):
        _seed_billable(count=2)
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}?rounding=euro")
        assert resp.status_code == 200

    def test_choose_scenario_marks_the_goal_decided(self, client, admin_user,
                                                    goal):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post(f"/cost-planning/goals/{goal.id}/choose",
                           data={"scenario": "balanced"})
        assert resp.status_code == 302
        g = db.session.get(FundingGoal, goal.id)
        assert g.chosen_scenario == "balanced"
        assert g.status == FundingGoal.STATUS_ACTIVE

    def test_choose_rejects_unknown_scenario(self, client, admin_user, goal):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.post(f"/cost-planning/goals/{goal.id}/choose",
                    data={"scenario": "quatsch"})
        assert db.session.get(FundingGoal, goal.id).chosen_scenario is None

    def test_apply_tariff_redirects_to_prefilled_form(self, client, admin_user,
                                                      goal, tariff):
        """Kein stiller Insert — der Nutzer landet im Tarifformular mit
        vorbefuellten Werten."""
        _seed_billable(count=2)
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post(f"/cost-planning/goals/{goal.id}/apply-tariff",
                           data={"scenario": "base"})
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "/invoices/tariffs/new" in location
        assert "price_per_m3" in location
        assert "valid_from=2026" in location
        # Es darf noch KEIN zweiter Tarif entstanden sein.
        assert WaterTariff.query.count() == 1

    def test_prefilled_tariff_form_shows_the_values(self, client, admin_user,
                                                    tariff):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get("/invoices/tariffs/new"
                          "?name=Tarif+ab+2026&valid_from=2026"
                          "&base_fee=150.00&price_per_m3=1.5000")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'value="Tarif ab 2026"' in body
        assert 'value="150.00"' in body


# ---------------------------------------------------------------------------
# Verbrauchsseite
# ---------------------------------------------------------------------------

class TestConsumptionPage:

    def test_page_renders(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        assert client.get("/cost-planning/consumption").status_code == 200

    def test_manual_year_can_be_added(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/consumption/manual", data={
            "year": "2019", "total_m3": "42500", "notes": "aus Excel",
        }, headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        row = consumption.for_year(2019)
        assert row.total_m3 == Decimal("42500")
        assert row.is_manual

    def test_manual_year_overrides_a_measured_year(self, client, admin_user):
        _seed_billable(count=1, year=2024, consumption_each="100")
        consumption.refresh_all(force=True)
        db.session.commit()

        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/consumption/manual", data={
            "year": "2024", "total_m3": "99999", "notes": "Zähler defekt",
        }, headers={"X-From-Modal": "1"})
        assert resp.status_code == 204

        row = consumption.for_year(2024)
        assert row.total_m3 == Decimal("99999")
        assert row.measured_m3 == Decimal("100.000")
        assert row.is_override is True

    def test_override_can_be_reverted(self, client, admin_user):
        _seed_billable(count=1, year=2024, consumption_each="100")
        consumption.refresh_all(force=True)
        db.session.commit()
        consumption.set_manual(2024, Decimal("99999"))
        db.session.commit()

        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/consumption/manual/2024/delete")
        assert resp.status_code == 302
        row = consumption.for_year(2024)
        assert row is not None                      # Zeile bleibt bestehen
        assert row.total_m3 == Decimal("100.000")
        assert row.is_manual is False

    def test_manual_year_rejects_negative(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.post("/cost-planning/consumption/manual", data={
            "year": "2019", "total_m3": "-5",
        }, headers={"X-From-Modal": "1"})
        assert ConsumptionYear.query.count() == 0

    def test_manual_year_can_be_deleted(self, client, admin_user):
        consumption.set_manual(2019, Decimal("42500"))
        db.session.commit()
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/consumption/manual/2019/delete")
        assert resp.status_code == 302
        assert consumption.for_year(2019) is None

    def test_recalculate_builds_the_cache(self, client, admin_user):
        _seed_billable(count=2, year=2024, consumption_each="100")
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/consumption/recalculate")
        assert resp.status_code == 302
        assert consumption.for_year(2024).total_m3 == Decimal("200.000")

    def test_opening_the_page_refreshes_stale_years(self, client, admin_user):
        """Die Neuberechnung haengt am Seitenaufruf, nicht am Schreibpfad."""
        period = _seed_billable(count=1, year=2024, consumption_each="100")
        consumption.refresh_all(force=True)
        db.session.commit()

        m = WaterMeter.query.first()
        save_reading(m, period, Decimal("300"))
        db.session.commit()
        assert consumption.for_year(2024).stale is True

        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        client.get("/cost-planning/consumption")
        row = consumption.for_year(2024)
        assert row.stale is False
        assert row.total_m3 == Decimal("300.000")

    def test_settings_are_persisted(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.post("/cost-planning/settings", data={
            "sample_household_m3": "150", "avg_years": "5",
        })
        assert resp.status_code == 302
        from app.models import AppSetting
        assert AppSetting.get("cost_planning.sample_household_m3") == "150"
        assert AppSetting.get("cost_planning.avg_years") == "5"


# ---------------------------------------------------------------------------
# Beschlussvorlage
# ---------------------------------------------------------------------------

class TestReport:

    def test_print_view_renders(self, client, admin_user, goal, tariff):
        _seed_billable(count=2)
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}/print")
        assert resp.status_code == 200
        assert "Beschlussvorlage" in resp.data.decode()

    def test_pdf_falls_back_to_print_view_without_weasyprint(self, client,
                                                             admin_user, goal,
                                                             tariff):
        """Ohne GTK3 (lokal unter Windows) muss die PDF-Route auf die
        Druckansicht ausweichen statt zu fehlern — im Container liefert sie ein
        echtes PDF."""
        _seed_billable(count=2)
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get(f"/cost-planning/goals/{goal.id}/report.pdf")
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            assert f"/cost-planning/goals/{goal.id}/print" in \
                resp.headers["Location"]
        else:
            assert resp.headers["Content-Type"] == "application/pdf"


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------

class TestOverview:

    def test_combined_requirement_sums_parallel_goals(self, client, admin_user,
                                                      tariff):
        """Laufen Kredit und Sparziel parallel, muss der Tarif beide tragen."""
        db.session.add_all([
            FundingGoal(name="Sparziel", goal_type=FundingGoal.TYPE_INVESTMENT,
                        target_amount=Decimal("100000"),
                        existing_reserve=Decimal("0"),
                        start_year=2026, target_year=2030,
                        status=FundingGoal.STATUS_ACTIVE),
            FundingGoal(name="Kredit", goal_type=FundingGoal.TYPE_LOAN,
                        target_amount=Decimal("50000"),
                        existing_reserve=Decimal("0"),
                        start_year=2026, target_year=2030,
                        status=FundingGoal.STATUS_ACTIVE),
        ])
        db.session.commit()

        from app.cost_planning import services
        rows, peak = services.combined_requirement()
        assert peak == Decimal("30000.00")     # 20.000 + 10.000
        assert len(rows) == 5

        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        assert client.get("/cost-planning/").status_code == 200

    def test_overview_without_goals_renders(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "cpadmin", "test")
        resp = client.get("/cost-planning/")
        assert resp.status_code == 200
        assert "Noch kein Finanzierungsziel" in resp.data.decode()
