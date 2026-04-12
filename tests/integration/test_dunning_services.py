"""Integrationstests für das Mahnwesen (ADR-003) — Services mit DB."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Customer, DunningNotice, DunningPolicy, DunningStage,
    Invoice, InvoiceItem, User,
)
from app.dunning.services import (
    cancel_dunnings_for_invoice,
    compute_fee,
    create_dunning_notice,
    current_dunning_level,
    defer_dunning_notice,
    dunning_summary,
    eligible_invoices_for_stage,
    reset_dunning_notice,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_user():
    u = User(username="admin", email="admin@test.at", role="admin", active=True)
    u.set_password("test1234")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def customer():
    c = Customer(name="Test Kunde", customer_number=1)
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture
def policy_with_stages():
    """Standard-Policy mit 4 Stufen analog init-db Seed."""
    p = DunningPolicy(name="Standard", is_default=True, active=True)
    db.session.add(p)
    db.session.flush()

    stages_data = [
        dict(level=1, name="Freundliche Erinnerung", days_after_due=14,
             fee_fixed=0, new_due_days=14, color="blue", icon="fa-envelope"),
        dict(level=2, name="Zahlungserinnerung", days_after_due=30,
             fee_fixed=0, new_due_days=14, color="orange", icon="fa-exclamation-circle"),
        dict(level=3, name="1. Mahnung", days_after_due=45,
             fee_fixed=5, new_due_days=14, color="red", icon="fa-exclamation-triangle"),
        dict(level=4, name="2. Mahnung", days_after_due=60,
             fee_fixed=10, new_due_days=7, color="pink", icon="fa-gavel"),
    ]
    stages = []
    for sd in stages_data:
        s = DunningStage(policy_id=p.id, active=True, **sd)
        db.session.add(s)
        stages.append(s)

    db.session.commit()
    return p, stages


def _create_invoice(customer, *, total=Decimal("100.00"), due_date=None, status=None):
    """Helferfunktion: Rechnung mit minimalem Setup."""
    inv = Invoice(
        invoice_number=f"2026-{Invoice.query.count() + 1:05d}",
        customer_id=customer.id,
        date=date.today(),
        due_date=due_date or date.today(),
        status=status or Invoice.STATUS_SENT,
        total_amount=total,
    )
    db.session.add(inv)
    db.session.commit()
    return inv


# ---------------------------------------------------------------------------
# current_dunning_level
# ---------------------------------------------------------------------------

class TestCurrentDunningLevel:
    def test_no_notices_returns_zero(self, customer):
        inv = _create_invoice(customer)
        assert current_dunning_level(inv) == 0

    def test_one_active_notice(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 1

    def test_multiple_active_notices_returns_max(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        create_dunning_notice(inv, stages[0], admin_user.id)
        create_dunning_notice(inv, stages[1], admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 2

    def test_reset_notice_not_counted(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        create_dunning_notice(inv, stages[1], admin_user.id)
        reset_dunning_notice(n1, admin_user)
        db.session.commit()
        # Nur Stage 2 (level=2) noch aktiv
        assert current_dunning_level(inv) == 2

    def test_all_reset_returns_zero(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        reset_dunning_notice(n1, admin_user)
        db.session.commit()
        assert current_dunning_level(inv) == 0


# ---------------------------------------------------------------------------
# eligible_invoices_for_stage
# ---------------------------------------------------------------------------

class TestEligibleInvoicesForStage:
    def test_not_overdue_not_eligible(self, customer, policy_with_stages):
        """Rechnung mit due_date = heute → nicht fällig."""
        policy, _ = policy_with_stages
        _create_invoice(customer, due_date=date.today())
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 0

    def test_overdue_14_days_eligible_for_stage_1(self, customer, policy_with_stages):
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=14)
        _create_invoice(customer, due_date=due)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 1
        inv, stage = result[0]
        assert stage.level == 1

    def test_overdue_45_days_eligible_for_stage_3(self, customer, policy_with_stages):
        """45 Tage überfällig → höchste passende Stage ist 3."""
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=45)
        _create_invoice(customer, due_date=due)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 1
        _, stage = result[0]
        assert stage.level == 3

    def test_overdue_60_days_eligible_for_stage_4(self, customer, policy_with_stages):
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=60)
        _create_invoice(customer, due_date=due)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 1
        _, stage = result[0]
        assert stage.level == 4

    def test_already_at_max_level_not_eligible(self, customer, admin_user, policy_with_stages):
        """Rechnung bereits auf höchster Stufe → kein Vorschlag mehr."""
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=90)
        inv = _create_invoice(customer, due_date=due)
        # Alle 4 Stufen erzeugen
        for s in stages:
            create_dunning_notice(inv, s, admin_user.id)
        db.session.commit()
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 0

    def test_already_at_level_2_eligible_for_level_3(self, customer, admin_user, policy_with_stages):
        """Rechnung auf Stufe 2, 45 Tage überfällig → Vorschlag für Stufe 3."""
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=45)
        inv = _create_invoice(customer, due_date=due)
        create_dunning_notice(inv, stages[0], admin_user.id)  # Level 1
        create_dunning_notice(inv, stages[1], admin_user.id)  # Level 2
        db.session.commit()
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 1
        _, stage = result[0]
        assert stage.level == 3

    def test_draft_invoice_not_eligible(self, customer, policy_with_stages):
        """Nur Status 'Versendet' ist eligible — Entwurf nicht."""
        policy, _ = policy_with_stages
        due = date.today() - timedelta(days=30)
        _create_invoice(customer, due_date=due, status=Invoice.STATUS_DRAFT)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 0

    def test_paid_invoice_not_eligible(self, customer, policy_with_stages):
        policy, _ = policy_with_stages
        due = date.today() - timedelta(days=30)
        _create_invoice(customer, due_date=due, status=Invoice.STATUS_PAID)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 0

    def test_multiple_invoices(self, customer, policy_with_stages):
        """Zwei überfällige Rechnungen → zwei Ergebnisse."""
        policy, _ = policy_with_stages
        due = date.today() - timedelta(days=20)
        _create_invoice(customer, due_date=due)
        _create_invoice(customer, due_date=due)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 2

    def test_no_due_date_not_eligible(self, customer, policy_with_stages):
        """Rechnung ohne due_date → nicht fällig."""
        policy, _ = policy_with_stages
        _create_invoice(customer, due_date=None)
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 0

    def test_inactive_stage_skipped(self, customer, policy_with_stages):
        """Deaktivierte Stage wird übersprungen."""
        policy, stages = policy_with_stages
        stages[0].active = False  # Level 1 deaktivieren
        db.session.commit()
        due = date.today() - timedelta(days=14)
        _create_invoice(customer, due_date=due)
        # 14 Tage → passt auf Stage 1, aber die ist inaktiv → kein Ergebnis
        result = eligible_invoices_for_stage(policy, today=date.today())
        assert len(result) == 0


# ---------------------------------------------------------------------------
# create_dunning_notice
# ---------------------------------------------------------------------------

class TestCreateDunningNotice:
    def test_creates_notice_with_status_aktiv(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer, total=Decimal("200.00"))
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        assert notice.status == DunningNotice.STATUS_AKTIV
        assert notice.invoice_id == inv.id
        assert notice.level_snapshot == 1
        assert notice.name_snapshot == "Freundliche Erinnerung"
        assert notice.created_by_id == admin_user.id

    def test_fee_zero_no_item_created(self, customer, admin_user, policy_with_stages):
        """Stage 1 hat fee_fixed=0 → kein Fee-InvoiceItem."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        assert notice.fee_amount == Decimal("0.00")
        assert notice.fee_invoice_item_id is None
        fee_items = InvoiceItem.query.filter_by(
            invoice_id=inv.id, is_dunning_fee=1
        ).all()
        assert len(fee_items) == 0

    def test_fee_positive_creates_item(self, customer, admin_user, policy_with_stages):
        """Stage 3 hat fee_fixed=5 → Fee-InvoiceItem wird angelegt."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer, total=Decimal("100.00"))
        notice = create_dunning_notice(inv, stages[2], admin_user.id)  # Stage 3
        db.session.commit()

        assert notice.fee_amount == Decimal("5.00")
        assert notice.fee_invoice_item_id is not None

        item = InvoiceItem.query.get(notice.fee_invoice_item_id)
        assert item.is_dunning_fee == 1
        assert item.dunning_notice_id == notice.id
        assert item.amount == Decimal("5.00")
        assert item.tax_rate is None  # Mahngebühren nicht USt-pflichtig
        assert "Mahngebühr" in item.description

    def test_fee_10_for_stage_4(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer, total=Decimal("500.00"))
        notice = create_dunning_notice(inv, stages[3], admin_user.id)  # Stage 4
        db.session.commit()
        assert notice.fee_amount == Decimal("10.00")

    def test_new_due_date_calculated(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()
        # Stage 1: new_due_days=14
        expected = date.today() + timedelta(days=14)
        assert notice.new_due_date == expected

    def test_snapshot_fields_independent_of_stage_changes(
        self, customer, admin_user, policy_with_stages
    ):
        """Snapshot-Felder bleiben stabil auch wenn Stage nachträglich geändert wird."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        original_name = notice.name_snapshot

        # Stage umbenennen
        stages[0].name = "Umbenannte Stufe"
        db.session.commit()

        # Snapshot unverändert
        db.session.refresh(notice)
        assert notice.name_snapshot == original_name


