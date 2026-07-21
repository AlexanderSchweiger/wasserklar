"""HTTP-Tests fuer die Mahnungs-Uebersicht („Mailing & Druck").

Deckt die drei Dinge ab, die hier real kaputt waren bzw. neu sind:
1. Die Liste rendert das Seiten-Skript **genau einmal** und ohne top-level
   ``const`` — ein zweites globales ``BULK_PRINT_MAX`` (die Rechnungsliste
   deklariert dasselbe) liess das ganze Skript beim hx-boost-Reexecute mit
   "Identifier has already been declared" sterben; damit war die Mehrfachauswahl
   tot.
2. Der Versandart-Filter trennt Mail- von Post-Kunden.
3. ``bulk_post_pdf`` markiert nur versandbereite Mahnungen als Post-Versand.
"""
import re
from datetime import date

import pytest

from app.extensions import db
from app.models import (
    Customer, DunningNotice, Invoice, User,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


def _notice(name, *, email, per_mail, invoice_number, status="Aktiv", sent_at=None):
    c = Customer(name=name, email=email, rechnung_per_email=per_mail)
    db.session.add(c)
    db.session.flush()
    inv = Invoice(customer_id=c.id, invoice_number=invoice_number,
                  date=date(2026, 1, 1), total_amount=100)
    db.session.add(inv)
    db.session.flush()
    n = DunningNotice(invoice_id=inv.id, level_snapshot=1, name_snapshot="1. Mahnung",
                      issued_date=date(2026, 2, 1), fee_amount=0,
                      status=status, sent_at=sent_at)
    db.session.add(n)
    db.session.commit()
    return n


class TestPageScript:
    def test_no_toplevel_const_in_page_script(self, client, admin):
        """Top-level const/let im Seiten-Skript = SyntaxError beim hx-boost-
        Reexecute -> gesamtes Skript (inkl. Select-All) tot."""
        _login(client)
        html = client.get("/dunning/notices").get_data(as_text=True)
        script = html.split("<script>")[-1].split("</script>")[0]
        offenders = re.findall(r"^(const|let)\s+\w+", script, re.MULTILINE)
        assert not offenders, f"top-level {offenders} kollidiert beim Re-Execute"

    def test_selection_and_versand_ui_present(self, client, admin):
        _login(client)
        _notice("Mailer", email="m@x.at", per_mail=True, invoice_number="2026-1")
        html = client.get("/dunning/notices").get_data(as_text=True)
        assert 'id="dn-select-all"' in html
        assert 'class="dn-cb"' in html
        assert 'id="dnVersandModal"' in html
        assert "dnOpenVersand()" in html
        assert 'data-versandart="mail"' in html
        assert 'data-sendable="true"' in html


class TestVersandFilter:
    def test_filters_mail_and_post(self, client, admin):
        _login(client)
        _notice("Mailer", email="m@x.at", per_mail=True, invoice_number="2026-1")
        _notice("Poster", email="p@x.at", per_mail=False, invoice_number="2026-2")
        _notice("Ohne Mail", email=None, per_mail=True, invoice_number="2026-3")

        alle = client.get("/dunning/notices").get_data(as_text=True)
        assert "Mailer" in alle and "Poster" in alle and "Ohne Mail" in alle

        mail = client.get("/dunning/notices?versand=mail").get_data(as_text=True)
        assert "Mailer" in mail
        assert "Poster" not in mail
        # rechnung_per_email=True ohne Adresse ist KEIN Mail-Versand.
        assert "Ohne Mail" not in mail

        post = client.get("/dunning/notices?versand=post").get_data(as_text=True)
        assert "Poster" in post and "Ohne Mail" in post
        assert "Mailer" not in post


class TestPostBulk:
    def test_marks_only_sendable_as_post(self, client, admin, monkeypatch):
        """Der Post-Download markiert versandbereite Mahnungen als versendet;
        bereits versendete bleiben unveraendert (Nachdruck)."""
        import sys
        import types
        import app.dunning.routes as dr
        from datetime import datetime

        # WeasyPrint fehlt lokal (GTK3 nur im Container) — die Route bricht sonst
        # vor dem Rendern mit einem Redirect ab. Gerendert wird hier ohnehin nicht
        # (``_render_dunning_pdf_bytes`` ist gepatcht), es geht um die Seiteneffekte.
        monkeypatch.setitem(sys.modules, "weasyprint", types.ModuleType("weasyprint"))

        _login(client)
        offen = _notice("Poster", email=None, per_mail=False, invoice_number="2026-1")
        frueher = datetime(2026, 1, 5, 8, 0, 0)
        schon = _notice("Alt", email=None, per_mail=False, invoice_number="2026-2",
                        sent_at=frueher)
        db.session.query(DunningNotice).filter_by(id=schon.id).update(
            {"sent_via": "email", "sent_to": "alt@x.at"})
        db.session.commit()

        monkeypatch.setattr(dr, "_render_dunning_pdf_bytes", lambda n: b"%PDF-1.4 fake")
        monkeypatch.setattr(dr, "_freeze_dunning_document",
                            lambda n, ext, data: "/tmp/fake.pdf")

        captured = {}

        class _Writer:
            def append(self, src): captured.setdefault("n", 0)
            def compress_identical_objects(self): pass
            def write(self, fh): fh.write(b"%PDF-1.4 merged")
            def close(self): pass

        monkeypatch.setattr("pypdf.PdfWriter", _Writer)

        resp = client.post("/dunning/bulk-post-pdf",
                           data={"notice_ids": [offen.id, schon.id]})
        assert resp.status_code == 200, resp.status_code

        db.session.expire_all()
        offen = db.session.get(DunningNotice, offen.id)
        schon = db.session.get(DunningNotice, schon.id)
        assert offen.sent_via == "post" and offen.sent_to == "Post"
        assert offen.sent_at is not None
        # Der schon versendete bleibt unangetastet.
        assert schon.sent_via == "email" and schon.sent_at == frueher

    def test_empty_selection_redirects(self, client, admin):
        _login(client)
        resp = client.post("/dunning/bulk-post-pdf", data={})
        assert resp.status_code == 302
