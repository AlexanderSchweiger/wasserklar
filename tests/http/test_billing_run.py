"""HTTP-Tests fuer den periodenbasierten Rechnungslauf (oss-v1.3.0).

Der Rechnungslauf ``/invoices/generate`` baut seit oss-v1.3.0 auf einer
``BillingPeriod`` auf statt auf einer Jahreszahl.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, BillingPeriod, BillingRun, Customer, FiscalYear, Invoice,
    MeterReading, Project, Property, PropertyOwnership, User, WaterMeter,
    WaterTariff,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def billing_setup(app):
    """Periode, Tarif, Konto, Buchungsjahr, Objekt mit Besitzer + Zaehler +
    Ablesung in der Periode."""
    today = date.today()
    db.session.add(FiscalYear(
        year=today.year, start_date=date(today.year, 1, 1),
        end_date=date(today.year, 12, 31)))
    period = BillingPeriod(
        name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        active=True)
    db.session.add(period)
    account = Account(name="Wasser")
    account_base = Account(name="Grundgebühren")
    db.session.add_all([account, account_base])
    db.session.flush()
    tariff = WaterTariff(
        name="T", valid_from=2024, base_fee=Decimal("30"),
        price_per_m3=Decimal("2"),
        price_per_m3_account_id=account.id,
        base_fee_account_id=account_base.id)
    db.session.add(tariff)
    project = Project(name="Wasserzins 2024", code="WZ4")
    db.session.add(project)
    cust = Customer(name="Kunde", customer_number=1)
    db.session.add(cust)
    db.session.flush()
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    db.session.add(PropertyOwnership(
        property_id=prop.id, customer_id=cust.id,
        valid_from=date(2020, 1, 1), valid_to=None))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main")
    db.session.add(meter)
    db.session.flush()
    db.session.add(MeterReading(
        meter_id=meter.id, billing_period_id=period.id,
        value=Decimal("150"), consumption=Decimal("50"),
        reading_date=date(2024, 12, 31)))
    db.session.commit()
    return {"period": period, "tariff": tariff, "account": account,
            "account_base": account_base, "project": project}


class TestBillingRun:
    def test_generate_creates_invoice_for_period(self, client, admin, billing_setup):
        _login(client)
        r = client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)
        assert r.status_code == 302

        run = BillingRun.query.one()
        assert run.billing_period_id == billing_setup["period"].id

        inv = Invoice.query.one()
        assert inv.billing_period_id == billing_setup["period"].id
        consumption_items = [i for i in inv.items if i.unit == "m³"]
        assert len(consumption_items) == 1
        assert consumption_items[0].quantity == Decimal("50")

    def test_generate_skips_duplicate_period(self, client, admin, billing_setup):
        _login(client)
        data = {
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }
        client.post("/invoices/generate", data=data, follow_redirects=False)
        client.post("/invoices/generate", data=data, follow_redirects=False)
        # Zweiter Lauf legt keine zweite Rechnung fuer dasselbe Objekt+Periode an.
        assert Invoice.query.count() == 1

    def test_generate_recreates_invoice_after_storno(self, client, admin, billing_setup):
        _login(client)
        data = {
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }
        client.post("/invoices/generate", data=data, follow_redirects=False)
        inv = Invoice.query.one()
        inv.status = Invoice.STATUS_CANCELLED
        db.session.commit()

        client.post("/invoices/generate", data=data, follow_redirects=False)
        # Stornierte Rechnung blockiert den naechsten Lauf nicht mehr.
        assert Invoice.query.count() == 2
        statuses = sorted(i.status for i in Invoice.query.all())
        assert statuses == sorted([Invoice.STATUS_CANCELLED, Invoice.STATUS_DRAFT])

    def test_generate_without_period_flashes_and_redirects(self, client, admin,
                                                           billing_setup):
        _login(client)
        r = client.post("/invoices/generate", data={
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert Invoice.query.count() == 0


class TestBillingRunDetail:
    def _generate(self, client, billing_setup):
        return client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)

    def test_detail_page_renders(self, client, admin, billing_setup):
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        r = client.get(f"/invoices/billing-runs/{run.id}")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # Neue Bausteine sind da (faengt Template-Runtime-Fehler ab).
        assert "Zusammenfassung" in html
        assert "Versenden" in html
        assert "per Post" in html          # Versand-Aufschlüsselung (Entwurf vorhanden)
        # Neue Finanz-Zusammenfassung: Gesamt / Bezahlt / Offen.
        assert "Gesamtsumme" in html
        assert "Zahlungseingang" in html
        assert "Brutto" in html
        # Kunde ohne Mail-Einwilligung -> Post-Versand vorausgewaehlt.
        assert 'data-versandart="post"' in html

    def test_detail_partitions_mail_vs_post(self, client, admin, billing_setup):
        # Kunde auf E-Mail-Versand umstellen (email + rechnung_per_email).
        cust = Customer.query.one()
        cust.email = "k@k.test"
        cust.rechnung_per_email = True
        db.session.commit()
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        r = client.get(f"/invoices/billing-runs/{run.id}")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # Mailbare Rechnung erscheint im Versenden-Dialog.
        assert "k@k.test" in html
        assert 'data-versandart="mail"' in html

    def test_post_bulk_without_weasyprint_redirects(self, client, admin, billing_setup):
        # Ohne WeasyPrint (requirements-dev) faellt der Post-Bulk sauber auf
        # Flash+Redirect zurueck statt zu crashen; Status bleibt Entwurf.
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        inv = Invoice.query.one()
        r = client.post(
            f"/invoices/billing-runs/{run.id}/post-bulk-merged",
            data={"invoice_ids": str(inv.id)}, follow_redirects=False)
        assert r.status_code == 302
        assert inv.status == Invoice.STATUS_DRAFT

    def test_detail_page_has_excel_export_button(self, client, admin, billing_setup):
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        r = client.get(f"/invoices/billing-runs/{run.id}")
        html = r.get_data(as_text=True)
        assert f"/invoices/billing-runs/{run.id}/export/excel" in html
        assert "Excel-Export" in html

    def test_excel_export_returns_xlsx_with_overview(self, client, admin, billing_setup):
        # Excel-Export der Uebersicht (kein WeasyPrint noetig): valides xlsx mit
        # Deckblatt-Titel und der Rechnung in der Tabelle; Status unveraendert.
        import io
        import openpyxl
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        inv = Invoice.query.one()
        r = client.get(f"/invoices/billing-runs/{run.id}/export/excel")
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["Content-Type"]
        body = r.get_data()
        assert body[:2] == b"PK"  # xlsx ist ein ZIP-Container
        wb = openpyxl.load_workbook(io.BytesIO(body))
        ws = wb.active
        cells = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
        # Deckblatt-Titel oben + die Rechnung in der Tabelle, aber kein Seiteneffekt.
        assert any(str(v).startswith("Rechnungslauf") for v in cells)
        assert inv.invoice_number in cells
        assert inv.status == Invoice.STATUS_DRAFT


class TestBillingRunInReport:
    def _generate(self, client, billing_setup):
        return client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "account_id": str(billing_setup["account"].id),
            "due_days": "30",
        }, follow_redirects=False)

    def test_report_lists_billing_run(self, client, admin, billing_setup):
        _login(client)
        self._generate(client, billing_setup)
        run = BillingRun.query.one()
        year = run.created_at.year
        r = client.get(f"/accounting/report?year={year}")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Rechnungsläufe" in html
        assert f"/invoices/billing-runs/{run.id}" in html


class TestBillingRunKontierung:
    """Der Lauf kontiert die Positionen aus dem Tarif und setzt ein Lauf-Projekt."""

    def test_items_get_tariff_accounts(self, client, admin, billing_setup):
        _login(client)
        client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "due_days": "30",
        })
        inv = Invoice.query.one()
        by_unit = {i.unit: i for i in inv.items}
        assert by_unit["m³"].account_id == billing_setup["account"].id
        assert by_unit["Pauschal"].account_id == billing_setup["account_base"].id

    def test_run_project_lands_on_every_item(self, client, admin, billing_setup):
        _login(client)
        project = billing_setup["project"]
        client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "project_id": str(project.id),
            "due_days": "30",
        })
        run = BillingRun.query.one()
        assert run.project_id == project.id
        inv = Invoice.query.one()
        assert len(inv.items) > 0
        assert all(i.project_id == project.id for i in inv.items)

    def test_without_project_items_stay_unassigned(self, client, admin, billing_setup):
        _login(client)
        client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "due_days": "30",
        })
        assert BillingRun.query.one().project_id is None
        assert all(i.project_id is None for i in Invoice.query.one().items)

    def test_generated_invoice_is_payable_without_dialog(self, client, admin,
                                                         billing_setup):
        """Ende-zu-Ende: Tarif kontiert -> Versendet -> Bezahlt ohne Konto-Abfrage."""
        from app.models import Booking, BookingGroup
        _login(client)
        client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(billing_setup["tariff"].id),
            "due_days": "30",
        })
        inv_id = Invoice.query.one().id
        client.post(f"/invoices/{inv_id}/status", data={"status": Invoice.STATUS_SENT})
        client.post(f"/invoices/{inv_id}/status", data={"status": Invoice.STATUS_PAID})

        assert db.session.get(Invoice, inv_id).status == Invoice.STATUS_PAID
        bookings = Booking.query.filter_by(invoice_id=inv_id).all()
        # Zwei Konten (m3 + Grundgebuehr) -> Sammelbuchung mit zwei Kindern
        assert len(bookings) == 2
        assert {b.account_id for b in bookings} == {
            billing_setup["account"].id, billing_setup["account_base"].id}
        assert BookingGroup.query.count() == 1

    def test_single_account_tariff_stays_one_booking(self, client, admin,
                                                     billing_setup):
        """Alles auf ein Konto -> normale Einzelbuchung, keine Sammelbuchung."""
        from app.models import Booking, BookingGroup
        tariff = billing_setup["tariff"]
        tariff.base_fee_account_id = billing_setup["account"].id
        db.session.commit()

        _login(client)
        client.post("/invoices/generate", data={
            "billing_period_id": str(billing_setup["period"].id),
            "tariff_id": str(tariff.id),
            "due_days": "30",
        })
        inv_id = Invoice.query.one().id
        client.post(f"/invoices/{inv_id}/status", data={"status": Invoice.STATUS_SENT})
        client.post(f"/invoices/{inv_id}/status", data={"status": Invoice.STATUS_PAID})

        bookings = Booking.query.filter_by(invoice_id=inv_id).all()
        assert len(bookings) == 1
        assert bookings[0].account_id == billing_setup["account"].id
        assert BookingGroup.query.count() == 0
