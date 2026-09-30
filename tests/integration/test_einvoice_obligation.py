"""E-Rechnungs-Pflicht (DE, B2B) und Versandweg: ``app/einvoice/obligation.py``.

Die Pflicht hat vier Voraussetzungen zugleich (Land DE, Unternehmer, USt-pflichtiges
Jahr + Betrag über 250 € brutto, Leistungsende ab dem Stichtag) — jede einzelne
wird hier für sich ausgeschaltet und muss die Pflicht aufheben.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.einvoice import obligation
from app.einvoice.model import FORMAT_XRECHNUNG
from app.extensions import db
from app.models import (
    AppSetting, Customer, CustomerEmailConsentLog, FiscalYear, Invoice,
)
from tests.integration.test_einvoice import DE_SELLER, _invoice, _item, _seller


def _business(**extra):
    customer = Customer(name="Müller Bau GmbH", is_company=True, is_business=True,
                        customer_number=77, plz="91234", ort="Musterdorf", **extra)
    db.session.add(customer)
    return customer


def _big_invoice(customer, **kw):
    """400 € netto, 7 % USt = 428 € brutto; Leistungszeitraum endet am 30.06.2027."""
    inv = _invoice([_item("Wasserverbrauch", "200", "m³", "2.00", "7")], customer=customer, **kw)
    inv.billing_period.end_date = date(2027, 6, 30)
    db.session.commit()
    return inv


@pytest.fixture
def mandate_2027(app):
    AppSetting.set(obligation.MANDATE_KEY, "2027-01-01")
    db.session.commit()


class TestRequiredFor:
    def test_all_conditions_met(self, app, mandate_2027):
        _seller(DE_SELLER)
        inv = _big_invoice(_business())
        with app.test_request_context():
            assert obligation.required_for(inv) is True

    def test_default_mandate_is_2028(self, app):
        """Ohne Setting gilt der 1.1.2028 — Leistungsende 30.06.2027 ist noch frei."""
        _seller(DE_SELLER)
        inv = _big_invoice(_business())
        with app.test_request_context():
            assert obligation.mandate_from() == date(2028, 1, 1)
            assert obligation.required_for(inv) is False
            inv.billing_period.end_date = date(2028, 1, 1)
            assert obligation.required_for(inv) is True

    def test_the_service_date_counts_not_the_invoice_date(self, app, mandate_2027):
        """§ 27 Abs. 38 UStG: maßgeblich ist der Leistungszeitpunkt, nicht das Rechnungsdatum."""
        _seller(DE_SELLER)
        inv = _big_invoice(_business())
        inv.billing_period.end_date = date(2026, 12, 31)      # Leistung 2026 …
        inv.date = date(2027, 2, 1)                           # … Rechnung erst 2027
        db.session.add(FiscalYear(year=2027, start_date=date(2027, 1, 1),
                                  end_date=date(2027, 12, 31), is_vat_liable=True))
        db.session.commit()
        with app.test_request_context():
            assert obligation.required_for(inv) is False

    def test_not_a_business(self, app, mandate_2027):
        _seller(DE_SELLER)
        customer = _business()
        customer.is_business = False
        inv = _big_invoice(customer)
        with app.test_request_context():
            assert obligation.required_for(inv) is False

    def test_small_business_issuer_is_exempt(self, app, mandate_2027):
        """Kleinunternehmer (§ 19 UStG) sind dauerhaft nicht ausstellungspflichtig."""
        _seller(DE_SELLER, vat_liable=False)
        inv = _big_invoice(_business())
        with app.test_request_context():
            assert obligation.required_for(inv) is False

    @pytest.mark.parametrize("quantity, gross, expected", [
        ("116", "248.24", False),    # 232,00 netto + 7 % → Kleinbetragsrechnung
        ("117", "250.38", True),     # 234,00 netto + 7 % → knapp darüber
        ("125", "267.50", True),
    ])
    def test_small_amounts_up_to_250_gross(self, app, mandate_2027, quantity, gross, expected):
        _seller(DE_SELLER)
        inv = _invoice([_item("Wasserverbrauch", quantity, "m³", "2.00", "7")],
                       customer=_business())
        inv.billing_period.end_date = date(2027, 6, 30)
        db.session.commit()
        with app.test_request_context():
            assert inv.total_amount == Decimal(gross)
            assert obligation.required_for(inv) is expected

    def test_exactly_250_gross_is_a_small_invoice(self, app, mandate_2027):
        _seller(DE_SELLER)
        inv = _invoice([_item("Pauschale", "1", "Pauschal", "250.00")], customer=_business())
        inv.billing_period.end_date = date(2027, 6, 30)
        db.session.commit()
        with app.test_request_context():
            assert inv.total_amount == Decimal("250.00")
            assert obligation.required_for(inv) is False

    def test_austria_has_no_domestic_obligation(self, app, mandate_2027):
        _seller(DE_SELLER)
        inv = _big_invoice(_business())
        AppSetting.set("org.country", "AT")
        db.session.commit()
        with app.test_request_context():
            assert obligation.required_for(inv) is False
            assert obligation.dashboard_summary() is None

    def test_cancelled_invoice_is_not_required(self, app, mandate_2027):
        _seller(DE_SELLER)
        inv = _big_invoice(_business(), status=Invoice.STATUS_CANCELLED)
        with app.test_request_context():
            assert obligation.required_for(inv) is False

    def test_a_credit_note_to_a_business_is_required_too(self, app, mandate_2027):
        _seller(DE_SELLER)
        customer = _business()
        original = _big_invoice(customer)
        credit = _invoice([_item("Wasserverbrauch", "-200", "m³", "2.00", "7")],
                          number="2026-00050", kind=Invoice.KIND_CREDIT_NOTE,
                          customer=customer, cancels=original)
        credit.billing_period.end_date = date(2027, 6, 30)
        db.session.commit()
        with app.test_request_context():
            assert obligation.required_for(credit) is True


class TestMandateSetting:
    @pytest.mark.parametrize("value, expected", [
        ("2027-01-01", date(2027, 1, 1)),
        ("2028-01-01", date(2028, 1, 1)),
        ("2029-05-05", date(2028, 1, 1)),      # kein angebotener Stichtag
        ("kaputt", date(2028, 1, 1)),
        ("", date(2028, 1, 1)),
    ])
    def test_value_or_default(self, app, value, expected):
        AppSetting.set(obligation.MANDATE_KEY, value)
        db.session.commit()
        with app.test_request_context():
            assert obligation.mandate_from() == expected


class TestElectronicOnlyAndPost:
    def test_reasons(self, app, mandate_2027):
        _seller(DE_SELLER)
        required = _big_invoice(_business())
        gemeinde = Customer(name="Gemeinde", customer_number=12, einvoice_format=FORMAT_XRECHNUNG)
        db.session.add(gemeinde)
        government = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                              number="2026-00060", customer=gemeinde)
        plain = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")],
                         number="2026-00061")
        with app.test_request_context():
            assert obligation.electronic_reason(required) == obligation.REASON_REQUIRED
            assert obligation.electronic_reason(government) == obligation.REASON_XRECHNUNG
            assert obligation.electronic_reason(plain) is None
            AppSetting.set("einvoice.enabled", "false")
            # Abgeschaltet: die XRechnung-Regel entfaellt, die gesetzliche Pflicht bleibt.
            assert obligation.electronic_reason(government) is None
            assert obligation.electronic_reason(required) == obligation.REASON_REQUIRED

    def test_post_split_blocks_only_drafts_that_must_be_electronic(self, app, mandate_2027):
        _seller(DE_SELLER)
        customer = _business()
        draft = _big_invoice(customer, status=Invoice.STATUS_DRAFT)
        sent = _invoice([_item("Wasserverbrauch", "200", "m³", "2.00", "7")], number="2026-00070",
                        customer=customer)
        sent.billing_period.end_date = date(2027, 6, 30)
        other = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], number="2026-00071",
                         status=Invoice.STATUS_DRAFT)
        db.session.commit()
        with app.test_request_context():
            printable, blocked = obligation.split_post_invoices([draft, sent, other])
        assert blocked == [draft]
        assert printable == [sent, other]        # Nachdruck und normale Entwuerfe bleiben


class TestDeliveryProblem:
    def _sent(self):
        _seller(DE_SELLER)
        return _big_invoice(_business())

    def test_not_yet_sent_electronically(self, app, mandate_2027):
        inv = self._sent()
        with app.test_request_context():
            level, text = obligation.delivery_problem(inv)
        assert level == "warning" and "noch nicht elektronisch zugestellt" in text

    def test_delivered_is_fine(self, app, mandate_2027):
        inv = self._sent()
        inv.email_sent_at = datetime(2026, 10, 1)
        inv.last_email_status = Invoice.EMAIL_STATUS_DELIVERED
        with app.test_request_context():
            assert obligation.delivery_problem(inv) is None

    def test_a_soft_bounce_is_only_delayed(self, app, mandate_2027):
        inv = self._sent()
        inv.email_sent_at = datetime(2026, 10, 1)
        inv.last_email_status = Invoice.EMAIL_STATUS_BOUNCED_SOFT
        with app.test_request_context():
            assert obligation.delivery_problem(inv) is None

    @pytest.mark.parametrize("status", [Invoice.EMAIL_STATUS_BOUNCED_HARD,
                                        Invoice.EMAIL_STATUS_SPAM, Invoice.EMAIL_STATUS_FAILED])
    def test_undelivered(self, app, mandate_2027, status):
        inv = self._sent()
        inv.email_sent_at = datetime(2026, 10, 1)
        inv.email_recipient = "buchhaltung@mueller-bau.example"
        inv.last_email_status = status
        with app.test_request_context():
            level, text = obligation.delivery_problem(inv)
        assert level == "danger"
        assert "nicht zugestellt" in text and "buchhaltung@mueller-bau.example" in text

    def test_drafts_and_non_required_invoices_have_no_note(self, app, mandate_2027):
        _seller(DE_SELLER)
        draft = _big_invoice(_business(), status=Invoice.STATUS_DRAFT)
        plain = _invoice([_item("Wasserverbrauch", "10", "m³", "2.00", "7")], number="2026-00080")
        with app.test_request_context():
            assert obligation.delivery_problem(draft) is None
            assert obligation.delivery_problem(plain) is None


class TestSetBusiness:
    def test_enabling_switches_on_email_and_logs_it(self, app):
        _seller(DE_SELLER)
        customer = Customer(name="Müller Bau", customer_number=77, email="m@mueller.example")
        db.session.add(customer)
        with app.test_request_context():
            obligation.set_business(customer, True)
        db.session.commit()
        assert customer.is_business and customer.rechnung_per_email and customer.wants_email
        (log,) = CustomerEmailConsentLog.query.filter_by(customer_id=customer.id).all()
        # Keine Einwilligung des Kunden, sondern eine Einstellung des Mandanten.
        assert log.action == CustomerEmailConsentLog.EINVOICE_B2B
        assert log.action != CustomerEmailConsentLog.OPT_IN
        assert log.email == "m@mueller.example"

    def test_without_an_address_nothing_is_switched_on(self, app):
        _seller(DE_SELLER)
        customer = Customer(name="Müller Bau", customer_number=77)
        db.session.add(customer)
        with app.test_request_context():
            obligation.set_business(customer, True)
            warning = obligation.business_warning(customer)
        db.session.commit()
        assert customer.is_business and not customer.rechnung_per_email
        assert CustomerEmailConsentLog.query.count() == 0
        assert "keine E-Mail" in warning and "Müller Bau" in warning

    def test_an_existing_business_keeps_its_email_setting(self, app):
        """Wer den Schriftverkehr per E-Mail abbestellt hat, wird nicht erneut eingeschaltet."""
        _seller(DE_SELLER)
        customer = Customer(name="Müller Bau", customer_number=77, email="m@mueller.example",
                            is_business=True, rechnung_per_email=False)
        db.session.add(customer)
        with app.test_request_context():
            obligation.set_business(customer, True)
        assert not customer.rechnung_per_email
        assert CustomerEmailConsentLog.query.count() == 0

    def test_austria_never_touches_the_consent(self, app):
        _seller({**DE_SELLER, "org.country": "AT"})
        customer = Customer(name="Müller Bau", customer_number=77, email="m@mueller.example")
        db.session.add(customer)
        with app.test_request_context():
            obligation.set_business(customer, True)
            assert obligation.business_warning(customer) is None
        assert customer.is_business and not customer.rechnung_per_email
        assert CustomerEmailConsentLog.query.count() == 0

    def test_switching_off_keeps_the_email_setting(self, app):
        _seller(DE_SELLER)
        customer = Customer(name="Müller Bau", customer_number=77, email="m@mueller.example",
                            is_business=True, rechnung_per_email=True)
        db.session.add(customer)
        with app.test_request_context():
            obligation.set_business(customer, False)
        assert not customer.is_business and customer.rechnung_per_email


class TestDashboardSummary:
    def test_counts_and_visibility(self, app):
        year = date.today().year
        _seller(DE_SELLER)                                   # legt FiscalYear 2026 an
        if year != 2026:
            db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1),
                                      end_date=date(year, 12, 31), is_vat_liable=True))
        db.session.add_all([
            Customer(name="A", customer_number=1, is_business=True, email="a@a.test",
                     rechnung_per_email=True),
            Customer(name="B", customer_number=2, is_business=True),                  # keine E-Mail
            Customer(name="C", customer_number=3, is_business=True, email="c@c.test"),  # kein Opt-in
            Customer(name="D", customer_number=4, is_business=False),
            Customer(name="E", customer_number=5, is_business=True, active=False),
        ])
        db.session.commit()
        with app.test_request_context():
            summary = obligation.dashboard_summary()
        assert summary == {"mandate_from": date(2028, 1, 1), "business": 3, "no_email": 2}

    def test_hidden_for_small_businesses(self, app):
        year = date.today().year
        _seller(DE_SELLER, vat_liable=False)
        if year != 2026:
            db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1),
                                      end_date=date(year, 12, 31), is_vat_liable=False))
            db.session.commit()
        with app.test_request_context():
            assert obligation.dashboard_summary() is None
