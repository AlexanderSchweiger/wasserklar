"""HTTP-Tests fuer das Ablese-Formular (``meters.add_reading`` GET).

Deckt ab:
- der "+"-Button (ohne ``period_id``) oeffnet einen NEUEN Stand, auch wenn die
  aktive Periode schon einen Stand hat (waehlt eine Periode ohne Stand);
- "Letzter Stand" ist der vom Datum her juengste Stand des Zaehlers, nicht der
  der Vorperiode;
- beim Bearbeiten eines alten Stands ist der Bezugswert der date-latest Stand
  und ``prevDate`` wird fuer den Obsolet-Check ans JS gereicht.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.meters.services import save_reading
from app.models import BillingPeriod, MeterReading, Property, User, WaterMeter
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def setup(app):
    """Zaehler mit Staenden 2023->100, 2024->175, aktive Periode 2025 mit
    230, plus leere Zukunftsperiode 2026."""
    p23 = BillingPeriod(name="2023", start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
    p24 = BillingPeriod(name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
    p25 = BillingPeriod(name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), active=True)
    p26 = BillingPeriod(name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
    db.session.add_all([p23, p24, p25, p26])
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    m = WaterMeter(property_id=prop.id, meter_number="Z-1", initial_value=Decimal("0"))
    db.session.add(m)
    db.session.flush()
    save_reading(m, p23, Decimal("100"), reading_date=date(2023, 12, 31))
    save_reading(m, p24, Decimal("175"), reading_date=date(2024, 12, 31))
    save_reading(m, p25, Decimal("230"), reading_date=date(2025, 12, 31))
    db.session.commit()
    return {"meter": m, "p23": p23, "p24": p24, "p25": p25, "p26": p26}


def _body(client, url):
    return client.get(url, headers={"HX-Request": "true"}).get_data(as_text=True)


class TestPlusButtonNewReading:
    def test_plus_opens_new_when_active_has_reading(self, client, admin, setup):
        """'+' (kein period_id): aktive Periode 2025 hat schon einen Stand ->
        es wird eine Periode ohne Stand (2026) vorausgewaehlt, ohne den letzten
        Stand zu laden (kein Loeschen-Button, Titel 'erfassen')."""
        _login(client)
        m = setup["meter"]
        b = _body(client, f"/meters/{m.id}/read")
        assert "/meters/reading/" not in b, "+ duerfte keinen bestehenden Stand (Loeschen-Button) laden"
        assert "erfassen" in b and "bearbeiten" not in b
        # leere Periode 2026 vorausgewaehlt
        import re
        assert re.search(rf'<option value="{setup["p26"].id}"[^>]*selected', b)

    def test_explicit_period_opens_edit(self, client, admin, setup):
        """Mit explizitem ?period_id einer Periode mit Stand -> Edit-Modus
        (Loeschen-Button da, Titel 'bearbeiten')."""
        _login(client)
        m = setup["meter"]
        b = _body(client, f"/meters/{m.id}/read?period_id={setup['p25'].id}")
        assert f"/meters/reading/" in b, "Edit-Modus sollte den Loeschen-Button zeigen"
        assert "bearbeiten" in b

    def test_plus_then_save_does_not_overwrite_last(self, client, admin, setup):
        """Kernbug-Regression: '+' (offene Periode 2026) + Speichern legt einen
        NEUEN Stand an, ohne den letzten (2025/230) zu ueberschreiben."""
        _login(client)
        m = setup["meter"]
        before = MeterReading.query.filter_by(meter_id=m.id).count()
        # '+' landet auf der leeren Periode 2026
        b = _body(client, f"/meters/{m.id}/read")
        import re
        sel = re.search(rf'<option value="{setup["p26"].id}"[^>]*selected', b)
        assert sel, "+ sollte die offene Periode 2026 vorauswaehlen"
        # Stand fuer 2026 speichern
        client.post(f"/meters/{m.id}/read", data={
            "billing_period_id": setup["p26"].id,
            "reading_date": "2026-06-01",
            "value": "300",
        })
        after = MeterReading.query.filter_by(meter_id=m.id).count()
        assert after == before + 1, "ein NEUER Stand, kein Ueberschreiben"
        # letzter Stand (2025) unveraendert
        r25 = MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=setup["p25"].id).one()
        assert r25.value == Decimal("230")

    def test_plus_no_open_period_shows_notice(self, client, admin):
        """Sind ALLE Perioden fuer den Zaehler abgelesen, zeigt '+' einen
        Hinweis (neue Periode anlegen) statt still den letzten Stand zu
        ueberschreiben."""
        _login(client)
        p24 = BillingPeriod(name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        p25 = BillingPeriod(name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), active=True)
        db.session.add_all([p24, p25])
        prop = Property(object_number="PX", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        m = WaterMeter(property_id=prop.id, meter_number="ZX", initial_value=Decimal("0"))
        db.session.add(m)
        db.session.flush()
        save_reading(m, p24, Decimal("100"), reading_date=date(2024, 6, 1))
        save_reading(m, p25, Decimal("180"), reading_date=date(2025, 6, 1))
        db.session.commit()
        b = _body(client, f"/meters/{m.id}/read")
        assert "Kein offener Abrechnungszeitraum" in b
        assert 'id="valueInput"' not in b, "kein Eingabeformular -> kein versehentliches Speichern"
        assert "/meters/reading/" not in b, "kein Loeschen-Button (es wird nichts geladen)"


class TestSwapTwoReadingsPerPeriod:
    """Zaehlertausch: zwei Staende in einer Periode sind erlaubt, weil sie auf
    ZWEI Zaehlern liegen (Constraint ist je Zaehler+Periode). Das '+' muss den
    neuen Zaehler in derselben Periode ablesbar lassen."""

    def test_new_meter_readable_same_period_after_swap(self, client, admin):
        _login(client)
        p25 = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                            end_date=date(2025, 12, 31), active=True)
        db.session.add(p25)
        prop = Property(object_number="PS", object_type="Haus")
        db.session.add(prop)
        db.session.flush()
        old = WaterMeter(property_id=prop.id, meter_number="ZA", active=False,
                         installed_to=date(2025, 6, 1), initial_value=Decimal("0"))
        new = WaterMeter(property_id=prop.id, meter_number="ZB", active=True,
                         installed_from=date(2025, 6, 1), initial_value=Decimal("0"))
        db.session.add_all([old, new])
        db.session.flush()
        # Abschlussablesung des alten Zaehlers in 2025
        save_reading(old, p25, Decimal("150"), reading_date=date(2025, 6, 1))
        db.session.commit()

        # '+' auf den NEUEN Zaehler -> Neuanlage in 2025 (kein Hinweis), obwohl
        # der alte Zaehler in 2025 schon einen Stand hat.
        import re
        b = _body(client, f"/meters/{new.id}/read")
        assert "Kein offener Abrechnungszeitraum" not in b
        assert 'id="valueInput"' in b, "neuer Zaehler muss ablesbar sein"
        assert re.search(rf'<option value="{p25.id}"[^>]*selected', b)

        # neuen Stand speichern -> beide Zaehler haben nun einen Stand in 2025
        client.post(f"/meters/{new.id}/read", data={
            "billing_period_id": p25.id, "reading_date": "2025-12-31", "value": "40"})
        assert MeterReading.query.filter_by(billing_period_id=p25.id).count() == 2
        assert MeterReading.query.filter_by(meter_id=old.id, billing_period_id=p25.id).one().value == Decimal("150")


class TestLetzterStandDateBased:
    def test_new_reading_prev_is_date_latest(self, client, admin, setup):
        # Neuer Stand (2026): letzter Stand = date-latest = 230 vom 31.12.2025.
        _login(client)
        m = setup["meter"]
        b = _body(client, f"/meters/{m.id}/read")
        assert "var prevValue = 230" in b
        assert 'var prevDate = "2025-12-31"' in b
        assert "31.12.2025" in b

    def test_edit_latest_prev_is_one_before(self, client, admin, setup):
        # Bearbeiten des juengsten Stands (2025): Bezug = 2024 (175), self ausgenommen.
        _login(client)
        m = setup["meter"]
        b = _body(client, f"/meters/{m.id}/read?period_id={setup['p25'].id}")
        assert "var prevValue = 175" in b
        assert 'var prevDate = "2024-12-31"' in b

    def test_edit_old_baseline_is_latest_for_obsolete_guard(self, client, admin, setup):
        # Bearbeiten eines alten Stands (2023): Bezug = date-latest (230/2025).
        # reading_date 2023 < prevDate 2025 -> Client blendet die Vorschau aus.
        _login(client)
        m = setup["meter"]
        b = _body(client, f"/meters/{m.id}/read?period_id={setup['p23'].id}")
        assert 'var prevDate = "2025-12-31"' in b
