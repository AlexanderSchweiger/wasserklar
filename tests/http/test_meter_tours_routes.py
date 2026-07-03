"""HTTP-Tests fuer das meter_tours-Blueprint (Feature-Gate, Permissions,
Tour-Lebenszyklus, Pauschalen-Rechnung, Ankuendigung)."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Customer, Invoice, MeterTour, MeterTourStop, Property, PropertyOwnership,
    User, WaterMeter,
)
from tests.conftest import _ensure_role


def _login(client, username, password):
    return client.post("/auth/login",
                       data={"username": username, "password": password})


@pytest.fixture
def admin_user(app):
    role = _ensure_role("Admin")
    u = User(username="touradmin", email="touradmin@test.com", role_id=role.id)
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def zaehler_user(app):
    """User mit NUR dem zaehler-Recht (kein rechnungen_op)."""
    role = _ensure_role("Wasserwart", perms=("zaehler",))
    u = User(username="wasserwart", email="ww@test.com", role_id=role.id)
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def stammdaten_user(app):
    role = _ensure_role("Buero", perms=("stammdaten",))
    u = User(username="buero", email="buero@test.com", role_id=role.id)
    u.set_password("test")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def tours_enabled(app):
    """Feature-Flag einschalten (wird pro Request geprueft — Laufzeit-Toggle)."""
    old = app.config.get("FEATURE_METER_TOURS")
    app.config["FEATURE_METER_TOURS"] = True
    yield
    app.config["FEATURE_METER_TOURS"] = old


def _seed_due_meter(object_number="P1", meter_number="M1", lat=48.001, lng=16.0,
                    eichjahr=2019):
    prop = Property(object_number=object_number, object_type="Haus",
                    strasse="Teststraße", hausnummer="1", plz="1234",
                    ort="Testdorf", lat=lat, lng=lng)
    db.session.add(prop)
    db.session.flush()
    m = WaterMeter(meter_number=meter_number, property_id=prop.id,
                   eichjahr=eichjahr)
    db.session.add(m)
    db.session.commit()
    return prop, m


def _owner(prop, name="Huber Anna", email=None, rechnung_per_email=False):
    c = Customer(name=name, email=email, rechnung_per_email=rechnung_per_email)
    db.session.add(c)
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=c.id,
                                     valid_from=date(2020, 1, 1)))
    db.session.commit()
    return c


def _create_tour(client, meter_ids, **extra):
    data = {"name": "Test-Tour", "meter_ids": [str(i) for i in meter_ids]}
    data.update(extra)
    return client.post("/meters/tours/", data=data, follow_redirects=False)


class TestFeatureGate:
    def test_all_routes_404_when_flag_off(self, client, admin_user):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        # OSS-Standalone-Default: Flag aus -> Routen existieren nicht.
        assert client.get("/meters/tours/due").status_code == 404
        assert client.get("/meters/tours/").status_code == 404
        assert client.post("/meters/tours/1/start").status_code == 404

    def test_due_page_renders_when_enabled(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        _seed_due_meter()
        r = client.get("/meters/tours/due")
        assert r.status_code == 200
        assert "M1".encode() in r.data


class TestPermissions:
    def test_requires_zaehler_permission(self, client, stammdaten_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "buero", "test")
        r = client.get("/meters/tours/due", follow_redirects=False)
        assert r.status_code == 302
        assert "/meters/tours" not in (r.headers.get("Location") or "")

    def test_zaehler_user_has_access(self, client, zaehler_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "wasserwart", "test")
        assert client.get("/meters/tours/due").status_code == 200

    def test_stop_invoice_requires_rechnungen(self, client, zaehler_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "wasserwart", "test")
        prop, m = _seed_due_meter()
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        stop = tour.stops[0]
        r = client.get(f"/meters/tours/{tour.id}/stops/{stop.id}/invoice",
                       follow_redirects=False)
        assert r.status_code == 302   # Redirect Dashboard (kein rechnungen_op)


class TestTourLifecycle:
    def test_create_orders_stops(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        p1, m1 = _seed_due_meter("P1", "M1", lat=48.001)
        p2, m2 = _seed_due_meter("P2", "M2", lat=48.010)
        p3, m3 = _seed_due_meter("P3", "M3", lat=None, lng=None)
        r = _create_tour(client, [m2.id, m3.id, m1.id],
                         start_lat="48.0", start_lng="16.0")
        assert r.status_code == 302
        tour = MeterTour.query.first()
        by_pos = {s.position: s.meter_id for s in tour.stops}
        # Geocodete nach Naehe, nicht geocodeter (M3) ans Ende.
        assert by_pos == {1: m1.id, 2: m2.id, 3: m3.id}

    def test_create_without_selection_flashes(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        r = _create_tour(client, [])
        assert r.status_code == 302
        assert MeterTour.query.count() == 0

    def test_stop_status_transitions(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        stop = tour.stops[0]
        base = f"/meters/tours/{tour.id}/stops/{stop.id}/status"

        r = client.post(base, data={"status": "skipped", "skip_reason": "niemand da"})
        assert r.status_code == 200
        db.session.refresh(stop)
        assert stop.status == MeterTourStop.STATUS_SKIPPED
        assert stop.skip_reason == "niemand da"
        assert stop.completed_at is not None

        r = client.post(base, data={"status": "pending"})
        db.session.refresh(stop)
        assert stop.status == MeterTourStop.STATUS_PENDING
        assert stop.completed_at is None

        assert client.post(base, data={"status": "quatsch"}).status_code == 400

        # Erledigte Stops sind fixiert.
        stop.status = MeterTourStop.STATUS_DONE
        db.session.commit()
        assert client.post(base, data={"status": "skipped"}).status_code == 409

    def test_stop_move_swaps_positions(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        p1, m1 = _seed_due_meter("P1", "M1", lat=48.001)
        p2, m2 = _seed_due_meter("P2", "M2", lat=48.010)
        p3, m3 = _seed_due_meter("P3", "M3", lat=48.005)
        _create_tour(client, [m1.id, m2.id, m3.id],
                     start_lat="48.0", start_lng="16.0")
        tour = MeterTour.query.first()
        # Auto-Reihenfolge: M1(1), M3(2), M2(3)
        second = next(s for s in tour.stops if s.position == 2)

        r = client.post(f"/meters/tours/{tour.id}/stops/{second.id}/move",
                        data={"direction": "up"})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        db.session.expire_all()
        assert second.position == 1

        # Am oberen Rand: kein Nachbar -> ok False, Position bleibt.
        r = client.post(f"/meters/tours/{tour.id}/stops/{second.id}/move",
                        data={"direction": "up"})
        assert r.get_json()["ok"] is False
        db.session.expire_all()
        assert second.position == 1

        r = client.post(f"/meters/tours/{tour.id}/stops/{second.id}/move",
                        data={"direction": "down"})
        assert r.get_json()["ok"] is True
        db.session.expire_all()
        assert second.position == 2

        assert client.post(
            f"/meters/tours/{tour.id}/stops/{second.id}/move",
            data={"direction": "sideways"}).status_code == 400

    def test_close_returns_meters_to_due_list(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        r = client.get("/meters/tours/due")
        assert b"M1" not in r.data   # in offener Tour -> ausgeblendet
        client.post(f"/meters/tours/{tour.id}/close")
        db.session.refresh(tour)
        assert tour.status == MeterTour.STATUS_DONE
        r = client.get("/meters/tours/due")
        assert b"M1" in r.data       # offener Stop wieder faellig

    def test_delete_only_planned_or_cancelled(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        client.post(f"/meters/tours/{tour.id}/start")
        client.post(f"/meters/tours/{tour.id}/delete")
        assert MeterTour.query.count() == 1   # aktiv -> nicht loeschbar
        client.post(f"/meters/tours/{tour.id}/cancel")
        client.post(f"/meters/tours/{tour.id}/delete")
        assert MeterTour.query.count() == 0
        assert MeterTourStop.query.count() == 0   # Cascade


class TestStopInvoice:
    def test_creates_draft_invoice_with_open_item_deferred(
            self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        customer = _owner(prop)
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        stop = tour.stops[0]
        r = client.post(
            f"/meters/tours/{tour.id}/stops/{stop.id}/invoice",
            data={"customer_id": customer.id, "description": "Zählertausch-Pauschale",
                  "amount": "60,00", "tax_rate": "10"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        inv = db.session.get(Invoice, data["invoice_id"])
        assert inv.status == Invoice.STATUS_DRAFT
        assert inv.total_amount == Decimal("66.00")
        assert inv.open_item is None   # OP entsteht erst beim Versand/Statuswechsel
        db.session.refresh(stop)
        assert stop.invoice_id == inv.id

    def test_second_invoice_conflicts(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        customer = _owner(prop)
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        stop = tour.stops[0]
        url = f"/meters/tours/{tour.id}/stops/{stop.id}/invoice"
        payload = {"customer_id": customer.id, "amount": "60", "tax_rate": ""}
        assert client.post(url, data=payload).status_code == 200
        assert client.post(url, data=payload).status_code == 409

    def test_invalid_amount_rejected(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        customer = _owner(prop)
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        stop = tour.stops[0]
        r = client.post(
            f"/meters/tours/{tour.id}/stops/{stop.id}/invoice",
            data={"customer_id": customer.id, "amount": "abc"})
        assert r.status_code == 400


class TestNotify:
    def test_refuses_customer_without_wants_email(self, client, admin_user,
                                                  tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        # E-Mail vorhanden, aber rechnung_per_email aus -> wants_email False.
        customer = _owner(prop, email="anna@test.com", rechnung_per_email=False)
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        r = client.post(f"/meters/tours/{tour.id}/notify/send",
                        data={"customer_id": customer.id,
                              "subject": "S", "body": "B"})
        assert r.status_code == 400
        assert "nicht aktiviert" in r.get_json()["error"]

    def test_sends_and_marks_notified(self, client, admin_user, tours_enabled,
                                      monkeypatch):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        customer = _owner(prop, email="anna@test.com", rechnung_per_email=True)
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()

        sent = {}
        from app.meter_tours import services as tours_svc

        def fake_send(cust, subject, body, channel="email"):
            sent["to"] = cust.email
            sent["subject"] = subject
            sent["body"] = body

        monkeypatch.setattr(tours_svc, "send_stop_notification", fake_send)
        r = client.post(
            f"/meters/tours/{tour.id}/notify/send",
            data={"customer_id": customer.id,
                  "subject": "Zählertausch am {datum}",
                  "body": "{anrede}\nZähler {zaehlernummer} an {objekt}."})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert sent["to"] == "anna@test.com"
        assert "M1" in sent["body"]
        stop = tour.stops[0]
        db.session.refresh(stop)
        assert stop.notified_at is not None

    def test_notify_page_lists_recipients(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        _owner(prop, name="Huber Anna", email="anna@test.com",
               rechnung_per_email=True)
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        r = client.get(f"/meters/tours/{tour.id}/notify")
        assert r.status_code == 200
        assert "Huber Anna".encode() in r.data


class TestDetailPages:
    def test_detail_and_batch_render(self, client, admin_user, tours_enabled):
        client.get("/auth/logout")
        _login(client, "touradmin", "test")
        prop, m = _seed_due_meter()
        _owner(prop, email="anna@test.com")
        _create_tour(client, [m.id])
        tour = MeterTour.query.first()
        r = client.get(f"/meters/tours/{tour.id}")
        assert r.status_code == 200
        assert b"tourQrModal" in r.data          # Handy-Einstieg (QR-Dialog)
        r = client.get(f"/meters/tours/{tour.id}/batch")
        assert r.status_code == 200
        assert b"tour-qr" in r.data              # QR im Zettel-Druckkopf
        r = client.get(f"/meters/tours/{tour.id}/stops.json")
        assert r.status_code == 200
        payload = r.get_json()
        assert payload["stops"][0]["meter_number"] == "M1"
        assert payload["stops"][0]["lat"] == prop.lat
