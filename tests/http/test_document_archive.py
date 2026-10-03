"""Belegarchiv in der Oberflaeche: Uebersicht je Jahr, Download als ZIP, Schutz."""
import io
import zipfile
from datetime import date

from app.documents import storage
from app.extensions import db
from app.models import Document, User
from tests.conftest import _ensure_role
from tests.http.test_documents import _login, _page, _upload, admin, pdf_dir  # noqa: F401  (Fixtures)
from tests.integration.test_document_service import pdf


def _doc_of_2025(client, name="rechnung.pdf", n=1):
    _upload(client, (name, pdf(n)))
    doc = Document.query.filter_by(original_name=name).one()
    doc.document_date = date(2025, 3, 14)
    db.session.commit()
    return doc


class TestPage:
    def test_the_years_with_documents_are_offered(self, client, admin, pdf_dir):
        _login(client)
        _doc_of_2025(client)
        html = _page(client, "/accounting/documents/archive")
        assert "Archiv 2025 herunterladen" in html and "01.01.2025 – 31.12.2025" in html
        assert "/accounting/documents/archive/2025/download" in html

    def test_an_empty_store_says_so(self, client, admin, pdf_dir):
        _login(client)
        assert "Noch keine Belege abgelegt" in _page(client, "/accounting/documents/archive")

    def test_the_list_links_to_the_archive(self, client, admin, pdf_dir):
        _login(client)
        assert "/accounting/documents/archive" in _page(client, "/accounting/documents")


class TestDownload:
    def test_the_zip_comes_with_files_index_and_checksums(self, client, admin, pdf_dir):
        _login(client)
        doc = _doc_of_2025(client)
        r = client.get("/accounting/documents/archive/2025/download")
        assert r.status_code == 200 and r.mimetype == "application/zip"
        assert 'filename=belegarchiv-2025.zip' in r.headers["Content-Disposition"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        zf = zipfile.ZipFile(io.BytesIO(r.get_data()))
        assert zf.testzip() is None
        names = zf.namelist()
        assert "belegarchiv-2025/index.csv" in names and "belegarchiv-2025/sha256sums.txt" in names
        assert zf.read(f"belegarchiv-2025/belege/{doc.id:06d}_rechnung.pdf") == pdf(1)
        r.close()

    def test_a_missing_file_is_announced(self, client, admin, pdf_dir):
        _login(client)
        doc = _doc_of_2025(client)
        storage.delete(doc.storage_key)
        client.get("/accounting/documents/archive/2025/download").close()
        assert "bei 1 Beleg(en) fehlte die Datei" in _page(client, "/accounting/documents/archive")

    def test_silly_years_are_not_found(self, client, admin, pdf_dir):
        _login(client)
        assert client.get("/accounting/documents/archive/1999/download").status_code == 404
        assert client.get("/accounting/documents/archive/2999/download").status_code == 404

    def test_no_space_no_archive(self, client, admin, pdf_dir, monkeypatch):
        _login(client)
        _doc_of_2025(client)
        monkeypatch.setattr(storage, "free_bytes", lambda: 10)
        r = client.get("/accounting/documents/archive/2025/download", follow_redirects=True)
        assert r.mimetype == "text/html" and "zu wenig Speicherplatz" in r.get_data(as_text=True)

    def test_the_temp_file_leaves_no_trace(self, client, admin, pdf_dir):
        _login(client)
        _doc_of_2025(client)
        before = set(pdf_dir.parent.iterdir())
        client.get("/accounting/documents/archive/2025/download").close()
        assert set(pdf_dir.parent.iterdir()) == before


class TestAccess:
    def test_login_and_the_accounting_permission_are_required(self, client, admin, pdf_dir):
        client.get("/auth/logout")
        assert client.get("/accounting/documents/archive/2025/download").status_code == 302
        role = _ensure_role("Nur Stammdaten", perms=("stammdaten",))
        user = User(username="leser", email="l@a.test", role_id=role.id)
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()
        _login(client, "leser")
        for path in ("/accounting/documents/archive", "/accounting/documents/archive/2025/download"):
            r = client.get(path)
            assert r.status_code == 302 and "/archive" not in r.headers["Location"]
