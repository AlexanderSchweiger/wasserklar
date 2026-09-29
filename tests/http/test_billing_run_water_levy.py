"""HTTP: Rechnungslauf mit Wassercent (DE) + individuellen Gebuehren,
Uebersichten/Excel mit Spalten je Gebuehrenart, Gebuehrenarten-Verwaltung,
Kunden-/Objekt-Overrides im Formular."""
import io
import json
from datetime import date
from decimal import Decimal

import pytest

from app import country
from app.extensions import db
from app.invoices.charges import build_tariff, charge_type
from app.models import (
    Account, AppSetting, BillingPeriod, BillingRun, ChargeOverride, ChargeType,
    Customer, FiscalYear, Invoice, MeterReading, Property, PropertyOwnership,
    User, WaterMeter,
)
from tests.conftest import _ensure_role

TODAY = date.today()


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


@pytest.fixture
def de_setup(app):
    """DE-Mandant, USt-pflichtiges Jahr, Tarif Wasser 1,85 + Wassercent 0,10 +
    Grundgebuehr 60 (alles 7 %), zwei Objekte mit je 100 m³."""
    AppSetting.set(country.SETTING_KEY, "DE")
    db.session.add(FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                              end_date=date(TODAY.year, 12, 31), is_vat_liable=True))
    period = BillingPeriod(name=str(TODAY.year), start_date=date(TODAY.year, 1, 1),
                           end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    acc_water = Account(name="Wasserverkauf")
    acc_levy = Account(name="Wasserentnahmeentgelt")
    db.session.add_all([acc_water, acc_levy])
    db.session.flush()
    tariff = build_tariff(
        name="Tarif DE", valid_from=TODAY.year, water_price=Decimal("1.85"),
        water_levy=Decimal("0.10"), base_fee=Decimal("60"), tax_rate=Decimal("7"),
        accounts={"water": acc_water.id, "water_levy": acc_levy.id})
    props = []
    for i in (1, 2):
        cust = Customer(name=f"Kunde {i}", customer_number=i)
        prop = Property(object_number=f"P-{i}", object_type="Haus")
        db.session.add_all([cust, prop])
        db.session.flush()
        db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                         valid_from=date(2020, 1, 1)))
        meter = WaterMeter(property_id=prop.id, meter_number=f"Z-{i}", meter_type="main")
        db.session.add(meter)
        db.session.flush()
        db.session.add(MeterReading(meter_id=meter.id, billing_period_id=period.id,
                                    value=Decimal("100"), consumption=Decimal("100"),
                                    reading_date=date(TODAY.year, 12, 31)))
        props.append((cust, prop))
    db.session.commit()
    return {"period": period, "tariff": tariff, "props": props,
            "acc_levy": acc_levy}


def _run(client, setup):
    return client.post("/invoices/generate", data={
        "billing_period_id": str(setup["period"].id),
        "tariff_id": str(setup["tariff"].id),
        "due_days": "30",
    })


def test_levy_is_own_line_with_7_percent(logged_in, de_setup):
    resp = _run(logged_in, de_setup)
    assert resp.status_code == 302
    inv = Invoice.query.filter_by(customer_id=de_setup["props"][0][0].id).one()
    items = {it.charge_key: it for it in inv.items}
    assert set(items) == {"water", "water_levy", "base_fee"}
    levy = items["water_levy"]
    assert levy.unit == "m³"
    assert levy.quantity == Decimal("100")
    assert levy.amount == Decimal("10.00")
    assert levy.tax_rate == Decimal("7")
    assert levy.account_id == de_setup["acc_levy"].id
    assert levy.description.startswith("Wasserentnahmeentgelt")
    # Verbrauch darf durch die zweite m³-Zeile NICHT doppelt zaehlen.
    assert inv.consumption == Decimal("100")
    # 185 + 10 + 60 = 255 netto, 7 % USt je Position
    assert inv.net_total == Decimal("255.00")
    assert inv.total_amount == Decimal("272.85")


def test_overrides_and_exemption(logged_in, de_setup):
    cust1, prop1 = de_setup["props"][0]
    cust2, prop2 = de_setup["props"][1]
    base = charge_type("base_fee")
    cust1.charge_overrides.append(ChargeOverride(charge_type_id=base.id, amount=Decimal("30")))
    prop2.charge_overrides.append(ChargeOverride(charge_type_id=base.id, amount=None))
    db.session.commit()
    _run(logged_in, de_setup)
    inv1 = Invoice.query.filter_by(customer_id=cust1.id).one()
    inv2 = Invoice.query.filter_by(customer_id=cust2.id).one()
    base1 = next(i for i in inv1.items if i.charge_key == "base_fee")
    assert base1.amount == Decimal("30.00")
    assert not [i for i in inv2.items if i.charge_key == "base_fee"]   # entfällt


