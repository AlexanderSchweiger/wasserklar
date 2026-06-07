"""HTTP-Tests fuer das Limit beim Massendruck/-export (``BULK_PRINT_MAX``).

Das Limit ist ein serverseitiges Sicherheitsnetz: die UI batcht zwar bereits in
100er-Gruppen, der Cap faengt aber direkte/veraltete Clients ab und schuetzt vor
RAM-/Timeout-Last (WeasyPrint rendert jedes Dokument einzeln in den RAM).

Der Cap greift in allen Bulk-Routen *vor* dem WeasyPrint-Import bzw. dem DB-Query
— die Ueberschreitungs-Tests laufen daher auch ohne installiertes WeasyPrint.
"""
import pytest

from app.extensions import db
from app.models import User
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


def _flashes(client):
    """Liest die (noch nicht konsumierten) Flash-Messages aus der Session."""
    with client.session_transaction() as sess:
        return [msg for _cat, msg in sess.get("_flashes", [])]


class TestInvoiceBulkPrintLimit:
    """Rechnungen: 4 Bulk-Routen (PDF/DOCX, je merged + ZIP)."""

    URLS = [
        "/invoices/bulk-pdf-merged",
        "/invoices/bulk-pdf-zip",
        "/invoices/bulk-docx-merged",
        "/invoices/bulk-docx-zip",
    ]

    @pytest.mark.parametrize("url", URLS)
    def test_over_limit_redirects_with_warning(self, client, admin, url):
        client.get("/auth/logout")
        _login(client)
        ids = [str(i) for i in range(1, 102)]  # 101 > 100
        r = client.post(url, data={"invoice_ids": ids}, follow_redirects=False)
        assert r.status_code == 302
        assert any("maximal" in m for m in _flashes(client))

    def test_at_limit_does_not_trip_cap(self, client, admin):
        """Genau 100 darf den Cap nicht ausloesen (Off-by-one-Schutz)."""
        client.get("/auth/logout")
        _login(client)
        ids = [str(i) for i in range(1, 101)]  # exakt 100
        # bulk-pdf-merged kappt vor dem WeasyPrint-Import; bei leerer DB folgt
        # eine andere Meldung ("Keine Rechnungen ..."), aber nie die Cap-Warnung.
        client.post("/invoices/bulk-pdf-merged",
                    data={"invoice_ids": ids}, follow_redirects=False)
        assert not any("maximal" in m for m in _flashes(client))


class TestDunningBulkPrintLimit:
    """Mahnungen: 2 Bulk-Routen (PDF + DOCX, jeweils merged)."""

    @pytest.mark.parametrize("url", [
        "/dunning/bulk-pdf-merged",
        "/dunning/bulk-docx-merged",
    ])
    def test_over_limit_redirects_with_warning(self, client, admin, url):
        client.get("/auth/logout")
        _login(client)
        ids = [str(i) for i in range(1, 102)]  # 101 > 100
        r = client.post(url, data={"notice_ids": ids}, follow_redirects=False)
        assert r.status_code == 302
        assert any("maximal" in m for m in _flashes(client))
