"""Tests fuer den zentralen Rechnungs-PDF-Weg (app/invoices/pdf_service.py).

Alle Rechnungs-PDFs — Einzel-PDF, ZIP, Sammel-PDF, Mail-Anhang, Post-Versand
eines Rechnungslaufs — entstehen ueber ``render_invoice_pdf`` und werden ueber
``write_invoice_pdf`` abgelegt. Hier wird geprueft, dass jeder Weg diesen
Render-Pfad nutzt und die Archiv-Semantik unveraendert bleibt: ``pdf_path``
bekommen nur gesperrte Rechnungen, ein vorhandenes Archiv-PDF wird wieder
ausgeliefert statt neu gerendert.

WeasyPrint ist lokal (Windows ohne GTK) nicht ladbar. Die Tests setzen deshalb
ein Ersatz-Modul ``weasyprint`` ein, das echte (leere) PDF-Seiten liefert —
so laufen auch die Merge- und ZIP-Pfade, die das Ergebnis mit pypdf weiter-
verarbeiten.
"""
import io
import os
import sys
import types
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader, PdfWriter

from app.extensions import db
from app.invoices import render_hooks
from app.invoices.pdf_service import render_invoice_pdf, write_invoice_pdf
from app.models import BillingPeriod, BillingRun, Customer, Invoice, InvoiceItem, User
from tests.conftest import _ensure_role


def _blank_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def rendered(monkeypatch):
    """Ersetzt WeasyPrint; die Liste sammelt jedes gerenderte HTML."""
    calls = []

    class FakeHTML:
        def __init__(self, string=None, **kwargs):
            calls.append(string)

        def render(self, **kwargs):
            return self

        def write_pdf(self, target=None, **kwargs):
            data = _blank_pdf()
            if target is None:
                return data
            with open(target, "wb") as fh:
                fh.write(data)
            return None

    module = types.ModuleType("weasyprint")
    module.HTML = FakeHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    return calls


@pytest.fixture
def pdf_dir(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "PDF_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    user = User(username="admin", email="a@a.test", role_id=role.id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _make_invoice(number, status, customer, period):
    inv = Invoice(invoice_number=number, customer_id=customer.id,
                  billing_period_id=period.id, date=date(2026, 3, 1),
                  due_date=date(2026, 3, 31), status=status)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Wasserverbrauch", quantity=Decimal("40"),
        unit="m³", unit_price=Decimal("2.5"), amount=Decimal("100"),
        tax_rate=Decimal("10")))
    db.session.flush()
    inv.recalculate_total()
    return inv


@pytest.fixture
def invoices(app):
    """Eine versendete (gesperrte) Rechnung und ein Entwurf desselben Kunden."""
    period = BillingPeriod(name="2026", start_date=date(2026, 1, 1),
                           end_date=date(2026, 12, 31), active=True)
    cust = Customer(name="Kunde Test", customer_number=7,
                    email="kunde@test.local", rechnung_per_email=True)
    db.session.add_all([period, cust])
    db.session.flush()
    sent = _make_invoice("2026-00042", Invoice.STATUS_SENT, cust, period)
    draft = _make_invoice("2026-00043", Invoice.STATUS_DRAFT, cust, period)
    db.session.commit()
    return sent, draft


class TestService:
    def test_renders_the_invoice_html(self, app, rendered, invoices):
        sent, _ = invoices
        with app.test_request_context():
            pdf = render_invoice_pdf(sent)
        assert pdf.startswith(b"%PDF")
        assert len(rendered) == 1
        assert "2026-00042" in rendered[0]

    def test_for_email_reaches_the_context_providers(self, app, rendered, invoices):
        sent, _ = invoices
        seen = []

        def provider(invoice, *, for_email):
            seen.append(for_email)
            return None

        render_hooks.register_pdf_context_provider(provider)
        try:
            with app.test_request_context():
                render_invoice_pdf(sent, for_email=True)
        finally:
            render_hooks._PROVIDERS.remove(provider)
        assert seen == [True]

    def test_write_versions_files_and_does_not_archive(self, app, pdf_dir, invoices):
        sent, _ = invoices
        with app.test_request_context():
            first = write_invoice_pdf(sent, b"%PDF-1")
            second = write_invoice_pdf(sent, b"%PDF-2")
        assert first == os.path.join(str(pdf_dir), "2026", "2026-00042.pdf")
        assert second == os.path.join(str(pdf_dir), "2026", "2026-00042_V2.pdf")
        with open(second, "rb") as fh:
            assert fh.read() == b"%PDF-2"
        assert sent.pdf_path is None


