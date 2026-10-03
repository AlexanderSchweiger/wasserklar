"""Vorübergehende Dateien (app/temp_files.py): Sammel-PDFs ohne festen Dateinamen, Zwischenstände der
Import-Assistenten im Mandantenordner, Aufräumen nach 24 Stunden."""
import io
import os
import time
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader, PdfWriter

from app import temp_files
from app.extensions import db
from app.imports import common as import_common
from app.models import BillingPeriod, Customer, Invoice, InvoiceItem, User
from tests.conftest import _ensure_role


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


def _pdf(width):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _writer(*widths):
    writer = PdfWriter()
    for width in widths:
        writer.append(io.BytesIO(_pdf(width)))
    return writer


def _age(path, hours):
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def _files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class TestSendTemporary:
    def test_two_merges_at_the_same_time_keep_their_own_file(self, app, tenant):
        """Früher teilten sich alle Sammeldrucke ``pdfs/_bulk_merged.pdf`` — wer zuerst startete,
        bekam unter Umständen die Datei des zweiten. Jetzt hat jede Antwort ihre eigene Temp-Datei."""
        with app.test_request_context():
            first = temp_files.send_pdf_writer(_writer(100), "A.pdf")
            second = temp_files.send_pdf_writer(_writer(200, 300), "B.pdf")   # startet, bevor A ausgeliefert ist
            first.direct_passthrough = second.direct_passthrough = False
            a, b = first.get_data(), second.get_data()
            first.close()
            second.close()
        assert [p.mediabox.width for p in PdfReader(io.BytesIO(a)).pages] == [100]
        assert [p.mediabox.width for p in PdfReader(io.BytesIO(b)).pages] == [200, 300]
        assert "attachment" in first.headers["Content-Disposition"] and "A.pdf" in first.headers["Content-Disposition"]

    def test_nothing_stays_on_disk(self, app, tenant):
        with app.test_request_context():
            resp = temp_files.send_pdf_writer(_writer(100), "A.pdf")
            resp.direct_passthrough = False
            resp.get_data()
            resp.close()
        assert _files(tenant) == []


class TestBulkRoutes:
    @pytest.fixture
    def invoices(self, app):
        user = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
        user.set_password("secret")
        period = BillingPeriod(name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), active=True)
        customer = Customer(name="Kunde", customer_number=1)
        db.session.add_all([user, period, customer])
        db.session.flush()
        ids = []
        for n in (1, 2):
            inv = Invoice(invoice_number=f"2026-0000{n}", customer_id=customer.id, billing_period_id=period.id,
                          date=date(2026, 3, 1), status=Invoice.STATUS_DRAFT)
            db.session.add(inv)
            db.session.flush()
            db.session.add(InvoiceItem(invoice_id=inv.id, description="Wasser", quantity=Decimal("1"),
                                       unit="m³", unit_price=Decimal("1"), amount=Decimal("1"),
                                       tax_rate=Decimal("10")))
            ids.append(inv.id)
        db.session.commit()
        return ids

    def test_the_merged_invoice_pdf_leaves_no_file(self, app, client, tenant, invoices, monkeypatch):
        import sys
        import types
        monkeypatch.setitem(sys.modules, "weasyprint", types.ModuleType("weasyprint"))   # Verfuegbarkeitspruefung
        widths = iter([111, 222])
        monkeypatch.setattr("app.invoices.routes.render_invoice_pdf", lambda invoice, **kw: _pdf(next(widths)))
        client.get("/auth/logout")
        client.post("/auth/login", data={"username": "admin", "password": "secret"})
        r = client.post("/invoices/bulk-pdf-merged", data={"invoice_ids": invoices})
        assert r.status_code == 200
        assert [p.mediabox.width for p in PdfReader(io.BytesIO(r.data)).pages] == [111, 222]
        r.close()
        assert not (tenant / "pdfs" / "_bulk_merged.pdf").exists()
        assert [f for f in _files(tenant) if not f.startswith("documents/")] == []


