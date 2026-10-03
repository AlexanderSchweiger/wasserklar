"""Belegablage Stufe 2 in der Oberflaeche: erkannte Angaben bestaetigen, Suche im Text, Scan-Hinweis."""
from pathlib import Path

from app.extensions import db
from app.models import Document
from tests.http.test_documents import _login, _page, _upload, admin, pdf_dir  # noqa: F401  (Fixtures)
from tests.integration.test_document_service import pdf

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"


def fixture(name):
    return (FIXTURES / f"{name}.pdf").read_bytes()


def _upload_zeilen(client):
    _upload(client, ("rechnung.pdf", fixture("zeilen")))
    return Document.query.one()


class TestDetailPage:
    def test_suggested_entries_are_flagged_until_confirmed(self, client, admin, pdf_dir):
        _login(client)
        doc = _upload_zeilen(client)
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "automatisch aus dem Belegtext erkannt" in html
        assert 'value="RE-2026-0042"' in html and 'value="2026-01-12"' in html and "98,40" in html
        assert "Bestätigen</button>" in html
        assert "Angaben automatisch erkannt" in html                      # Verlauf

    def test_saving_confirms_and_the_flag_disappears(self, client, admin, pdf_dir):
        _login(client)
        doc = _upload_zeilen(client)
        r = client.post(f"/accounting/documents/{doc.id}/update", data={
            "kind": doc.kind, "title": "", "number": doc.number, "document_date": "2026-01-12",
            "amount": "98,40", "supplier_id": ""}, follow_redirects=True)
        html = r.get_data(as_text=True)
        assert "Angaben bestätigt." in html
        assert "automatisch aus dem Belegtext erkannt" not in html and "Angaben bestätigt" in html
        assert db.session.get(Document, doc.id).meta_auto is False

    def test_the_read_text_and_identifiers_are_shown(self, client, admin, pdf_dir):
        _login(client)
        doc = _upload_zeilen(client)
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "durchsuchbar" in html and "ATU23456789" in html and "AT089999900000012345" in html
        assert "Steuerung Pumpenhaus" in html                              # unter „Gelesenen Text anzeigen“

    def test_a_scan_gets_an_honest_hint(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("scan.pdf", pdf()))
        doc = Document.query.one()
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "keinen lesbaren Text" in html and "automatisch aus dem Belegtext erkannt" not in html


class TestList:
    def test_a_text_hit_shows_where_it_was_found(self, client, admin, pdf_dir):
        _login(client)
        _upload_zeilen(client)
        html = _page(client, "/accounting/documents?tab=all&q=Pumpenhaus")
        assert "rechnung.pdf" in html and "im Text:" in html
        assert "<mark>Pumpenhaus</mark>" in html

    def test_a_hit_in_the_entries_gets_no_snippet(self, client, admin, pdf_dir):
        _login(client)
        _upload_zeilen(client)
        html = _page(client, "/accounting/documents?tab=all&q=RE-2026-0042")
        assert "rechnung.pdf" in html and "im Text:" not in html

    def test_no_hit_no_document(self, client, admin, pdf_dir):
        _login(client)
        _upload_zeilen(client)
        assert "Keine Belege zur Suche" in _page(client, "/accounting/documents?tab=all&q=Hochbehaelter")

    def test_the_list_marks_unconfirmed_documents(self, client, admin, pdf_dir):
        _login(client)
        _upload_zeilen(client)
        assert ">erkannt</span>" in _page(client, "/accounting/documents?tab=all")

    def test_text_from_the_file_is_escaped_in_the_snippet(self, client, admin, pdf_dir):
        _login(client)
        doc = _upload_zeilen(client)
        doc.text_content = "vorher <script>alert(1)</script> boom nachher"
        db.session.commit()
        html = _page(client, "/accounting/documents?tab=all&q=boom")
        assert "&lt;script&gt;" in html and "<script>alert(1)" not in html