class TestRoutes:
    def test_single_pdf_archives_a_locked_invoice(self, client, admin, rendered,
                                                  pdf_dir, invoices):
        sent, _ = invoices
        _login(client)
        r = client.get(f"/invoices/{sent.id}/pdf")
        assert r.status_code == 200
        assert r.data.startswith(b"%PDF")
        archived = db.session.get(Invoice, sent.id).pdf_path
        assert archived and os.path.exists(archived)
        # Zweiter Abruf liefert das Archiv-PDF aus, ohne neu zu rendern.
        r = client.get(f"/invoices/{sent.id}/pdf")
        assert r.status_code == 200
        assert len(rendered) == 1

    def test_single_pdf_does_not_archive_a_draft(self, client, admin, rendered,
                                                 pdf_dir, invoices):
        _, draft = invoices
        _login(client)
        r = client.get(f"/invoices/{draft.id}/pdf")
        assert r.status_code == 200
        assert r.data.startswith(b"%PDF")
        assert db.session.get(Invoice, draft.id).pdf_path is None

    def test_zip_contains_one_pdf_per_invoice(self, client, admin, rendered,
                                              pdf_dir, invoices):
        sent, draft = invoices
        _login(client)
        r = client.post("/invoices/bulk-pdf-zip",
                        data={"invoice_ids": [str(sent.id), str(draft.id)]})
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            assert sorted(zf.namelist()) == ["2026-00042.pdf", "2026-00043.pdf"]
            assert zf.read("2026-00042.pdf").startswith(b"%PDF")
        assert db.session.get(Invoice, sent.id).pdf_path is not None
        assert db.session.get(Invoice, draft.id).pdf_path is None

    def test_merged_pdf_has_a_page_per_invoice(self, client, admin, rendered,
                                               pdf_dir, invoices):
        sent, draft = invoices
        _login(client)
        r = client.post("/invoices/bulk-pdf-merged",
                        data={"invoice_ids": [str(sent.id), str(draft.id)]})
        assert r.status_code == 200
        assert len(PdfReader(io.BytesIO(r.data)).pages) == 2
        assert len(rendered) == 2
        assert db.session.get(Invoice, sent.id).pdf_path is not None
        assert db.session.get(Invoice, draft.id).pdf_path is None

    def test_email_attaches_the_rendered_pdf(self, client, admin, rendered,
                                             pdf_dir, invoices, monkeypatch):
        from app.invoices import routes as inv_routes

        sent_mails = []
        monkeypatch.setattr(inv_routes, "send_mail", sent_mails.append)
        sent, _ = invoices
        _login(client)
        client.post(f"/invoices/{sent.id}/send-email", data={"test_mode": "0"})
        assert len(sent_mails) == 1
        attachment = sent_mails[0].attachments[0]
        assert attachment.filename == "2026-00042.pdf"
        assert attachment.data.startswith(b"%PDF")
        archived = db.session.get(Invoice, sent.id).pdf_path
        assert archived and os.path.exists(archived)

    def test_post_bulk_sends_drafts_and_archives(self, client, admin, rendered,
                                                 pdf_dir, invoices):
        sent, draft = invoices
        run = BillingRun(billing_period_id=draft.billing_period_id, tariff_name="T",
                         tariff_price_per_m3=Decimal("2.5"))
        db.session.add(run)
        db.session.flush()
        for inv in (sent, draft):
            inv.billing_run_id = run.id
        db.session.commit()
        _login(client)
        r = client.post(f"/invoices/billing-runs/{run.id}/post-bulk-merged",
                        data={"invoice_ids": [str(sent.id), str(draft.id)]})
        assert r.status_code == 200
        assert len(PdfReader(io.BytesIO(r.data)).pages) == 2
        draft = db.session.get(Invoice, draft.id)
        assert draft.status == Invoice.STATUS_SENT
        assert draft.pdf_path and os.path.exists(draft.pdf_path)