# ---------------------------------------------------------------------------
# reset_dunning_notice
# ---------------------------------------------------------------------------

class TestResetDunningNotice:
    def test_status_changed_to_zurueckgesetzt(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[2], admin_user.id)  # Stage 3 mit Fee
        db.session.commit()

        reset_dunning_notice(notice, admin_user, reason="Kulanz")
        db.session.commit()

        assert notice.status == DunningNotice.STATUS_ZURUECKGESETZT
        assert notice.reset_by_id == admin_user.id
        assert notice.reset_reason == "Kulanz"
        assert notice.reset_at is not None

    def test_fee_item_deleted_on_reset(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[2], admin_user.id)
        db.session.commit()
        fee_item_id = notice.fee_invoice_item_id
        assert fee_item_id is not None

        reset_dunning_notice(notice, admin_user)
        db.session.commit()

        assert notice.fee_invoice_item_id is None
        assert InvoiceItem.query.get(fee_item_id) is None

    def test_reset_without_fee(self, customer, admin_user, policy_with_stages):
        """Reset einer gebührenfreien Mahnung (Stage 1)."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        reset_dunning_notice(notice, admin_user)
        db.session.commit()

        assert notice.status == DunningNotice.STATUS_ZURUECKGESETZT
        assert notice.fee_invoice_item_id is None

    def test_reset_only_affects_one_notice(self, customer, admin_user, policy_with_stages):
        """Reset einer Notice ändert andere Notices der gleichen Rechnung nicht."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        n2 = create_dunning_notice(inv, stages[1], admin_user.id)
        db.session.commit()

        reset_dunning_notice(n1, admin_user)
        db.session.commit()

        assert n1.status == DunningNotice.STATUS_ZURUECKGESETZT
        assert n2.status == DunningNotice.STATUS_AKTIV

    def test_reason_optional(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        reset_dunning_notice(notice, admin_user)  # kein reason
        db.session.commit()

        assert notice.reset_reason is None


# ---------------------------------------------------------------------------
# defer_dunning_notice
# ---------------------------------------------------------------------------

class TestDeferDunningNotice:
    def test_updates_due_date(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        new_due = date.today() + timedelta(days=30)
        defer_dunning_notice(notice, new_due, admin_user)
        db.session.commit()

        assert notice.new_due_date == new_due

    def test_status_stays_aktiv(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        defer_dunning_notice(notice, date.today() + timedelta(days=30), admin_user)
        db.session.commit()

        assert notice.status == DunningNotice.STATUS_AKTIV

    def test_audit_trail_in_notes(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        defer_dunning_notice(notice, date.today() + timedelta(days=30), admin_user)
        db.session.commit()

        assert notice.notes is not None
        assert "Nachfrist verlängert" in notice.notes
        assert admin_user.username in notice.notes

    def test_multiple_defers_append_notes(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[0], admin_user.id)
        db.session.commit()

        defer_dunning_notice(notice, date.today() + timedelta(days=20), admin_user)
        defer_dunning_notice(notice, date.today() + timedelta(days=40), admin_user)
        db.session.commit()

        lines = notice.notes.strip().split("\n")
        assert len(lines) == 2

    def test_fee_unchanged_after_defer(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[2], admin_user.id)  # Stage 3 mit Fee
        db.session.commit()
        original_fee = notice.fee_amount
        original_item_id = notice.fee_invoice_item_id

        defer_dunning_notice(notice, date.today() + timedelta(days=30), admin_user)
        db.session.commit()

        assert notice.fee_amount == original_fee
        assert notice.fee_invoice_item_id == original_item_id


# ---------------------------------------------------------------------------
# cancel_dunnings_for_invoice
# ---------------------------------------------------------------------------

class TestCancelDunningsForInvoice:
    def test_all_active_notices_cancelled(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        n2 = create_dunning_notice(inv, stages[2], admin_user.id)
        db.session.commit()

        cancel_dunnings_for_invoice(inv)
        db.session.commit()

        assert n1.status == DunningNotice.STATUS_STORNIERT
        assert n2.status == DunningNotice.STATUS_STORNIERT

    def test_fee_items_deleted_on_cancel(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        notice = create_dunning_notice(inv, stages[2], admin_user.id)  # Stage 3 mit Fee
        db.session.commit()
        fee_item_id = notice.fee_invoice_item_id

        cancel_dunnings_for_invoice(inv)
        db.session.commit()

        assert notice.fee_invoice_item_id is None
        assert InvoiceItem.query.get(fee_item_id) is None

    def test_already_reset_notices_untouched(self, customer, admin_user, policy_with_stages):
        """Nur aktive Notices werden storniert — bereits zurückgesetzte bleiben."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer)
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        n2 = create_dunning_notice(inv, stages[1], admin_user.id)
        reset_dunning_notice(n1, admin_user)
        db.session.commit()

        cancel_dunnings_for_invoice(inv)
        db.session.commit()

        assert n1.status == DunningNotice.STATUS_ZURUECKGESETZT  # unverändert
        assert n2.status == DunningNotice.STATUS_STORNIERT

    def test_no_notices_no_error(self, customer):
        """Cancel auf Rechnung ohne Mahnungen → kein Fehler."""
        inv = _create_invoice(customer)
        cancel_dunnings_for_invoice(inv)
        db.session.commit()  # kein Fehler


# ---------------------------------------------------------------------------
# dunning_summary
# ---------------------------------------------------------------------------

class TestDunningSummary:
    def test_no_notices(self, customer):
        inv = _create_invoice(customer, total=Decimal("100.00"))
        summary = dunning_summary(inv)
        assert summary["level"] == 0
        assert summary["notices"] == []
        assert summary["total_fees"] == Decimal("0")
        assert summary["principal"] == Decimal("100.00")
        assert summary["gross_total"] == Decimal("100.00")

    def test_with_fee_notices(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer, total=Decimal("200.00"))
        create_dunning_notice(inv, stages[2], admin_user.id)  # Fee 5
        create_dunning_notice(inv, stages[3], admin_user.id)  # Fee 10
        db.session.commit()

        summary = dunning_summary(inv)
        assert summary["level"] == 4
        assert len(summary["notices"]) == 2
        assert summary["total_fees"] == Decimal("15.00")
        assert summary["principal"] == Decimal("200.00")
        assert summary["gross_total"] == Decimal("215.00")

    def test_reset_notice_excluded_from_summary(self, customer, admin_user, policy_with_stages):
        _, stages = policy_with_stages
        inv = _create_invoice(customer, total=Decimal("100.00"))
        n1 = create_dunning_notice(inv, stages[2], admin_user.id)  # Fee 5
        create_dunning_notice(inv, stages[3], admin_user.id)  # Fee 10
        reset_dunning_notice(n1, admin_user)
        db.session.commit()

        summary = dunning_summary(inv)
        assert summary["level"] == 4
        assert len(summary["notices"]) == 1
        assert summary["total_fees"] == Decimal("10.00")
        assert summary["gross_total"] == Decimal("110.00")


# ---------------------------------------------------------------------------
# Stufen-Eskalation (End-to-End-Szenario)
# ---------------------------------------------------------------------------

class TestDunningEscalation:
    """Vollständiger Durchlauf: Rechnung eskaliert von Stufe 1 bis 4."""

    def test_full_escalation(self, customer, admin_user, policy_with_stages):
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=70)  # 70 Tage überfällig
        inv = _create_invoice(customer, total=Decimal("500.00"), due_date=due)

        # Tag 14: Stufe 1 fällig
        day_14 = due + timedelta(days=14)
        result = eligible_invoices_for_stage(policy, today=day_14)
        assert len(result) == 1
        _, target = result[0]
        assert target.level == 1
        n1 = create_dunning_notice(inv, target, admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 1
        assert n1.fee_amount == Decimal("0.00")

        # Tag 30: Stufe 2 fällig
        day_30 = due + timedelta(days=30)
        result = eligible_invoices_for_stage(policy, today=day_30)
        assert len(result) == 1
        _, target = result[0]
        assert target.level == 2
        n2 = create_dunning_notice(inv, target, admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 2
        assert n2.fee_amount == Decimal("0.00")

        # Tag 45: Stufe 3 fällig — erstmals Gebühr
        day_45 = due + timedelta(days=45)
        result = eligible_invoices_for_stage(policy, today=day_45)
        assert len(result) == 1
        _, target = result[0]
        assert target.level == 3
        n3 = create_dunning_notice(inv, target, admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 3
        assert n3.fee_amount == Decimal("5.00")

        # Tag 60: Stufe 4 fällig — höhere Gebühr
        day_60 = due + timedelta(days=60)
        result = eligible_invoices_for_stage(policy, today=day_60)
        assert len(result) == 1
        _, target = result[0]
        assert target.level == 4
        n4 = create_dunning_notice(inv, target, admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 4
        assert n4.fee_amount == Decimal("10.00")

        # Keine weitere Eskalation möglich
        day_90 = due + timedelta(days=90)
        result = eligible_invoices_for_stage(policy, today=day_90)
        assert len(result) == 0

        # Summary prüfen
        summary = dunning_summary(inv)
        assert summary["level"] == 4
        assert summary["total_fees"] == Decimal("15.00")  # 0 + 0 + 5 + 10
        assert summary["gross_total"] == Decimal("515.00")

    def test_reset_and_re_escalate(self, customer, admin_user, policy_with_stages):
        """Reset der höchsten Stufe → bei nächstem Lauf wieder eligible."""
        policy, stages = policy_with_stages
        due = date.today() - timedelta(days=50)
        inv = _create_invoice(customer, total=Decimal("100.00"), due_date=due)

        # Stufe 1 und 2 erzeugen
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        n2 = create_dunning_notice(inv, stages[1], admin_user.id)
        db.session.commit()
        assert current_dunning_level(inv) == 2

        # Stufe 2 zurücksetzen
        reset_dunning_notice(n2, admin_user, reason="Teilzahlung eingegangen")
        db.session.commit()
        assert current_dunning_level(inv) == 1

        # 45 Tage überfällig, Level 1 → Stufe 2 und 3 wären eligible,
        # höchste passende = Stufe 3
        result = eligible_invoices_for_stage(policy, today=due + timedelta(days=45))
        assert len(result) == 1
        _, target = result[0]
        assert target.level == 3

    def test_cancel_after_escalation(self, customer, admin_user, policy_with_stages):
        """Storno der Rechnung setzt alle aktiven Mahnungen auf Storniert."""
        _, stages = policy_with_stages
        inv = _create_invoice(customer, total=Decimal("100.00"))
        n1 = create_dunning_notice(inv, stages[0], admin_user.id)
        n2 = create_dunning_notice(inv, stages[2], admin_user.id)
        db.session.commit()

        cancel_dunnings_for_invoice(inv)
        db.session.commit()

        assert n1.status == DunningNotice.STATUS_STORNIERT
        assert n2.status == DunningNotice.STATUS_STORNIERT
        assert current_dunning_level(inv) == 0

        summary = dunning_summary(inv)
        assert summary["total_fees"] == Decimal("0")
        assert summary["level"] == 0
