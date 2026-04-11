"""HTTP-Tests: Authentifizierung, Login-Schutz, Admin-Only-Routes."""
import pytest

from app.extensions import db
from app.models import User


@pytest.fixture
def admin_user(app):
    u = User(username="admin", email="admin@test.com", role="admin")
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def normal_user(app):
    u = User(username="normal", email="normal@test.com", role="user")
    u.set_password("pass")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username, password):
    return client.post("/auth/login", data={"username": username, "password": password})


class TestLoginProtection:
    """Alle Routen außer /auth/login müssen @login_required sein."""

    def test_dashboard_requires_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_customers_requires_login(self, client):
        r = client.get("/customers/", follow_redirects=False)
        assert r.status_code == 302

    def test_invoices_requires_login(self, client):
        r = client.get("/invoices/", follow_redirects=False)
        assert r.status_code == 302

    def test_accounting_requires_login(self, client):
        r = client.get("/accounting/bookings", follow_redirects=False)
        assert r.status_code == 302


class TestLoginPage:
    def test_login_page_accessible(self, client):
        r = client.get("/auth/login")
        assert r.status_code == 200

    def test_wrong_password_stays_on_login(self, client, admin_user):
        r = client.post("/auth/login", data={"username": "admin", "password": "falsch"})
        assert r.status_code == 200
        assert "Benutzername oder Passwort falsch" in r.data.decode("utf-8")

    def test_wrong_username_stays_on_login(self, client):
        r = client.post("/auth/login", data={"username": "nobody", "password": "x"})
        assert r.status_code == 200
        assert "Benutzername oder Passwort falsch" in r.data.decode("utf-8")

    def test_successful_login_redirects(self, client, admin_user):
        r = _login(client, "admin", "secret")
        assert r.status_code == 302
        assert "/auth/login" not in r.headers["Location"]

    def test_authenticated_can_access_dashboard(self, client, admin_user):
        _login(client, "admin", "secret")
        r = client.get("/")
        assert r.status_code == 200

    def test_logout_ends_session(self, client, admin_user):
        _login(client, "admin", "secret")
        client.get("/auth/logout")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


class TestAdminRoutes:
    def test_admin_can_access_user_list(self, client, admin_user):
        _login(client, "admin", "secret")
        r = client.get("/auth/users")
        assert r.status_code == 200

    def test_normal_user_redirected_from_admin_route(self, client, normal_user):
        _login(client, "normal", "pass")
        # admin_required redirects to main.dashboard, not 403
        r = client.get("/auth/users", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" not in r.headers["Location"]


class TestHtmxPartials:
    def test_invoices_full_page_without_htmx(self, client, admin_user):
        _login(client, "admin", "secret")
        r = client.get("/invoices/")
        assert r.status_code == 200
        # Vollständige Seite enthält <html>
        assert b"<html" in r.data

    def test_invoices_partial_with_htmx_header(self, client, admin_user):
        _login(client, "admin", "secret")
        r = client.get("/invoices/", headers={"HX-Request": "true"})
        assert r.status_code == 200
        # Partielles HTML hat kein <html>-Wrapper
        assert b"<html" not in r.data
