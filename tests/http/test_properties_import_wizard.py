"""HTTP-Tests für den Objekte-Import-Wizard.

Deckt alle drei Endpoints ab:
  /properties/import          (Upload, Schritt 1)
  /properties/import/preview  (Vorschau-Editor, Schritt 2)
  /properties/import/result   (Ergebnis, Schritt 3)

Inkl. Login-Schutz, Session-Handling und Pickle-Cleanup.

Stolperer:
- Zu Beginn jedes Tests ``client.get("/auth/logout")`` ausführen — der
  CookieJar wird zwischen test_client-Instanzen geteilt (Werkzeug 3.x).
- Property-Fixtures immer mit ``object_type`` anlegen.
"""
import io
import os

import pytest

from app.extensions import db
from app.models import Customer, Property, PropertyOwnership, User
from tests.conftest import _ensure_role


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="prop_admin", email="prop_admin@test.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="prop_admin", password="secret"):
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
        "/properties/import",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _cleanup_pickles(client):
    with client.session_transaction() as s:
        path = s.get("property_import_file")
        s.pop("property_import_file", None)
        s.pop("property_import_cfg", None)
    if path and os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Login-Schutz
# ---------------------------------------------------------------------------

class TestLoginRequired:
    def test_upload_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/properties/import", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_preview_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/properties/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_result_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/properties/import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Schritt 1: Upload
# ---------------------------------------------------------------------------

class TestUploadStep:
    def test_get_renders_step_1(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        r = client.get("/properties/import")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 1" in body
        assert "Datei" in body
        assert "Duplikat" in body or "Überspringen" in body

    def test_post_without_file_redirects(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        r = client.post(
            "/properties/import",
            data={"duplicate_mode": "skip"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/properties/import" in r.headers["Location"]

    def test_post_csv_redirects_to_preview(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        r = _upload(client, _csv("Objekt-Nr.;Typ;Straße;Ort\n1;Haus;Hauptstraße;Wien\n"))
        assert r.status_code == 302
        assert "/properties/import/preview" in r.headers["Location"]
        with client.session_transaction() as s:
            assert s.get("property_import_file")
            assert s.get("property_import_cfg") is not None
        _cleanup_pickles(client)

    def test_pickle_file_actually_created(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Typ\nHaus\n"))
        with client.session_transaction() as s:
            path = s.get("property_import_file")
        assert path and os.path.exists(path)
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Schritt 2: Vorschau
# ---------------------------------------------------------------------------

class TestPreviewStep:
    def test_get_without_session_redirects_to_upload(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        r = client.get("/properties/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/properties/import" in r.headers["Location"]

    def test_get_renders_preview_table(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Objekt-Nr.;Typ;Straße;Ort\n42;Haus;Bergstraße;Linz\n"))
        r = client.get("/properties/import/preview")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 2" in body
        assert "Bergstraße" in body
        assert "Vorschau aktualisieren" in body
        assert "Import ausführen" in body
        _cleanup_pickles(client)

    def test_preview_shows_new_row(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Objekt-Nr.;Ort\n999;Graz\n"))
        r = client.get("/properties/import/preview")
        body = r.get_data(as_text=True)
        assert "table-success" in body or "Neu" in body
        _cleanup_pickles(client)

    def test_preview_shows_existing_as_exists(self, client, admin):
        prop = Property(object_number="55", object_type="Haus")
        db.session.add(prop)
        db.session.commit()

        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Objekt-Nr.;Ort\n55;Wien\n"), duplicate_mode="skip")
        r = client.get("/properties/import/preview")
        body = r.get_data(as_text=True)
        assert "Bereits vorhanden" in body or "bg-secondary-lt" in body
        _cleanup_pickles(client)

    def test_post_refresh_re_renders(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Objekt-Nr.;Ort\n1;Refresh Stadt\n"))
        r = client.post("/properties/import/preview", data={
            "action": "refresh",
            "col_object_number": "Objekt-Nr.",
            "col_ort": "Ort",
            "duplicate_mode": "skip",
        })
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Refresh Stadt" in body
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Bestätigen (POST mit action=confirm)
# ---------------------------------------------------------------------------

class TestConfirmStep:
    def test_confirm_creates_property_in_db(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Objekt-Nr.;Typ;Straße;Ort\n100;Haus;Importstraße;Wien\n"))
        r = client.post("/properties/import/preview", data={
            "action": "confirm",
            "col_object_number": "Objekt-Nr.",
            "col_object_type": "Typ",
            "col_strasse": "Straße",
            "col_ort": "Ort",
            "duplicate_mode": "skip",
            "rows[0][object_number]": "100",
            "rows[0][object_type]": "Haus",
            "rows[0][strasse]": "Importstraße",
            "rows[0][ort]": "Wien",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/properties/import/result" in r.headers["Location"]

        prop = Property.query.filter_by(object_number="100").first()
        assert prop is not None
        assert prop.object_type == "Haus"
        assert prop.active is True

    def test_confirm_redirects_to_result(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Ort\nResult Stadt\n"))
        r = client.post("/properties/import/preview", data={
            "action": "confirm",
            "col_ort": "Ort",
            "duplicate_mode": "skip",
            "rows[0][ort]": "Result Stadt",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/properties/import/result" in r.headers["Location"]

    def test_confirm_clears_session_and_pickle(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Ort\nAufraeum Stadt\n"))
        with client.session_transaction() as s:
            path = s.get("property_import_file")
        assert os.path.exists(path)

        client.post("/properties/import/preview", data={
            "action": "confirm",
            "col_ort": "Ort",
            "duplicate_mode": "skip",
            "rows[0][ort]": "Aufraeum Stadt",
        }, follow_redirects=False)

        assert not os.path.exists(path)
        with client.session_transaction() as s:
            assert "property_import_file" not in s


# ---------------------------------------------------------------------------
# Ergebnis-Seite
# ---------------------------------------------------------------------------

class TestResultStep:
    def test_result_without_stats_redirects_to_index(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        r = client.get("/properties/import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/properties" in r.headers["Location"]

    def test_result_renders_after_confirm(self, client, admin):
        client.get("/auth/logout")
        _login(client)
        _upload(client, _csv("Ort\nResult Ort\n"))
        client.post("/properties/import/preview", data={
            "action": "confirm",
            "col_ort": "Ort",
            "duplicate_mode": "skip",
            "rows[0][ort]": "Result Ort",
        }, follow_redirects=False)
        r = client.get("/properties/import/result")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Import abgeschlossen" in body
        assert "Neu angelegt" in body
