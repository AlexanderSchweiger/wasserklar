"""SEPA-Lastschrift (HTTP): Kontaktformular, Einstellungen, Kontaktliste, Import,
Einzugsseiten, Downloads und Rechte."""
from datetime import date

import pytest

from app.extensions import db
from app.models import AppSetting, Customer, DirectDebitRun, User
from tests.conftest import _ensure_role
from tests.http.test_customers_import_wizard import (  # noqa: F401  (Fixtures/Helfer)
    _cleanup_pickles, _csv, _upload,
)
from tests.integration.test_direct_debit import (  # noqa: F401
    SELLER, TODAY, _customer, _invoice, _run, setup,
)

IBAN = "AT61 1904 3002 3457 3201"


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": "secret"})


def _person(**extra):
    return {"last_name": "Beispiel", "first_name": "Anna", "is_customer": "1", "force": "1",
            "payment_fields": "1", **extra}


class TestCustomerForm:
    def test_direct_debit_with_auto_reference(self, client, admin):
        AppSetting.set("sepa.mandate_prefix", "WG-")
        db.session.commit()
        _login(client)
        client.post("/customers/new", data=_person(
            payment_method="direct_debit", iban=IBAN.lower(), bic="rzooat2l",
            mandate_signed_on="2024-01-15"))
        c = Customer.query.filter_by(last_name="Beispiel").one()
        assert c.is_direct_debit and c.iban == "AT611904300234573201" and c.bic == "RZOOAT2L"
        assert c.mandate_reference == f"WG-{c.customer_number}"
        assert c.mandate_signed_on == date(2024, 1, 15)
        # Ein geaendertes Praefix benennt das bestehende Mandat nicht um.
        AppSetting.set("sepa.mandate_prefix", "NEU-")
        db.session.commit()
        client.post(f"/customers/{c.id}/edit", data=_person(
            payment_method="direct_debit", iban=IBAN, mandate_reference=c.mandate_reference,
            mandate_signed_on="2024-01-15"))
        assert db.session.get(Customer, c.id).mandate_reference.startswith("WG-")

    @pytest.mark.parametrize("extra, message", [
        ({"payment_method": "direct_debit", "mandate_signed_on": "2024-01-15"}, "IBAN Pflicht"),
        ({"payment_method": "direct_debit", "iban": IBAN}, "Mandatsunterschrift Pflicht"),
        ({"iban": "AT61 1904 3002 3457 3202"}, "IBAN ist ungültig"),
        ({"bic": "XYZ"}, "BIC ist ungültig"),
        ({"payment_method": "direct_debit", "iban": IBAN, "mandate_signed_on": "2024-01-15",
          "mandate_reference": "WG 1"}, "Mandatsreferenz darf nur"),
        ({"payment_method": "direct_debit", "iban": "US64SVBKUS6S3300958879",
          "mandate_signed_on": "2024-01-15"}, "außerhalb des SEPA-Raums"),
    ])
    def test_validation(self, client, admin, extra, message):
        _login(client)
        r = client.post("/customers/new", data=_person(**extra))
        assert message in r.get_data(as_text=True)
        assert Customer.query.filter_by(last_name="Beispiel").first() is None

    def test_duplicate_mandate_reference(self, client, admin):
        db.session.add(Customer(name="Anderer", customer_number=5, mandate_reference="M-1"))
        db.session.commit()
        _login(client)
        r = client.post("/customers/new", data=_person(
            payment_method="direct_debit", iban=IBAN, mandate_signed_on="2024-01-15",
            mandate_reference="M-1"))
        assert "schon bei einem anderen Kontakt" in r.get_data(as_text=True)

    def test_form_without_marker_keeps_bank_data(self, client, admin):
        c = Customer(name="Beispiel Anna", last_name="Beispiel", first_name="Anna", is_customer=True,
                     customer_number=7, payment_method="direct_debit", iban="AT611904300234573201",
                     mandate_reference="7", mandate_signed_on=date(2024, 1, 1))
        db.session.add(c)
        db.session.commit()
        _login(client)
        data = _person()
        data.pop("payment_fields")
        client.post(f"/customers/{c.id}/edit", data=data)
        c = db.session.get(Customer, c.id)
        assert c.is_direct_debit and c.iban == "AT611904300234573201"

    def test_form_and_detail_render(self, client, admin):
        c = Customer(name="Beispiel Anna", is_customer=True, customer_number=8,
                     payment_method="direct_debit", iban="AT611904300234573201")
        db.session.add(c)
        db.session.commit()
        _login(client)
        body = client.get(f"/customers/{c.id}/edit", headers={"X-From-Modal": "1"}).get_data(as_text=True)
        assert 'name="payment_method"' in body and 'value="AT61 1904 3002 3457 3201"' in body
        detail = client.get(f"/customers/{c.id}").get_data(as_text=True)
        assert "Bankeinzug" in detail and "Datum der Mandatsunterschrift fehlt" in detail


