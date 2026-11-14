"""Einstellungsseite als Ganzes: jedes Feld speichert, nichts verschwindet,
der geoeffnete Reiter bleibt nach dem Speichern offen.

Anlass (Prod, 2026-11): ein eigener SMTP-Server liess sich nicht speichern —
das versteckte Feld ``mail_use_platform_relay=false`` der SaaS galt als „an“,
die Route verwarf darauf alle SMTP-Eingaben. Und nach jedem Speichern sprang
die Seite auf den ersten Reiter (der ``#anker`` ging im hx-boost-Redirect
verloren).

Der Kern ist ``test_unchanged_form_keeps_every_value``: das Formular wird so
ausgelesen, wie der Browser es abschickt (deaktivierte Felder fehlen, nicht
angehakte Checkboxen fehlen), unveraendert gespeichert — danach muss jede
Einstellung noch genau so dastehen.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from lxml import html as lxml_html

from app.extensions import db
from app.models import AppSetting, User
from app.settings_service import decrypt_password
from tests.conftest import _ensure_role


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
    resp = client.post("/auth/login", data={"username": "admin", "password": "secret"})
    assert resp.status_code in (200, 302)


def _browser_form(page_html: str) -> dict[str, list[str]]:
    """Was der Browser beim Klick auf „Einstellungen speichern“ sendet."""
    tree = lxml_html.fromstring(page_html)
    forms = tree.xpath('//form[@id="settingsForm"]')
    assert forms, "Formular #settingsForm fehlt"
    data: dict[str, list[str]] = {}

    def add(name, value):
        data.setdefault(name, []).append(value)

    for el in forms[0].xpath('.//input|.//select|.//textarea'):
        name = el.get("name")
        if not name or el.get("disabled") is not None:
            continue
        if el.tag == "input":
            typ = (el.get("type") or "text").lower()
            if typ in ("checkbox", "radio"):
                if el.get("checked") is not None:
                    add(name, el.get("value") or "on")
            elif typ in ("submit", "button", "file", "image", "reset"):
                continue
            else:
                add(name, el.get("value") or "")
        elif el.tag == "textarea":
            add(name, el.text or "")
        else:  # select
            options = el.xpath(".//option")
            chosen = [o for o in options if o.get("selected") is not None]
            if not chosen and options:
                chosen = options[:1]
            for o in chosen:
                add(name, o.get("value") if o.get("value") is not None else (o.text or ""))
    return data


def _settings():
    db.session.expire_all()
    return {row.key: row.value for row in AppSetting.query.all()}


# Ein Mandant, bei dem jede Einstellung der Seite einen Wert hat.
_FILLED = {
    "org.type": "cooperative",
    "org.country": "AT",
    "map.ortho_wms_url": "https://wms.example.test/dop",
    "map.ortho_wms_layers": "dop20",
    "map.ortho_attribution": "© Land",
    "tax.water_rate": "10",
    "tax.vat_return_period": "month",
    "wg.name": "WG Musterdorf",
    "wg.address": "Hauptstraße 1\n4232 Musterdorf",
    "wg.email": "office@wg-musterdorf.test",
    "wg.phone": "+43 7236 1234",
    "wg.iban": "AT611904300234573201",
    "wg.bic": "BKAUATWW",
    "wg.account_holder": "WG Musterdorf",
    "wg.vat_id": "ATU12345678",
    "wg.tax_number": "12 345/6789",
    "wg.street": "Hauptstraße 1",
    "wg.postal_code": "4232",
    "wg.city": "Musterdorf",
    "wg.contact_name": "Maria Muster",
    "wg.register_number": "FN 123456a",
    "sepa.creditor_id": "AT61ZZZ01234567890",
    "sepa.mandate_prefix": "WG-",
    "mail.use_platform_relay": "false",
    "mail.server": "smtp.wg-musterdorf.test",
    "mail.port": "465",
    "mail.use_tls": "false",
    "mail.username": "office@wg-musterdorf.test",
    "mail.default_sender": "office@wg-musterdorf.test",
    "einvoice.enabled": "true",
    "einvoice.exempt_reason": "Echter Mitgliedsbeitrag",
    "meters.replacement_interval_years": "7",
}


def _fill(values):
    for key, value in values.items():
        AppSetting.set(key, value)
    db.session.commit()


_VARIANTS = {
    "at_cooperative": {
        "org.share_area_per_share": "500",
        "org.share_area_rounding": "nearest",
    },
    # Deutschland: Stichtag der B2B-Pflicht erscheint, andere Steuersaetze.
    "de_utility": {
        "org.country": "DE",
        "org.type": "utility",
        "tax.water_rate": "7",
        "wg.vat_id": "DE123456789",
        "einvoice.mandate_from": "2027-01-01",
        "sepa.creditor_id": "DE98ZZZ09999999999",
    },
}


class TestUnchangedForm:
    @pytest.mark.parametrize("variant", sorted(_VARIANTS))
    def test_unchanged_form_keeps_every_value(self, client, admin, variant):
        if _VARIANTS[variant].get("org.country") == "DE":
            from app.settings.country_defaults import apply_country_defaults
            apply_country_defaults("DE", previous_code="AT")
        _fill({**_FILLED, **_VARIANTS[variant]})
        from app.settings_service import encrypt_password
        AppSetting.set("mail.password", encrypt_password("geheim"))
        db.session.commit()
        before = _settings()
        _login(client)

        page = client.get("/einstellungen/")
        assert page.status_code == 200
        resp = client.post("/einstellungen/", data=_browser_form(page.get_data(as_text=True)))
        assert resp.status_code == 302

        after = _settings()
        changed = {k: (before.get(k), after.get(k))
                   for k in set(before) | set(after) if before.get(k) != after.get(k)}
        # Neu dazukommen duerfen nur Eintraege, die vorher fehlten und jetzt den
        # angezeigten Standard tragen — geaendert oder geleert werden darf nichts.
        lost = {k: v for k, v in changed.items() if v[0] is not None}
        assert not lost, f"Speichern ohne Aenderung hat Werte veraendert: {lost}"
        assert decrypt_password(after["mail.password"]) == "geheim"

    def test_second_save_changes_nothing(self, client, admin):
        """Leerer Mandant: das erste Speichern legt Standards an, das zweite
        darf daran nichts mehr aendern (sonst „wandern“ Werte)."""
        _login(client)
        page = client.get("/einstellungen/")
        client.post("/einstellungen/", data=_browser_form(page.get_data(as_text=True)))
        first = _settings()
        page = client.get("/einstellungen/")
        client.post("/einstellungen/", data=_browser_form(page.get_data(as_text=True)))
        assert _settings() == first


class TestOwnSmtp:
    def _post_mail(self, client, **extra):
        page = client.get("/einstellungen/?tab=email")
        form = _browser_form(page.get_data(as_text=True))
        form.update({
            "mail_server": ["smtp.neu.test"],
            "mail_port": ["587"],
            "mail_use_tls": ["true"],
            "mail_username": ["user@neu.test"],
            "mail_password": ["s3cret"],
            "mail_default_sender": ["office@neu.test"],
        })
        form.update({k: [v] for k, v in extra.items()})
        return client.post("/einstellungen/", data=form)

    def test_smtp_fields_are_saved(self, client, admin):
        _login(client)
        self._post_mail(client)
        s = _settings()
        assert s["mail.server"] == "smtp.neu.test"
        assert s["mail.port"] == "587"
        assert s["mail.use_tls"] == "true"
        assert s["mail.username"] == "user@neu.test"
        assert s["mail.default_sender"] == "office@neu.test"
        assert decrypt_password(s["mail.password"]) == "s3cret"

        body = client.get("/einstellungen/?tab=email").get_data(as_text=True)
        assert 'value="smtp.neu.test"' in body
        assert "gesetzt" in body

    def test_hidden_false_is_not_relay(self, client, admin):
        """Regression: die SaaS schickte ``mail_use_platform_relay=false`` mit —
        ein nicht leerer String, der als „an“ galt und die SMTP-Daten verwarf."""
        _login(client)
        self._post_mail(client, mail_use_platform_relay="false")
        s = _settings()
        assert s["mail.server"] == "smtp.neu.test"
        assert s["mail.use_platform_relay"] == "false"

    def test_relay_on_keeps_stored_smtp(self, client, admin):
        _fill({"mail.server": "smtp.alt.test", "mail.username": "alt@alt.test"})
        _login(client)
        client.post("/einstellungen/", data={
            "mail_relay_field": "1", "mail_use_platform_relay": "true"})
        s = _settings()
        assert s["mail.use_platform_relay"] == "true"
        assert s["mail.server"] == "smtp.alt.test"
        assert s["mail.username"] == "alt@alt.test"

    def test_unchecked_switch_with_marker_turns_relay_off(self, client, admin):
        _fill({"mail.use_platform_relay": "true"})
        _login(client)
        client.post("/einstellungen/", data={"mail_relay_field": "1"})
        assert _settings()["mail.use_platform_relay"] == "false"

    def test_form_without_switch_leaves_relay_choice(self, client, admin):
        """Ohne Schalter im Formular (OSS, SaaS ohne Relay-Recht) bleibt die Wahl."""
        _fill({"mail.use_platform_relay": "true"})
        _login(client)
        client.post("/einstellungen/", data={"wg_name": "X"})
        assert _settings()["mail.use_platform_relay"] == "true"

    def test_form_without_smtp_fields_leaves_smtp(self, client, admin):
        _fill({"mail.server": "smtp.alt.test", "mail.use_tls": "true"})
        _login(client)
        client.post("/einstellungen/", data={"wg_name": "X"})
        s = _settings()
        assert s["mail.server"] == "smtp.alt.test"
        assert s["mail.use_tls"] == "true"

    def test_empty_password_keeps_stored_one(self, client, admin):
        _login(client)
        self._post_mail(client)
        self._post_mail(client, mail_password="")
        assert decrypt_password(_settings()["mail.password"]) == "s3cret"


class TestEveryTabSaves:
    def test_values_of_all_tabs_are_stored(self, client, admin):
        _login(client)
        page = client.get("/einstellungen/")
        form = _browser_form(page.get_data(as_text=True))
        new = {
            # Mandant
            "org_type": "utility",
            "map_ortho_wms_url": "https://wms.example.test/x",
            "map_ortho_wms_layers": "l1",
            "map_ortho_attribution": "© X",
            "supplier_number_start": "80001",
            # Steuern
            "tax_vat_return_period": "month",
            # Kontaktdaten + Bank + SEPA
            "wg_name": "WG Neu",
            "wg_address": "Weg 2\n1234 Ort",
            "wg_email": "neu@wg.test",
            "wg_phone": "0664 1",
            "wg_iban": "AT611904300234573201",
            "wg_bic": "BKAUATWW",
            "wg_account_holder": "WG Neu",
            "wg_vat_id": "ATU99999999",
            "wg_tax_number": "99/999",
            "sepa_creditor_id": "AT61ZZZ01234567890",
            "sepa_mandate_prefix": "N-",
            "sepa_mandate_scope": "für Wasser und Kanal",
            # E-Rechnung
            "wg_street": "Weg 2",
            "wg_postal_code": "1234",
            "wg_city": "Ort",
            "wg_contact_name": "Max",
            "wg_register_number": "ZVR 1",
            "einvoice_exempt_reason": "Befreit",
            # Zähler
            "meter_replacement_interval": "6",
        }
        form.update({k: [v] for k, v in new.items()})
        form["einvoice_enabled"] = ["on"]
        resp = client.post("/einstellungen/", data=form)
        assert resp.status_code == 302

        s = _settings()
        assert s["org.type"] == "utility"
        assert s["map.ortho_wms_url"] == "https://wms.example.test/x"
        assert s["map.ortho_wms_layers"] == "l1"
        assert s["map.ortho_attribution"] == "© X"
        assert s["numbers.supplier_start"] == "80001"
        assert s["tax.vat_return_period"] == "month"
        for attr in ("name", "email", "phone", "iban", "bic", "account_holder", "vat_id",
                     "tax_number", "street", "postal_code", "city", "contact_name",
                     "register_number"):
            assert s[f"wg.{attr}"] == new[f"wg_{attr}"], attr
        assert s["wg.address"].replace("\r\n", "\n") == "Weg 2\n1234 Ort"
        assert s["sepa.creditor_id"] == "AT61ZZZ01234567890"
        assert s["sepa.mandate_prefix"] == "N-"
        assert s["sepa.mandate_scope"] == "für Wasser und Kanal"
        assert s["einvoice.enabled"] == "true"
        assert s["einvoice.exempt_reason"] == "Befreit"
        assert s["meters.replacement_interval_years"] == "6"

        # … und die Seite zeigt sie wieder an.
        body = client.get("/einstellungen/").get_data(as_text=True)
        for value in ("WG Neu", "neu@wg.test", "AT61ZZZ01234567890", "für Wasser und Kanal",
                      "https://wms.example.test/x", "ZVR 1"):
            assert value in body, value

    def test_switches_can_be_turned_off(self, client, admin):
        _fill({"einvoice.enabled": "true", "sepa.mandate_form_enclosure": "true"})
        _login(client)
        page = client.get("/einstellungen/")
        form = _browser_form(page.get_data(as_text=True))
        form.pop("einvoice_enabled", None)
        form.pop("sepa_mandate_form_enclosure", None)
        client.post("/einstellungen/", data=form)
        s = _settings()
        assert s["einvoice.enabled"] == "false"
        assert s.get("sepa.mandate_form_enclosure") is None

    def test_country_switch_applies_defaults(self, client, admin):
        _login(client)
        page = client.get("/einstellungen/")
        form = _browser_form(page.get_data(as_text=True))
        form["org_country"] = ["DE"]
        client.post("/einstellungen/", data=form)
        s = _settings()
        assert s["org.country"] == "DE"
        assert s["tax.water_rate"] == "7"


class TestActiveTab:
    @pytest.mark.parametrize("tab", ["email", "kontakt", "tax", "rechnung", "zaehler"])
    def test_save_returns_to_the_open_tab(self, client, admin, tab):
        _login(client)
        resp = client.post("/einstellungen/", data={"active_tab": tab})
        assert resp.status_code == 302
        query = parse_qs(urlparse(resp.headers["Location"]).query)
        assert query.get("tab") == [tab]

        body = client.get(f"/einstellungen/?tab={tab}").get_data(as_text=True)
        tree = lxml_html.fromstring(body)
        active = tree.xpath('//ul[@id="settingsTabs"]//a[contains(@class, "active")]/@id')
        assert active == [f"tab-{tab}"]
        panes = tree.xpath('//div[contains(@class, "tab-pane") and contains(@class, "active")]/@id')
        assert panes == [f"pane-{tab}"]
        assert tree.xpath('//input[@id="settingsActiveTab"]/@value') == [tab]

    def test_default_tab_has_clean_url(self, client, admin):
        _login(client)
        resp = client.post("/einstellungen/", data={"active_tab": "mandant"})
        assert urlparse(resp.headers["Location"]).query == ""

    @pytest.mark.parametrize("raw", ["", "unbekannt", "../x", "pane-email"])
    def test_tab_values_are_validated(self, client, admin, raw):
        _login(client)
        resp = client.post("/einstellungen/", data={"active_tab": raw})
        query = parse_qs(urlparse(resp.headers["Location"]).query)
        assert query.get("tab") == (["email"] if raw == "pane-email" else None)

    def test_danger_tab_only_for_admins(self, client, app):
        from app.auth.permissions import PERM_VERWALTUNG
        from app.models import Role, RolePermission
        role = Role(name="Verwalter")
        db.session.add(role)
        db.session.flush()
        db.session.add(RolePermission(role_id=role.id, permission_key=PERM_VERWALTUNG))
        u = User(username="verw", email="v@a.test", role_id=role.id)
        u.set_password("secret")
        db.session.add(u)
        db.session.commit()
        client.get("/auth/logout")
        client.post("/auth/login", data={"username": "verw", "password": "secret"})
        body = client.get("/einstellungen/?tab=danger").get_data(as_text=True)
        tree = lxml_html.fromstring(body)
        assert tree.xpath('//a[@id="tab-mandant" and contains(@class, "active")]')
        assert not tree.xpath('//*[@id="pane-danger"]')


class TestFlashMessages:
    def test_save_message_appears_once(self, client, admin):
        """Die Steuersatz-Karte gab die Meldungen beim Voll-Render ein zweites Mal
        aus (gedacht nur fuer ihr htmx-Neuladen nach dem Modal)."""
        _login(client)
        resp = client.post("/einstellungen/", data={"active_tab": "tax"}, follow_redirects=True)
        assert resp.get_data(as_text=True).count("Einstellungen gespeichert.") == 1

    def test_tax_card_fragment_still_shows_messages(self, client, admin):
        _login(client)
        with client.session_transaction() as sess:
            sess["_flashes"] = [("success", "Steuersatz 5 % angelegt.")]
        body = client.get("/einstellungen/tax-rates/card").get_data(as_text=True)
        assert "Steuersatz 5 % angelegt." in body
