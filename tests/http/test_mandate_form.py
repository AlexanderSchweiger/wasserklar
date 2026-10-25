"""SEPA-Lastschrift-Mandat: Formular, Beilage im Rechnungsdruck, Sammeldruck, Einstellungen,
online erteiltes Mandat + Nachweis (app/direct_debit/mandate_form.py)."""
import io
import sys
import types
from datetime import date

import pytest
from pypdf import PdfReader, PdfWriter

from app.direct_debit import mandate_form
from app.extensions import db
from app.models import AppSetting, Customer, DirectDebitMandateEvent
from tests.http.test_direct_debit_routes import _login, admin  # noqa: F401
from tests.integration.test_direct_debit import IBAN, _customer, _invoice, setup  # noqa: F401
from tests.integration.test_document_transfer import do_import, export, tenant  # noqa: F401


@pytest.fixture
def pdfs(monkeypatch):
    """WeasyPrint-Ersatz: merkt sich jedes gerenderte HTML, liefert ein einseitiges PDF."""
    rendered = []

    def _pdf():
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    class FakeDocument:
        metadata = types.SimpleNamespace(attachments=[], xmp_metadata=[])

        def write_pdf(self, target=None, **kwargs):
            return _pdf()

    class FakeHTML:
        def __init__(self, string=None, **kwargs):
            rendered.append(string or "")

        def render(self, **kwargs):
            return FakeDocument()

        def write_pdf(self, target=None, **kwargs):
            return _pdf()

    module = types.ModuleType("weasyprint")
    module.HTML = FakeHTML
    module.Attachment = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    return rendered


def _forms(rendered):
    return [html for html in rendered if "SEPA-Lastschrift-Mandat</h1>" in html]


def _transfer(number, name="Huber Franz"):
    return _customer(number, debit=False, name=name)


# ---------------------------------------------------------------------------
# Wer braucht ein Formular, was steht drauf
# ---------------------------------------------------------------------------

class TestNeedsForm:
    def test_rules(self, app, setup):
        transfer = _transfer(40)
        complete = _customer(41)
        incomplete = _customer(42, signed=None)
        supplier = Customer(name="Rohr AG", is_customer=False, is_supplier=True, active=True)
        db.session.add(supplier)
        db.session.commit()
        assert mandate_form.needs_mandate_form(transfer)
        assert not mandate_form.needs_mandate_form(complete)
        assert mandate_form.needs_mandate_form(incomplete)
        assert not mandate_form.needs_mandate_form(supplier)
        AppSetting.set("sepa.creditor_id", None)
        db.session.commit()
        assert not mandate_form.needs_mandate_form(transfer)

    def test_no_mandate_clause_matches_needs(self, app, setup):
        ids = {_transfer(40).id, _customer(42, signed=None).id}
        _customer(41)
        db.session.commit()
        found = {c.id for c in Customer.query.filter(mandate_form.no_mandate_clause())}
        assert found == ids


