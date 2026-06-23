"""HTTP-Tests fuer das (modal-faehige) Buchungs-Anlage-/Bearbeiten-Formular
(``accounting.booking_new`` / ``accounting.booking_edit``).

Schwerpunkt: Robustheit. Jede ungueltige Eingabe muss eine saubere
Formular-Antwort liefern (kein 500), gueltige Eingaben legen genau eine Buchung
an, und das HX-/Modal-Verhalten (Fragment, ``HX-Trigger: booking-saved``)
stimmt. Verbuchte Buchungen bleiben in Datum/Betrag/Steuer gesperrt.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, FiscalYear, Project,
    RealAccount, User,
)
from tests.conftest import _ensure_role

HX = {"HX-Request": "true"}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.com", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")  # Werkzeug-3 CookieJar-Workaround
    return client.post("/auth/login", data={"username": username, "password": password})


@pytest.fixture
def fixtures(app):
    """Konten, Projekt (mit Farbe), Kontakt, Bankkonto + ein OFFENES Buchungsjahr
    fuer das aktuelle Jahr (sonst greift die open_fiscal_year-Pruefung)."""
    today = date.today()
    fy = FiscalYear(year=today.year, start_date=date(today.year, 1, 1),
                    end_date=date(today.year, 12, 31), closed=False)
    acc1 = Account(name="Wassereinnahmen", code="W01")
    acc2 = Account(name="Materialaufwand", code="M01")
    proj = Project(name="Sanierung 2026", color="#e74c3c")
    cust = Customer(name="Bestandskunde")
    ra = RealAccount(name="Girokonto", iban="AT001", opening_balance=Decimal("0"),
                     is_default=True)
    db.session.add_all([fy, acc1, acc2, proj, cust, ra])
    db.session.commit()
    return {"acc1": acc1, "acc2": acc2, "proj": proj, "cust": cust, "ra": ra}


def _valid_payload(fixtures, **overrides):
    data = {
        "date": date.today().isoformat(),
        "amount": "-145.00",
        "tax_rate": "20",
        "account_id": fixtures["acc1"].id,
        "project_id": "",
        "description": "Rechnung Baumarkt",
        "reference": "BM-1",
        "customer_id": "",
        "real_account_id": fixtures["ra"].id,
        "action": "",
    }
    data.update(overrides)
    return data


def _booking_count():
    return Booking.query.count()


# --------------------------------------------------------------------------- #
# GET — Fragment vs. Vollseite
# --------------------------------------------------------------------------- #

def test_get_returns_fragment_on_hx(client, admin, fixtures):
    _login(client)
    r = client.get("/accounting/bookings/new", headers=HX)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="booking-form"' in body
    assert 'name="account_id"' in body
    # Fragment → KEINE Vollseiten-Chrome (Sidebar).
    assert "navbar-vertical" not in body


def test_get_returns_full_page_without_hx(client, admin, fixtures):
    _login(client)
    r = client.get("/accounting/bookings/new")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="booking-form"' in body
    assert "navbar-vertical" in body  # Vollseite mit Sidebar


def test_project_color_rendered_as_data_attr(client, admin, fixtures):
    _login(client)
    r = client.get("/accounting/bookings/new", headers=HX)
    assert 'data-color="#e74c3c"' in r.get_data(as_text=True)


def test_tax_rate_is_native_select(client, admin, fixtures):
    """Steuersatz ist bewusst KEIN tom-select (nativ, tastaturrobust)."""
    _login(client)
    body = client.get("/accounting/bookings/new", headers=HX).get_data(as_text=True)
    # Das tax_rate-Select traegt nicht die tom-select-Klasse.
    assert 'name="tax_rate" id="tax_rate" class="form-select"' in body


def test_contact_field_offers_inline_create(client, admin, fixtures):
    """Kontakt-Feld erlaubt Inline-Anlage via data-create-url; kein Modal mehr."""
    _login(client)
    body = client.get("/accounting/bookings/new", headers=HX).get_data(as_text=True)
    assert 'id="booking-customer-select"' in body
    assert 'data-create-url=' in body
    # Der alte "Neu"-Button / das Quick-Create-Modal sind aus dem Flow entfernt.
    assert "customerQuickCreateModal" not in body


def test_inline_contact_create_contract(client, admin, fixtures):
    """Vertrag, auf den der TomSelect-Create-Handler baut (data-create-type=supplier):
    name+force+is_supplier legt einen Lieferanten mit leeren Adressdaten an und
    liefert {ok, id, name}; der Kontakt ist sofort an einer Buchung verwendbar."""
    _login(client)
    r = client.post(
        "/customers/quick-create",
        data={"name": "Spontan Lieferant", "force": "1", "is_supplier": "1"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True and data["name"] == "Spontan Lieferant"
    c = db.session.get(Customer, data["id"])
    assert c is not None
    assert c.is_supplier is True
    assert (c.strasse or "") == ""   # leere Adressdaten — wie gewünscht
    # Und dieser Kontakt ist danach an einer Buchung verwendbar.
    r2 = client.post("/accounting/bookings/new",
                     data=_valid_payload(fixtures, customer_id=c.id), headers=HX)
    assert "booking-saved" in r2.headers.get("HX-Trigger", "")
    assert Booking.query.one().customer_id == c.id


# --------------------------------------------------------------------------- #
# POST (HX) — Erfolgsfaelle
# --------------------------------------------------------------------------- #

def test_post_creates_booking_and_triggers(client, admin, fixtures):
    _login(client)
    r = client.post("/accounting/bookings/new", data=_valid_payload(fixtures), headers=HX)
    assert r.status_code == 200
    assert "booking-saved" in r.headers.get("HX-Trigger", "")
    assert _booking_count() == 1
    b = Booking.query.one()
    assert b.amount == Decimal("-145.00")
    assert b.account_id == fixtures["acc1"].id
    assert b.description == "Rechnung Baumarkt"
    assert b.real_account_id == fixtures["ra"].id
    assert b.tax_rate == Decimal("20")


def test_post_weiteres_signals_reopen(client, admin, fixtures):
    """„weiteres" legt an und liefert nur einen Trigger mit Sticky-Datum und
    -Bankkonto; das Formular kommt NICHT zurueck (Client schliesst+oeffnet neu),
    damit keine doppelten TomSelects durch In-Place-Swaps entstehen."""
    _login(client)
    r = client.post("/accounting/bookings/new",
                    data=_valid_payload(fixtures, action="weiteres"), headers=HX)
    assert r.status_code == 200
    trig = r.headers.get("HX-Trigger", "")
    assert '"action": "weiteres"' in trig
    assert '"date"' in trig                        # Sticky-Datum für den Reopen
    assert str(fixtures["ra"].id) in trig          # Sticky-Bankkonto
    assert _booking_count() == 1
    # Platzhalter statt Formular im Body.
    assert 'id="booking-form"' not in r.get_data(as_text=True)


def test_get_honors_sticky_real_account(client, admin, fixtures):
    """Reopen nach „weiteres": real_account_id-Param wird vorausgewählt
    (auch wenn es nicht das Default-Bankkonto ist)."""
    _login(client)
    ra2 = RealAccount(name="Sparkonto", opening_balance=Decimal("0"), is_default=False)
    db.session.add(ra2)
    db.session.commit()
    r = client.get(f"/accounting/bookings/new?real_account_id={ra2.id}", headers=HX)
    body = r.get_data(as_text=True)
    assert ('value="%s" selected' % ra2.id) in body
    assert ('value="%s" selected' % fixtures["ra"].id) not in body  # Default nicht vorgewählt


def test_post_save_signals_save_action(client, admin, fixtures):
    _login(client)
    r = client.post("/accounting/bookings/new",
                    data=_valid_payload(fixtures, action=""), headers=HX)
    assert '"action": "save"' in r.headers.get("HX-Trigger", "")


def test_tax_rate_zero_stored_as_null(client, admin, fixtures):
    _login(client)
    client.post("/accounting/bookings/new",
                data=_valid_payload(fixtures, tax_rate="0"), headers=HX)
    assert Booking.query.one().tax_rate is None


def test_unknown_tax_rate_ignored(client, admin, fixtures):
    """Ein nicht angebotener Satz (z.B. manipuliert) wird verworfen, nicht gespeichert."""
    _login(client)
    client.post("/accounting/bookings/new",
                data=_valid_payload(fixtures, tax_rate="99"), headers=HX)
    assert Booking.query.one().tax_rate is None


def test_optional_assignments_persist(client, admin, fixtures):
    _login(client)
    client.post("/accounting/bookings/new", data=_valid_payload(
        fixtures, project_id=fixtures["proj"].id, customer_id=fixtures["cust"].id),
        headers=HX)
    b = Booking.query.one()
    assert b.project_id == fixtures["proj"].id
    assert b.customer_id == fixtures["cust"].id


# --------------------------------------------------------------------------- #
# POST (HX) — Validierung (kein 500, keine Buchung, keine Trigger)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("override", [
    {"amount": ""},                         # Pflicht
    {"amount": "abc"},                      # ungueltig
    {"amount": "0"},                        # 0 verboten
    {"account_id": ""},                     # Pflicht
    {"account_id": "999999"},               # existiert nicht
    {"description": ""},                    # Pflicht
    {"date": ""},                           # Pflicht
    {"date": "nonsense"},                   # ungueltig
])
def test_invalid_input_is_rejected_cleanly(client, admin, fixtures, override):
    _login(client)
    r = client.post("/accounting/bookings/new",
                    data=_valid_payload(fixtures, **override), headers=HX)
    assert r.status_code == 200                       # kein 500
    assert "booking-saved" not in r.headers.get("HX-Trigger", "")
    assert _booking_count() == 0                       # nichts angelegt
    assert 'id="booking-form"' in r.get_data(as_text=True)  # Formular zurueck


def test_future_date_rejected(client, admin, fixtures):
    _login(client)
    from datetime import timedelta
    future = (date.today() + timedelta(days=5)).isoformat()
    r = client.post("/accounting/bookings/new",
                    data=_valid_payload(fixtures, date=future), headers=HX)
    assert r.status_code == 200
    assert _booking_count() == 0
    assert "Zukunft" in r.get_data(as_text=True)


def test_no_open_fiscal_year_rejected(client, admin, fixtures):
    """Ein Datum ausserhalb jeden (offenen) Buchungsjahres wird abgelehnt."""
    _login(client)
    # 2099 hat kein Buchungsjahr → aber auch in der Zukunft; nimm Vergangenheit.
    old = date(2000, 6, 1).isoformat()
    r = client.post("/accounting/bookings/new",
                    data=_valid_payload(fixtures, date=old), headers=HX)
    assert r.status_code == 200
    assert _booking_count() == 0
    assert "Buchungsjahr" in r.get_data(as_text=True)


def test_input_preserved_on_error(client, admin, fixtures):
    """Bei Fehler bleiben die anderen Felder erhalten (Kern-Beschwerde)."""
    _login(client)
    r = client.post("/accounting/bookings/new", data=_valid_payload(
        fixtures, amount="", description="Wichtige Beschreibung",
        customer_id=fixtures["cust"].id), headers=HX)
    body = r.get_data(as_text=True)
    assert "Wichtige Beschreibung" in body
    # Kontakt bleibt vorausgewaehlt.
    assert ('value="%s" selected' % fixtures["cust"].id) in body or \
           ('selected' in body and str(fixtures["cust"].id) in body)


# --------------------------------------------------------------------------- #
# POST (non-HX) — Vollseiten-Fallback
# --------------------------------------------------------------------------- #

def test_non_hx_post_redirects(client, admin, fixtures):
    _login(client)
    r = client.post("/accounting/bookings/new", data=_valid_payload(fixtures))
    assert r.status_code == 302
    assert "/accounting/bookings" in r.headers["Location"]
    assert _booking_count() == 1


def test_non_hx_invalid_rerenders_full_page(client, admin, fixtures):
    _login(client)
    r = client.post("/accounting/bookings/new",
                    data=_valid_payload(fixtures, amount=""))
    assert r.status_code == 200
    assert _booking_count() == 0
    assert "navbar-vertical" in r.get_data(as_text=True)  # Vollseite


# --------------------------------------------------------------------------- #
# Bearbeiten
# --------------------------------------------------------------------------- #

def _booking(fixtures, **kw):
    b = Booking(
        date=kw.get("date", date.today()),
        account_id=fixtures["acc1"].id,
        amount=Decimal(kw.get("amount", "100.00")),
        description=kw.get("description", "Alt"),
        status=kw.get("status", Booking.STATUS_OFFEN),
    )
    db.session.add(b)
    db.session.commit()
    return b


def test_edit_get_prefills(client, admin, fixtures):
    _login(client)
    b = _booking(fixtures, amount="55.00", description="Vorhanden")
    body = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX).get_data(as_text=True)
    assert "Vorhanden" in body
    assert 'value="55.00"' in body


def test_edit_updates_fields(client, admin, fixtures):
    _login(client)
    b = _booking(fixtures)
    r = client.post(f"/accounting/bookings/{b.id}/edit", data=_valid_payload(
        fixtures, amount="-12.50", description="Neu", account_id=fixtures["acc2"].id),
        headers=HX)
    assert "booking-saved" in r.headers.get("HX-Trigger", "")
    db.session.expire_all()
    b = db.session.get(Booking, b.id)
    assert b.amount == Decimal("-12.50")
    assert b.description == "Neu"
    assert b.account_id == fixtures["acc2"].id


def test_edit_verbucht_locks_amount_and_date(client, admin, fixtures):
    """Bei verbuchter Buchung bleiben Betrag/Datum/Steuer gesperrt; Konto/
    Beschreibung/Zuordnungen sind aenderbar."""
    _login(client)
    b = _booking(fixtures, amount="100.00", status=Booking.STATUS_VERBUCHT)
    orig_amount = b.amount
    orig_date = b.date
    r = client.post(f"/accounting/bookings/{b.id}/edit", data=_valid_payload(
        fixtures, amount="-999.00", date=date(2000, 1, 1).isoformat(),
        description="Geaendert", account_id=fixtures["acc2"].id),
        headers=HX)
    assert r.status_code == 200
    db.session.expire_all()
    b = db.session.get(Booking, b.id)
    assert b.amount == orig_amount          # gesperrt
    assert b.date == orig_date              # gesperrt
    assert b.description == "Geaendert"     # aenderbar
    assert b.account_id == fixtures["acc2"].id


def test_edit_storniert_blocked(client, admin, fixtures):
    _login(client)
    b = _booking(fixtures, status=Booking.STATUS_STORNIERT)
    r = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX)
    assert r.status_code == 200
    assert "Stornierte Buchungen" in r.get_data(as_text=True)


def test_edit_group_child_blocked(client, admin, fixtures):
    _login(client)
    group = BookingGroup(date=date.today(), description="Sammel",
                         total_amount=Decimal("0"), status=BookingGroup.STATUS_AKTIV)
    db.session.add(group)
    db.session.commit()
    b = _booking(fixtures)
    b.group_id = group.id
    db.session.commit()
    r = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX)
    assert r.status_code == 200
    assert "Sammelbuchung" in r.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Listenseite enthaelt Modal-Verdrahtung
# --------------------------------------------------------------------------- #

def test_bookings_page_wires_modal(client, admin, fixtures):
    _login(client)
    body = client.get("/accounting/bookings").get_data(as_text=True)
    assert 'id="bookingFormModal"' in body
    assert 'id="booking-form-host"' in body
    assert 'id="bookings-refresher"' in body
    # „Neue Buchung" oeffnet das Modal per hx-get.
    assert 'data-bs-target="#bookingFormModal"' in body
