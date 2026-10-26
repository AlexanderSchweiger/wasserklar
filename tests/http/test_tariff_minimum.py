"""HTTP: Mindestabnahme im Tarif — Rechnungslauf, individuelle Ausnahmen je
Kunde/Objekt, Tarifformular, Tarifrechner, Anzeigen, Positions-Editor.

Tarif der Tests: Wasser 1,40 €/m³ mit 50 m³ Mindestabnahme, Grundgebühr 30 €;
das Jahr ist nicht umsatzsteuerpflichtig (Netto = Brutto).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices import meter_statement
from app.invoices.charges import build_tariff, charge_type, tariff_form_params
from app.models import (
    AppSetting, BillingPeriod, ChargeOverride, ChargeType, Customer, FiscalYear, Invoice,
    MeterReading, MinimumOverride, Property, PropertyOwnership, TariffComponent, User,
    WaterMeter, WaterTariff,
)
from tests.conftest import _ensure_role

TODAY = date.today()
D = Decimal
MODAL = {"X-From-Modal": "1", "HX-Request": "true"}


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


def _property(name, number, period, consumptions):
    cust = Customer(name=name, customer_number=number)
    prop = Property(object_number=f"P-{number}", object_type="Haus")
    db.session.add_all([cust, prop])
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                     valid_from=date(2020, 1, 1)))
    for i, cons in enumerate(consumptions, start=1):
        meter = WaterMeter(property_id=prop.id, meter_number=f"Z-{number}-{i}",
                           meter_type="main", initial_value=D("0"))
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
    tariff = build_tariff(name="Mindesttarif", valid_from=TODAY.year, water_price=D("1.40"),
                          base_fee=D("30"), tax_rate=D("10"), minimums={"water": "50"})
    small = _property("Klein", 1, period, ["30"])
    big = _property("Gross", 2, period, ["80"])
    zero = _property("Leer", 3, period, ["0"])
    db.session.commit()
    return {"period": period, "tariff": tariff, "small": small, "big": big, "zero": zero}


def _run(client, s):
    return client.post("/invoices/generate", data={
        "billing_period_id": str(s["period"].id),
        "tariff_id": str(s["tariff"].id),
        "due_days": "30",
    }, follow_redirects=False)


def _invoice(customer):
    return Invoice.query.filter_by(customer_id=customer.id).one()


def _water(inv):
    return [(i.quantity, i.amount, i.is_minimum_fill) for i in inv.items
            if i.charge_key == "water"]


class TestBillingRun:
    def test_fill_line_and_consumption(self, logged_in, setup):
        assert _run(logged_in, setup).status_code == 302
        inv = _invoice(setup["small"][0])
        assert _water(inv) == [(D("30"), D("42.00"), False), (D("20"), D("28.00"), True)]
        fill = [i for i in inv.items if i.is_minimum_fill][0]
        assert fill.description == (f"Wasserverbrauch {TODAY.year} – Mindestabnahme 50 m³ "
                                    f"(verbraucht 30 m³): 20 m³ × 1,40 €/m³")
        assert all(i.min_quantity == D("50") for i in inv.items if i.charge_key == "water")
        # Verbrauch bleibt der echte Verbrauch — der Verbrauchsnachweis passt.
        assert inv.consumption == D("30")
        assert meter_statement.statement_for(inv).matches
        assert inv.total_amount == D("100.00")

    def test_above_minimum_unchanged(self, logged_in, setup):
        _run(logged_in, setup)
        inv = _invoice(setup["big"][0])
        assert _water(inv) == [(D("80"), D("112.00"), False)]

    def test_zero_consumption_pays_minimum(self, logged_in, setup):
        _run(logged_in, setup)
        inv = _invoice(setup["zero"][0])
        assert _water(inv) == [(D("0"), D("0.00"), False), (D("50"), D("70.00"), True)]

    def test_info_flash_counts_objects(self, logged_in, setup):
        resp = _run(logged_in, setup)
        html = logged_in.get(resp.headers["Location"]).get_data(as_text=True)
        assert "Mindestabnahme bei 2 Objekten verrechnet" in html
        assert "Mindestabnahme 50 m³" in html          # Tarif-Snapshot im Laufdetail

    def test_meter_swap_one_fill_line(self, logged_in, setup):
        AppSetting.set("invoice.print_meter_swap", "true")
        swap = _property("Tausch", 4, setup["period"], ["10", "15"])
        db.session.commit()
        _run(logged_in, setup)
        inv = _invoice(swap[0])
        assert _water(inv) == [(D("10"), D("14.00"), False), (D("15"), D("21.00"), False),
                               (D("25"), D("35.00"), True)]
        assert inv.consumption == D("25")

    def test_overview_counts_fill_as_water_revenue(self, logged_in, setup):
        resp = _run(logged_in, setup)
        html = logged_in.get(resp.headers["Location"]).get_data(as_text=True)
        # Klein: 30 m³ und 70,00 € Wasser (42 + 28 Auffuellung)
        assert 'data-m3="30.000"' in html or 'data-m3="30"' in html
        assert 'data-wasser="70.00"' in html


class TestOverrides:
    def test_property_exempt_beats_tariff(self, logged_in, setup):
        water = charge_type("water")
        db.session.add(MinimumOverride(charge_type_id=water.id,
                                       property_id=setup["small"][1].id, min_quantity=None))
        db.session.commit()
        _run(logged_in, setup)
        assert _water(_invoice(setup["small"][0])) == [(D("30"), D("42.00"), False)]

    def test_customer_quantity_and_property_priority(self, logged_in, setup):
        water = charge_type("water")
        db.session.add_all([
            MinimumOverride(charge_type_id=water.id, customer_id=setup["small"][0].id,
                            min_quantity=D("100")),
            MinimumOverride(charge_type_id=water.id, customer_id=setup["big"][0].id,
                            min_quantity=D("100")),
            MinimumOverride(charge_type_id=water.id, property_id=setup["big"][1].id,
                            min_quantity=D("60")),
        ])
        db.session.commit()
        _run(logged_in, setup)
        assert _water(_invoice(setup["small"][0])) == [
            (D("30"), D("42.00"), False), (D("70"), D("98.00"), True)]
        # Objekt (60) schlaegt Kunde (100) — 80 m³ liegen darueber.
        assert _water(_invoice(setup["big"][0])) == [(D("80"), D("112.00"), False)]

    def test_individual_minimum_without_tariff_minimum(self, logged_in, setup):
        setup["tariff"].water_component.min_quantity = None
        water = charge_type("water")
        db.session.add(MinimumOverride(charge_type_id=water.id,
                                       customer_id=setup["big"][0].id, min_quantity=D("200")))
        db.session.commit()
        _run(logged_in, setup)
        assert _water(_invoice(setup["small"][0])) == [(D("30"), D("42.00"), False)]
        assert _water(_invoice(setup["big"][0])) == [
            (D("80"), D("112.00"), False), (D("120"), D("168.00"), True)]

    def test_individual_price_keeps_minimum(self, logged_in, setup):
        sewer = ChargeType(key="custom_1", label="Kanal", calc_type=ChargeType.CALC_PER_M3,
                           overridable=True, active=True, sort_order=50)
        db.session.add(sewer)
        db.session.flush()
        setup["tariff"].components.append(TariffComponent(
            charge_type=sewer, label="Kanal", amount=D("2.00"), sort_order=50,
            min_quantity=D("40")))
        db.session.add(ChargeOverride(charge_type_id=sewer.id,
                                      property_id=setup["small"][1].id, amount=D("3.00")))
        db.session.commit()
        _run(logged_in, setup)
        inv = _invoice(setup["small"][0])
        assert [(i.quantity, i.unit_price, i.is_minimum_fill) for i in inv.items
                if i.charge_key == "custom_1"] == [
            (D("30"), D("3"), False), (D("10"), D("3"), True)]

    def test_customer_form_saves_minimum(self, logged_in, setup):
        cust = setup["small"][0]
        water = charge_type("water")
        resp = logged_in.post(f"/customers/{cust.id}/edit", headers=MODAL, data={
            "last_name": "Klein", "is_customer": "1", "customer_number": "1",
            "land": "Österreich",
            f"min_mode_{water.id}": "amount", f"min_qty_{water.id}": "1.000",
        })
        assert resp.status_code in (200, 204)
        db.session.expire_all()
        cust = db.session.get(Customer, cust.id)
        assert [(m.charge_type_id, m.min_quantity) for m in cust.minimum_overrides] == [
            (water.id, D("1000"))]
        detail = logged_in.get(f"/customers/{cust.id}").get_data(as_text=True)
        assert "Mindestabnahme 1.000 m³" in detail

    def test_customer_form_shows_minimum_section(self, logged_in, setup):
        cust = setup["small"][0]
        water = charge_type("water")
        html = logged_in.get(f"/customers/{cust.id}/edit", headers=MODAL).get_data(as_text=True)
        assert f'name="min_mode_{water.id}"' in html
        assert "keine Mindestabnahme" in html

    def test_property_form_exempt_and_invalid(self, logged_in, setup):
        _, prop = setup["small"]
        water = charge_type("water")
        resp = logged_in.post(f"/properties/{prop.id}/edit", headers=MODAL, data={
            "object_type": "Haus", "object_number": "P-1",
            f"min_mode_{water.id}": "amount", f"min_qty_{water.id}": "-5",
        })
        assert resp.status_code == 200
        assert "nicht negativ" in resp.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(Property, prop.id).minimum_overrides == []

        logged_in.post(f"/properties/{prop.id}/edit", headers=MODAL, data={
            "object_type": "Haus", "object_number": "P-1",
            f"min_mode_{water.id}": "exempt", f"min_qty_{water.id}": "",
        })
        db.session.expire_all()
        [mo] = db.session.get(Property, prop.id).minimum_overrides
        assert mo.min_quantity is None
        detail = logged_in.get(f"/properties/{prop.id}").get_data(as_text=True)
        assert "keine Mindestabnahme" in detail

        # „wie Tarif“ entfernt die Ausnahme wieder.
        logged_in.post(f"/properties/{prop.id}/edit", headers=MODAL, data={
            "object_type": "Haus", "object_number": "P-1",
            f"min_mode_{water.id}": "", f"min_qty_{water.id}": "",
        })
        db.session.expire_all()
        assert db.session.get(Property, prop.id).minimum_overrides == []


def _form(tariff, **extra):
    params = tariff_form_params(tariff)
    params.update(extra)
    return params


class TestTariffForm:
    def test_saves_minimum_and_validates(self, logged_in, setup):
        water = charge_type("water")
        data = _form(setup["tariff"], name="Neu", valid_from=TODAY.year + 1)
        assert data[f"comp_min_qty_{water.id}"] == "50"          # Kopie nimmt sie mit
        data[f"comp_min_qty_{water.id}"] = "abc"
        resp = logged_in.post("/invoices/tariffs/new", data=data, headers={"X-From-Modal": "1"})
        assert resp.status_code == 200
        assert "Mindestabnahme bei" in resp.get_data(as_text=True)

        data[f"comp_min_qty_{water.id}"] = "1.000"
        resp = logged_in.post("/invoices/tariffs/new", data=data, headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        assert WaterTariff.query.filter_by(name="Neu").one().water_component.min_quantity == D("1000")

    def test_empty_removes_minimum(self, logged_in, setup):
        water = charge_type("water")
        data = _form(setup["tariff"])
        data[f"comp_min_qty_{water.id}"] = ""
        resp = logged_in.post(f"/invoices/tariffs/{setup['tariff'].id}/edit", data=data,
                              headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        db.session.expire_all()
        assert db.session.get(WaterTariff, setup["tariff"].id).water_component.min_quantity is None

    def test_edit_form_shows_minimum(self, logged_in, setup):
        water = charge_type("water")
        html = logged_in.get(f"/invoices/tariffs/{setup['tariff'].id}/edit",
                             headers={"X-From-Modal": "1"}).get_data(as_text=True)
        assert f'name="comp_min_qty_{water.id}"' in html
        assert "mind. 50 m³" in html
        assert "Staffel / Mindestabnahme" in html

    def test_list_and_summary(self, logged_in, setup):
        html = logged_in.get("/invoices/tariffs").get_data(as_text=True)
        assert "mind. 50 m³" in html
        assert setup["tariff"].summary.startswith("1,40 €/m³ (mind. 50 m³)")


class TestCalculator:
    def test_fragment_shows_fill_line(self, logged_in, setup):
        params = _form(setup["tariff"], calc_m3="30")
        html = logged_in.get("/invoices/tariffs/calc", query_string=params).get_data(as_text=True)
        assert "Mindestabnahme 50 m³ (verbraucht 30 m³)" in html
        assert "100,00 €" in html             # 42 + 28 + 30
        assert "abweichende Mengen am Kontakt" in html

    def test_parity_with_billing_run(self, logged_in, setup):
        _run(logged_in, setup)
        inv = _invoice(setup["small"][0])
        url = (f"/invoices/tariffs/calc?tariff_id={setup['tariff'].id}&m3=30"
               f"&customer_id={setup['small'][0].id}")
        assert D(logged_in.get(url).get_json()["gross"]) == inv.total_amount


class TestItemsEditor:
    def test_save_keeps_fill_flags(self, logged_in, setup):
        """Der Positions-Editor schickt bestehende Zeilen als „frei“ zurueck —
        Gebührenart und Kennzeichen bleiben trotzdem erhalten."""
        _run(logged_in, setup)
        inv = _invoice(setup["small"][0])
        items = sorted(inv.items, key=lambda i: i.id)
        data = {"row_type[]": [], "row_item_id[]": [], "row_tariff_id[]": [],
                "row_consumption_m3[]": [], "row_description[]": [], "row_quantity[]": [],
                "row_unit[]": [], "row_unit_price[]": [], "row_tax_rate[]": [],
                "row_account_id[]": [], "row_project_id[]": []}
        for it in items:
            data["row_type[]"].append("free")
            data["row_item_id[]"].append(str(it.id))
            data["row_tariff_id[]"].append("")
            data["row_consumption_m3[]"].append("")
            data["row_description[]"].append(it.description)
            data["row_quantity[]"].append(str(it.quantity))
            data["row_unit[]"].append(it.unit)
            data["row_unit_price[]"].append(str(it.unit_price))
            data["row_tax_rate[]"].append("0")
            data["row_account_id[]"].append("")
            data["row_project_id[]"].append("")
        # Eine neue freie Position dazu (ohne Zeilen-ID).
        for key, value in (("row_type[]", "free"), ("row_item_id[]", ""),
                           ("row_tariff_id[]", ""), ("row_consumption_m3[]", ""),
                           ("row_description[]", "Zählermiete"), ("row_quantity[]", "1"),
                           ("row_unit[]", "Stk"), ("row_unit_price[]", "5"),
                           ("row_tax_rate[]", "0"), ("row_account_id[]", ""),
                           ("row_project_id[]", "")):
            data[key].append(value)
        resp = logged_in.post(f"/invoices/{inv.id}/items/save", data=data)
        assert resp.status_code == 302
        db.session.expire_all()
        inv = db.session.get(Invoice, inv.id)
        assert _water(inv) == [(D("30"), D("42.00"), False), (D("20"), D("28.00"), True)]
        assert [i.charge_key for i in inv.items] == ["water", "water", "base_fee", None]
        assert inv.consumption == D("30")
        assert meter_statement.statement_for(inv).matches

    def test_editor_renders_row_item_ids(self, logged_in, setup):
        _run(logged_in, setup)
        inv = _invoice(setup["small"][0])
        html = logged_in.get(f"/invoices/{inv.id}").get_data(as_text=True)
        assert html.count('name="row_item_id[]"') >= len(inv.items) + 3
