"""HTTP: Tarif-Staffeln + Bedingungen (OSS v1.47.0) — Rechnungslauf, manuelle
Tarifzeile, Tarifformular, Tarifrechner (Fragment + JSON).

Tarif der Tests: Wasser bis 200 m³ 1,20 €, über 200 bis 500 m³ 1,50 €, über
500 m³ 2,00 € (anteilig); Grundgebühr 50 € nur für Mitglieder. Das Jahr ist
nicht umsatzsteuerpflichtig (Netto = Brutto).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices.charges import build_tariff, charge_type, tariff_form_params
from app.models import (
    AppSetting, BillingPeriod, Customer, FiscalYear, Invoice, MeterReading, Property,
    PropertyOwnership, User, WaterMeter, WaterTariff,
)
from tests.conftest import _ensure_role

TODAY = date.today()
D = Decimal


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def logged_in(client, admin):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


def _property(name, number, period, consumptions, status=None):
    cust = Customer(name=name, customer_number=number)
    prop = Property(object_number=f"P-{number}", object_type="Haus")
    db.session.add_all([cust, prop])
    db.session.flush()
    if status:
        cust.ensure_wg_profile().status = status
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                     valid_from=date(2020, 1, 1)))
    for i, cons in enumerate(consumptions, start=1):
        meter = WaterMeter(property_id=prop.id, meter_number=f"Z-{number}-{i}",
                           meter_type="main")
        db.session.add(meter)
        db.session.flush()
        db.session.add(MeterReading(meter_id=meter.id, billing_period_id=period.id,
                                    value=D(cons), consumption=D(cons),
                                    reading_date=date(TODAY.year, 12, 31)))
    return cust, prop


@pytest.fixture
def setup(app):
    db.session.add(FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                              end_date=date(TODAY.year, 12, 31), is_vat_liable=False))
    period = BillingPeriod(name=str(TODAY.year), start_date=date(TODAY.year, 1, 1),
                           end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    db.session.flush()
    tariff = build_tariff(
        name="Staffeltarif", valid_from=TODAY.year, water_price=D("1.20"),
        base_fee=D("50"), tax_rate=D("10"),
        tiers={"water": [("200", "1.50"), ("500", "2.00")]},
        conditions={"base_fee": ["member"]})
    member = _property("Mitglied", 1, period, ["550"])
    external = _property("Extern", 2, period, ["100"], status="external")
    db.session.commit()
    return {"period": period, "tariff": tariff, "member": member, "external": external}


def _run(client, s):
    return client.post("/invoices/generate", data={
        "billing_period_id": str(s["period"].id),
        "tariff_id": str(s["tariff"].id),
        "due_days": "30",
    })


def _invoice(customer):
    return Invoice.query.filter_by(customer_id=customer.id).one()


class TestBillingRun:
    def test_graduated_lines_and_member_condition(self, logged_in, setup):
        assert _run(logged_in, setup).status_code == 302
        inv = _invoice(setup["member"][0])
        water = [i for i in inv.items if i.charge_key == "water"]
        assert [(i.quantity, i.unit_price, i.amount) for i in water] == [
            (D("200"), D("1.2"), D("240.00")),
            (D("300"), D("1.5"), D("450.00")),
            (D("50"), D("2"), D("100.00"))]
        assert "Stufe 2 (über 200 bis 500 m³)" in water[1].description
        assert inv.consumption == D("550")
        assert {i.charge_key for i in inv.items} == {"water", "base_fee"}
        assert inv.total_amount == D("840.00")

    def test_external_without_base_fee(self, logged_in, setup):
        _run(logged_in, setup)
        inv = _invoice(setup["external"][0])
        assert [(i.charge_key, i.amount) for i in inv.items] == [("water", D("120.00"))]
        assert inv.total_amount == D("120.00")

    def test_whole_mode_single_line(self, logged_in, setup):
        setup["tariff"].water_component.tier_mode = "whole"
        db.session.commit()
        _run(logged_in, setup)
        inv = _invoice(setup["member"][0])
        [water] = [i for i in inv.items if i.charge_key == "water"]
        assert (water.quantity, water.unit_price, water.amount) == (D("550"), D("2"), D("1100.00"))
        assert "Stufenpreis über 500 m³" in water.description

    def test_meter_swap_tiers_on_property_sum(self, logged_in, setup):
        AppSetting.set("invoice.print_meter_swap", "true")
        swap = _property("Tausch", 3, setup["period"], ["300", "250"])
        db.session.commit()
        _run(logged_in, setup)
        inv = _invoice(swap[0])
        water = [i for i in inv.items if i.charge_key == "water"]
        assert [i.quantity for i in water] == [D("200"), D("300"), D("50")]
        assert "Zähler Z-3-1: 300 m³, Zähler Z-3-2: 250 m³" in water[0].description
        assert inv.consumption == D("550")

    def test_snapshot_shows_tiers_and_condition(self, logged_in, setup):
        resp = _run(logged_in, setup)
        html = logged_in.get(resp.headers["Location"]).get_data(as_text=True)
        assert "über 200 bis 500 m³: 1,50" in html
        assert "nur Mitglied" in html


class TestManualTariffRow:
    def test_condition_follows_invoice_customer(self, logged_in, setup):
        cust = setup["external"][0]
        resp = logged_in.post("/invoices/new", data={
            "customer_id": str(cust.id), "date": TODAY.isoformat(),
            "row_type[]": ["tariff"], "row_tariff_id[]": [str(setup["tariff"].id)],
            "row_consumption_m3[]": ["250"], "row_description[]": [""],
            "row_quantity[]": [""], "row_unit[]": [""], "row_unit_price[]": [""],
            "row_tax_rate[]": ["0"], "row_account_id[]": [""], "row_project_id[]": [""],
        })
        assert resp.status_code == 302
        inv = Invoice.query.filter_by(customer_id=cust.id).one()
        assert [(i.charge_key, i.quantity, i.amount) for i in inv.items] == [
            ("water", D("200"), D("240.00")), ("water", D("50"), D("75.00"))]


def _form(tariff, **extra):
    params = tariff_form_params(tariff)
    params.update(extra)
    return params


class TestTariffForm:
    def test_saves_tiers_mode_and_condition(self, logged_in, setup):
        water = charge_type("water")
        base = charge_type("base_fee")
        data = _form(setup["tariff"], name="Neu", valid_from=TODAY.year + 1)
        data.update({
            f"comp_tier_above_{water.id}": ["1.000", "250", ""],
            f"comp_tier_price_{water.id}": ["3,00", "1,75", ""],
            f"comp_tier_mode_{water.id}": "whole",
            f"comp_cond_status_{base.id}": ["member", "prospect"],
        })
        # Grenzen muessen von Stufe zu Stufe steigen.
        resp = logged_in.post("/invoices/tariffs/new", data=data,
                              headers={"X-From-Modal": "1"})
        assert resp.status_code == 200
        assert "Grenzen der Staffel" in resp.get_data(as_text=True)

        data[f"comp_tier_above_{water.id}"] = ["250", "1.000"]
        data[f"comp_tier_price_{water.id}"] = ["1,75", "3,00"]
        resp = logged_in.post("/invoices/tariffs/new", data=data,
                              headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        tariff = WaterTariff.query.filter_by(name="Neu").one()
        comp = tariff.water_component
        assert [(s.above, s.price) for s in comp.tier_steps] == [
            (D("250"), D("1.75")), (D("1000"), D("3"))]        # „1.000" = Tausenderpunkt
        assert comp.tier_mode == "whole"
        assert tariff.component("base_fee").contact_statuses == {"member", "prospect"}

    def test_edit_form_shows_existing_tiers(self, logged_in, setup):
        html = logged_in.get(f"/invoices/tariffs/{setup['tariff'].id}/edit",
                             headers={"X-From-Modal": "1"}).get_data(as_text=True)
        assert 'value="1,50"' in html and 'value="200"' in html
        assert "tariffCalcResult" in html
        assert 'name="comp_cond_status_' in html

    def test_condition_on_water_ignored_and_tiers_on_flat_rejected_silently(self, logged_in, setup):
        water = charge_type("water")
        base = charge_type("base_fee")
        data = _form(setup["tariff"], name="Neu2", valid_from=TODAY.year + 2)
        data[f"comp_cond_status_{water.id}"] = ["member"]
        data[f"comp_tier_above_{base.id}"] = ["10"]
        data[f"comp_tier_price_{base.id}"] = ["5"]
        resp = logged_in.post("/invoices/tariffs/new", data=data,
                              headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        tariff = WaterTariff.query.filter_by(name="Neu2").one()
        assert tariff.water_component.conditions is None
        assert tariff.component("base_fee").tiers is None

    def test_utility_mode_keeps_hidden_condition(self, logged_in, setup):
        AppSetting.set("org.type", "utility")
        db.session.commit()
        html = logged_in.get(f"/invoices/tariffs/{setup['tariff'].id}/edit",
                             headers={"X-From-Modal": "1"}).get_data(as_text=True)
        assert 'type="hidden" name="comp_cond_status_' in html
        assert "wirkt nur im Mandant-Typ Wassergenossenschaft" in html


class TestCalculator:
    def test_fragment_from_unsaved_form(self, logged_in, setup):
        water = charge_type("water")
        params = _form(setup["tariff"], calc_m3="550", calc_status="member")
        params[f"comp_amount_{water.id}"] = "1,00"          # ungespeichert geaendert
        html = logged_in.get("/invoices/tariffs/calc", query_string=params).get_data(as_text=True)
        assert "Stufe 3 (über 500 m³)" in html
        # 200 × 1,00 + 300 × 1,50 + 50 × 2,00 + 50 = 800
        assert "800,00 €" in html
        assert db.session.get(WaterTariff, setup["tariff"].id).water_price == D("1.2")

    def test_fragment_status_without_base_fee(self, logged_in, setup):
        params = _form(setup["tariff"], calc_m3="100", calc_status="external")
        html = logged_in.get("/invoices/tariffs/calc", query_string=params).get_data(as_text=True)
        assert "120,00 €" in html
        assert "entfällt für den Status Extern" in html

    def test_fragment_vat_toggle(self, logged_in, setup):
        params = _form(setup["tariff"], calc_m3="100", calc_status="member", calc_vat="1")
        html = logged_in.get("/invoices/tariffs/calc", query_string=params).get_data(as_text=True)
        # 120 + 50 netto, 10 % USt je Position = 187,00
        assert "187,00 €" in html and "USt 10 %" in html

    def test_fragment_shows_validation_error(self, logged_in, setup):
        water = charge_type("water")
        params = _form(setup["tariff"], calc_m3="100")
        params[f"comp_tier_above_{water.id}"] = ["abc"]
        params[f"comp_tier_price_{water.id}"] = ["1"]
        html = logged_in.get("/invoices/tariffs/calc", query_string=params).get_data(as_text=True)
        assert "alert-warning" in html and "Staffel bei" in html

    def test_json_for_saved_tariff_and_customer(self, logged_in, setup):
        member = setup["member"][0]
        external = setup["external"][0]
        url = f"/invoices/tariffs/calc?tariff_id={setup['tariff'].id}&m3=550"
        assert logged_in.get(f"{url}&customer_id={member.id}").get_json()["net"] == "840.00"
        data = logged_in.get(f"{url}&customer_id={external.id}").get_json()
        assert data["net"] == "790.00" and data["tiered"] is True
        assert len(data["lines"]) == 3

    def test_parity_with_billing_run(self, logged_in, setup):
        """Rechner und Rechnungslauf rechnen dieselbe Zahl."""
        _run(logged_in, setup)
        inv = _invoice(setup["member"][0])
        url = (f"/invoices/tariffs/calc?tariff_id={setup['tariff'].id}&m3=550"
               f"&customer_id={setup['member'][0].id}")
        assert D(logged_in.get(url).get_json()["gross"]) == inv.total_amount


class TestTariffList:
    def test_badges(self, logged_in, setup):
        html = logged_in.get("/invoices/tariffs").get_data(as_text=True)
        assert "gestaffelt" in html and "nur Mitglied" in html
        assert "bis 200 m³: 1,20" in html
