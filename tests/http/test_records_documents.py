"""Schriftfuehrung im Dokumentenregister: Protokolle (dauerhaft) und Schriftverkehr (Geschaeftsbriefe).

Uploads gehen ueber ``documents.service.store_record_upload`` (Inhalt statt Endung, Kontingent,
Office-Formate), das Protokoll-PDF ueber ``store_generated`` (ohne Kontingent). Eine Sitzung mit
abgeschlossenem Protokoll ist nicht loeschbar; Schriftverkehr nur am Tag des Hochladens.
"""
import io
import sys
import types
import zipfile
from datetime import date, datetime, timedelta

import pytest
from pypdf import PdfWriter

from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import Document, Meeting, MeetingProtocol, SchriftverkehrDocument, User
from tests.conftest import _ensure_role


def _pdf(n=0):
    writer = PdfWriter()
    writer.add_blank_page(width=300 + n, height=400)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


def _odt():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", "<office:document-content/>")
    return buf.getvalue()


OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


def _user(name, admin=False):
    role = _ensure_role("Admin") if admin else _ensure_role("Schriftführer", perms=["schriftfuehrung"])
    user = User(username=name, email=f"{name}@test.test", role_id=role.id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, name):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": name, "password": "secret"})


def _upload(client, name, data, **form):
    payload = {"title": form.pop("title", "Brief"), "doc_type": "incoming", "document_date": "2026-07-01",
               "document": (io.BytesIO(data), name), **form}
    return client.post("/schriftfuehrung/archive/upload", data=payload, content_type="multipart/form-data")


class TestSniffRecords:
    @pytest.mark.parametrize("data,name,ext", [
        (_docx(), "brief.docx", "docx"), (_odt(), "brief.odt", "odt"), (OLE, "alt.doc", "doc"),
        (OLE, "tabelle.xls", "xls"), (b"# Notiz\nText", "notiz.md", "md"), (b"hallo", "x.txt", "txt"),
        (_pdf(), "brief.pdf", "pdf"),
    ])
    def test_records_profile(self, app, data, name, ext):
        assert svc.sniff(data, Document.AREA_RECORDS, name) == ext

    @pytest.mark.parametrize("data", [b"MZ\x90\x00\x03\x00", b"PK\x03\x04kaputt", b"\xff\xfe\x00\x00"])
    def test_records_rejects_binaries(self, app, data):
        with pytest.raises(svc.DocumentError):
            svc.sniff(data, Document.AREA_RECORDS, "x.bin")

    def test_receipts_stay_strict(self, app):
        with pytest.raises(svc.DocumentError):
            svc.sniff(_docx(), Document.AREA_ACCOUNTING, "brief.docx")


class TestCorrespondence:
    def test_upload_lands_in_the_register(self, client, tenant):
        _user("schrift")
        _login(client, "schrift")
        r = _upload(client, "brief.docx", _docx(), title="Bescheid BH")
        assert r.status_code == 302
        entry = SchriftverkehrDocument.query.one()
        doc = entry.document
        assert doc.area == Document.AREA_RECORDS and doc.kind == Document.KIND_CORRESPONDENCE
        assert doc.title == "Bescheid BH" and doc.document_date == date(2026, 7, 1)
        assert entry.file_path == str(storage.path_for(doc.storage_key))
        download = client.get(f"/schriftfuehrung/archive/{entry.id}/download")
        assert download.status_code == 200 and download.data == _docx()

    def test_pdf_text_is_read_for_the_search(self, client, tenant):
        _user("schrift")
        _login(client, "schrift")
        _upload(client, "brief.pdf", _pdf())
        assert SchriftverkehrDocument.query.one().document.text_status in ("ok", "empty")

    def test_quota_blocks_the_upload(self, client, tenant, monkeypatch):
        monkeypatch.setattr(svc, "_quota_limit_bytes", lambda: 1)
        _user("schrift")
        _login(client, "schrift")
        _upload(client, "brief.docx", _docx())
        assert SchriftverkehrDocument.query.count() == 0 and Document.query.count() == 0

    def test_delete_on_the_upload_day(self, client, tenant):
        _user("schrift")
        _login(client, "schrift")
        _upload(client, "brief.docx", _docx())
        entry = SchriftverkehrDocument.query.one()
        key = entry.document.storage_key
        assert "Löschen (Fehlablage)" in client.get("/schriftfuehrung/archive").get_data(as_text=True)
        client.post(f"/schriftfuehrung/archive/{entry.id}/delete")
        assert SchriftverkehrDocument.query.count() == 0 and Document.query.count() == 0
        assert not storage.exists(key)

    def test_no_delete_after_the_upload_day(self, client, tenant):
        _user("schrift")
        _user("chef", admin=True)
        _login(client, "schrift")
        _upload(client, "brief.docx", _docx())
        entry = SchriftverkehrDocument.query.one()
        entry.document.created_at = datetime.utcnow() - timedelta(days=2)
        db.session.commit()
        for name in ("schrift", "chef"):                         # Frist laeuft: auch nicht fuer Admins
            _login(client, name)
            client.post(f"/schriftfuehrung/archive/{entry.id}/delete")
            assert SchriftverkehrDocument.query.count() == 1

    def test_someone_else_cannot_delete_it_the_same_day(self, client, tenant):
        _user("schrift")
        _user("andere")
        _login(client, "schrift")
        _upload(client, "brief.docx", _docx())
        _login(client, "andere")
        entry = SchriftverkehrDocument.query.one()
        client.post(f"/schriftfuehrung/archive/{entry.id}/delete")
        assert SchriftverkehrDocument.query.count() == 1


