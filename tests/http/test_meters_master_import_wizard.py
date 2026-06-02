"""HTTP-Tests für den Zähler-Stammdaten-Import-Wizard.

Deckt alle drei Endpoints ab:
  /meters/master-import         (Upload)
  /meters/master-import/preview (Vorschau-Editor)
  /meters/master-import/result  (Stats)

Inkl. Login-Schutz, Session-Handling, Pickle-Cleanup.

KOLLISIONSFREI: testet NICHT /meters/import (Ablesungs-Import).
"""
import io
import os
from datetime import date

import pytest

from app.extensions import db
from app.models import Customer, Property, PropertyOwnership, User, WaterMeter
from tests.conftest import _ensure_role


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin_mmi", email="admin_mmi@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def sample_property(app):
    """A single property for testing meter imports against."""
    p = Property(object_number="P-MMI-01", object_type="Haus", ort="Wien")
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def sample_meter(app, sample_property):
    """An existing meter linked to sample_property."""
    m = WaterMeter(
        property_id=sample_property.id,
        meter_number="Z-MMI-001",
        meter_type="main",
        active=True,
    )
    db.session.add(m)
    db.session.commit()
    return m


def _login(client, username="admin_mmi", password="secret"):
    return client.post("/auth/login", data={"username": username, "password": password})


def _csv(content: str) -> bytes:
    return content.encode("utf-8")


