"""HTTP-Tests: Aendert sich eine render-relevante Rechnungs-Einstellung (z.B.
GiroCode aktivieren, Designwechsel, IBAN), muss der gecachte PDF-/DOCX-Stand
gesperrter Rechnungen verworfen werden — sonst wird ewig der alte Stand ohne
das neue Element ausgeliefert (der Bug, der den GiroCode nur auf frisch
gerenderten Rechnungen erscheinen liess).
"""
import pytest

from app.extensions import db
from app.models import AppSetting, Customer, Invoice, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def locked_invoice(app):
    """Eine gesperrte (versendete) Rechnung mit gecachtem Dokument-Zeiger."""
    cust = Customer(name="Testkunde")
    db.session.add(cust)
    db.session.flush()
    inv = Invoice(
        invoice_number="2025-00042",
        customer_id=cust.id,
        status=Invoice.STATUS_SENT,
        pdf_path="/cache/2025-00042.pdf",
        doc_path="/cache/2025-00042.docx",
    )
    db.session.add(inv)
    db.session.commit()
    return inv


def _login(client, username="admin", password="secret"):
    return client.post("/auth/login", data={"username": username, "password": password})


def _settings_form(*, show_payment_qr):
    """Minimales, gueltiges Einstellungs-Formular; nur der QR-Toggle variiert."""
    data = {
        "org_type": "cooperative",
        "invoice_document_format": "pdf",
        "invoice_design": "classic",
    }
    if show_payment_qr:
        data["invoice_show_payment_qr"] = "on"
    return data


def _invoice(inv_id):
    db.session.expire_all()
    return db.session.get(Invoice, inv_id)


def test_render_change_invalidates_invoice_cache(client, admin, locked_invoice):
    client.get("/auth/logout")
    _login(client)
    inv_id = locked_invoice.id

    # 1. Baseline-Save etabliert die gespeicherte Render-Signatur (und verwirft
    #    dabei bereits einmalig den vor-Feature-Cache: gespeicherte Signatur fehlt).
    client.post("/einstellungen/", data=_settings_form(show_payment_qr=False))

    # Cache-Zeiger nach dem Baseline-Save wieder setzen.
    inv = _invoice(inv_id)
    inv.pdf_path = "/cache/2025-00042.pdf"
    inv.doc_path = "/cache/2025-00042.docx"
    db.session.commit()

    # 2. GiroCode aktivieren -> render-relevante Aenderung -> Cache muss weg.
    client.post("/einstellungen/", data=_settings_form(show_payment_qr=True))

    inv = _invoice(inv_id)
    assert inv.pdf_path is None
    assert inv.doc_path is None
    assert AppSetting.get("invoice.show_payment_qr") == "true"


def test_unchanged_resave_keeps_invoice_cache(client, admin, locked_invoice):
    client.get("/auth/logout")
    _login(client)
    inv_id = locked_invoice.id

    # Baseline etablieren.
    client.post("/einstellungen/", data=_settings_form(show_payment_qr=True))

    inv = _invoice(inv_id)
    inv.pdf_path = "/cache/2025-00042.pdf"
    db.session.commit()

    # Identischer Save -> Signatur unveraendert -> Cache bleibt erhalten.
    client.post("/einstellungen/", data=_settings_form(show_payment_qr=True))

    inv = _invoice(inv_id)
    assert inv.pdf_path == "/cache/2025-00042.pdf"


def test_draft_invoice_cache_untouched(client, admin, locked_invoice):
    """Entwuerfe cachen nie — die Invalidierung fasst sie nicht an (Filter)."""
    client.get("/auth/logout")
    _login(client)

    draft = Invoice(
        invoice_number="2025-09999",
        customer_id=locked_invoice.customer_id,
        status=Invoice.STATUS_DRAFT,
        pdf_path="/cache/should-not-exist.pdf",
    )
    db.session.add(draft)
    db.session.commit()
    draft_id = draft.id

    # Render-Aenderung ausloesen.
    client.post("/einstellungen/", data=_settings_form(show_payment_qr=False))
    client.post("/einstellungen/", data=_settings_form(show_payment_qr=True))

    # Der (untypische) Entwurfs-Zeiger bleibt unangetastet, da der Filter nur
    # gesperrte Rechnungen trifft.
    assert _invoice(draft_id).pdf_path == "/cache/should-not-exist.pdf"
