"""E-Rechnungs-Pflicht (DE, B2B) in der Oberfläche: Formular, Liste, Dashboard,
Rechnungslauf, Post-Versand, Mailversand, Statuswechsel, Rechnungsdetail, Einstellung.

Die Fixtures (Admin-Login, Fake-WeasyPrint, Rechnungen) kommen aus
``test_invoice_pdf_service.py``.
"""
import io
import os
from datetime import date
from decimal import Decimal

from pypdf import PdfReader

from app.einvoice import obligation
from app.extensions import db
from app.models import (
    AppSetting, BillingRun, Customer, CustomerEmailConsentLog, FiscalYear, Invoice,
)
from tests.http.test_invoice_pdf_service import (  # noqa: F401  (Fixtures)
    _login, _ready_seller, admin, invoices, pdf_dir, rendered,
)


def _make_required(invoice, *, email):
    """Die Rechnung wird zur Pflicht-E-Rechnung: Unternehmer, DE, > 250 €, Leistung 2027."""
    customer = invoice.customer
    customer.is_business = True
    customer.email = "buchhaltung@mueller-bau.example" if email else None
    customer.rechnung_per_email = email
    item = invoice.items[0]
    item.quantity, item.unit_price, item.amount = Decimal("200"), Decimal("2.5"), Decimal("500")
    invoice.recalculate_total()
    invoice.billing_period.end_date = date(2027, 6, 30)
    AppSetting.set(obligation.MANDATE_KEY, "2027-01-01")
    db.session.commit()


def _billing_run(*invoices_):
    run = BillingRun(billing_period_id=invoices_[0].billing_period_id, tariff_name="T",
                     tariff_price_per_m3=Decimal("2.5"))
    db.session.add(run)
    db.session.flush()
    for inv in invoices_:
        inv.billing_run_id = run.id
    db.session.commit()
    return run


# ---------------------------------------------------------------------------
# Kundenformular, Liste, Dashboard
# ---------------------------------------------------------------------------

def _company_form(**extra):
    return {"is_company": "1", "company_name": "Müller Bau GmbH", "is_customer": "1",
            "force": "1", **extra}


class TestCustomerForm:
    def test_the_switch_exists_only_for_german_tenants(self, client, admin):
        _login(client)
        assert 'name="is_business"' not in client.get("/customers/new").get_data(as_text=True)
        AppSetting.set("org.country", "DE")
        db.session.commit()
        html = client.get("/customers/new").get_data(as_text=True)
        assert 'name="is_business"' in html and 'name="einvoice_b2b_fields"' in html

    def test_saving_a_business_switches_on_the_email(self, client, admin):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        _login(client)
        client.post("/customers/new", data=_company_form(
            einvoice_b2b_fields="1", is_business="1", email="m@mueller.example"))
        customer = Customer.query.filter_by(name="Müller Bau GmbH").one()
        assert customer.is_business and customer.wants_email
        (log,) = CustomerEmailConsentLog.query.filter_by(customer_id=customer.id).all()
        assert log.action == CustomerEmailConsentLog.EINVOICE_B2B

    def test_a_business_without_an_address_gets_a_warning(self, client, admin):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        _login(client)
        r = client.post("/customers/new", data=_company_form(
            einvoice_b2b_fields="1", is_business="1"), follow_redirects=True)
        assert "kann aber keine E-Mail bekommen" in r.get_data(as_text=True)
        assert Customer.query.filter_by(name="Müller Bau GmbH").one().is_business

    def test_a_form_without_the_marker_keeps_the_flag(self, client, admin):
        """Ein Formular ohne Schalter (AT) darf das Kennzeichen nicht löschen."""
        customer = Customer(name="Müller Bau GmbH", is_company=True, is_customer=True,
                            is_business=True)
        db.session.add(customer)
        db.session.commit()
        _login(client)
        client.post(f"/customers/{customer.id}/edit", data=_company_form())
        assert db.session.get(Customer, customer.id).is_business is True

    def test_unchecking_the_switch_clears_the_flag(self, client, admin):
        AppSetting.set("org.country", "DE")
        customer = Customer(name="Müller Bau GmbH", is_company=True, is_customer=True,
                            is_business=True)
        db.session.add(customer)
        db.session.commit()
        _login(client)
        client.post(f"/customers/{customer.id}/edit", data=_company_form(einvoice_b2b_fields="1"))
        assert db.session.get(Customer, customer.id).is_business is False

    def test_the_form_shows_the_current_value(self, client, admin):
        AppSetting.set("org.country", "DE")
        customer = Customer(name="Müller Bau GmbH", is_company=True, is_customer=True,
                            is_business=True)
        db.session.add(customer)
        db.session.commit()
        _login(client)
        html = client.get(f"/customers/{customer.id}/edit").get_data(as_text=True)
        assert 'id="is_business" name="is_business" value="1"' in html
        assert "checked" in html.split('id="is_business"', 1)[1].split(">", 1)[0]