class TestCustomerList:
    def test_payment_filter(self, client, admin):
        db.session.add_all([
            Customer(name="Einzug Komplett", customer_number=1, payment_method="direct_debit",
                     iban="AT611904300234573201", mandate_reference="1",
                     mandate_signed_on=date(2024, 1, 1)),
            Customer(name="Einzug Halb", customer_number=2, payment_method="direct_debit",
                     iban="AT611904300234573201"),
            Customer(name="Zahlschein", customer_number=3),
        ])
        db.session.commit()
        _login(client)
        body = client.get("/customers/?payment=direct_debit").get_data(as_text=True)
        assert "Einzug Komplett" in body and "Einzug Halb" in body and "Zahlschein" not in body
        body = client.get("/customers/?payment=incomplete").get_data(as_text=True)
        assert "Einzug Halb" in body and "Einzug Komplett" not in body


class TestSettings:
    def _post(self, client, **extra):
        data = {"sepa_fields": "1", "sepa_creditor_id": "", "sepa_mandate_prefix": "",
                "sepa_notice_title": "", "sepa_notice_text": "", **extra}
        return client.post("/einstellungen/", data=data, follow_redirects=True)

    def test_valid_values_are_saved(self, client, admin):
        _login(client)
        self._post(client, sepa_creditor_id="at61 zzz0 1234 5678 90", sepa_mandate_prefix="WG-",
                   sepa_notice_text="Abbuchung {{ abbuchungstermin }}.")
        assert AppSetting.get("sepa.creditor_id") == "AT61ZZZ01234567890"
        assert AppSetting.get("sepa.mandate_prefix") == "WG-"
        assert AppSetting.get("sepa.notice_text") == "Abbuchung {{ abbuchungstermin }}."

    def test_invalid_values_are_rejected(self, client, admin):
        AppSetting.set("sepa.creditor_id", "AT61ZZZ01234567890")
        db.session.commit()
        _login(client)
        body = self._post(client, sepa_creditor_id="AT12ZZZ00000000001",
                          sepa_notice_text="kaputt {{ betrag").get_data(as_text=True)
        assert "Gläubiger-ID ist ungültig" in body and "Platzhalter-Fehler" in body
        assert AppSetting.get("sepa.creditor_id") == "AT61ZZZ01234567890"
        assert AppSetting.get("sepa.notice_text") is None

    def test_card_is_rendered(self, client, admin):
        _login(client)
        body = client.get("/einstellungen/").get_data(as_text=True)
        assert 'name="sepa_creditor_id"' in body and "Bitte nicht einzahlen." in body