class TestRender:
    def test_prefilled_without_writing_the_reference(self, app, setup):
        AppSetting.set("sepa.mandate_prefix", "WG-")
        c = _transfer(40)
        db.session.commit()
        html = mandate_form.render_mandate_html(c)
        assert "WG-40" in html and "AT61ZZZ01234567890" in html
        assert "Wassergenossenschaft Treffling" in html and "Franz Huber" in html
        assert "acht Wochen" in html and "Poolbefüllung" in html
        assert "Schneller geht es online" not in html
        db.session.expire_all()
        assert db.session.get(Customer, c.id).mandate_reference is None

    def test_custom_scope_and_intro(self, app, setup):
        AppSetting.set(mandate_form.SCOPE_KEY, "für Wassergebühren")
        AppSetting.set(mandate_form.INTRO_KEY, "Liebe/r {{ name }}, Ref {{ mandatsreferenz }}")
        c = _transfer(40)
        db.session.commit()
        html = mandate_form.render_mandate_html(c)
        assert "Zahlungen für Wassergebühren von meinem" in html
        assert "Liebe/r Franz Huber, Ref 40" in html
        assert "Poolbefüllung" not in html

    def test_broken_intro_falls_back(self, app, setup):
        AppSetting.set(mandate_form.INTRO_KEY, "{{ name ")
        c = _transfer(40)
        db.session.commit()
        assert "bequem per Lastschrift" in mandate_form.render_mandate_html(c)

    def test_overflow_renders_denser_until_one_page(self, app, setup, monkeypatch):
        """Ein Mandat zum Unterschreiben muss auf EINE Seite: bei Ueberlauf dichter rendern."""
        c = _transfer(40)
        db.session.commit()
        seen = []

        class Doc:
            def __init__(self, pages):
                self.pages = [object()] * pages

            def write_pdf(self):
                return b"%PDF-fake"

        class HTML:
            def __init__(self, string):
                seen.append(string)

            def render(self):
                # Stufe 0 und 1 laufen ueber, Stufe 2 (ohne Einleitung) passt.
                return Doc(1 if "bequem per Lastschrift" not in seen[-1] and len(seen) == 3 else 2)

        monkeypatch.setattr(mandate_form, "_weasyprint", lambda: HTML)
        assert mandate_form.render_mandate_pdf(c) == b"%PDF-fake"
        assert len(seen) == 3
        assert "font-size: 9.5pt" in seen[0] and "font-size: 8.5pt" in seen[1]
        assert "bequem per Lastschrift" in seen[1] and "bequem per Lastschrift" not in seen[2]

    def test_provider_adds_online_block_and_errors_are_swallowed(self, app, setup):
        c = _transfer(40)
        db.session.commit()

        def broken(customer):
            raise RuntimeError("kaputt")

        def online(customer):
            return {"online_mandate": {"url": "https://x.test/m/ABCD", "qr_src": "", "code": "ABCD"}}

        mandate_form.register_mandate_form_context_provider(broken)
        mandate_form.register_mandate_form_context_provider(online)
        try:
            html = mandate_form.render_mandate_html(c)
        finally:
            mandate_form.reset_mandate_form_context_providers()
        assert "Schneller geht es online" in html and "https://x.test/m/ABCD" in html


# ---------------------------------------------------------------------------
# Beilage im Rechnungsdruck
# ---------------------------------------------------------------------------

def _pages(response):
    return len(PdfReader(io.BytesIO(response.data)).pages)


class TestEnclosure:
    def _invoices(self, setup):
        transfer = _transfer(40)
        complete = _customer(41, name="Gruber Eva")
        inv = [
            _invoice(transfer, setup["account"], number="2026-00001"),
            _invoice(transfer, setup["account"], number="2026-00002"),
            _invoice(complete, setup["account"], number="2026-00003"),
        ]
        return [i.id for i in inv]

    def test_off_by_default(self, client, admin, setup, tenant, pdfs):
        ids = self._invoices(setup)
        _login(client)
        r = client.post("/invoices/bulk-pdf-merged", data={"invoice_ids": ids})
        assert r.status_code == 200 and _pages(r) == 3
        assert not _forms(pdfs)

    def test_once_per_customer_without_mandate(self, client, admin, setup, tenant, pdfs):
        AppSetting.set(mandate_form.ENCLOSURE_KEY, "true")
        ids = self._invoices(setup)
        _login(client)
        r = client.post("/invoices/bulk-pdf-merged", data={"invoice_ids": ids})
        assert _pages(r) == 4
        forms = _forms(pdfs)
        assert len(forms) == 1 and "Franz Huber" in forms[0]

    def test_post_print_of_a_run(self, client, admin, setup, tenant, pdfs):
        from decimal import Decimal
        from app.models import BillingRun
        AppSetting.set(mandate_form.ENCLOSURE_KEY, "true")
        transfer = _transfer(40)
        inv = _invoice(transfer, setup["account"], number="2026-00010", send=False)
        run = BillingRun(billing_period_id=inv.billing_period_id, tariff_name="T",
                         tariff_price_per_m3=Decimal("2.5"))
        db.session.add(run)
        db.session.flush()
        inv.billing_run_id = run.id
        db.session.commit()
        _login(client)
        r = client.post(f"/invoices/billing-runs/{run.id}/post-bulk-merged",
                        data={"invoice_ids": [inv.id]})
        assert r.status_code == 200 and _pages(r) == 2
        assert len(_forms(pdfs)) == 1

    def test_never_behind_a_credit_note(self, app, setup):
        AppSetting.set(mandate_form.ENCLOSURE_KEY, "true")
        transfer = _transfer(40)
        inv = _invoice(transfer, setup["account"])
        inv.invoice_kind = "credit_note"
        db.session.commit()
        appender = mandate_form.EnclosureAppender()
        writer = PdfWriter()
        appender.after(writer, inv)
        assert appender.count == 0 and len(writer.pages) == 0


