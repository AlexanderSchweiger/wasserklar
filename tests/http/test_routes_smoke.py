"""Smoke-Tests fuer alle ungetesteten Blueprint-Routen.

Pragmatischer Pre-Coverage-Boost: jede Hauptroute jedes Blueprints
mindestens 1x als eingeloggter Admin abrufen, damit Routing-, Template-
und Query-Fehler beim Refactoring sofort sichtbar werden. Tieferes
Verhalten (Status-Wechsel, FK-Cascades, …) gehoert in eigene Tests.

Blueprints abgedeckt:
- main (Dashboard)
- customers
- properties
- invoices + tariffs
- accounting (accounts, bookings, fiscal_years, real_accounts)
- projects
- settings
- import_csv

Auth + Meters sind separat in test_auth.py / test_meters_*.py getestet.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account,
    Customer,
    FiscalYear,
    Property,
    RealAccount,
    User,
    WaterTariff,
)


@pytest.fixture
def admin(app):
    u = User(username="admin", email="admin@test.com", role="admin")
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    # Werkzeug-3 Cookie-Bug Workaround: vorher logout
    client.get("/auth/logout")
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
    )


# ---------- Main / Dashboard ----------

class TestMainDashboard:
    def test_dashboard_loads(self, client, admin):
        _login(client)
        r = client.get("/")
        assert r.status_code == 200


# ---------- Customers ----------

class TestCustomersRoutes:
    def test_list_loads(self, client, admin):
        _login(client)
        r = client.get("/customers/")
        assert r.status_code == 200

    def test_new_form_loads(self, client, admin):
        _login(client)
        r = client.get("/customers/new")
        assert r.status_code == 200

    def test_create_customer(self, client, admin):
        _login(client)
        # Customer braucht is_customer ODER is_supplier + Pflichtfelder.
        # Wir testen primaer dass POST nicht 500 wird; eine konkrete Validierung
        # ist Sache der domain-spezifischen Tests.
        r = client.post(
            "/customers/new",
            data={
                "name": "Neu Kunde",
                "email": "k@x.test",
                "is_customer": "1",
                "force": "1",  # Dubletten-Pruefung umgehen
            },
            follow_redirects=False,
        )
        # 302/303 (success) oder 200 (Form-Re-render mit fehlenden Pflichtfeldern)
        assert r.status_code in (200, 302, 303)

    def test_edit_form_loads(self, client, admin):
        _login(client)
        c = Customer(name="Edit Test")
        db.session.add(c)
        db.session.commit()
        r = client.get(f"/customers/{c.id}/edit")
        assert r.status_code == 200

    def test_search_with_htmx(self, client, admin):
        _login(client)
        c = Customer(name="Searchable Kunde")
        db.session.add(c)
        db.session.commit()
        r = client.get("/customers/?q=Search", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert b"<html" not in r.data  # HTMX-Partial


# ---------- Properties ----------

class TestPropertiesRoutes:
    def test_list_loads(self, client, admin):
        _login(client)
        r = client.get("/properties/")
        assert r.status_code == 200

    def test_new_form_loads(self, client, admin):
        _login(client)
        r = client.get("/properties/new")
        assert r.status_code == 200

    def test_create_with_object_type(self, client, admin):
        """object_type ist NOT NULL — POST muss es setzen koennen."""
        _login(client)
        r = client.post(
            "/properties/new",
            data={"address": "Musterweg 1", "object_type": "Haus"},
            follow_redirects=False,
        )
        # 302/303 (success) oder 200 (Re-render mit Validation-Fehler bei
        # fehlenden Pflichtfeldern); Hauptsache nicht 500.
        assert r.status_code in (200, 302, 303)


# ---------- Invoices + Tariffs ----------

class TestInvoicesRoutes:
    def test_list_loads(self, client, admin):
        _login(client)
        r = client.get("/invoices/")
        assert r.status_code == 200

    def test_tariffs_list_loads(self, client, admin):
        _login(client)
        # Realer Pfad ist /invoices/tariffs (ohne trailing slash)
        r = client.get("/invoices/tariffs")
        assert r.status_code == 200

    def test_create_tariff_form_loads(self, client, admin):
        _login(client)
        r = client.get("/invoices/tariffs/new")
        assert r.status_code == 200


# ---------- Accounting ----------

class TestAccountingRoutes:
    def test_accounts_list(self, client, admin):
        _login(client)
        r = client.get("/accounting/accounts")
        assert r.status_code == 200

    def test_bookings_list(self, client, admin):
        _login(client)
        r = client.get("/accounting/bookings")
        assert r.status_code == 200

    def test_real_accounts_list(self, client, admin):
        _login(client)
        r = client.get("/accounting/real-accounts")
        assert r.status_code == 200

    def test_fiscal_years_list(self, client, admin):
        _login(client)
        r = client.get("/accounting/fiscal-years")
        assert r.status_code == 200

    def test_open_items_list(self, client, admin):
        _login(client)
        r = client.get("/accounting/open-items")
        assert r.status_code == 200

    def test_csv_export_route(self, client, admin):
        _login(client)
        # CSV-Export ist eine wichtige Schreibroute — sollte ohne Daten leer
        # zurueckkommen, aber kein 500.
        r = client.get("/accounting/bookings/export.csv")
        assert r.status_code in (200, 302, 404)


# ---------- Projects ----------

class TestProjectsRoutes:
    """Blueprint-Prefix ist /projekte (nicht /projects)."""

    def test_list_loads(self, client, admin):
        _login(client)
        r = client.get("/projekte/")
        assert r.status_code == 200

    def test_new_form_loads(self, client, admin):
        _login(client)
        r = client.get("/projekte/neu")
        assert r.status_code == 200


# ---------- Settings ----------

class TestSettingsRoutes:
    """Blueprint-Prefix ist /einstellungen."""

    def test_settings_loads(self, client, admin):
        _login(client)
        r = client.get("/einstellungen/")
        assert r.status_code == 200

    def test_settings_post_updates(self, client, admin):
        _login(client)
        r = client.post(
            "/einstellungen/",
            data={"wg_name": "Test-WG"},
            follow_redirects=False,
        )
        assert r.status_code in (200, 302, 303)


# ---------- Import-CSV ----------

class TestImportCsvRoutes:
    """Blueprint-Prefix ist /import."""

    def test_index_loads(self, client, admin):
        _login(client)
        r = client.get("/import/")
        assert r.status_code == 200
