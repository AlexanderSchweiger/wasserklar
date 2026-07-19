"""HTTP-Tests fuer die Perioden-Gesamtuebersicht (``/invoices/period/<id>``).

Hintergrund: Werden in einer Abrechnungsperiode mehrere Rechnungslaeufe
gefahren (z.B. weil beim ersten Lauf noch nicht alle Zaehlerstaende da waren),
zeigt keine Lauf-Detailseite mehr den Gesamtstand. Der Lauf bleibt bewusst das
unveraenderliche Protokoll eines Versand-Vorgangs — diese Seite ist die
Bestandssicht darueber, und muss deshalb auch die Rechnungen erfassen, die in
gar keinem Lauf stecken (Einzel- und Schlussrechnungen).
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    BillingPeriod, BillingRun, Customer, Invoice, InvoiceItem, MeterReading,
    Property, PropertyOwnership, User, WaterMeter,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    client.get("/auth/logout")
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


def _mk_run(period, when):
    run = BillingRun(
        created_at=when, billing_period_id=period.id, tariff_name="Standard",
        tariff_base_fee=Decimal("60"), tariff_base_fee_label="Grundgebühr",
        tariff_additional_fee_label="Zusatzgebühr",
        tariff_price_per_m3=Decimal("1.5"))
    db.session.add(run)
    db.session.flush()
    return run


def _mk_invoice(number, cust, period_id, *, run=None, status=Invoice.STATUS_SENT,
                kind=Invoice.KIND_STANDARD, amount="100", when=date(2025, 7, 1)):
    inv = Invoice(
        invoice_number=number, customer_id=cust.id, billing_period_id=period_id,
        billing_run_id=run.id if run else None, invoice_kind=kind,
        date=when, status=status, total_amount=Decimal(amount))
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Wasserverbrauch", quantity=Decimal("10"),
        unit="m³", unit_price=Decimal("1.5"), amount=Decimal(amount)))
    return inv


@pytest.fixture
def period_with_two_runs(app):
    """Periode mit zwei Laeufen + einer Schlussrechnung + einer Rechnung ohne
    ``billing_period_id`` (Altbestand, nur ueber das Datum zuordenbar)."""
    period = BillingPeriod(
        name="2025/26", start_date=date(2025, 6, 1), end_date=date(2026, 5, 31),
        active=True)
    db.session.add(period)
    db.session.flush()

    custs = []
    for i in range(4):
        c = Customer(name=f"Kunde {i}", customer_number=i + 1)
        db.session.add(c)
        custs.append(c)
    db.session.flush()

    run1 = _mk_run(period, datetime(2026, 1, 12, 9, 0))
    run2 = _mk_run(period, datetime(2026, 3, 4, 11, 0))   # Nachzuegler-Lauf

    _mk_invoice("2026-00001", custs[0], period.id, run=run1,
                status=Invoice.STATUS_PAID, amount="100")
    _mk_invoice("2026-00002", custs[1], period.id, run=run1,
                status=Invoice.STATUS_SENT, amount="200")
    _mk_invoice("2026-00003", custs[2], period.id, run=run2,
                status=Invoice.STATUS_SENT, amount="300")
    _mk_invoice("2026-00004", custs[3], period.id, run=None,
                kind=Invoice.KIND_FINAL_SETTLEMENT, status=Invoice.STATUS_PAID,
                amount="50")
    # Ohne Periode gespeichert — muss ueber das Rechnungsdatum reingezogen werden
    _mk_invoice("2026-00005", custs[0], None, run=None,
                status=Invoice.STATUS_DRAFT, amount="25", when=date(2026, 2, 20))
    db.session.commit()
    return period


class TestPeriodOverview:
    def test_requires_login(self, client, period_with_two_runs):
        client.get("/auth/logout")
        r = client.get(f"/invoices/period/{period_with_two_runs.id}",
                       follow_redirects=False)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_unknown_period_404(self, client, admin):
        _login(client)
        assert client.get("/invoices/period/9999").status_code == 404

    def test_lists_invoices_from_all_runs(self, client, admin, period_with_two_runs):
        """Der eigentliche Zweck: lauf-uebergreifende Sicht."""
        _login(client)
        html = client.get(
            f"/invoices/period/{period_with_two_runs.id}").get_data(as_text=True)
        for nr in ("2026-00001", "2026-00002", "2026-00003"):
            assert nr in html

    def test_includes_invoices_outside_any_run(self, client, admin, period_with_two_runs):
        """Schlussrechnung (Eigentuemerwechsel) und Einzelrechnung haengen an
        keinem Lauf — genau die fehlen in jeder Lauf-Detailseite."""
        _login(client)
        html = client.get(
            f"/invoices/period/{period_with_two_runs.id}").get_data(as_text=True)
        assert "2026-00004" in html
        assert 'data-herkunft="final"' in html

    def test_includes_invoice_without_period_via_date(self, client, admin,
                                                      period_with_two_runs):
        """Altbestand ohne ``billing_period_id`` wird ueber das Rechnungsdatum
        in die Periode gezogen, sonst waere die Uebersicht unvollstaendig."""
        _login(client)
        html = client.get(
            f"/invoices/period/{period_with_two_runs.id}").get_data(as_text=True)
        assert "2026-00005" in html
        assert 'data-herkunft="single"' in html

    def test_sums_exclude_cancelled(self, client, admin, period_with_two_runs):
        """Gesamtsumme = Bezahlt + Offen; Stornierte sind gegenstandslos.

        Die stornierte Rechnung bleibt in der Tabelle sichtbar (man will sie ja
        finden) und wird unter „Weitere Status" ausgewiesen — sie darf nur die
        Summen nicht verfaelschen. Gleiches Verhalten wie die Lauf-Detailseite.
        """
        cust = Customer.query.first()
        _mk_invoice("2026-09999", cust, period_with_two_runs.id,
                    status=Invoice.STATUS_CANCELLED, amount="999")
        db.session.commit()

        _login(client)
        html = client.get(
            f"/invoices/period/{period_with_two_runs.id}").get_data(as_text=True)
        # 100 + 200 + 300 + 50 + 25 = 675, die stornierten 999 bleiben draussen
        assert "675,00" in html
        # ... aber die Zeile ist da und der Status wird gesondert gezaehlt
        assert "2026-09999" in html
        assert "Storniert: 1" in html

    def test_invoice_of_other_period_not_listed(self, client, admin,
                                                period_with_two_runs):
        other = BillingPeriod(
            name="2024/25", start_date=date(2024, 6, 1), end_date=date(2025, 5, 31))
        db.session.add(other)
        db.session.flush()
        _mk_invoice("2025-00042", Customer.query.first(), other.id,
                    when=date(2024, 9, 1))
        db.session.commit()

        _login(client)
        html = client.get(
            f"/invoices/period/{period_with_two_runs.id}").get_data(as_text=True)
        assert "2025-00042" not in html


class TestMenuEntryPoint:
    """Der Menuepunkt „Jahresuebersicht" haengt an keiner festen Perioden-ID,
    sondern loest zur Laufzeit auf die aktive Periode auf."""

    def test_redirects_to_active_period(self, client, admin, period_with_two_runs):
        _login(client)
        r = client.get("/invoices/period", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["Location"].endswith(f"/invoices/period/{period_with_two_runs.id}")

    def test_falls_back_to_newest_when_none_active(self, client, admin,
                                                   period_with_two_runs):
        period_with_two_runs.active = False
        db.session.commit()
        _login(client)
        r = client.get("/invoices/period", follow_redirects=False)
        assert r.status_code == 302
        assert f"/invoices/period/{period_with_two_runs.id}" in r.headers["Location"]

    def test_without_any_period_redirects_to_periods(self, client, admin):
        _login(client)
        r = client.get("/invoices/period", follow_redirects=False)
        assert r.status_code == 302
        assert "/perioden" in r.headers["Location"]


@pytest.fixture
def completeness_period(app):
    """Vier Objekte, je eines pro Vollstaendigkeits-Zustand."""
    period = BillingPeriod(
        name="2025/26", start_date=date(2025, 6, 1), end_date=date(2026, 5, 31),
        active=True)
    db.session.add(period)
    db.session.flush()

    def mk(nr, *, owner=True, reading=True):
        p = Property(object_number=nr, object_type="Haus", strasse="Weg",
                     hausnummer="1", active=True)
        db.session.add(p)
        db.session.flush()
        if owner:
            c = Customer(name=f"Eigentuemer {nr}")
            db.session.add(c)
            db.session.flush()
            db.session.add(PropertyOwnership(
                property_id=p.id, customer_id=c.id, valid_from=date(2020, 1, 1)))
        m = WaterMeter(property_id=p.id, meter_number=f"M-{nr}", active=True)
        db.session.add(m)
        db.session.flush()
        if reading:
            db.session.add(MeterReading(
                meter_id=m.id, billing_period_id=period.id,
                reading_date=date(2026, 5, 30), value=Decimal("100")))
        return p

    props = {
        "billed": mk("OBJ-A"),
        "ready": mk("OBJ-B"),
        "no_reading": mk("OBJ-C", reading=False),
        "no_owner": mk("OBJ-D", owner=False),
    }
    db.session.flush()

    own = PropertyOwnership.query.filter_by(
        property_id=props["billed"].id).first()
    inv = Invoice(
        invoice_number="2026-00001", customer_id=own.customer_id,
        property_id=props["billed"].id, billing_period_id=period.id,
        invoice_kind=Invoice.KIND_STANDARD, date=date(2026, 6, 1),
        status=Invoice.STATUS_SENT, total_amount=Decimal("150"))
    db.session.add(inv)
    db.session.commit()
    return period, props


class TestPeriodCompleteness:
    """Der Vollstaendigkeits-Block muss die Auswahl-Logik des Rechnungslaufs
    spiegeln — sonst zeigt er Zahlen, die der naechste Lauf nicht reproduziert."""

    def test_classifies_three_buckets(self, app, completeness_period):
        from app.invoices.routes import _period_completeness
        period, props = completeness_period
        c = _period_completeness(period)

        assert [e["prop"].id for e in c["ready"]] == [props["ready"].id]
        assert [e["prop"].id for e in c["no_reading"]] == [props["no_reading"].id]
        assert [e["prop"].id for e in c["no_owner"]] == [props["no_owner"].id]
        assert c["billed_count"] == 1
        assert c["total_count"] == 4

    def test_billed_property_is_not_open(self, app, completeness_period):
        from app.invoices.routes import _period_completeness
        period, props = completeness_period
        c = _period_completeness(period)
        open_ids = {e["prop"].id
                    for k in ("ready", "no_owner", "no_reading") for e in c[k]}
        assert props["billed"].id not in open_ids

    def test_cancelled_invoice_reopens_property(self, app, completeness_period):
        """Wie im Lauf: eine stornierte Rechnung blockiert nicht, das Objekt
        muss wieder als abzurechnen erscheinen."""
        from app.invoices.routes import _period_completeness
        period, props = completeness_period
        inv = Invoice.query.filter_by(property_id=props["billed"].id).one()
        inv.status = Invoice.STATUS_CANCELLED
        db.session.commit()

        c = _period_completeness(period)
        assert props["billed"].id in {e["prop"].id for e in c["ready"]}
        assert c["billed_count"] == 0

    def test_final_settlement_does_not_count_as_billed(self, app,
                                                       completeness_period):
        """Eine Schlussrechnung blockiert den Lauf nicht — sie darf das Objekt
        also auch hier nicht als erledigt markieren."""
        from app.invoices.routes import _period_completeness
        period, props = completeness_period
        own = PropertyOwnership.query.filter_by(
            property_id=props["ready"].id).first()
        db.session.add(Invoice(
            invoice_number="2026-07777", customer_id=own.customer_id,
            property_id=props["ready"].id, billing_period_id=period.id,
            invoice_kind=Invoice.KIND_FINAL_SETTLEMENT, date=date(2026, 2, 1),
            status=Invoice.STATUS_SENT, total_amount=Decimal("40")))
        db.session.commit()

        c = _period_completeness(period)
        assert props["ready"].id in {e["prop"].id for e in c["ready"]}

    def test_block_renders_only_open_properties(self, client, admin,
                                                completeness_period):
        period, props = completeness_period
        _login(client)
        html = client.get(
            f"/invoices/period/{period.id}").get_data(as_text=True)
        card = html[html.index("Vollständigkeit"):
                    html.index("Wie kamen die Rechnungen zustande?")]
        assert "OBJ-A" not in card       # abgerechnet
        for nr in ("OBJ-B", "OBJ-C", "OBJ-D"):
            assert nr in card
        assert "3 offen" in card


class TestBillingRunDetailUnaffected:
    """Der Refactor (`_invoice_overview` geteilt) darf die Lauf-Sicht nicht
    veraendern — sie zeigt weiterhin NUR die Rechnungen ihres Laufs."""

    def test_run_detail_shows_only_own_invoices(self, client, admin,
                                                period_with_two_runs):
        _login(client)
        run1 = BillingRun.query.order_by(BillingRun.created_at).first()
        html = client.get(
            f"/invoices/billing-runs/{run1.id}").get_data(as_text=True)
        assert "2026-00001" in html
        assert "2026-00002" in html
        assert "2026-00003" not in html   # gehoert zum zweiten Lauf
        assert "2026-00004" not in html   # Schlussrechnung, kein Lauf
        # Summe nur ueber den eigenen Lauf: 100 + 200
        assert "300,00" in html