class TestCustomerList:
    def _customers(self):
        db.session.add_all([
            Customer(name="Mit Mail", customer_number=1, is_business=True,
                     email="a@a.test", rechnung_per_email=True),
            Customer(name="Ohne Mail", customer_number=2, is_business=True),
            Customer(name="Ohne Zustimmung", customer_number=3, is_business=True,
                     email="c@c.test"),
            Customer(name="Privat", customer_number=4),
        ])
        db.session.commit()

    def test_filters(self, client, admin):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        self._customers()
        _login(client)
        everyone = client.get("/customers/").get_data(as_text=True)
        assert all(name in everyone for name in ("Mit Mail", "Ohne Mail", "Privat"))
        assert "Unternehmer ohne E-Mail-Versand" in everyone

        business = client.get("/customers/?business=yes").get_data(as_text=True)
        assert "Mit Mail" in business and "Ohne Mail" in business and "Privat" not in business

        no_email = client.get("/customers/?business=no_email").get_data(as_text=True)
        assert "Ohne Mail" in no_email and "Ohne Zustimmung" in no_email
        assert "Mit Mail" not in no_email and "Privat" not in no_email

    def test_the_badge_shows_for_businesses(self, client, admin):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        self._customers()
        _login(client)
        assert client.get("/customers/").get_data(as_text=True).count("Unternehmer</span>") == 3

    def test_nothing_of_it_in_austria(self, client, admin):
        self._customers()
        _login(client)
        html = client.get("/customers/").get_data(as_text=True)
        assert "Unternehmer ohne E-Mail-Versand" not in html
        assert "Unternehmer</span>" not in html


class TestDashboard:
    def _prepare(self, *, liable=True):
        year = date.today().year
        AppSetting.set("org.country", "DE")
        fy = FiscalYear.query.filter_by(year=year).first() or FiscalYear(
            year=year, start_date=date(year, 1, 1), end_date=date(year, 12, 31))
        fy.is_vat_liable = liable
        db.session.add(fy)
        db.session.add_all([
            Customer(name="Mit Mail", customer_number=1, is_business=True,
                     email="a@a.test", rechnung_per_email=True),
            Customer(name="Ohne Mail", customer_number=2, is_business=True),
        ])
        db.session.commit()

    def test_card_with_counts(self, client, admin):
        self._prepare()
        _login(client)
        html = client.get("/").get_data(as_text=True)
        assert "E-Rechnungspflicht ab 01.01.2028" in html
        assert "<strong>2</strong> Unternehmer-Kunden" in html
        assert "business=no_email" in html

    def test_the_stage_follows_the_setting(self, client, admin):
        self._prepare()
        AppSetting.set(obligation.MANDATE_KEY, "2027-01-01")
        db.session.commit()
        _login(client)
        assert "E-Rechnungspflicht ab 01.01.2027" in client.get("/").get_data(as_text=True)

    def test_hidden_for_small_businesses(self, client, admin):
        self._prepare(liable=False)
        _login(client)
        assert "E-Rechnungspflicht ab" not in client.get("/").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Rechnungslauf und Post-Versand