# ---------------------------------------------------------------------------
# Routen: einzeln, Sammeldruck, Filter
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_single_form(self, client, admin, setup, pdfs):
        c = _transfer(40)
        db.session.commit()
        _login(client)
        r = client.get(f"/customers/{c.id}/mandate-form.pdf")
        assert r.status_code == 200 and r.mimetype == "application/pdf"
        assert len(_forms(pdfs)) == 1
        page = client.get(f"/customers/{c.id}").get_data(as_text=True)
        assert f"/customers/{c.id}/mandate-form.pdf" in page

    def test_single_form_needs_creditor_id(self, client, admin, setup, pdfs):
        AppSetting.set("sepa.creditor_id", None)
        c = _transfer(40)
        db.session.commit()
        _login(client)
        r = client.get(f"/customers/{c.id}/mandate-form.pdf")
        assert r.status_code == 302 and not pdfs
        assert "mandate-form.pdf" not in client.get(f"/customers/{c.id}").get_data(as_text=True)

    def test_list_filter_no_mandate(self, client, admin, setup):
        _transfer(40, name="Huber Franz")
        _customer(41, name="Gruber Eva")
        db.session.commit()
        _login(client)
        html = client.get("/customers/?payment=no_mandate", headers={"HX-Request": "true"}) \
            .get_data(as_text=True)
        assert "Huber" in html and "Gruber" not in html

    def test_preview_counts_and_splits(self, client, admin, setup, app, monkeypatch):
        monkeypatch.setitem(app.config, "BULK_PRINT_MAX", 2)
        for n in range(40, 45):
            _transfer(n, name=f"Kunde{n} Max")
        _customer(50, name="Gruber Eva")
        db.session.commit()
        _login(client)
        html = client.get("/customers/mandate-forms/preview").get_data(as_text=True)
        assert "<strong>5</strong>" in html
        assert "Teil 3 (5–5)" in html
        html = client.get("/customers/mandate-forms/preview?q=Kunde40").get_data(as_text=True)
        assert "<strong>1</strong>" in html and "PDF erstellen" in html

    def test_bulk_print_part(self, client, admin, setup, app, monkeypatch, pdfs):
        monkeypatch.setitem(app.config, "BULK_PRINT_MAX", 2)
        for n in range(40, 45):
            _transfer(n, name=f"Kunde{n} Max")
        db.session.commit()
        _login(client)
        r = client.post("/customers/mandate-forms", data={"part": "2", "sort": "nr"})
        assert r.status_code == 200 and _pages(r) == 1
        assert "Max Kunde44" in _forms(pdfs)[0]
        r = client.post("/customers/mandate-forms", data={"part": "0", "type": "supplier"})
        assert r.status_code == 302


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

class TestSettings:
    def _post(self, client, **extra):
        data = {"sepa_fields": "1", "sepa_creditor_id": "AT61ZZZ01234567890", **extra}
        return client.post("/einstellungen/", data=data, follow_redirects=True)

    def test_save(self, client, admin, setup):
        _login(client)
        self._post(client, sepa_mandate_scope="  für   Wassergebühren ",
                   sepa_mandate_form_intro="Hallo {{ name }}",
                   sepa_mandate_form_enclosure="on")
        assert AppSetting.get(mandate_form.SCOPE_KEY) == "für Wassergebühren"
        assert AppSetting.get(mandate_form.INTRO_KEY) == "Hallo {{ name }}"
        assert AppSetting.get(mandate_form.ENCLOSURE_KEY) == "true"
        self._post(client)
        assert AppSetting.get(mandate_form.SCOPE_KEY) is None
        assert AppSetting.get(mandate_form.ENCLOSURE_KEY) is None

    def test_broken_intro_is_rejected(self, client, admin, setup):
        _login(client)
        r = self._post(client, sepa_mandate_form_intro="{{ name ")
        assert "Einleitungstext des Mandatsformulars" in r.get_data(as_text=True)
        assert AppSetting.get(mandate_form.INTRO_KEY) is None


