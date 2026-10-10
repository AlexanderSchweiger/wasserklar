"""USt-Voranmeldung je Monat: Zeitraum-Auswahl, Drill-Down, CSV, Jahresbericht."""
import io
from datetime import date
from decimal import Decimal

import pytest

from app import tax_service
from app.extensions import db
from app.models import Account, AppSetting, Booking, FiscalYear, User
from tests.conftest import _ensure_role


@pytest.fixture
def logged_in(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id, active=True)
    u.set_password("secret")
    fy = FiscalYear(year=2026, start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31), is_vat_liable=True)
    income = Account(name="Wassergeld", active=True)
    expense = Account(name="Material", active=True)
    db.session.add_all([u, fy, income, expense])
    db.session.flush()
    db.session.add_all([
        # Februar: 110 brutto @ 10 % → 10,00 USt; 24 brutto @ 20 % → 4,00 VSt
        Booking(date=date(2026, 2, 3), amount=Decimal("110.00"), tax_rate=Decimal("10"),
                account_id=income.id, description="Feb Einnahme", status="Offen"),
        Booking(date=date(2026, 2, 20), amount=Decimal("-24.00"), tax_rate=Decimal("20"),
                account_id=expense.id, description="Feb Ausgabe", status="Offen"),
        # März: zählt nicht in den Februar, aber ins Quartal
        Booking(date=date(2026, 3, 10), amount=Decimal("220.00"), tax_rate=Decimal("10"),
                account_id=income.id, description="Mär Einnahme", status="Offen"),
    ])
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


def test_month_view_totals_and_drilldown(logged_in):
    r = logged_in.get("/accounting/ust?year=2026&period=m2")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert '<option value="m2" selected>' in html
    assert "01.02.2026" in html and "28.02.2026" in html
    # Zahllast Februar: 10,00 − 4,00
    assert "6,00 €" in html
    assert "/accounting/bookings?year=2026&amp;month=2&amp;kind=income&amp;tax=any" in html
    assert "/accounting/bookings?year=2026&amp;month=2&amp;kind=income&amp;tax=10" in html
    assert "/accounting/bookings?year=2026&amp;month=2&amp;kind=expense&amp;tax=20" in html
    assert "/accounting/bookings?year=2026&amp;quarter" not in html
    assert "/accounting/ust/export?year=2026&amp;period=m2" in html

    # Zielseite nimmt den Monatsfilter an
    r = logged_in.get("/accounting/bookings?year=2026&month=2&kind=income&tax=10")
    body = r.get_data(as_text=True)
    assert "Feb Einnahme" in body and "Mär Einnahme" not in body


def test_quarter_still_works_with_period_and_legacy_param(logged_in):
    for url in ("/accounting/ust?year=2026&period=q1", "/accounting/ust?year=2026&quartal=1"):
        html = logged_in.get(url).get_data(as_text=True)
        assert '<option value="q1" selected>' in html
        assert "31.03.2026" in html
        # Q1: 30,00 USt − 4,00 VSt
        assert "26,00 €" in html


def test_invalid_period_falls_back_to_year(logged_in):
    html = logged_in.get("/accounting/ust?year=2026&period=m13").get_data(as_text=True)
    assert '<option value="year" selected>' in html
    assert "31.12.2026" in html


def test_month_csv_export(logged_in):
    r = logged_in.get("/accounting/ust/export?year=2026&period=m2")
    assert r.status_code == 200
    assert "ust_2026_02.csv" in r.headers["Content-Disposition"]
    text = r.get_data(as_text=True)
    assert "Zeitraum;Februar 2026" in text
    assert "Zahllast;;;;6,00" in text


def test_quarter_csv_filename_unchanged(logged_in):
    r = logged_in.get("/accounting/ust/export?year=2026&period=q1")
    assert "ust_Q1_2026.csv" in r.headers["Content-Disposition"]
    assert "Zeitraum;Q1/2026" in r.get_data(as_text=True)


def test_order_follows_setting(logged_in):
    html = logged_in.get("/accounting/ust?year=2026").get_data(as_text=True)
    assert html.index('label="Quartale"') < html.index('label="Monate"')
    assert "vierteljährlich" in html

    AppSetting.set(tax_service.VAT_RETURN_PERIOD_KEY, "month")
    db.session.commit()
    html = logged_in.get("/accounting/ust?year=2026").get_data(as_text=True)
    assert html.index('label="Monate"') < html.index('label="Quartale"')
    assert "monatlich" in html


def _sheet_names(client):
    import openpyxl
    r = client.get("/accounting/report/export/excel?year=2026")
    assert r.status_code == 200
    return openpyxl.load_workbook(io.BytesIO(r.data)).sheetnames


def test_annual_report_sheets_follow_setting(logged_in):
    names = _sheet_names(logged_in)
    assert "USt Q1" in names and "USt Gesamtjahr" in names
    assert not any(n.startswith("USt Feb") for n in names)

    AppSetting.set(tax_service.VAT_RETURN_PERIOD_KEY, "month")
    db.session.commit()
    names = _sheet_names(logged_in)
    assert "USt Februar" in names and "USt Dezember" in names
    assert "USt Q1" not in names
    assert "USt Gesamtjahr" in names
