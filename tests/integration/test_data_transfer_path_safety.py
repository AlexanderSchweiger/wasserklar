"""data_transfer-Import: Dateipfade und Namen aus der ZIP sind Nutzereingabe.

Der Voll-Import (Recht ``verwaltung`` — im SaaS jeder Mandanten-Admin) uebernahm
Pfadspalten (``invoices.pdf_path`` …) roh aus der hochgeladenen ZIP, und die
Download-Routen lieferten den Pfad per ``send_file`` aus. Im SaaS liegen alle
Mandanten im selben Dateisystem: ein absoluter Pfad auf
``tenants/<fremd>/backups/db/<dump>.sql.gz`` war ein Cross-Tenant-Lesezugriff.

Abgedeckt:
- Import: Pfadspalten nie aus der ZIP (beide Modi), Bundle-Dateien nur aus dem
  Bundle (kein ``..``), Ziel ist der Mandanten-``PDF_DIR``; Schriftfuehrung
  behaelt nur Pfade im eigenen ``schriftverkehr/``.
- Entpacken: Zip-Slip, Groessen- und Anzahl-Limit.
- Manifest: Tabellennamen landen nie im SQL.
- Defense in depth: Routen liefern einen fremden Pfad in Altdaten nicht aus.
"""
import hashlib
import io
import json
import os
import re
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import g

from app.data_transfer.registry import FILE_PATH_COLS, INSERT_ORDER, LOCAL_FILE_SUBDIRS
from app.data_transfer.services import (
    _bundle_file, _extract_members, export_to_zip, extract_to_temp, import_from_zip,
    validate_manifest,
)
from app.extensions import db
from app.file_safety import safe_tenant_path
from app.models import (
    Customer, DunningNotice, Document, Invoice, InvoiceItem, Meeting,
    MeetingProtocol, SchriftverkehrDocument, User,
)
from tests.conftest import _ensure_role

SECRET = b"FREMDER MANDANT: geheimer DB-Dump"
SELECTION = {"stammdaten": True, "buchungen": True, "mahnwesen": True,
             "einstellungen": False, "years": []}


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    """Mandant ``alm`` mit PDF_DIR wie im SaaS — und die Datei eines fremden Mandanten."""
    pdf_dir = tmp_path / "tenants" / "alm" / "pdfs"
    pdf_dir.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(pdf_dir))
    foreign = tmp_path / "tenants" / "other" / "backups" / "db" / "dump.sql.gz"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(SECRET)
    instance = tmp_path / "instance"
    instance.mkdir()
    return SimpleNamespace(pdf_dir=pdf_dir, root=pdf_dir.parent, foreign=foreign,
                           instance=instance)


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    user = User(username="admin", email="admin@test.test", role_id=role.id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _fresh_session():
    """Identity-Map nach den Raw-DELETEs des Imports verwerfen; Flask-Login cached
    den User in ``g`` — der waere danach detached."""
    db.session.remove()
    for attr in ("_login_user", "_flask_login_user"):
        g.pop(attr, None)


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _invoice(number="2025-00001", **paths):
    """Versendete (gesperrte) Rechnung mit einer Position."""
    customer = Customer(name="Muster Max")
    db.session.add(customer)
    db.session.flush()
    inv = Invoice(invoice_number=number, customer_id=customer.id, date=date(2025, 3, 1),
                  due_date=date(2025, 3, 31), status=Invoice.STATUS_SENT, **paths)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Wasserverbrauch",
                               quantity=Decimal("40"), unit="m³", unit_price=Decimal("2.5"),
                               amount=Decimal("100"), tax_rate=Decimal("10")))
    db.session.flush()
    inv.recalculate_total()
    db.session.commit()
    return inv


def _export(include_pdfs=False):
    buf = io.BytesIO()
    export_to_zip({**SELECTION, "include_pdfs": include_pdfs}, buf, exported_by="test")
    return buf.getvalue()


