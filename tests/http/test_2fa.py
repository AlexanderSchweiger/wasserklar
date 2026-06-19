"""HTTP-Tests: optionale Zwei-Faktor-Authentifizierung (TOTP).

Deckt Enrollment, den zweistufigen Login, Recovery-Codes, Lockout,
Deaktivierung, Admin-Reset und die ``next``-Open-Redirect-Absicherung ab.
"""
import pyotp
import pytest

from app.auth import totp_service
from app.extensions import db
from app.models import User
from tests.conftest import _ensure_role


@pytest.fixture(autouse=True)
def _logout_around(client):
    # Werkzeug-3.x teilt den CookieJar zwischen test_client-Instanzen (CLAUDE.md-
    # Stolperer). Vor dem Test fuer sauberen Start, nach dem Test damit der hier
    # erzeugte Login-State nicht in nachfolgende Test-Dateien leakt.
    client.get("/auth/logout")
    yield
    client.get("/auth/logout")


@pytest.fixture
def admin_user(app):
    admin = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.com", role_id=admin.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def normal_user(app):
    role = _ensure_role("NurLesen")
    u = User(username="normal", email="normal@test.com", role_id=role.id)
    u.set_password("pass")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username, password):
    return client.post("/auth/login", data={"username": username, "password": password})


def _fresh(user_id):
    db.session.expire_all()
    return db.session.get(User, user_id)


def _enroll(client):
    """Aktiviert 2FA fuer den aktuell eingeloggten User. Liefert (secret, recovery_codes)."""
    client.get("/auth/security/2fa/enable")
    with client.session_transaction() as sess:
        secret = sess["enroll_2fa_secret"]
    client.post("/auth/security/2fa/enable", data={"code": pyotp.TOTP(secret).now()})
    with client.session_transaction() as sess:
        recovery = sess.get("recovery_codes_once")
    return secret, recovery


class TestEnrollment:
    def test_enroll_enables_2fa_and_shows_recovery_codes(self, client, admin_user):
        _login(client, "admin", "secret")
        client.get("/auth/security/2fa/enable")
        with client.session_transaction() as sess:
            secret = sess["enroll_2fa_secret"]
        r = client.post("/auth/security/2fa/enable", data={"code": pyotp.TOTP(secret).now()})
        assert r.status_code == 302
        assert "/auth/security/2fa/recovery-codes" in r.headers["Location"]

        refreshed = _fresh(admin_user.id)
        assert refreshed.totp_enabled is True
        assert refreshed.totp_secret_enc
        assert refreshed.get_totp_secret() == secret  # round-trippt via Fernet

        # Recovery-Codes werden genau einmal angezeigt ...
        r2 = client.get("/auth/security/2fa/recovery-codes")
        assert r2.status_code == 200
        # ... und beim Reload nicht erneut
        r3 = client.get("/auth/security/2fa/recovery-codes", follow_redirects=False)
        assert r3.status_code == 302
        assert "/auth/security" in r3.headers["Location"]

    def test_enroll_wrong_code_does_not_enable(self, client, admin_user):
        _login(client, "admin", "secret")
        client.get("/auth/security/2fa/enable")
        r = client.post("/auth/security/2fa/enable", data={"code": "000000"})
        assert r.status_code == 200
        assert _fresh(admin_user.id).totp_enabled is False


