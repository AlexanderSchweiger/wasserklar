"""Integration-Tests für app/utils.py (next_invoice_number benötigt DB)."""
from decimal import Decimal

from app.extensions import db
from app.models import Customer, Invoice, InvoiceCounter
from app.utils import next_invoice_number


class TestNextInvoiceNumber:
    def test_first_number_for_new_year(self, app):
        number = next_invoice_number(2025)
        db.session.commit()
        assert number == "2025-00001"

    def test_second_call_increments(self, app):
        next_invoice_number(2025)
        db.session.commit()
        number = next_invoice_number(2025)
        db.session.commit()
        assert number == "2025-00002"

    def test_different_years_independent_counters(self, app):
        n1 = next_invoice_number(2025)
        db.session.commit()
        n2 = next_invoice_number(2026)
        db.session.commit()
        assert n1 == "2025-00001"
        assert n2 == "2026-00001"

    def test_format_is_year_dash_five_digits(self, app):
        number = next_invoice_number(2025)
        db.session.commit()
        year, seq = number.split("-")
        assert year == "2025"
        assert len(seq) == 5
        assert seq.isdigit()

    def test_existing_invoice_seeds_counter(self, app):
        """Wenn noch kein InvoiceCounter existiert, wird der höchste Rechnungs-Seq+1 verwendet."""
        cust = Customer(name="Seed-Kunde")
        db.session.add(cust)
        db.session.flush()

        inv = Invoice(
            invoice_number="2030-00007",
            customer_id=cust.id,
            status="Entwurf",
            total_amount=Decimal("0"),
        )
        db.session.add(inv)
        db.session.commit()

        # Kein InvoiceCounter für 2030 vorhanden → wird aus Rechnung abgeleitet
        from app.extensions import db as _db
        assert _db.session.get(InvoiceCounter, 2030) is None
        number = next_invoice_number(2030)
        assert number == "2030-00008"
