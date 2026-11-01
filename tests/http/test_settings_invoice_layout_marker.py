"""Einstellungen: Darstellungsfelder der Rechnung (Logo, Kontaktinfo, Absenderzeile,
Dokumentformat, Design, Schalter) speichert die Seite nur mit Marker.

Hintergrund: eine Erweiterung (SaaS) verlegt diese Felder auf eine eigene Seite.
Ohne Marker haette ein Speichern der Einstellungen die fehlenden Checkboxen als
„aus“ und die fehlenden Texte als leer geschrieben.
"""
import pytest

from app.extensions import db
from app.models import AppSetting, User
from tests.conftest import _ensure_role

_PNG = ("data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _layout_form(**extra):
    data = {
        "org_type": "cooperative",
        "invoice_layout_fields": "1",
        "invoice_document_format": "both",
        "invoice_design": "blue",
        "invoice_show_payment_qr": "on",
        "invoice_print_meter_swap": "on",
        "invoice_contact_info": "<b>Obmann</b> Max Muster",
        "invoice_sender_address": "WG Test · Weg 1 · 1234 Ort",
        "invoice_small_business_note": "Kleinunternehmer",
        "wg_logo_text": "WG Test",
        "wg_logo_subtitle": "Trinkwasser",
        "wg_logo_data": _PNG,
    }
    data.update(extra)
    return data


def _values():
    db.session.expire_all()
    keys = ("invoice.document_format", "invoice.design", "invoice.show_payment_qr",
            "invoice.print_meter_swap", "invoice.show_meter_readings",
            "invoice.contact_info", "invoice.sender_address",
            "invoice.small_business_note", "wg.logo_text", "wg.logo_subtitle", "wg.logo")
    return {k: AppSetting.get(k) for k in keys}


def test_form_with_marker_saves_the_layout(client, admin):
    _login(client)
    client.post("/einstellungen/", data=_layout_form())
    v = _values()
    assert v["invoice.document_format"] == "both"
    assert v["invoice.design"] == "blue"
    assert v["invoice.show_payment_qr"] == "true"
    assert v["invoice.print_meter_swap"] == "true"
    assert v["invoice.show_meter_readings"] == "false"
    assert v["invoice.contact_info"] == "<b>Obmann</b> Max Muster"
    assert v["invoice.sender_address"] == "WG Test · Weg 1 · 1234 Ort"
    assert v["invoice.small_business_note"] == "Kleinunternehmer"
    assert v["wg.logo_text"] == "WG Test"
    assert v["wg.logo"] == _PNG


def test_form_without_marker_leaves_the_layout_alone(client, admin):
    _login(client)
    client.post("/einstellungen/", data=_layout_form())
    before = _values()
    # Ein Formular ohne die Karten (und ohne Marker) — z. B. die SaaS-Einstellungen.
    client.post("/einstellungen/", data={"org_type": "cooperative", "wg_name": "WG Neu"})
    assert _values() == before
    assert AppSetting.get("wg.name") == "WG Neu"


def test_missing_master_data_field_is_kept(client, admin):
    _login(client)
    client.post("/einstellungen/", data={"org_type": "cooperative",
                                         "wg_name": "WG Test", "wg_iban": "AT611904300234573201"})
    client.post("/einstellungen/", data={"org_type": "cooperative", "wg_name": "WG Test"})
    db.session.expire_all()
    assert AppSetting.get("wg.iban") == "AT611904300234573201"
    # Ein gesendetes, geleertes Feld leert weiterhin.
    client.post("/einstellungen/", data={"org_type": "cooperative", "wg_name": "WG Test",
                                         "wg_iban": ""})
    db.session.expire_all()
    assert AppSetting.get("wg.iban") is None


def test_font_size_only_with_field(client, admin):
    _login(client)
    AppSetting.set("invoice.contact_info_font_size", "12")
    db.session.commit()
    client.post("/einstellungen/", data=_layout_form())
    db.session.expire_all()
    assert AppSetting.get("invoice.contact_info_font_size") == "12"
    client.post("/einstellungen/", data=_layout_form(invoice_contact_info_font_size="99"))
    db.session.expire_all()
    assert AppSetting.get("invoice.contact_info_font_size") == "10"


def test_settings_page_renders_the_cards_with_marker(client, admin):
    _login(client)
    html = client.get("/einstellungen/").get_data(as_text=True)
    assert 'name="invoice_layout_fields" value="1"' in html
    assert 'id="contactInfoEditor"' in html
    assert 'id="logoFileInput"' in html
    assert 'id="logoCropModal"' in html
    assert 'name="invoice_sender_address"' in html