class TestTwoStepLogin:
    def test_enabled_user_is_redirected_to_verify_and_not_logged_in(self, client, admin_user):
        _login(client, "admin", "secret")
        _enroll(client)
        client.get("/auth/logout")

        r = _login(client, "admin", "secret")
        assert r.status_code == 302
        assert "/auth/verify-2fa" in r.headers["Location"]
        # Noch NICHT eingeloggt
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 302
        assert "/auth/login" in r2.headers["Location"]

    def test_correct_code_completes_login(self, client, admin_user):
        _login(client, "admin", "secret")
        secret, _ = _enroll(client)
        client.get("/auth/logout")

        _login(client, "admin", "secret")
        r = client.post("/auth/verify-2fa", data={"code": pyotp.TOTP(secret).now()})
        assert r.status_code == 302
        assert "/auth/verify-2fa" not in r.headers["Location"]
        assert "/auth/login" not in r.headers["Location"]
        assert client.get("/").status_code == 200

    def test_wrong_code_stays_and_increments(self, client, admin_user):
        _login(client, "admin", "secret")
        secret, _ = _enroll(client)
        client.get("/auth/logout")
        _login(client, "admin", "secret")

        current = pyotp.TOTP(secret).now()
        wrong = "654321" if current != "654321" else "123456"
        r = client.post("/auth/verify-2fa", data={"code": wrong})
        assert r.status_code == 200
        assert "Der Code ist ungültig" in r.data.decode("utf-8")
        assert _fresh(admin_user.id).totp_failed_attempts == 1

    def test_lockout_after_five_failures(self, client, admin_user):
        _login(client, "admin", "secret")
        secret, _ = _enroll(client)
        client.get("/auth/logout")
        _login(client, "admin", "secret")

        current = pyotp.TOTP(secret).now()
        wrong = "654321" if current != "654321" else "123456"
        for _ in range(5):
            client.post("/auth/verify-2fa", data={"code": wrong})
        assert _fresh(admin_user.id).is_totp_locked()

        # Auch ein korrekter Code wird waehrend der Sperre abgelehnt
        r = client.post("/auth/verify-2fa", data={"code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200
        assert "Zu viele Fehlversuche" in r.data.decode("utf-8")

    def test_recovery_code_logs_in_and_is_consumed(self, client, admin_user):
        _login(client, "admin", "secret")
        _, recovery = _enroll(client)
        code = recovery[0]
        client.get("/auth/logout")

        _login(client, "admin", "secret")
        r = client.post("/auth/verify-2fa", data={"code": code})
        assert r.status_code == 302
        assert "/auth/login" not in r.headers["Location"]

        # Derselbe Recovery-Code funktioniert kein zweites Mal
        client.get("/auth/logout")
        _login(client, "admin", "secret")
        r2 = client.post("/auth/verify-2fa", data={"code": code})
        assert r2.status_code == 200

    def test_verify_without_pending_redirects_to_login(self, client):
        r = client.get("/auth/verify-2fa", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_expired_pending_redirects_to_login(self, client, admin_user):
        _login(client, "admin", "secret")
        _enroll(client)
        client.get("/auth/logout")
        _login(client, "admin", "secret")
        with client.session_transaction() as sess:
            sess["pending_2fa_ts"] = 0  # weit in der Vergangenheit
        r = client.get("/auth/verify-2fa", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


class TestDisable:
    def test_disable_with_password(self, client, admin_user):
        _login(client, "admin", "secret")
        _enroll(client)
        r = client.post("/auth/security/2fa/disable", data={"current_password": "secret"})
        assert r.status_code == 302
        refreshed = _fresh(admin_user.id)
        assert refreshed.totp_enabled is False
        assert refreshed.totp_secret_enc is None

    def test_disable_wrong_password_keeps_2fa(self, client, admin_user):
        _login(client, "admin", "secret")
        _enroll(client)
        client.post("/auth/security/2fa/disable", data={"current_password": "falsch"})
        assert _fresh(admin_user.id).totp_enabled is True


class TestAdminReset:
    def _make_2fa_user(self):
        role = _ensure_role("NurLesen")
        u = User(username="ziel", email="ziel@test.com", role_id=role.id)
        u.set_password("pass")
        u.set_totp_secret(totp_service.new_secret())
        u.totp_enabled = True
        u.generate_recovery_codes()
        db.session.add(u)
        db.session.commit()
        return u

    def test_admin_resets_user_2fa(self, client, admin_user):
        target = self._make_2fa_user()
        _login(client, "admin", "secret")
        r = client.post(f"/auth/users/{target.id}/reset-2fa")
        assert r.status_code == 302
        refreshed = _fresh(target.id)
        assert refreshed.totp_enabled is False
        assert refreshed.totp_secret_enc is None
        assert refreshed.totp_recovery_codes is None

    def test_non_verwaltung_user_cannot_reset(self, client, normal_user, admin_user):
        _login(client, "normal", "pass")
        r = client.post(f"/auth/users/{admin_user.id}/reset-2fa", follow_redirects=False)
        assert r.status_code == 302
        # permission_required leitet aufs Dashboard, nicht auf die Aktion
        assert "/auth/login" not in r.headers["Location"]


class TestNextSafety:
    def test_open_redirect_next_is_ignored(self, client, admin_user):
        r = client.post(
            "/auth/login?next=https://evil.example/",
            data={"username": "admin", "password": "secret"},
        )
        assert r.status_code == 302
        assert "evil.example" not in r.headers["Location"]
