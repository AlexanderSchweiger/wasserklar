"""HTTP: Nachkommastellen beim Preis je m³ einstellbar (Gebührenart) —
Rechnungstext, Rechnungsansicht, Tarifformular, Positions-Editor; die
Rechnungsposition friert den Wert ein."""
from datetime import date
from decimal import Decimal as D

import pytest

from app import country
from app.extensions import db
from app.invoices.charges import build_tariff, charge_type
from app.models import (
    AppSetting, BillingPeriod, Customer, FiscalYear, Invoice, InvoiceItem,
    MeterReading, Property, PropertyOwnership, User, WaterMeter,
)
from tests.conftest import _ensure_role

TODAY = date.today()
MODAL = {"X-From-Modal": "1", "HX-Request": "true"}


@pytest.fixture
def logged_in(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


@pytest.fixture
def setup(app):
    """DE-Mandant, Tarif Wasser 1,85 + Wassercent 0,10, ein Objekt mit 120 m³."""
    AppSetting.set(country.SETTING_KEY, "DE")
    db.session.add(FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                              end_date=date(TODAY.year, 12, 31), is_vat_liable=False))
    period = BillingPeriod(name=str(TODAY.year), start_date=date(TODAY.year, 1, 1),
                           end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    tariff = build_tariff(name="Tarif DE", valid_from=TODAY.year, water_price=D("1.85"),
                          water_levy=D("0.10"), tax_rate=None)
    cust = Customer(name="Kunde Eins", customer_number=1)
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add_all([cust, prop])
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                     valid_from=date(2020, 1, 1)))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main")
    db.session.add(meter)
    db.session.flush()
    db.session.add(MeterReading(meter_id=meter.id, billing_period_id=period.id,
                                value=D("120"), consumption=D("120"),
                                reading_date=date(TODAY.year, 12, 31)))
    db.session.commit()
    return {"period": period, "tariff": tariff, "cust": cust}


def _set_places(client, key, places, label):
    ct = charge_type(key)
    return client.post(f"/invoices/charge-types/{ct.id}/edit", headers=MODAL, data={
        "label": label, "active": "1", "price_decimals": str(places)})


def _run(client, setup):
    resp = client.post("/invoices/generate", data={
        "billing_period_id": str(setup["period"].id),
        "tariff_id": str(setup["tariff"].id), "due_days": "30"})
    assert resp.status_code == 302
    return Invoice.query.filter_by(customer_id=setup["cust"].id).one()


def test_charge_type_form_saves_places(logged_in, setup):
    html = logged_in.get(f"/invoices/charge-types/{charge_type('water_levy').id}/edit",
                         headers=MODAL).get_data(as_text=True)
    assert 'name="price_decimals"' in html
    assert '<option value="2" selected>' in html        # Voreinstellung
    resp = _set_places(logged_in, "water_levy", 3, "Wasserentnahmeentgelt")
    assert resp.status_code == 204
    assert charge_type("water_levy").price_decimals == 3


def test_charge_type_form_rejects_invalid_places(logged_in, setup):
    resp = _set_places(logged_in, "water_levy", 5, "Wasserentnahmeentgelt")
    assert resp.status_code == 200
    assert "Nachkommastellen" in resp.get_data(as_text=True)
    assert charge_type("water_levy").price_decimals == 2


def test_flat_charge_type_has_no_places_field(logged_in, setup):
    html = logged_in.get(f"/invoices/charge-types/{charge_type('base_fee').id}/edit",
                         headers=MODAL).get_data(as_text=True)
    assert 'name="price_decimals"' not in html


def test_billing_run_uses_and_freezes_places(logged_in, setup):
    """Voreinstellung 2 Stellen: „0,10 €/m³“ statt „0,1000 €/m³“."""
    inv = _run(logged_in, setup)
    levy = next(i for i in inv.items if i.charge_key == "water_levy")
    assert levy.price_decimals == 2
    assert "120 m³ × 0,10 €/m³" in levy.description
    assert levy.amount == D("12.00")
    water = next(i for i in inv.items if i.charge_key == "water")
    assert water.price_decimals == 2 and "× 1,85 €/m³" in water.description

    html = logged_in.get(f"/invoices/{inv.id}").get_data(as_text=True)
    assert "0,10 €" in html
    assert "0,1000" not in html

    # Spaetere Umstellung aendert den Beleg nicht.
    _set_places(logged_in, "water_levy", 4, "Wasserentnahmeentgelt")
    db.session.expire_all()
    levy = db.session.get(InvoiceItem, levy.id)
    assert levy.price_decimals == 2 and levy.price_places == 2


def test_four_places_when_configured(logged_in, setup):
    _set_places(logged_in, "water_levy", 4, "Wasserentnahmeentgelt")
    inv = _run(logged_in, setup)
    levy = next(i for i in inv.items if i.charge_key == "water_levy")
    assert levy.price_decimals == 4 and "120 m³ × 0,1000 €/m³" in levy.description
    assert "0,1000 €" in logged_in.get(f"/invoices/{inv.id}").get_data(as_text=True)


def test_tariff_form_shows_places(logged_in, setup):
    html = logged_in.get(f"/invoices/tariffs/{setup['tariff'].id}/edit",
                         headers=MODAL).get_data(as_text=True)
    levy = charge_type("water_levy")
    assert f'name="comp_amount_{levy.id}"' in html
    assert 'value="0,10"' in html
    tariffs = logged_in.get("/invoices/tariffs").get_data(as_text=True)
    assert "0,10 €/m³" in tariffs
    assert "2 Nachkommastellen" in tariffs


def test_editor_keeps_places_as_typed(logged_in, setup):
    _set_places(logged_in, "water", 4, "Wasserverbrauch")
    inv = _run(logged_in, setup)
    html = logged_in.get(f"/invoices/{inv.id}").get_data(as_text=True)
    assert 'value="0.10"' in html            # Preisfeld der bestehenden Position

    items = sorted(inv.items, key=lambda i: i.id)
    n = len(items)
    resp = logged_in.post(f"/invoices/{inv.id}/items/save", data={
        "row_type[]": ["free"] * n, "row_tariff_id[]": [""] * n,
        "row_consumption_m3[]": [""] * n,
        "row_description[]": [i.description for i in items],
        "row_quantity[]": [str(i.quantity) for i in items],
        "row_unit[]": [i.unit for i in items],
        "row_unit_price[]": ["%.*f" % (i.price_places, i.unit_price) for i in items],
        "row_tax_rate[]": ["0"] * n,
        "row_account_id[]": [""] * n, "row_project_id[]": [""] * n,
    })
    assert resp.status_code == 302
    saved = {i.description: i for i in db.session.get(Invoice, inv.id).items}
    levy = next(i for d, i in saved.items() if d.startswith("Wasserentnahmeentgelt"))
    water = next(i for d, i in saved.items() if d.startswith("Wasserverbrauch"))
    assert levy.price_places == 2 and levy.unit_price == D("0.10")
    assert water.price_places == 4