def test_snapshot_and_overview_columns(logged_in, de_setup):
    _run(logged_in, de_setup)
    run = BillingRun.query.one()
    keys = [c["key"] for c in run.tariff_components_snapshot]
    assert keys[:2] == ["water", "water_levy"]
    assert json.loads(run.tariff_snapshot)[1]["amount"] == "0.1000"
    html = logged_in.get(f"/invoices/billing-runs/{run.id}").get_data(as_text=True)
    assert "Wasserentnahmeentgelt" in html
    assert 'data-charge0="10.00"' in html   # Wassercent-Spalte je Rechnung
    period_html = logged_in.get(
        f"/invoices/period/{de_setup['period'].id}").get_data(as_text=True)
    assert "Wasserentnahmeentgelt" in period_html


def test_excel_export_has_levy_column(logged_in, de_setup):
    import openpyxl
    _run(logged_in, de_setup)
    run = BillingRun.query.one()
    resp = logged_in.get(f"/invoices/billing-runs/{run.id}/export/excel")
    assert resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    values = [c.value for row in wb.active.iter_rows() for c in row if c.value]
    assert "Wasserentnahmeentgelt" in values
    assert "Wasserentnahmeentgelt (je m³)" in values   # Deckblatt Tarif-Snapshot


def test_levy_valid_from_is_apportioned(logged_in, de_setup):
    levy = de_setup["tariff"].component("water_levy")
    levy.valid_from = date(TODAY.year, 7, 1)
    db.session.commit()
    _run(logged_in, de_setup)
    inv = Invoice.query.filter_by(customer_id=de_setup["props"][0][0].id).one()
    levy_item = next(i for i in inv.items if i.charge_key == "water_levy")
    days_total = (date(TODAY.year, 12, 31) - date(TODAY.year, 1, 1)).days + 1
    days_valid = (date(TODAY.year, 12, 31) - date(TODAY.year, 7, 1)).days + 1
    expected_qty = (Decimal("100") * days_valid / days_total).quantize(Decimal("0.001"))
    assert levy_item.quantity == expected_qty
    assert "zeitanteilig" in levy_item.description


def test_ignored_override_is_reported(logged_in, de_setup):
    cust1, _ = de_setup["props"][0]
    extra = ChargeType(key="custom_1", label="Zählermiete", calc_type="flat",
                       overridable=True)
    db.session.add(extra)
    db.session.flush()
    cust1.charge_overrides.append(ChargeOverride(charge_type_id=extra.id, amount=Decimal("5")))
    db.session.commit()
    resp = logged_in.post("/invoices/generate", data={
        "billing_period_id": str(de_setup["period"].id),
        "tariff_id": str(de_setup["tariff"].id), "due_days": "30",
    }, follow_redirects=True)
    assert "Zählermiete" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Gebuehrenarten + Overrides im Formular
# ---------------------------------------------------------------------------

MODAL = {"X-From-Modal": "1", "HX-Request": "true"}


def test_tariffs_page_lists_charge_types(logged_in, de_setup):
    html = logged_in.get("/invoices/tariffs").get_data(as_text=True)
    assert "Gebührenarten" in html
    assert "Wasserentnahmeentgelt" in html
    assert "nur individuell" in html   # Zusatzgebuehr ohne Betrag


def test_create_and_delete_custom_charge_type(logged_in, de_setup):
    resp = logged_in.post("/invoices/charge-types/new", headers=MODAL, data={
        "label": "Schmutzwasser", "calc_type": "per_m3", "active": "1"})
    assert resp.status_code == 204
    ct = ChargeType.query.filter_by(label="Schmutzwasser").one()
    assert ct.key.startswith("custom_") and ct.is_per_m3 and not ct.overridable
    resp = logged_in.post(f"/invoices/charge-types/{ct.id}/delete")
    assert resp.status_code == 302
    assert ChargeType.query.filter_by(label="Schmutzwasser").count() == 0


def test_system_charge_type_cannot_be_deleted(logged_in, de_setup):
    water = charge_type("water")
    logged_in.post(f"/invoices/charge-types/{water.id}/delete")
    assert charge_type("water") is not None


def test_customer_form_saves_overrides(logged_in, de_setup):
    cust, _ = de_setup["props"][0]
    base = charge_type("base_fee")
    resp = logged_in.post(f"/customers/{cust.id}/edit", headers=MODAL, data={
        "last_name": "Kunde", "first_name": "Eins", "is_customer": "1",
        "customer_number": "1", "land": "Deutschland",
        f"ov_mode_{base.id}": "amount", f"ov_amount_{base.id}": "42,00",
    })
    assert resp.status_code in (200, 204)
    db.session.expire_all()
    cust = db.session.get(Customer, cust.id)
    assert [(o.charge_type_id, o.amount) for o in cust.charge_overrides] == [
        (base.id, Decimal("42.00"))]
    detail = logged_in.get(f"/customers/{cust.id}").get_data(as_text=True)
    assert "Individuelle Gebühren" in detail and "42,00 €" in detail


def test_property_form_rejects_invalid_override(logged_in, de_setup):
    _, prop = de_setup["props"][0]
    base = charge_type("base_fee")
    resp = logged_in.post(f"/properties/{prop.id}/edit", headers=MODAL, data={
        "object_type": "Haus", "object_number": "P-1",
        f"ov_mode_{base.id}": "amount", f"ov_amount_{base.id}": "abc",
    })
    assert resp.status_code == 200
    assert "gültigen Betrag" in resp.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Property, prop.id).charge_overrides == []
