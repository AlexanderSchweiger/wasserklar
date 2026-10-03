"""Belege nach Fristablauf in der Oberflaeche: Uebersicht, Rechte, Loeschen."""
from datetime import date, datetime

from app.documents import storage
from app.extensions import db
from app.models import AppSetting, Document, User
from tests.conftest import _ensure_role
from tests.http.test_documents import _login, _page, _upload, admin, pdf_dir  # noqa: F401  (Fixtures)
from tests.integration.test_document_service import pdf


def _old_document(client, name="alt.pdf", n=1, year=2015):
    _upload(client, (name, pdf(n)))
    doc = Document.query.filter_by(original_name=name).one()
    doc.created_at, doc.document_date = datetime(year, 6, 1), date(year, 3, 1)
    db.session.commit()
    return doc


def _bookkeeper():
    role = _ensure_role("Buchhalter", perms=("buchhaltung",))
    user = User(username="buchhalter", email="b@a.test", role_id=role.id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


class TestOverview:
    def test_only_expired_documents_are_listed(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client, "alt.pdf", 1)
        _upload(client, ("neu.pdf", pdf(2)))
        html = _page(client, "/accounting/documents/expired")
        assert "alt.pdf" in html and "neu.pdf" not in html
        assert f'value="{old.id}"' in html and "Gewählte löschen" in html

    def test_the_overview_is_announced_on_the_list_only_when_there_is_something(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("neu.pdf", pdf(2)))
        assert "Aufbewahrung abgelaufen" not in _page(client, "/accounting/documents")
        _old_document(client, "alt.pdf", 1)
        assert "Aufbewahrung abgelaufen" in _page(client, "/accounting/documents")

    def test_an_empty_overview_says_so(self, client, admin, pdf_dir):
        _login(client)
        assert "Keine Belege mit abgelaufener Aufbewahrungsfrist" in _page(client, "/accounting/documents/expired")

    def test_the_hint_names_the_country_term(self, client, admin, pdf_dir):
        AppSetting.set("org.country", "DE")
        db.session.commit()
        _login(client)
        assert "8 Jahren" in _page(client, "/accounting/documents/expired")


class TestDeleting:
    def test_an_admin_deletes_expired_documents(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client)
        key = old.storage_key
        r = client.post("/accounting/documents/expired/delete", data={"ids": [str(old.id)]}, follow_redirects=True)
        assert "1 Beleg(e) gelöscht" in r.get_data(as_text=True)
        assert Document.query.count() == 0 and not storage.exists(key)

    def test_a_bookkeeper_may_look_but_not_delete(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client)
        _bookkeeper()
        _login(client, "buchhalter")
        page = _page(client, "/accounting/documents/expired")
        assert "alt.pdf" in page and "Gewählte löschen" not in page and "nur Administratoren" in page
        r = client.post("/accounting/documents/expired/delete", data={"ids": [str(old.id)]}, follow_redirects=True)
        assert "nur Administratoren" in r.get_data(as_text=True)
        assert Document.query.count() == 1

    def test_a_forged_id_never_deletes_a_running_term(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("neu.pdf", pdf(2)))
        recent = Document.query.one()
        r = client.post("/accounting/documents/expired/delete", data={"ids": [str(recent.id)]}, follow_redirects=True)
        assert "läuft noch bis" in r.get_data(as_text=True)
        assert Document.query.count() == 1 and storage.exists(recent.storage_key)

    def test_nothing_chosen_is_a_hint_not_an_error(self, client, admin, pdf_dir):
        _login(client)
        r = client.post("/accounting/documents/expired/delete", data={}, follow_redirects=True)
        assert "mindestens einen Beleg" in r.get_data(as_text=True)

    def test_garbage_ids_are_ignored(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client)
        client.post("/accounting/documents/expired/delete", data={"ids": ["abc", "-1", "1; DROP", "٣"]})
        assert Document.query.get(old.id) is not None


class TestDetailPage:
    def test_an_admin_sees_the_delete_button_once_the_term_is_over(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client)
        old.status = "Abgelegt"                          # aufbewahrungspflichtig: sonst genügt das normale Löschen
        db.session.commit()
        html = _page(client, f"/accounting/documents/{old.id}")
        assert "Löschen (Frist abgelaufen)" in html and "Nicht löschbar" not in html

    def test_a_never_booked_document_keeps_the_normal_delete(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client)
        html = _page(client, f"/accounting/documents/{old.id}")
        assert "nie gebucht oder abgelegt" in html and "Frist abgelaufen)" not in html

    def test_a_running_term_still_shows_the_lock(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("neu.pdf", pdf(3)))
        doc = Document.query.one()
        doc.status = "Abgelegt"                          # abgelegt = aufbewahrungspflichtig
        db.session.commit()
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "Nicht löschbar" in html and "Frist abgelaufen" not in html

    def test_a_bookkeeper_gets_a_note_instead_of_the_button(self, client, admin, pdf_dir):
        _login(client)
        old = _old_document(client)
        old.status = "Abgelegt"
        db.session.commit()
        _bookkeeper()
        _login(client, "buchhalter")
        html = _page(client, f"/accounting/documents/{old.id}")
        assert "Löschen durch einen Administrator möglich" in html and "Löschen (Frist abgelaufen)" not in html
