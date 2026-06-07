"""HTTP-Tests fuer "Mandant zuruecksetzen" (Einstellungen -> Danger Zone).

Geprueft wird das doppelte Gate (Admin-Rolle UND Passwort) sowie der Kontrakt
"alle Daten weg, Einstellungen + Benutzer/Rollen bleiben, Defaults re-seeded".

Das Datei-Cleanup wird ueber einen ``PDF_DIR``-Monkeypatch in ein tmp-Verzeichnis
umgeleitet, damit der Test niemals echte Dev-Dateien loescht.
"""
import pytest

from app.extensions import db
from app.models import (
    AppSetting, BillingPeriod, Customer, TaxRate, User,
)
from app.auth.permissions import PERM_VERWALTUNG
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def verwalter(app):
    """Nicht-Admin, aber mit 'verwaltung'-Recht — kommt durch das Blueprint-Gate,
    scheitert aber an der strengeren is_admin-Pruefung der Reset-Route."""
    role = _ensure_role("Verwalter", perms=[PERM_VERWALTUNG])
    u = User(username="verwalter", email="v@v.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture(autouse=True)
def safe_pdf_dir(app, tmp_path, monkeypatch):
    """Datei-Cleanup in ein tmp-Verzeichnis lenken (schuetzt Dev-Dateien)."""
    monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path / "pdfs"))


def _login(client, username="admin", password="secret"):
    return client.post("/auth/login", data={"username": username, "password": password})


def _flashes(client):
    with client.session_transaction() as sess:
        return [msg for _cat, msg in sess.get("_flashes", [])]


def _seed_data():
    """Eine Einstellung (soll bleiben) und ein Datensatz (soll weg)."""
    AppSetting.set("wg.name", "KeepMe GmbH")
    db.session.add(Customer(name="Reset Testkunde"))
    db.session.commit()


def test_admin_reset_wipes_data_keeps_settings(client, admin):
    client.get("/auth/logout")
    _login(client)
    _seed_data()
    assert Customer.query.count() == 1

    r = client.post("/einstellungen/reset",
                    data={"confirm_password": "secret"}, follow_redirects=False)
    assert r.status_code == 302

    # Daten weg …
    assert Customer.query.count() == 0
    # … Einstellungen bleiben …
    assert AppSetting.get("wg.name") == "KeepMe GmbH"
    # … Defaults re-seeded (Steuersaetze + eine aktive Periode) …
    assert TaxRate.query.count() > 0
    assert BillingPeriod.query.filter_by(active=True).first() is not None
    # … Admin bleibt eingeloggt (User-Tabelle nicht angetastet).
    assert User.query.filter_by(username="admin").first() is not None
    assert any("zurückgesetzt" in m.lower() for m in _flashes(client))


def test_wrong_password_aborts(client, admin):
    client.get("/auth/logout")
    _login(client)
    _seed_data()

    r = client.post("/einstellungen/reset",
                    data={"confirm_password": "falsch"}, follow_redirects=False)
    assert r.status_code == 302
    # Daten unangetastet.
    assert Customer.query.count() == 1
    assert any("passwort falsch" in m.lower() for m in _flashes(client))


def test_missing_password_aborts(client, admin):
    client.get("/auth/logout")
    _login(client)
    _seed_data()

    r = client.post("/einstellungen/reset", data={}, follow_redirects=False)
    assert r.status_code == 302
    assert Customer.query.count() == 1


def test_non_admin_forbidden(client, verwalter):
    client.get("/auth/logout")
    _login(client, username="verwalter")
    _seed_data()

    r = client.post("/einstellungen/reset",
                    data={"confirm_password": "secret"}, follow_redirects=False)
    assert r.status_code == 302
    # Trotz korrektem Passwort: kein Admin -> keine Loeschung.
    assert Customer.query.count() == 1
    assert any("administrator" in m.lower() for m in _flashes(client))
