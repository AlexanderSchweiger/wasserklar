"""HTTP-Tests fuer den Ablesungs-Import-Wizard.

Deckt alle vier Endpoints ab: /meters/import (Upload), /meters/import/preview
(Vorschau-Editor), /meters/import/confirm-Pfad (POST mit action=confirm),
/meters/import/result (Stats). Inkl. Login-Schutz, Session-Handling,
Pickle-Cleanup.

Ablesungen werden seit oss-v1.3.0 einer ``BillingPeriod`` zugeordnet.
"""
import io
import os
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    BillingPeriod, Customer, MeterReading, Property, PropertyOwnership, User,
    WaterMeter,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(app):
    u = User(username="admin", email="admin@test.test", role="admin")
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def period(app):
    """Aktive Abrechnungsperiode 2024 — vom Wizard als Default verwendet."""
    p = BillingPeriod(
        name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        active=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def sample(app, period):
    """Liefert einen kompletten Stack: Customer + Property + Ownership + Meter."""
    c = Customer(name="Mueller Hans", customer_number=42)
    db.session.add(c)
    db.session.flush()
    p = Property(object_number="P-1", object_type="Haus", ort="Wien")
    db.session.add(p)
    db.session.flush()
    db.session.add(PropertyOwnership(
        property_id=p.id, customer_id=c.id,
        valid_from=date(2020, 1, 1), valid_to=None,
    ))
    m = WaterMeter(
        property_id=p.id, meter_number="Z-001",
        meter_type="main", active=True,
    )
    db.session.add(m)
    db.session.commit()
    return {"customer": c, "property": p, "meter": m, "period": period}


def _login(client, username="admin", password="secret"):
    return client.post("/auth/login", data={"username": username, "password": password})


def _csv(content: str) -> bytes:
    return content.encode("utf-8")


def _upload(client, csv_bytes, filename="test.csv", **form):
    data = {
        "mode": "meter_number",
        "duplicate_mode": "update",
        "file": (io.BytesIO(csv_bytes), filename),
        **form,
    }
    return client.post("/meters/import", data=data,
                       content_type="multipart/form-data",
                       follow_redirects=False)


def _cleanup_pickles(client):
    """Loescht alle Pickle-Files aus dem Session-State + instance/."""
    with client.session_transaction() as s:
        path = s.get("meter_import_file")
        s.pop("meter_import_file", None)
        s.pop("meter_import_cfg", None)
    if path and os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Login-Schutz
# ---------------------------------------------------------------------------

class TestLoginRequired:
    def test_upload_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/import", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_preview_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_result_requires_login(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Step 1: Upload
# ---------------------------------------------------------------------------

class TestUploadStep:
    def test_get_renders_step_1(self, client, admin, period):
        _login(client)
        r = client.get("/meters/import")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 1" in body
        assert "Zuordnungsmodus" in body
        assert "Zählernummer" in body
        assert "Kundennummer" in body
        assert "Kundenname" in body

    def test_post_without_file_flashes_warning(self, client, admin):
        _login(client)
        r = client.post("/meters/import", data={
            "mode": "meter_number",
            "duplicate_mode": "update",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/meters/import" in r.headers["Location"]

    def test_post_with_unsupported_format_flashes_error(self, client, admin):
        _login(client)
        r = client.post("/meters/import", data={
            "mode": "meter_number",
            "duplicate_mode": "update",
            "file": (io.BytesIO(b"x"), "evil.exe"),
        }, content_type="multipart/form-data", follow_redirects=False)
        assert r.status_code == 302

    def test_post_csv_redirects_to_preview(self, client, admin, sample):
        _login(client)
        r = _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100,5\n"))
        assert r.status_code == 302
        assert "/meters/import/preview" in r.headers["Location"]
        with client.session_transaction() as s:
            assert s.get("meter_import_file")
            assert s.get("meter_import_cfg") is not None
        _cleanup_pickles(client)

    def test_pickle_file_actually_created(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Nr;Stand\nZ-001;100\n"))
        with client.session_transaction() as s:
            path = s.get("meter_import_file")
        assert path and os.path.exists(path)
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Step 2: Preview
# ---------------------------------------------------------------------------

class TestPreviewStep:
    def test_get_without_session_redirects_to_upload(self, client, admin):
        _login(client)
        r = client.get("/meters/import/preview", follow_redirects=False)
        assert r.status_code == 302
        assert "/meters/import" in r.headers["Location"]

    def test_get_renders_table_with_resolved_rows(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100,5\n"))
        r = client.get("/meters/import/preview")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Schritt 2" in body
        assert "Z-001" in body
        assert "Mueller Hans" in body
        assert "table-success" in body
        assert "Vorschau aktualisieren" in body
        assert "Import ausführen" in body
        _cleanup_pickles(client)

    def test_preview_shows_not_found_red(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nXXX-not-existing;100\n"))
        r = client.get("/meters/import/preview")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "table-danger" in body
        assert "nicht gefunden" in body or "Nicht gemappt" in body
        _cleanup_pickles(client)

    def test_preview_shows_ambiguous_yellow(self, client, admin, period):
        _login(client)
        c = Customer(name="Multi", customer_number=99)
        db.session.add(c); db.session.flush()
        p1 = Property(object_number="P-1", object_type="Haus", ort="X")
        p2 = Property(object_number="P-2", object_type="Haus", ort="Y")
        db.session.add_all([p1, p2]); db.session.flush()
        db.session.add_all([
            PropertyOwnership(property_id=p1.id, customer_id=c.id,
                              valid_from=date(2020, 1, 1), valid_to=None),
            PropertyOwnership(property_id=p2.id, customer_id=c.id,
                              valid_from=date(2020, 1, 1), valid_to=None),
        ])
        db.session.add_all([
            WaterMeter(property_id=p1.id, meter_number="Z-A", meter_type="main"),
            WaterMeter(property_id=p2.id, meter_number="Z-B", meter_type="main"),
        ])
        db.session.commit()

        _upload(client, _csv("Kundennr;Stand\n99;100\n"), mode="customer_number")
        r = client.get("/meters/import/preview")
        body = r.get_data(as_text=True)
        assert "table-warning" in body
        assert "Mehrdeutig" in body or "mehrdeutig" in body
        assert "Z-A" in body
        assert "Z-B" in body
        _cleanup_pickles(client)

    def test_post_refresh_re_renders(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100\n"))
        r = client.post("/meters/import/preview", data={
            "action": "refresh",
            "mode": "meter_number",
            "col_lookup": "Nr",
            "col_value": "Stand",
            "billing_period_id": str(sample["period"].id),
            "duplicate_mode": "update",
            "value_format": "auto",
            "date_format": "auto",
        })
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Z-001" in body
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Confirm-Pfad (POST /preview mit action=confirm)
# ---------------------------------------------------------------------------

class TestConfirmStep:
    def _confirm(self, client, period, **rows_data):
        """rows_data: dict mit 'rows[N][feld]' keys."""
        data = {
            "action": "confirm",
            "mode": "meter_number",
            "col_lookup": "Nr",
            "col_value": "Stand",
            "billing_period_id": str(period.id),
            "duplicate_mode": "update",
            "value_format": "auto",
            "date_format": "auto",
            **rows_data,
        }
        return client.post("/meters/import/preview", data=data,
                           follow_redirects=False)

    def test_confirm_creates_reading(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100,5\n"))
        r = self._confirm(client, sample["period"], **{
            "rows[0][value]": "100,5",
            "rows[0][meter_id]": str(sample["meter"].id),
            "rows[0][date]": "2024-12-31",
        })
        assert r.status_code == 302
        assert "/meters/import/result" in r.headers["Location"]
        rd = MeterReading.query.filter_by(
            meter_id=sample["meter"].id,
            billing_period_id=sample["period"].id).one()
        assert rd.value == Decimal("100.5")
        assert rd.created_by_id == admin.id
        _cleanup_pickles(client)

    def test_confirm_clears_session_pickle(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100\n"))
        with client.session_transaction() as s:
            path = s.get("meter_import_file")
        assert os.path.exists(path)
        self._confirm(client, sample["period"], **{
            "rows[0][value]": "100",
            "rows[0][meter_id]": str(sample["meter"].id),
            "rows[0][date]": "2024-12-31",
        })
        assert not os.path.exists(path)
        with client.session_transaction() as s:
            assert "meter_import_file" not in s

    def test_confirm_skip_flag_skips_row(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100\n"))
        self._confirm(client, sample["period"], **{
            "rows[0][skip]": "on",
            "rows[0][value]": "100",
            "rows[0][meter_id]": str(sample["meter"].id),
            "rows[0][date]": "2024-12-31",
        })
        assert MeterReading.query.count() == 0

    def test_confirm_user_override_meter_works(self, client, admin, sample):
        _login(client)
        m2 = WaterMeter(property_id=sample["property"].id, meter_number="Z-002",
                        meter_type="sub", active=True)
        db.session.add(m2)
        db.session.commit()

        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100\n"))
        self._confirm(client, sample["period"], **{
            "rows[0][value]": "100",
            "rows[0][meter_id]": str(m2.id),  # User waehlt ANDEREN Meter
            "rows[0][date]": "2024-12-31",
        })
        assert MeterReading.query.filter_by(
            meter_id=m2.id, billing_period_id=sample["period"].id).count() == 1
        assert MeterReading.query.filter_by(
            meter_id=sample["meter"].id,
            billing_period_id=sample["period"].id).count() == 0

    def test_confirm_duplicate_mode_skip(self, client, admin, sample):
        _login(client)
        db.session.add(MeterReading(
            meter_id=sample["meter"].id, billing_period_id=sample["period"].id,
            value=Decimal("50"), reading_date=date(2024, 12, 31),
        ))
        db.session.commit()

        _upload(client, _csv("Nr;Stand\nZ-001;999\n"), duplicate_mode="skip")
        self._confirm(client, sample["period"], **{
            "duplicate_mode": "skip",
            "rows[0][value]": "999",
            "rows[0][meter_id]": str(sample["meter"].id),
            "rows[0][date]": "2024-12-31",
        })
        rd = MeterReading.query.filter_by(
            meter_id=sample["meter"].id,
            billing_period_id=sample["period"].id).one()
        assert rd.value == Decimal("50")


# ---------------------------------------------------------------------------
# Preview-Verbrauchsspalten
# ---------------------------------------------------------------------------

class TestPreviewConsumptionColumns:
    """Vorschau zeigt letzten Stand + berechneter Verbrauch + (optional)
    importierter Verbrauch mit Mismatch-Highlight.
    """

    def _prev_period(self):
        p = BillingPeriod(
            name="2023", start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31), active=False,
        )
        db.session.add(p)
        db.session.flush()
        return p

    def test_prior_and_computed_consumption_shown(self, client, admin, sample):
        m = sample["meter"]
        prev = self._prev_period()
        db.session.add(MeterReading(
            meter_id=m.id, billing_period_id=prev.id, value=Decimal("100"),
            reading_date=date(2023, 12, 31),
        ))
        db.session.commit()

        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;150\n"))
        r = client.get("/meters/import/preview")
        body = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "100" in body
        assert "Verbrauch" in body
        _cleanup_pickles(client)

    def test_imported_consumption_match_no_warning(self, client, admin, sample):
        m = sample["meter"]
        prev = self._prev_period()
        db.session.add(MeterReading(
            meter_id=m.id, billing_period_id=prev.id, value=Decimal("100"),
            reading_date=date(2023, 12, 31),
        ))
        db.session.commit()

        _login(client)
        _upload(client, _csv(
            "Zaehlernummer;Stand;Verbrauch\nZ-001;150;50\n"
        ))
        r = client.get("/meters/import/preview")
        body = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "Import: 50" in body or "Import: 50,00" in body
        assert "Abweichung vom berechneten Verbrauch" not in body
        _cleanup_pickles(client)

    def test_imported_consumption_mismatch_warning(self, client, admin, sample):
        m = sample["meter"]
        prev = self._prev_period()
        db.session.add(MeterReading(
            meter_id=m.id, billing_period_id=prev.id, value=Decimal("100"),
            reading_date=date(2023, 12, 31),
        ))
        db.session.commit()

        _login(client)
        _upload(client, _csv(
            "Zaehlernummer;Stand;Verbrauch\nZ-001;150;75\n"
        ))
        r = client.get("/meters/import/preview")
        body = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "Abweichung vom berechneten Verbrauch" in body
        assert "text-danger" in body
        _cleanup_pickles(client)

    def test_consumption_column_select_present(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100\n"))
        r = client.get("/meters/import/preview")
        body = r.get_data(as_text=True)
        assert 'name="col_consumption"' in body
        assert "kein Vergleich" in body
        _cleanup_pickles(client)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class TestResultStep:
    def test_result_without_stats_redirects(self, client, admin):
        _login(client)
        r = client.get("/meters/import/result", follow_redirects=False)
        assert r.status_code == 302
        assert "/meters/readings" in r.headers["Location"] \
               or "/meters/" in r.headers["Location"]

    def test_result_renders_after_confirm(self, client, admin, sample):
        _login(client)
        _upload(client, _csv("Zaehlernummer;Stand\nZ-001;100\n"))
        client.post("/meters/import/preview", data={
            "action": "confirm",
            "mode": "meter_number",
            "col_lookup": "Nr",
            "col_value": "Stand",
            "billing_period_id": str(sample["period"].id),
            "duplicate_mode": "update",
            "value_format": "auto",
            "date_format": "auto",
            "rows[0][value]": "100",
            "rows[0][meter_id]": str(sample["meter"].id),
            "rows[0][date]": "2024-12-31",
        }, follow_redirects=False)
        r = client.get("/meters/import/result")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Import abgeschlossen" in body
        assert "Neu angelegt" in body