def _upload(client, csv_bytes, filename="test.csv", duplicate_mode="skip"):
    data = {
        "duplicate_mode": duplicate_mode,
        "file": (io.BytesIO(csv_bytes), filename),
    }
    return client.post(
        "/meters/master-import",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def _cleanup_pickles(client):
    """Remove meter_master_import pickle from session and disk."""
    with client.session_transaction() as s:
        path = s.get("meter_master_import_file")
        s.pop("meter_master_import_file", None)
        s.pop("meter_master_import_cfg", None)
    if path and os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Login-Schutz
# ---------------------------------------------------------------------------

class TestLoginRequired:
    def test_upload_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/master-import", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_preview_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/master-import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_result_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/master-import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Step 1: Upload
# ---------------------------------------------------------------------------

class TestUploadStep:
    def test_get_renders_step_1(self, client, admin):
        _login(client)
        r = client.get("/meters/master-import")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 1" in body
        assert "Zählernummer" in body
        assert "Objekt-Nr." in body

    def test_post_without_file_redirects(self, client, admin):
        _login(client)
        r = client.post(
            "/meters/master-import",
            data={"duplicate_mode": "skip"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "master-import" in r.headers["Location"]

    def test_post_unsupported_format_flashes_error(self, client, admin):
        _login(client)
        r = client.post(
            "/meters/master-import",
            data={
                "duplicate_mode": "skip",
                "file": (io.BytesIO(b"x"), "evil.exe"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_post_csv_redirects_to_preview(self, client, admin, sample_property):
        _login(client)
        csv = _csv("Zählernummer;Objekt-Nr.\nZ-NEW-1;P-MMI-01\n")
        r = _upload(client, csv)
        assert r.status_code == 302
        assert "/meters/master-import/preview" in r.headers["Location"]
        with client.session_transaction() as s:
            assert s.get("meter_master_import_file")
            assert s.get("meter_master_import_cfg") is not None
        _cleanup_pickles(client)

    def test_pickle_file_created(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-P1;P-MMI-01\n"))
        with client.session_transaction() as s:
            path = s.get("meter_master_import_file")
        assert path and os.path.exists(path)
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Step 2: Preview
# ---------------------------------------------------------------------------

class TestPreviewStep:
    def test_get_without_session_redirects_to_upload(self, client, admin):
        _login(client)
        r = client.get("/meters/master-import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "master-import" in r.headers["Location"]

    def test_get_renders_preview_table(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-PRV-1;P-MMI-01\n"))
        r = client.get("/meters/master-import/preview")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 2" in body
        assert "Vorschau aktualisieren" in body
        assert "Import ausführen" in body
        _cleanup_pickles(client)

    def test_preview_shows_new_meter_green(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-NEW-99;P-MMI-01\n"))
        r = client.get("/meters/master-import/preview")
        body = r.get_data(as_text=True)
        assert "table-success" in body
        _cleanup_pickles(client)

    def test_preview_shows_unknown_object_red(self, client, admin):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-ERR-1;UNKNOWN-OBJ\n"))
        r = client.get("/meters/master-import/preview")
        body = r.get_data(as_text=True)
        assert "table-danger" in body
        assert "nicht gefunden" in body
        _cleanup_pickles(client)

    def test_preview_shows_existing_meter_neutral(self, client, admin, sample_meter, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-MMI-001;P-MMI-01\n"), duplicate_mode="skip")
        r = client.get("/meters/master-import/preview")
        body = r.get_data(as_text=True)
        assert "Bereits vorhanden" in body or "bg-secondary-lt" in body
        _cleanup_pickles(client)

    def test_post_refresh_rerenders(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-REF-1;P-MMI-01\n"))
        r = client.post("/meters/master-import/preview", data={
            "action": "refresh",
            "col_meter_number": "Zählernummer",
            "col_object_number": "Objekt-Nr.",
            "duplicate_mode": "skip",
        })
        assert r.status_code == 200
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Confirm-Pfad (POST /master-import/preview mit action=confirm)
# ---------------------------------------------------------------------------

class TestConfirmStep:
    def _confirm(self, client, **rows_data):
        data = {
            "action": "confirm",
            "col_meter_number": "Zählernummer",
            "col_object_number": "Objekt-Nr.",
            "duplicate_mode": "skip",
            **rows_data,
        }
        return client.post(
            "/meters/master-import/preview",
            data=data,
            follow_redirects=False,
        )

    def test_confirm_creates_meter(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-CFM-1;P-MMI-01\n"))
        r = self._confirm(client)
        assert r.status_code == 302
        assert "master-import/result" in r.headers["Location"]
        m = WaterMeter.query.filter_by(meter_number="Z-CFM-1").first()
        assert m is not None
        assert m.property_id == sample_property.id

    def test_confirm_clears_session(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-CLR-1;P-MMI-01\n"))
        with client.session_transaction() as s:
            path = s.get("meter_master_import_file")
        assert os.path.exists(path)
        self._confirm(client)
        assert not os.path.exists(path)
        with client.session_transaction() as s:
            assert "meter_master_import_file" not in s

    def test_confirm_skip_row_with_checkbox(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-SKP-1;P-MMI-01\n"))
        self._confirm(client, **{"rows[0][skip]": "on"})
        assert WaterMeter.query.filter_by(meter_number="Z-SKP-1").first() is None


# ---------------------------------------------------------------------------
# Step 3: Result
# ---------------------------------------------------------------------------

class TestResultStep:
    def test_result_without_stats_redirects_to_index(self, client, admin):
        _login(client)
        r = client.get("/meters/master-import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/meters" in r.headers["Location"]

    def test_result_renders_after_confirm(self, client, admin, sample_property):
        _login(client)
        _upload(client, _csv("Zählernummer;Objekt-Nr.\nZ-RES-1;P-MMI-01\n"))
        client.post("/meters/master-import/preview", data={
            "action": "confirm",
            "col_meter_number": "Zählernummer",
            "col_object_number": "Objekt-Nr.",
            "duplicate_mode": "skip",
        }, follow_redirects=False)
        r = client.get("/meters/master-import/result")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Import abgeschlossen" in body
        assert "Neu angelegt" in body


# ---------------------------------------------------------------------------
# Kollisionsfreiheit: Ablesungs-Import-Endpoints existieren noch
# ---------------------------------------------------------------------------

class TestReadingImportEndpointsUnchanged:
    """Ensure the existing reading-import wizard is unaffected."""

    def test_reading_import_upload_still_reachable(self, client, admin):
        _login(client)
        r = client.get("/meters/import", follow_redirects=False)
        # Must be 200 (the reading import upload page), not 404 or redirect to master-import
        assert r.status_code == 200

    def test_reading_import_preview_still_redirects_to_upload_without_session(
        self, client, admin
    ):
        _login(client)
        r = client.get("/meters/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/meters/import" in r.headers["Location"]
        assert "master" not in r.headers["Location"]