# ---------------------------------------------------------------------------
# Online erteiltes Mandat + Nachweis
# ---------------------------------------------------------------------------

class TestOnlineGrant:
    def test_validate_account(self, app):
        values, errors = mandate_form.validate_account("at61 1904 3002 3457 3201", "rzooat2l", " Anna ")
        assert not errors
        assert values == {"iban": IBAN, "bic": "RZOOAT2L", "account_holder": "Anna"}
        _values, errors = mandate_form.validate_account("AT61 1904 3002 3457 3202", "", "")
        assert any("Kontoinhaber" in e for e in errors) and any("IBAN ist ungültig" in e for e in errors)
        _values, errors = mandate_form.validate_account("US64SVBKUS6S3300958879", "XYZ", "A")
        assert any("SEPA" in e for e in errors) and any("BIC" in e for e in errors)

    def test_grant_switches_to_direct_debit_and_logs(self, app, setup):
        AppSetting.set("sepa.mandate_prefix", "WG-")
        c = _transfer(40)
        db.session.commit()
        event = mandate_form.record_online_grant(
            c, iban=IBAN, bic=None, account_holder="Franz Huber", email="f@h.test",
            ip="10.0.0.1", user_agent="pytest", today=date(2026, 10, 25))
        db.session.commit()
        c = db.session.get(Customer, c.id)
        assert c.is_direct_debit and c.iban == IBAN and c.mandate_signed_on == date(2026, 10, 25)
        assert c.mandate_reference == "WG-40" and not c.debit_issues
        assert event.mandate_reference == "WG-40" and event.creditor_id == "AT61ZZZ01234567890"
        assert "acht Wochen" in event.mandate_text
        assert event.mandate_text_version == mandate_form.MANDATE_TEXT_VERSION
        assert not mandate_form.needs_mandate_form(c)

    def test_existing_reference_is_kept(self, app, setup):
        c = _customer(41)
        ref = c.mandate_reference
        db.session.commit()
        mandate_form.record_online_grant(c, iban="AT483200000012345864", bic=None,
                                         account_holder="Anna")
        db.session.commit()
        assert db.session.get(Customer, c.id).mandate_reference == ref

    def test_proof_route(self, client, admin, setup, pdfs):
        c = _transfer(40)
        other = _transfer(41, name="Gruber Eva")
        db.session.commit()
        event = mandate_form.record_online_grant(c, iban=IBAN, bic=None, account_holder="Franz Huber",
                                                 ip="10.0.0.1")
        db.session.commit()
        _login(client)
        page = client.get(f"/customers/{c.id}").get_data(as_text=True)
        assert "Mandat online erteilt" in page and "10.0.0.1" in page
        r = client.get(f"/customers/{c.id}/mandate-proof/{event.id}.pdf")
        assert r.status_code == 200
        assert "AT61 1904 3002 3457 3201" in pdfs[-1] and "10.0.0.1" in pdfs[-1]
        assert client.get(f"/customers/{other.id}/mandate-proof/{event.id}.pdf").status_code == 404

    def test_events_travel_with_the_export(self, app, setup, tenant):  # noqa: F811
        c = _transfer(40)
        db.session.commit()
        mandate_form.record_online_grant(c, iban=IBAN, bic=None, account_holder="Franz Huber")
        db.session.commit()
        data = export(include_pdfs=False)
        do_import(data, tenant, mode="replace")
        event = DirectDebitMandateEvent.query.one()
        assert event.customer.customer_number == 40 and event.iban == IBAN