class TestImport:
    def test_bank_columns_reach_the_contact(self, client, admin):
        _login(client)
        csv = ("Kunden-Nr.;Name;Zahlungsart;IBAN;Mandatsreferenz;Mandatsdatum\n"
               "400;Einzug Erna;Bankeinzug;AT61 1904 3002 3457 3201;ALT-77;15.01.2020\n"
               "401;Falsch Fritz;Lastschrift;AT00 1234;;\n")
        _upload(client, _csv(csv))
        body = client.get("/customers/import/preview").get_data(as_text=True)
        assert 'name="col_iban"' in body and "Bank / Lastschrift" in body
        assert "IBAN »AT00 1234« ist ungültig" in body
        r = client.post("/customers/import/preview", data={
            "action": "confirm", "duplicate_mode": "skip",
            "col_customer_number": "Kunden-Nr.", "col_name": "Name",
            "col_payment_method": "Zahlungsart", "col_iban": "IBAN",
            "col_mandate_reference": "Mandatsreferenz", "col_mandate_signed_on": "Mandatsdatum",
        })
        assert r.status_code == 302
        erna = Customer.query.filter_by(customer_number=400).one()
        assert erna.is_direct_debit and erna.iban == "AT611904300234573201"
        assert erna.mandate_reference == "ALT-77" and erna.mandate_signed_on == date(2020, 1, 15)
        fritz = Customer.query.filter_by(customer_number=401).one()
        assert not fritz.is_direct_debit and fritz.iban is None
        _cleanup_pickles(client)


class TestDirectDebitPages:
    def test_pages_and_downloads(self, client, admin, setup):
        inv = _invoice(_customer(), setup["account"])
        _login(client)
        assert "Lastschrift-Einzug" in client.get("/").get_data(as_text=True)      # Seitenleiste
        assert client.get("/direct-debit/").status_code == 200
        body = client.get("/direct-debit/new?due_until=2026-12-31").get_data(as_text=True)
        assert inv.invoice_number in body and f'value="{inv.open_item.id}"' in body
        assert "Nicht in der Auswahl" not in body
        hint = client.get("/direct-debit/new?due_until=2020-01-01").get_data(as_text=True)
        assert "Nicht in der Auswahl" in hint and "erst nach dem 01.01.2020" in hint
        r = client.post("/direct-debit/create", data={"open_item_id": str(inv.open_item.id)})
        assert r.status_code == 302
        run = DirectDebitRun.query.one()
        detail = client.get(f"/direct-debit/{run.id}").get_data(as_text=True)
        assert run.message_id in detail and "Als bezahlt verbuchen" in detail
        xml = client.get(f"/direct-debit/{run.id}/pain008.xml")
        assert xml.mimetype == "application/xml" and b"pain.008.001.08" in xml.data
        assert "attachment" in xml.headers["Content-Disposition"]
        csv = client.get(f"/direct-debit/{run.id}/export.csv")
        assert csv.mimetype == "text/csv" and "Einzugsdatum" in csv.get_data(as_text=True)
        # Rechnungsdetail verweist auf den Lauf, Offene Posten zeigen „im Einzug“.
        assert "SEPA-Lastschrift" in client.get(f"/invoices/{inv.id}").get_data(as_text=True)
        assert "im Einzug" in client.get("/accounting/open-items").get_data(as_text=True)

    def test_book_return_and_cancel(self, client, admin, setup):
        inv = _invoice(_customer(), setup["account"])
        run = _run(inv.open_item)
        _login(client)
        client.post(f"/direct-debit/{run.id}/book",
                    data={"real_account_id": str(setup["real_account"].id)})
        assert db.session.get(type(inv), inv.id).status == "Bezahlt"
        r = client.post(f"/direct-debit/{run.id}/cancel", follow_redirects=True)
        assert "schon Zahlungen gebucht" in r.get_data(as_text=True)
        item = run.items[0]
        client.post(f"/direct-debit/items/{item.id}/return",
                    data={"real_account_id": str(setup["real_account"].id),
                          "returned_on": "2026-10-20", "reason": "Widerspruch"})
        assert db.session.get(type(inv), inv.id).status == "Versendet"

    def test_permission(self, client, setup):
        role = _ensure_role("Zaehler", perms=("zaehler",))
        u = User(username="zaehler", email="z@a.test", role_id=role.id)
        u.set_password("secret")
        db.session.add(u)
        db.session.commit()
        _login(client, "zaehler")
        r = client.get("/direct-debit/")
        assert r.status_code == 302

    def test_nav_hidden_without_use(self, client, admin):
        _login(client)
        assert "Lastschrift-Einzug" not in client.get("/").get_data(as_text=True)
