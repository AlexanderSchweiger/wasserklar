"""HTTP-Tests für den Kunden-Import-Wizard.

Deckt alle drei Endpoints ab:
  /customers/import          (Upload, Schritt 1)
  /customers/import/preview  (Vorschau-Editor, Schritt 2)
  /customers/import/result   (Ergebnis, Schritt 3)

Inkl. Login-Schutz, Session-Handling und Pickle-Cleanup.
"""
import io
import os

import pytest

from app.extensions import db
from app.models import Customer, User
from tests.conftest import _ensure_role


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    return client.post("/auth/login", data={"username": username, "password": password})


def _csv(content: str) -> bytes:
    return content.encode("utf-8")


def _upload(client, csv_bytes, filename="test.csv", **form):
    data = {
        "duplicate_mode": "skip",
        "file": (io.BytesIO(csv_bytes), filename),
        **form,
    }
    return client.post(
        "/customers/import",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _cleanup_pickles(client):
    with client.session_transaction() as s:
        path = s.get("customer_import_file")
        s.pop("customer_import_file", None)
        s.pop("customer_import_cfg", None)
    if path and os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Login-Schutz
# ---------------------------------------------------------------------------

class TestLoginRequired:
    def test_upload_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/customers/import", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_preview_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/customers/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_result_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/customers/import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Schritt 1: Upload
# ---------------------------------------------------------------------------

class TestUploadStep:
    def test_get_renders_step_1(self, client, admin):
        _login(client)
        r = client.get("/customers/import")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 1" in body
        assert "Datei" in body
        assert "Duplikat" in body or "Überspringen" in body

    def test_post_without_file_flashes_warning(self, client, admin):
        _login(client)
        r = client.post(
            "/customers/import",
            data={"duplicate_mode": "skip"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/customers/import" in r.headers["Location"]

    def test_post_with_unsupported_format_flashes_error(self, client, admin):
        _login(client)
        r = client.post(
            "/customers/import",
            data={
                "duplicate_mode": "skip",
                "file": (io.BytesIO(b"x"), "evil.exe"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_post_csv_redirects_to_preview(self, client, admin):
        _login(client)
        r = _upload(client, _csv("Kunden-Nr.;Name;Ort\n1;Max Muster;Wien\n"))
        assert r.status_code == 302
        assert "/customers/import/preview" in r.headers["Location"]
        with client.session_transaction() as s:
            assert s.get("customer_import_file")
            assert s.get("customer_import_cfg") is not None
        _cleanup_pickles(client)

    def test_pickle_file_actually_created(self, client, admin):
        _login(client)
        _upload(client, _csv("Name\nTestkunde\n"))
        with client.session_transaction() as s:
            path = s.get("customer_import_file")
        assert path and os.path.exists(path)
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Schritt 2: Vorschau
# ---------------------------------------------------------------------------

class TestPreviewStep:
    def test_get_without_session_redirects_to_upload(self, client, admin):
        _login(client)
        r = client.get("/customers/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/customers/import" in r.headers["Location"]

    def test_get_renders_preview_table(self, client, admin):
        _login(client)
        _upload(client, _csv("Kunden-Nr.;Name;Ort\n42;Vorschau Kunde;Linz\n"))
        r = client.get("/customers/import/preview")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 2" in body
        assert "Vorschau Kunde" in body
        assert "Vorschau aktualisieren" in body
        assert "Import ausführen" in body
        _cleanup_pickles(client)

    def test_preview_shows_new_row(self, client, admin):
        _login(client)
        _upload(client, _csv("Kunden-Nr.;Name\n999;Neuer Kunde\n"))
        r = client.get("/customers/import/preview")
        body = r.get_data(as_text=True)
        assert "table-success" in body or "Neu" in body
        _cleanup_pickles(client)

    def test_preview_shows_existing_as_exists(self, client, admin):
        c = Customer(name="Bestehend", customer_number=5)
        db.session.add(c)
        db.session.commit()

        _login(client)
        _upload(client, _csv("Kunden-Nr.;Name\n5;Bestehend\n"),
                duplicate_mode="skip")
        r = client.get("/customers/import/preview")
        body = r.get_data(as_text=True)
        assert "Bereits vorhanden" in body or "bg-secondary-lt" in body
        _cleanup_pickles(client)

    def test_post_refresh_re_renders(self, client, admin):
        _login(client)
        _upload(client, _csv("Kunden-Nr.;Name\n1;Refresh Test\n"))
        r = client.post("/customers/import/preview", data={
            "action": "refresh",
            "col_customer_number": "Kunden-Nr.",
            "col_name": "Name",
            "duplicate_mode": "skip",
        })
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Refresh Test" in body
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Bestätigen (POST mit action=confirm)
# ---------------------------------------------------------------------------

class TestConfirmStep:
    def test_confirm_creates_customer_in_db(self, client, admin):
        _login(client)
        _upload(client, _csv("Kunden-Nr.;Name;Ort\n100;Neuer Import Kunde;Wien\n"))
        r = client.post("/customers/import/preview", data={
            "action": "confirm",
            "col_customer_number": "Kunden-Nr.",
            "col_name": "Name",
            "col_ort": "Ort",
            "duplicate_mode": "skip",
            "rows[0][customer_number]": "100",
            "rows[0][name]": "Neuer Import Kunde",
            "rows[0][ort]": "Wien",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/customers/import/result" in r.headers["Location"]

        c = Customer.query.filter_by(customer_number=100).first()
        assert c is not None
        assert c.name == "Neuer Import Kunde"
        assert c.is_customer is True

    def test_confirm_redirects_to_result(self, client, admin):
        _login(client)
        _upload(client, _csv("Name\nKunde Redirect\n"))
        r = client.post("/customers/import/preview", data={
            "action": "confirm",
            "col_name": "Name",
            "duplicate_mode": "skip",
            "rows[0][name]": "Kunde Redirect",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/customers/import/result" in r.headers["Location"]

    def test_confirm_clears_session_and_pickle(self, client, admin):
        _login(client)
        _upload(client, _csv("Name\nAufraeum Kunde\n"))
        with client.session_transaction() as s:
            path = s.get("customer_import_file")
        assert os.path.exists(path)

        client.post("/customers/import/preview", data={
            "action": "confirm",
            "col_name": "Name",
            "duplicate_mode": "skip",
            "rows[0][name]": "Aufraeum Kunde",
        }, follow_redirects=False)

        assert not os.path.exists(path)
        with client.session_transaction() as s:
            assert "customer_import_file" not in s


# ---------------------------------------------------------------------------
# Ergebnis-Seite
# ---------------------------------------------------------------------------

class TestResultStep:
    def test_result_without_stats_redirects_to_index(self, client, admin):
        _login(client)
        r = client.get("/customers/import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/customers" in r.headers["Location"]

    def test_result_renders_after_confirm(self, client, admin):
        _login(client)
        _upload(client, _csv("Name\nResult Kunde\n"))
        client.post("/customers/import/preview", data={
            "action": "confirm",
            "col_name": "Name",
            "duplicate_mode": "skip",
            "rows[0][name]": "Result Kunde",
        }, follow_redirects=False)
        r = client.get("/customers/import/result")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Import abgeschlossen" in body
        assert "Neu angelegt" in body
