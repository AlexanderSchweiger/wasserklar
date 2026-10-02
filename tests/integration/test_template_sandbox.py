"""Mandanten-editierbare Jinja-Texte laufen in der Sandbox.

Betreff/Text der Rechnungsmail (Einstellungen, Recht ``verwaltung``) und die
Texte der Mahnstufen (``mahnwesen``) pflegt der Mandant selbst — beide auch per
Daten-Import setzbar. Ein normales ``jinja2.Environment`` reichte ueber
``lipsum.__globals__`` bis an ``os``: Codeausfuehrung im App-Container, im SaaS
mit Zugriff auf alle Mandanten. Die Sandbox blockt das; die Platzhalter rendern
unveraendert.
"""
import os
from datetime import date
from decimal import Decimal

import pytest
from jinja2.sandbox import SecurityError

from app.dunning.services import _render_text
from app.extensions import db
from app.invoices.routes import _render_email_body, _render_email_subject
from app.models import AppSetting, Customer, Invoice

PAYLOAD = "{{ lipsum.__globals__.os.getcwd() }}"


@pytest.fixture
def invoice(app):
    customer = Customer(name="Muster Max")
    db.session.add(customer)
    db.session.flush()
    inv = Invoice(invoice_number="2025-00007", customer_id=customer.id, date=date(2025, 3, 1),
                  status=Invoice.STATUS_SENT, total_amount=Decimal("12.50"))
    db.session.add(inv)
    db.session.commit()
    return inv


def _set(key, value):
    AppSetting.set(key, value)
    db.session.commit()


class TestInvoiceMailTemplates:
    def test_subject_cannot_reach_os(self, app, invoice):
        _set("email_subject_template", PAYLOAD)
        subject = _render_email_subject(invoice)
        assert os.getcwd() not in subject
        assert subject == "Rechnung 2025-00007"          # Rueckfall wie bei kaputter Vorlage

    def test_body_cannot_reach_os(self, app, invoice):
        _set("email_body_template", PAYLOAD)
        with pytest.raises(SecurityError):
            _render_email_body(invoice)

    def test_placeholders_still_render(self, app, invoice):
        _set("email_subject_template", "Rechnung {{ rechnungsnummer }} über {{ betrag }} €")
        _set("email_body_template", "Hallo {{ name }}")
        assert _render_email_subject(invoice) == "Rechnung 2025-00007 über 12.50 €"
        assert _render_email_body(invoice) == "Hallo Muster Max"


class TestDunningTexts:
    def test_stage_text_cannot_reach_os(self, app):
        assert _render_text(PAYLOAD, {}) == PAYLOAD      # Fehler → Rohtext, nichts ausgefuehrt

    def test_placeholders_still_render(self, app):
        assert _render_text("Mahnstufe {{ stufe }} zu {{ rechnungsnummer }}",
                            {"stufe": 2, "rechnungsnummer": "2025-00007"}) == \
            "Mahnstufe 2 zu 2025-00007"