class TestProtocols:
    def _meeting(self):
        meeting = Meeting(meeting_type="board", title="Vorstand März", status="held", meeting_date=date(2026, 3, 5))
        db.session.add(meeting)
        db.session.commit()
        return meeting

    def test_uploaded_protocol_is_permanent_and_locks_the_meeting(self, client, tenant):
        _user("schrift")
        _login(client, "schrift")
        meeting = self._meeting()
        r = client.post(f"/schriftfuehrung/meetings/{meeting.id}/protocol/upload",
                        data={"document": (io.BytesIO(_pdf(3)), "protokoll.pdf")},
                        content_type="multipart/form-data")
        assert r.status_code == 302
        doc = Document.query.one()
        assert doc.kind == Document.KIND_PROTOCOL and [row.meeting_id for row in doc.links] == [meeting.id]
        assert svc.retention_end(doc) is None
        protocol = MeetingProtocol.query.one()
        assert protocol.is_locked and protocol.file_path == str(storage.path_for(doc.storage_key))
        meeting = db.session.get(Meeting, meeting.id)
        meeting.status = "planning"
        db.session.commit()
        assert not meeting.can_delete
        client.post(f"/schriftfuehrung/meetings/{meeting.id}/delete")
        assert db.session.get(Meeting, meeting.id) is not None

    def test_generated_protocol_ignores_the_quota(self, client, tenant, monkeypatch):
        class FakeHTML:
            def __init__(self, string=None, **kw):
                pass

            def write_pdf(self, target=None, **kw):
                return _pdf(7)

        monkeypatch.setitem(sys.modules, "weasyprint", types.SimpleNamespace(HTML=FakeHTML))
        monkeypatch.setattr(svc, "_quota_limit_bytes", lambda: 1)
        _user("schrift")
        _login(client, "schrift")
        meeting = self._meeting()
        client.post(f"/schriftfuehrung/meetings/{meeting.id}/protocol/finalize", data={"content_html": "<p>Text</p>"})
        doc = Document.query.one()
        assert doc.kind == Document.KIND_PROTOCOL and doc.area == Document.AREA_RECORDS
        assert storage.read(doc.storage_key) == _pdf(7)
        assert MeetingProtocol.query.one().file_path == str(storage.path_for(doc.storage_key))

    def test_protocols_cannot_be_deleted(self, app, tenant):
        doc = Document(area=Document.AREA_RECORDS, kind=Document.KIND_PROTOCOL, original_name="p.pdf",
                       content_type="application/pdf", sha256="1" * 64)
        db.session.add(doc)
        db.session.commit()
        admin = _user("chef", admin=True)
        allowed, why = svc.can_delete_record(doc, admin)
        assert not allowed and "dauerhaft" in why