# ---------------------------------------------------------------------------

class TestBillingRunShipping:
    def test_a_required_invoice_without_email_is_not_offered_for_post(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        sent, draft = invoices
        _make_required(draft, email=False)
        run = _billing_run(draft)
        _login(client)
        html = client.get(f"/invoices/billing-runs/{run.id}").get_data(as_text=True)
        assert "Nur elektronisch zustellbar" in html
        assert obligation.REASON_REQUIRED in html
        assert "const BR_POST_IDS = [];" in html          # nichts zum Drucken
        assert "1 nur elektronisch" in html               # Zusammenfassung

    def test_a_required_invoice_with_email_goes_to_the_mail_list(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        _, draft = invoices
        _make_required(draft, email=True)
        run = _billing_run(draft)
        _login(client)
        html = client.get(f"/invoices/billing-runs/{run.id}").get_data(as_text=True)
        assert "Nur elektronisch zustellbar" not in html
        assert "buchhaltung@mueller-bau.example" in html
        assert "const BR_POST_IDS = [];" in html

    def test_an_xrechnung_customer_without_email_is_listed_with_an_xml_link(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        _, draft = invoices
        draft.customer.einvoice_format = "xrechnung"
        draft.customer.email, draft.customer.rechnung_per_email = None, False
        run = _billing_run(draft)
        _login(client)
        html = client.get(f"/invoices/billing-runs/{run.id}").get_data(as_text=True)
        assert obligation.REASON_XRECHNUNG in html
        assert f"/invoices/{draft.id}/einvoice.xml" in html

    def test_an_ordinary_customer_still_goes_to_the_post_list(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        _, draft = invoices
        draft.customer.rechnung_per_email = False
        run = _billing_run(draft)
        db.session.commit()
        _login(client)
        html = client.get(f"/invoices/billing-runs/{run.id}").get_data(as_text=True)
        assert "Nur elektronisch zustellbar" not in html
        assert f"const BR_POST_IDS = [{draft.id}];" in html

    def test_post_bulk_refuses_a_draft_that_must_be_electronic(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        sent, draft = invoices
        _make_required(draft, email=False)
        run = _billing_run(sent, draft)
        _login(client)
        r = client.post(f"/invoices/billing-runs/{run.id}/post-bulk-merged",
                        data={"invoice_ids": [str(sent.id), str(draft.id)]})
        # Nur der bereits versendete Beleg wird (nach-)gedruckt, der Entwurf bleibt Entwurf.
        assert r.status_code == 200
        assert len(PdfReader(io.BytesIO(r.data)).pages) == 1
        draft = db.session.get(Invoice, draft.id)
        assert draft.status == Invoice.STATUS_DRAFT and draft.pdf_path is None
        follow = client.get("/invoices/")
        assert "nicht gedruckt" in follow.get_data(as_text=True)

    def test_post_bulk_with_only_blocked_drafts_prints_nothing(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        _, draft = invoices
        _make_required(draft, email=False)
        run = _billing_run(draft)
        _login(client)
        r = client.post(f"/invoices/billing-runs/{run.id}/post-bulk-merged",
                        data={"invoice_ids": [str(draft.id)]})
        assert r.status_code == 302
        assert db.session.get(Invoice, draft.id).status == Invoice.STATUS_DRAFT
        assert not rendered                                # es wurde gar nichts gerendert


# ---------------------------------------------------------------------------
# Mailversand, Statuswechsel, Rechnungsdetail
# ---------------------------------------------------------------------------

class TestMailAndStatus:
    def test_a_business_always_gets_the_pdf_never_only_word(
            self, client, admin, rendered, pdf_dir, invoices, monkeypatch):
        from app.invoices import routes as inv_routes

        mails = []
        monkeypatch.setattr(inv_routes, "send_mail", mails.append)
        _ready_seller()
        sent, _ = invoices
        AppSetting.set("invoice.document_format", "docx")
        sent.customer.is_business = True
        db.session.commit()
        _login(client)
        client.post(f"/invoices/{sent.id}/send-email", data={"test_mode": "0"})
        assert [a.filename for a in mails[0].attachments] == ["2026-00042.pdf"]

    def test_marking_a_required_invoice_as_sent_warns(self, client, admin, rendered, pdf_dir,
                                                      invoices):
        _ready_seller()
        _, draft = invoices
        _make_required(draft, email=False)
        _login(client)
        r = client.post(f"/invoices/{draft.id}/status", data={"status": Invoice.STATUS_SENT},
                        follow_redirects=True)
        html = r.get_data(as_text=True)
        assert "muss als E-Rechnung" in html and "Papier oder ein reines PDF" in html
        assert db.session.get(Invoice, draft.id).status == Invoice.STATUS_SENT

    def test_no_warning_for_an_ordinary_customer(self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        _, draft = invoices
        _login(client)
        r = client.post(f"/invoices/{draft.id}/status", data={"status": Invoice.STATUS_SENT},
                        follow_redirects=True)
        assert "muss als E-Rechnung" not in r.get_data(as_text=True)

    def test_detail_shows_the_obligation_and_the_delivery_state(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        sent, _ = invoices
        _make_required(sent, email=True)
        _login(client)
        html = client.get(f"/invoices/{sent.id}").get_data(as_text=True)
        assert "Pflicht-E-Rechnung" in html
        assert "noch nicht elektronisch zugestellt" in html

        sent = db.session.get(Invoice, sent.id)
        sent.email_sent_at = sent.created_at
        sent.email_recipient = "buchhaltung@mueller-bau.example"
        sent.last_email_status = Invoice.EMAIL_STATUS_BOUNCED_HARD
        db.session.commit()
        html = client.get(f"/invoices/{sent.id}").get_data(as_text=True)
        assert "E-Rechnung nicht zugestellt" in html and "Unzustellbar" in html

        sent.last_email_status = Invoice.EMAIL_STATUS_DELIVERED
        db.session.commit()
        html = client.get(f"/invoices/{sent.id}").get_data(as_text=True)
        assert "Pflicht-E-Rechnung" in html
        assert "nicht zugestellt" not in html and "noch nicht elektronisch" not in html

    def test_detail_of_an_ordinary_invoice_has_no_obligation_block(
            self, client, admin, rendered, pdf_dir, invoices):
        _ready_seller()
        sent, _ = invoices
        _login(client)
        assert "Pflicht-E-Rechnung" not in client.get(f"/invoices/{sent.id}").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Einstellung: Stichtag
# ---------------------------------------------------------------------------

class TestMandateSettings:
    def test_german_tenants_can_pick_the_stage(self, client, admin):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        _login(client)
        html = client.get("/einstellungen/").get_data(as_text=True)
        assert 'name="einvoice_mandate_from"' in html
        assert '<option value="2028-01-01" selected>' in html
        client.post("/einstellungen/", data={"wg_name": "WG Test",
                                             "einvoice_mandate_from": "2027-01-01"})
        assert AppSetting.get(obligation.MANDATE_KEY) == "2027-01-01"
        html = client.get("/einstellungen/").get_data(as_text=True)
        assert '<option value="2027-01-01" selected>' in html

    def test_an_unknown_stage_is_ignored(self, client, admin):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        _login(client)
        client.post("/einstellungen/", data={"wg_name": "WG Test",
                                             "einvoice_mandate_from": "2030-01-01"})
        assert AppSetting.get(obligation.MANDATE_KEY) in (None, "")

    def test_austria_has_no_stage_field(self, client, admin):
        _login(client)
        assert 'name="einvoice_mandate_from"' not in client.get("/einstellungen/").get_data(as_text=True)
