"""HTTP-Tests: Passwort vergessen / zuruecksetzen / aendern."""
import pytest

from app.auth.routes import _make_reset_token
from app.extensions import db
from app.models import User


@pytest.fixture
def user(app):
    u = User(username="tester", email="tester@example.com", role="user")
    u.set_password("Altes-Passwort-2026")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def admin(app):
    u = User(username="chef", email="chef@example.com", role="admin")
    u.set_password("Admin-Passwort-2026")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username, password):
    return client.post(
        "/auth/login", data={"username": username, "password": password}
    )


class TestForgotPassword:
    def test_page_accessible(self, client):
        client.get("/auth/logout")
        r = client.get("/auth/forgot-password")
        assert r.status_code == 200

    def test_existing_and_unknown_email_give_identical_response(self, client, user):
        """Non-Enumeration: bekannte und unbekannte Adresse sind ununterscheidbar."""
        client.get("/auth/logout")
        r_known = client.post(
            "/auth/forgot-password", data={"email": "tester@example.com"}
        )
        r_unknown = client.post(
            "/auth/forgot-password", data={"email": "nobody@example.com"}
        )
        assert r_known.status_code == r_unknown.status_code == 200
        assert r_known.data == r_unknown.data


class TestResetPassword:
    def test_valid_token_sets_new_password(self, client, user):
        client.get("/auth/logout")
        token = _make_reset_token(user)
        r = client.post(
            f"/auth/reset-password/{token}",
            data={
                "password": "Neues-Sicheres-PW-99",
                "password_confirm": "Neues-Sicheres-PW-99",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]
        db.session.refresh(user)
        assert user.check_password("Neues-Sicheres-PW-99")

    def test_tampered_token_is_rejected(self, client, user):
        client.get("/auth/logout")
        r = client.get("/auth/reset-password/garbage-token")
        assert r.status_code == 410

    def test_token_dies_after_use(self, client, user):
        client.get("/auth/logout")
        token = _make_reset_token(user)
        client.post(
            f"/auth/reset-password/{token}",
            data={
                "password": "Neues-Sicheres-PW-99",
                "password_confirm": "Neues-Sicheres-PW-99",
            },
        )
        # Gleiches Token erneut: der Passwort-Hash hat sich geaendert -> tot.
        r = client.get(f"/auth/reset-password/{token}")
        assert r.status_code == 410

    def test_weak_password_is_rejected(self, client, user):
        client.get("/auth/logout")
        token = _make_reset_token(user)
        r = client.post(
            f"/auth/reset-password/{token}",
            data={"password": "kurz", "password_confirm": "kurz"},
        )
        assert r.status_code == 200
        db.session.refresh(user)
        assert user.check_password("Altes-Passwort-2026")

    def test_mismatched_confirmation_is_rejected(self, client, user):
        client.get("/auth/logout")
        token = _make_reset_token(user)
        r = client.post(
            f"/auth/reset-password/{token}",
            data={
                "password": "Neues-Sicheres-PW-99",
                "password_confirm": "Tippfehler-Sicher-88",
            },
        )
        assert r.status_code == 200
        db.session.refresh(user)
        assert user.check_password("Altes-Passwort-2026")


class TestChangePassword:
    def test_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/auth/change-password", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_wrong_current_password_is_rejected(self, client, user):
        client.get("/auth/logout")
        _login(client, "tester", "Altes-Passwort-2026")
        r = client.post(
            "/auth/change-password",
            data={
                "current_password": "falsch",
                "password": "Neues-Sicheres-PW-99",
                "password_confirm": "Neues-Sicheres-PW-99",
            },
        )
        assert r.status_code == 200
        assert "aktuelle Passwort ist falsch" in r.data.decode("utf-8")
        db.session.refresh(user)
        assert user.check_password("Altes-Passwort-2026")

    def test_weak_new_password_is_rejected(self, client, user):
        client.get("/auth/logout")
        _login(client, "tester", "Altes-Passwort-2026")
        r = client.post(
            "/auth/change-password",
            data={
                "current_password": "Altes-Passwort-2026",
                "password": "schwach",
                "password_confirm": "schwach",
            },
        )
        assert r.status_code == 200
        db.session.refresh(user)
        assert user.check_password("Altes-Passwort-2026")

    def test_successful_change(self, client, user):
        client.get("/auth/logout")
        _login(client, "tester", "Altes-Passwort-2026")
        r = client.post(
            "/auth/change-password",
            data={
                "current_password": "Altes-Passwort-2026",
                "password": "Neues-Sicheres-PW-99",
                "password_confirm": "Neues-Sicheres-PW-99",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(user)
        assert user.check_password("Neues-Sicheres-PW-99")


class TestAdminUserForm:
    """Teil 4: die Policy greift auch auf den Admin-Benutzerformularen."""

    def test_new_user_form_renders(self, client, admin):
        client.get("/auth/logout")
        _login(client, "chef", "Admin-Passwort-2026")
        r = client.get("/auth/users/new")
        assert r.status_code == 200
        assert b'name="password"' in r.data

    def test_new_user_with_weak_password_is_rejected(self, client, admin):
        client.get("/auth/logout")
        _login(client, "chef", "Admin-Passwort-2026")
        r = client.post(
            "/auth/users/new",
            data={
                "username": "neuer",
                "email": "neuer@example.com",
                "password": "schwach",
                "role": "user",
            },
        )
        assert r.status_code == 200
        assert User.query.filter_by(username="neuer").first() is None

    def test_new_user_with_strong_password_is_created(self, client, admin):
        client.get("/auth/logout")
        _login(client, "chef", "Admin-Passwort-2026")
        r = client.post(
            "/auth/users/new",
            data={
                "username": "neuer",
                "email": "neuer@example.com",
                "password": "Starkes-Passwort-2026",
                "role": "user",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert User.query.filter_by(username="neuer").first() is not None
