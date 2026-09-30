"""Kunden-Import-Wizard: die E-Rechnung-Spalten in Vorschau und Import (HTTP)."""
from app.extensions import db
from app.models import AppSetting, Customer
from tests.http.test_customers_import_wizard import (  # noqa: F401  (Fixtures/Helfer)
    _cleanup_pickles, _csv, _login, _upload, admin,
)

CSV = ("Kunden-Nr.;Name;E-Mail;USt-IdNr.;Leitweg-ID;Rechnungsformat;Unternehmer\n"
       "300;Gemeinde Testdorf;;;04011000-1234512345-06;XRechnung;\n"
       "301;Müller Bau GmbH;m@mueller.example;de 123 456 789;;;ja\n"
       "302;Fehlerhaft GmbH;;123;04011000-1234512345-07;;\n")


def _germany():
    AppSetting.set("org.country", "DE")
    db.session.commit()


class TestPreviewColumns:
    def test_columns_are_suggested_and_rendered(self, client, admin):
        _germany()
        _login(client)
        _upload(client, _csv(CSV))
        body = client.get("/customers/import/preview").get_data(as_text=True)
        # Spalten wurden aus den Überschriften erkannt …
        for field in ("col_vat_id", "col_buyer_reference", "col_einvoice_format", "col_is_business"):
            assert f'name="{field}"' in body
        # … und erscheinen als eigene Spalte mit editierbaren Feldern.
        assert "<th style=\"min-width:190px\">E-Rechnung</th>" in body
        assert 'name="rows[0][buyer_reference]"' in body
        assert 'value="04011000-1234512345-06"' in body
        assert 'value="DE123456789"' in body                  # normalisiert
        assert '<option value="xrechnung" selected>XRechnung</option>' in body
        assert '<option value="1" selected>Unternehmer</option>' in body
        # Die fehlerhafte Zeile trägt zwei Warnungen.
        assert "braucht das Länderkürzel" in body and "ungültige Prüfziffer" in body
        _cleanup_pickles(client)

    def test_the_business_column_is_german_only(self, client, admin):
        _login(client)
        _upload(client, _csv(CSV))
        body = client.get("/customers/import/preview").get_data(as_text=True)
        assert 'name="col_vat_id"' in body
        assert 'name="col_is_business"' not in body
        _cleanup_pickles(client)

    def test_no_extra_column_without_mapped_einvoice_fields(self, client, admin):
        _login(client)
        _upload(client, _csv("Kunden-Nr.;Name\n1;Schlicht\n"))
        body = client.get("/customers/import/preview").get_data(as_text=True)
        assert ">E-Rechnung</th>" not in body
        _cleanup_pickles(client)


class TestConfirm:
    def test_the_fields_reach_the_database(self, client, admin):
        _germany()
        _login(client)
        _upload(client, _csv(CSV))
        r = client.post("/customers/import/preview", data={
            "action": "confirm", "duplicate_mode": "skip",
            "col_customer_number": "Kunden-Nr.", "col_name": "Name", "col_email": "E-Mail",
            "col_vat_id": "USt-IdNr.", "col_buyer_reference": "Leitweg-ID",
            "col_einvoice_format": "Rechnungsformat", "col_is_business": "Unternehmer",
            "rows[0][customer_number]": "300", "rows[0][name]": "Gemeinde Testdorf",
            "rows[0][buyer_reference]": "04011000-1234512345-06",
            "rows[0][einvoice_format]": "xrechnung",
            "rows[1][customer_number]": "301", "rows[1][name]": "Müller Bau GmbH",
            "rows[1][email]": "m@mueller.example", "rows[1][vat_id]": "DE123456789",
            "rows[1][is_business]": "1",
        }, follow_redirects=False)
        assert r.status_code == 302
        gov = Customer.query.filter_by(customer_number=300).one()
        assert gov.einvoice_format == "xrechnung" and gov.buyer_reference == "04011000-1234512345-06"
        biz = Customer.query.filter_by(customer_number=301).one()
        assert biz.is_business and biz.vat_id == "DE123456789" and biz.wants_email