class TestWizardFiles:
    def test_dataframes_live_in_the_tenant_tmp_folder(self, app, tenant):
        import pandas as pd
        path = import_common.save_dataframe(pd.DataFrame({"a": [1]}), prefix="customer_import_")
        assert os.path.dirname(path) == str((tenant / "tmp" / "wizard").resolve()) or \
            os.path.dirname(path) == str(tenant / "tmp" / "wizard")
        assert import_common.load_dataframe(path)["a"].tolist() == [1]
        import_common.delete_dataframe(path)
        assert not os.path.exists(path)

    def test_paths_outside_the_wizard_folder_are_never_unpickled(self, app, tenant, tmp_path):
        import pandas as pd
        foreign = tmp_path / "fremd.pkl"
        pd.DataFrame({"a": [1]}).to_pickle(foreign)
        legacy = tenant / "import_0123456789abcdef0123456789abcdef.pkl"     # Altpfad im Instanzordner
        pd.DataFrame({"a": [1]}).to_pickle(legacy)
        assert import_common.load_dataframe(str(foreign)) is None
        assert import_common.load_dataframe(str(legacy)) is None
        import_common.delete_dataframe(str(foreign))
        assert foreign.exists()                                            # fremde Dateien bleiben

    def test_a_new_wizard_clears_abandoned_ones(self, app, tenant):
        old = temp_files.wizard_path("meter_import_", "pkl")
        open(old, "wb").close()
        _age(old, 30)
        fresh = temp_files.wizard_path("meter_import_", "pkl")
        assert not os.path.exists(old) and fresh != old


class TestCleanup:
    def test_cleanup_stale(self, app, tenant):
        wizard = tenant / "tmp" / "wizard"
        imports = tenant / "tmp" / "imports" / "abc"
        legacy_dir = tenant / "pdfs" / "_bulk"
        for folder in (wizard, imports, legacy_dir):
            folder.mkdir(parents=True)
        files = {
            "old_wizard": wizard / "x.pkl", "fresh_wizard": wizard / "y.pkl", "old_import": imports / "m.json",
            "old_bulk": tenant / "pdfs" / "_bulk_merged.pdf", "old_dunning": legacy_dir / "Mahnungen_gesamt.pdf",
            "document": tenant / "documents" / "2026" / "10" / "1_aabbccdd.pdf",
        }
        for path in files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        for key in ("old_wizard", "old_bulk", "old_dunning", "document"):
            _age(files[key], 30)
        _age(imports, 30)
        removed, _freed = temp_files.cleanup_stale()
        assert removed == 4
        assert files["fresh_wizard"].exists() and files["document"].exists()
        assert not files["old_wizard"].exists() and not imports.exists()
        assert not files["old_bulk"].exists() and not files["old_dunning"].exists()

    def test_legacy_instance_files_only_by_name(self, app, tmp_path):
        names = ["customer_import_0123456789abcdef0123456789abcdef.pkl",
                 "network_import_0123456789abcdef0123456789abcdef.json",
                 "wg.db", "bev_addresses.sqlite", "import_kaputt.pkl"]
        for name in names:
            (tmp_path / name).write_bytes(b"x")
            _age(tmp_path / name, 48)
        removed, _ = temp_files.cleanup_legacy_instance_files(str(tmp_path))
        assert removed == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == ["bev_addresses.sqlite", "import_kaputt.pkl", "wg.db"]

    def test_cli(self, app, tenant, tmp_path, monkeypatch):
        instance = tmp_path / "instance"            # nie den echten Instanzordner aufraeumen
        instance.mkdir()
        monkeypatch.setattr(app, "instance_path", str(instance))
        old = tenant / "tmp" / "wizard" / "x.pkl"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x")
        _age(old, 30)
        result = app.test_cli_runner().invoke(args=["cleanup-temp"])
        assert result.exit_code == 0 and "1 vorübergehende Datei(en) entfernt" in result.output
        assert not old.exists()