def _set_values(values):
    def edit(records):
        for rec in records:
            rec.update(values)
    return edit


def _tamper(zip_bytes, edit=None, extra_tables=()):
    """Die ZIP wie ein Angreifer umbauen: Tabellen-JSON aendern und die (unge-
    schluesselte) Checksumme neu berechnen — die Datei besteht die Validierung."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(files.pop("manifest.json"))
    for table, fn in (edit or {}).items():
        records = json.loads(files[f"tables/{table}.json"])
        fn(records)
        files[f"tables/{table}.json"] = json.dumps(records).encode("utf-8")
    manifest["tables"].extend(extra_tables)
    checksum = hashlib.sha256()
    for tm in manifest["tables"]:
        checksum.update(files.get(f"tables/{tm['name']}.json", b""))
    manifest["checksum_sha256"] = checksum.hexdigest()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest))
    return out.getvalue()


def _import(zip_bytes, tenant, mode="replace", update_existing=False):
    extract_dir, manifest = extract_to_temp(io.BytesIO(zip_bytes), str(tenant.instance))
    assert validate_manifest(manifest, extract_dir)["errors"] == []
    import_from_zip(extract_dir, manifest, mode=mode, update_existing=update_existing,
                    instance_path=str(tenant.instance))
    _fresh_session()


def _zip(members: dict) -> bytes:
    """ZIP mit Eintragsnamen genau wie angegeben (auch ``..`` oder Backslashes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo("x")
            info.filename = name
            zf.writestr(info, data)
    return buf.getvalue()


class TestCraftedZipThroughTheRoutes:
    def test_absolute_paths_are_dropped_and_never_served(self, app, client, tenant, admin,
                                                         monkeypatch):
        invoice_id = _invoice().id
        crafted = _tamper(_export(), edit={"invoices": _set_values(
            {col: str(tenant.foreign) for col in ("pdf_path", "doc_path", "xml_path")})})
        monkeypatch.setattr(app, "instance_path", str(tenant.instance))
        _fresh_session()
        _login(client)

        resp = client.post("/data-transfer/import", content_type="multipart/form-data", data={
            "file": (io.BytesIO(crafted), "export.zip"), "mode": "replace"})
        assert resp.status_code == 200
        token = re.search(rb"/data-transfer/import/([0-9a-f]{32})/confirm", resp.data).group(1)
        resp = client.post(f"/data-transfer/import/{token.decode()}/confirm",
                           data={"mode": "replace"})
        assert resp.headers["Location"].endswith("/data-transfer/")      # Erfolg
        _fresh_session()

        inv = db.session.get(Invoice, invoice_id)
        assert (inv.pdf_path, inv.doc_path, inv.xml_path) == (None, None, None)
        for url in (f"/invoices/{invoice_id}/pdf", f"/invoices/{invoice_id}/pdf?fmt=docx",
                    f"/invoices/{invoice_id}/einvoice.xml"):
            assert SECRET not in client.get(url).get_data(), url


class TestImportNeverTrustsPaths:
    @pytest.fixture
    def rows(self, tenant):
        """Je Model mit Pfadspalte eine Zeile (Pfade leer bzw. im eigenen Mandanten)."""
        inv = _invoice(pdf_path=_write(tenant.pdf_dir / "2025" / "2025-00001.pdf", b"%PDF"))
        meeting = Meeting(meeting_type=Meeting.TYPE_BOARD, title="Vorstand")
        db.session.add_all([
            DunningNotice(invoice_id=inv.id, level_snapshot=1, name_snapshot="1. Mahnung"),
            SchriftverkehrDocument(year=2025, title="Brief", file_path=""),
            meeting,
        ])
        db.session.flush()
        db.session.add(MeetingProtocol(meeting_id=meeting.id))
        db.session.commit()

    @pytest.mark.parametrize("mode,update_existing", [("replace", False), ("merge", True)])
    def test_absolute_paths_from_the_zip_are_dropped(self, app, tenant, rows, mode,
                                                     update_existing):
        foreign = str(tenant.foreign)
        crafted = _tamper(_export(), edit={
            model.__tablename__: _set_values({col: foreign for col in cols})
            for model, cols in FILE_PATH_COLS.items()})

        _import(crafted, tenant, mode=mode, update_existing=update_existing)

        for model, cols in FILE_PATH_COLS.items():
            stored = [getattr(row, col) for row in model.query for col in cols]
            assert stored and set(stored) <= {None, ""}, (model.__name__, stored)

    def test_own_schriftverkehr_files_survive_a_restore(self, app, tenant):
        letter = _write(tenant.root / "schriftverkehr" / "2025" / "Brief.pdf", b"%PDF brief")
        minutes = _write(tenant.root / "schriftverkehr" / "2025" / "Protokoll.pdf", b"%PDF")
        elsewhere = _write(tenant.pdf_dir / "2025" / "2025-00009.pdf", b"%PDF")
        meeting = Meeting(meeting_type=Meeting.TYPE_BOARD, title="Vorstand")
        db.session.add(meeting)
        db.session.flush()
        db.session.add_all([
            MeetingProtocol(meeting_id=meeting.id, file_path=minutes),
            SchriftverkehrDocument(year=2025, title="eigen", file_path=letter),
            SchriftverkehrDocument(year=2025, title="falscher Ordner", file_path=elsewhere),
            SchriftverkehrDocument(year=2025, title="fremd", file_path=str(tenant.foreign)),
        ])
        db.session.commit()

        _import(_export(), tenant)

        stored = {doc.title: doc.file_path for doc in SchriftverkehrDocument.query}
        assert stored == {"eigen": str(Path(letter).resolve()), "falscher Ordner": "",
                          "fremd": ""}
        assert MeetingProtocol.query.one().file_path == str(Path(minutes).resolve())


class TestBundleFiles:
    def test_restored_files_land_in_the_tenant_pdf_dir(self, app, tenant):
        pdf = _write(tenant.pdf_dir / "2025" / "2025-00001.pdf", b"%PDF-1.4 rechnung")
        xml = _write(tenant.pdf_dir / "2025" / "2025-00001.xml", b"<rechnung/>")
        inv = _invoice(pdf_path=pdf, xml_path=xml)
        dunning_pdf = _write(tenant.pdf_dir / "2025" / "dunning" / "2025-00001_M1.pdf",
                             b"%PDF-1.4 mahnung")
        notice = DunningNotice(invoice_id=inv.id, level_snapshot=1, name_snapshot="1. Mahnung",
                               pdf_path=dunning_pdf)
        db.session.add(notice)
        db.session.commit()
        invoice_id, notice_id = inv.id, notice.id
        bundle = _export(include_pdfs=True)
        for path in (pdf, xml, dunning_pdf):
            os.remove(path)                       # nur das Bundle bringt sie zurueck

        _import(bundle, tenant)

        inv = db.session.get(Invoice, invoice_id)
        notice = db.session.get(DunningNotice, notice_id)
        assert inv.pdf_path == str(tenant.pdf_dir / "invoices" / "2025-00001.pdf")
        assert inv.xml_path == str(tenant.pdf_dir / "invoices" / "2025-00001.xml")
        assert notice.pdf_path == str(tenant.pdf_dir / "dunning" / f"{notice_id}.pdf")
        assert Path(inv.pdf_path).read_bytes() == b"%PDF-1.4 rechnung"
        assert Path(inv.xml_path).read_bytes() == b"<rechnung/>"
        assert Path(notice.pdf_path).read_bytes() == b"%PDF-1.4 mahnung"
        assert not (tenant.instance / "pdfs").exists()      # nicht mehr ins (globale) instance/

    def test_traversal_and_foreign_bundle_values_are_not_copied(self, app, tenant):
        pdf = _write(tenant.pdf_dir / "2025" / "2025-00001.pdf", b"%PDF-1.4 rechnung")
        invoice_id = _invoice(pdf_path=pdf).id
        # So tief liegt <bundle>/pdfs/invoices unter instance/tmp/imports/<uuid>.
        escape = os.path.relpath(tenant.foreign,
                                 tenant.instance / "tmp" / "imports" / "x" / "pdfs" / "invoices")
        crafted = _tamper(_export(include_pdfs=True), edit={"invoices": _set_values({
            "pdf_path": "pdfs/invoices/" + escape.replace(os.sep, "/"),
            "doc_path": str(tenant.foreign),
            "xml_path": "pdfs/invoices/../../manifest.json",
        })})

        _import(crafted, tenant)

        inv = db.session.get(Invoice, invoice_id)
        assert (inv.pdf_path, inv.doc_path, inv.xml_path) == (None, None, None)
        leaked = [p for base in (tenant.root, tenant.instance / "pdfs") if base.exists()
                  for p in base.rglob("*") if p.is_file() and p.read_bytes() == SECRET]
        assert leaked == []

    def test_bundle_file_only_resolves_inside_its_folder(self, app, tmp_path):
        bundle = tmp_path / "bundle"
        _write(bundle / "pdfs" / "invoices" / "2025-00001.pdf", b"%PDF")
        _write(bundle / "manifest.json", b"{}")
        assert _bundle_file(bundle, "pdfs/invoices/2025-00001.pdf", "invoices") == \
            (bundle / "pdfs" / "invoices" / "2025-00001.pdf").resolve()
        for value in ("pdfs/invoices/../../manifest.json", "pdfs/invoices/fehlt.pdf",
                      "pdfs/dunning/2025-00001.pdf", str(bundle / "manifest.json"),
                      "/etc/passwd", None, 42):
            assert _bundle_file(bundle, value, "invoices") is None, value


class TestExtraction:
    @pytest.mark.parametrize("name", [
        "../evil.txt", "tables/../../evil.txt", "..\\evil.txt", "/abs-evil.txt", "C:/evil.txt",
    ])
    def test_zip_slip_member_is_refused(self, app, tmp_path, name):
        instance = tmp_path / "instance"
        with pytest.raises(ValueError, match="Ungueltiger Eintrag"):
            extract_to_temp(io.BytesIO(_zip({"manifest.json": b"{}", name: b"evil"})),
                            str(instance))
        assert list(tmp_path.rglob("*evil.txt")) == []
        assert list((instance / "tmp" / "imports").iterdir()) == []   # aufgeraeumt

    def test_archive_over_the_size_limit_is_refused(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "DATA_TRANSFER_MAX_UNCOMPRESSED_BYTES", 1000)
        instance = tmp_path / "instance"
        with pytest.raises(ValueError, match="zu gross"):
            extract_to_temp(io.BytesIO(_zip({"manifest.json": b"{}",
                                              "pdfs/gross.bin": b"0" * 5000})), str(instance))
        assert list((instance / "tmp" / "imports").iterdir()) == []

    def test_written_bytes_count_even_if_the_header_lies(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "DATA_TRANSFER_MAX_UNCOMPRESSED_BYTES", 1000)

        class LyingZip:
            def infolist(self):
                info = zipfile.ZipInfo("pdfs/klein.bin")
                info.file_size = 10                   # deklariert klein …
                return [info]

            def open(self, info):
                return io.BytesIO(b"0" * 5000)        # … entpackt gross

        with pytest.raises(ValueError, match="zu gross"):
            _extract_members(LyingZip(), tmp_path)

    def test_too_many_members_are_refused(self, app, tmp_path, monkeypatch):
        monkeypatch.setitem(app.config, "DATA_TRANSFER_MAX_MEMBERS", 3)
        members = {"manifest.json": b"{}", **{f"tables/t{i}.json": b"[]" for i in range(4)}}
        with pytest.raises(ValueError, match="zu viele"):
            extract_to_temp(io.BytesIO(_zip(members)), str(tmp_path / "instance"))


class TestManifestTableNames:
    def test_table_names_never_reach_sql(self, app, tenant):
        _invoice()
        injected = "(SELECT 1 UNION ALL SELECT 2) AS t"
        crafted = _tamper(_export(), extra_tables=[{"name": injected, "rows": 0}])
        extract_dir, manifest = extract_to_temp(io.BytesIO(crafted), str(tenant.instance))

        validation = validate_manifest(manifest, extract_dir)

        assert any("Tabellenname" in err for err in validation["errors"])
        assert injected not in {t["name"] for t in validation["tables_overview"]}

    def test_only_registered_tables_are_counted(self, app, tenant, admin):
        _invoice()
        crafted = _tamper(_export(), extra_tables=[{"name": "users", "rows": 0}])
        extract_dir, manifest = extract_to_temp(io.BytesIO(crafted), str(tenant.instance))

        validation = validate_manifest(manifest, extract_dir)

        counts = {t["name"]: t["current_count"] for t in validation["tables_overview"]}
        assert validation["errors"] == []
        assert counts["customers"] == 1 and counts["invoices"] == 1
        assert counts["users"] == 0               # nicht exportiert → nie gezaehlt


class TestForeignPathsInExistingRows:
    """Altdaten (z. B. aus einem Import vor dem Fix) mit fremdem Pfad: kein
    Download liefert die Datei aus — sie verhaelt sich wie eine fehlende."""

    @pytest.mark.parametrize("method,url", [
        ("get", "/invoices/{id}/pdf"),
        ("get", "/invoices/{id}/pdf?fmt=docx"),
        ("get", "/invoices/{id}/einvoice.xml"),
        ("post", "/invoices/bulk-docx-zip"),
    ])
    def test_invoice_documents(self, app, client, tenant, admin, method, url):
        foreign = str(tenant.foreign)
        invoice_id = _invoice(pdf_path=foreign, doc_path=foreign, xml_path=foreign).id
        _login(client)
        if method == "get":
            resp = client.get(url.format(id=invoice_id))
        else:
            resp = client.post(url, data={"invoice_ids": [invoice_id]})
        assert SECRET not in resp.get_data()
        if resp.mimetype == "application/zip":
            with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
                assert all(zf.read(name) != SECRET for name in zf.namelist())

    @pytest.mark.parametrize("fmt", ["pdf", "docx"])
    def test_dunning_notice(self, app, client, tenant, admin, fmt):
        foreign = str(tenant.foreign)
        notice = DunningNotice(invoice_id=_invoice().id, level_snapshot=1,
                               name_snapshot="1. Mahnung", pdf_path=foreign, doc_path=foreign)
        db.session.add(notice)
        db.session.commit()
        _login(client)
        assert SECRET not in client.get(f"/dunning/notices/{notice.id}/pdf?fmt={fmt}").get_data()

    @pytest.mark.parametrize("key", [
        "{foreign}",                                              # absoluter Pfad
        "../../other/backups/db/dump.sql.gz",                     # Ausbruch nach oben
        "documents/../../../other/backups/db/dump.sql.gz",        # Ausbruch mitten im Schluessel
        "documents/2025/01/1_aaaaaaaa.pdf/../../../../../other/backups/db/dump.sql.gz",
        "incoming/2025/1_aaaaaaaa_..",
    ])
    def test_document_storage_key(self, app, client, tenant, admin, key):
        """Die Belegablage speichert nur relative, geprueft Schluessel — ein manipulierter Wert
        in einer bestehenden Zeile verhaelt sich wie „Datei fehlt“ und liefert nie fremde Bytes."""
        doc = Document(original_name="rechnung.pdf", content_type="application/pdf", sha256="a" * 64,
                       storage_key=key.format(foreign=tenant.foreign), size_bytes=1)
        db.session.add(doc)
        db.session.commit()
        _login(client)
        for suffix in ("file", "file?download=1", "xml", "attachment/0"):
            assert SECRET not in client.get(f"/accounting/documents/{doc.id}/{suffix}").get_data()
        assert SECRET not in client.get(f"/accounting/documents/{doc.id}").get_data()

    def test_schriftverkehr_and_protocol(self, app, client, tenant, admin):
        foreign = str(tenant.foreign)
        doc = SchriftverkehrDocument(year=2025, title="Brief", file_path=foreign)
        meeting = Meeting(meeting_type=Meeting.TYPE_BOARD, title="Vorstand")
        db.session.add_all([doc, meeting])
        db.session.flush()
        db.session.add(MeetingProtocol(meeting_id=meeting.id, file_path=foreign))
        db.session.commit()
        _login(client)
        for url in (f"/schriftfuehrung/archive/{doc.id}/download",
                    f"/schriftfuehrung/meetings/{meeting.id}/protocol/download"):
            assert SECRET not in client.get(url).get_data(), url

    def test_archived_files_inside_the_tenant_are_still_served(self, app, client, tenant, admin):
        archived = _write(tenant.pdf_dir / "2025" / "2025-00001.pdf", b"%PDF-1.4 archiviert")
        frozen = _write(tenant.pdf_dir / "2025" / "2025-00001.xml", b"<rechnung/>")
        invoice_id = _invoice(pdf_path=archived, xml_path=frozen).id
        _login(client)
        for url, expected in ((f"/invoices/{invoice_id}/pdf", b"%PDF-1.4 archiviert"),
                              (f"/invoices/{invoice_id}/einvoice.xml", b"<rechnung/>")):
            resp = client.get(url)
            assert (resp.status_code, resp.get_data()) == (200, expected), url
            resp.close()


class TestSafeTenantPath:
    def test_only_existing_files_inside_the_tenant(self, app, tenant):
        inside = _write(tenant.pdf_dir / "2025" / "a.pdf", b"%PDF")
        assert safe_tenant_path(inside) == str(Path(inside).resolve())
        escape = str(tenant.pdf_dir / ".." / ".." / "other" / "backups" / "db" / "dump.sql.gz")
        for value in (str(tenant.foreign), escape, str(tenant.pdf_dir / "2025" / "fehlt.pdf"),
                      "pdfs/2025/a.pdf", "\\\\server\\share\\a.pdf", "", None, 42):
            assert safe_tenant_path(value) is None, value

    def test_subdir_narrows_the_root(self, app, tenant):
        letter = _write(tenant.root / "schriftverkehr" / "2025" / "brief.pdf", b"%PDF")
        invoice_pdf = _write(tenant.pdf_dir / "2025" / "a.pdf", b"%PDF")
        assert safe_tenant_path(letter, "schriftverkehr") == str(Path(letter).resolve())
        assert safe_tenant_path(invoice_pdf, "schriftverkehr") is None

    def test_symlink_out_of_the_tenant_is_refused(self, app, tenant):
        link = tenant.pdf_dir / "2025" / "link.pdf"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(tenant.foreign)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks hier nicht erlaubt (Windows ohne Entwicklermodus)")
        assert safe_tenant_path(str(link)) is None


class TestRegistryGuard:
    def test_every_path_column_is_registered(self, app):
        """Neue Pfadspalte ohne Eintrag → der Import uebernaehme sie roh aus der ZIP."""
        missing = [f"{m.__name__}.{c.name}" for m in INSERT_ORDER for c in m.__table__.columns
                   if c.name.endswith("_path") and c.name not in FILE_PATH_COLS.get(m, ())]
        assert not missing, f"In registry.FILE_PATH_COLS eintragen: {missing}"

    def test_registered_path_columns_exist(self, app):
        for model, cols in FILE_PATH_COLS.items():
            assert model in INSERT_ORDER, model.__name__
            for col in cols:
                assert col in model.__table__.c, f"{model.__name__}.{col}"
        assert set(LOCAL_FILE_SUBDIRS) <= set(FILE_PATH_COLS)
