"""Übergabe an die Steuerberatung — Gerüst (app/accounting/handover.py).

Anmeldung/Aktivierung eines Formats, Normalisierung der Zuordnungsfelder,
Steuerschlüssel (gepflegt vor Länder-Default) und die Sperr-Abfragen
(aktive vs. zurückgezogene Übergabe).
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.accounting import handover
from app.extensions import db
from app.models import (AccountingHandover, AccountingHandoverItem, AppSetting, Booking,
                        TaxRate, Transfer)


@pytest.fixture
def test_format(app):
    """Meldet ein Test-Format an und räumt es wieder ab (app ist session-weit)."""
    def defaults(rate):
        return {Decimal("19"): ("3", "9"), Decimal("7"): ("2", "8")}.get(rate, (None, None))
    handover.register_format(app, "test", "Testformat", tax_key_defaults=defaults)
    yield
    app.extensions.get(handover.FORMATS_EXTENSION, {}).pop("test", None)


def _handover(status=AccountingHandover.STATUS_ACTIVE, **kw):
    h = AccountingHandover(format="test", fiscal_year=2026, date_from=date(2026, 1, 1),
                           date_to=date(2026, 1, 31), status=status,
                           created_at=datetime(2026, 2, 3, 10, 0), **kw)
    db.session.add(h)
    db.session.flush()
    return h


class TestActivation:
    def test_without_registered_format_nothing_is_active(self, app):
        AppSetting.set(handover.SETTING_KEY, "test")
        db.session.commit()
        assert handover.available_formats() == {}
        assert handover.active_format() is None
        assert handover.is_active() is False

    def test_registered_but_switched_off(self, app, test_format):
        assert "test" in handover.available_formats()
        assert handover.is_active() is False

    def test_switch_on_and_off(self, app, test_format):
        assert handover.set_format("test") is True
        db.session.commit()
        assert handover.active_format() == "test"
        assert handover.active_format_info().label == "Testformat"
        assert handover.set_format("") is True
        db.session.commit()
        assert handover.active_format() is None

    def test_unknown_format_is_rejected(self, app, test_format):
        assert handover.set_format("bmd") is False
        assert AppSetting.get(handover.SETTING_KEY) is None

    def test_unregistered_format_counts_as_off(self, app, test_format):
        handover.set_format("test")
        db.session.commit()
        app.extensions[handover.FORMATS_EXTENSION].pop("test")
        assert handover.active_format() is None

    def test_save_from_form_only_with_field(self, app, test_format):
        handover.save_setting_from_form({})
        assert AppSetting.get(handover.SETTING_KEY) is None
        handover.save_setting_from_form({"accounting_handover_format": "test"})
        assert AppSetting.get(handover.SETTING_KEY) == "test"
        handover.save_setting_from_form({"accounting_handover_format": "evil"})
        assert AppSetting.get(handover.SETTING_KEY) == "test"   # verworfen


class TestNormalize:
    @pytest.mark.parametrize("raw, expected", [
        ("", None), ("  ", None), ("1200", "1200"), (" 84 00 ", "8400"), ("123456789", "123456789"),
    ])
    def test_ledger_account_ok(self, raw, expected):
        assert handover.normalize_ledger_account(raw) == (expected, None)

    @pytest.mark.parametrize("raw", ["12A0", "1234567890", "-1200"])
    def test_ledger_account_invalid(self, raw):
        value, error = handover.normalize_ledger_account(raw)
        assert value is None and error

    def test_tax_key(self):
        assert handover.normalize_tax_key("9") == ("9", None)
        assert handover.normalize_tax_key("")[0] is None
        assert handover.normalize_tax_key("12345")[1]
        assert handover.normalize_tax_key("x")[1]


class TestTaxKeys:
    def test_defaults_only_when_active(self, app, test_format):
        assert handover.default_tax_keys(Decimal("19")) == (None, None)
        handover.set_format("test")
        db.session.commit()
        assert handover.default_tax_keys(Decimal("19")) == ("3", "9")
        assert handover.default_tax_keys("7.00") == ("2", "8")

    def test_maintained_value_beats_default(self, app, test_format):
        handover.set_format("test")
        db.session.add(TaxRate(rate=Decimal("19"), ledger_tax_key_output="1003"))
        db.session.commit()
        assert handover.effective_tax_keys(Decimal("19")) == ("1003", "9")
        assert handover.effective_tax_keys(Decimal("5")) == (None, None)


class TestLock:
    def test_active_vs_withdrawn(self, app, account, real_account):
        b1 = Booking(date=date(2026, 1, 5), account_id=account.id, amount=Decimal("10"),
                     description="A", status="Verbucht")
        b2 = Booking(date=date(2026, 1, 6), account_id=account.id, amount=Decimal("20"),
                     description="B", status="Verbucht")
        t = Transfer(date=date(2026, 1, 7), amount=Decimal("5"), description="Kasse",
                     from_real_account_id=real_account.id, to_real_account_id=real_account.id)
        db.session.add_all([b1, b2, t])
        db.session.flush()
        active = _handover()
        withdrawn = _handover(status=AccountingHandover.STATUS_WITHDRAWN)
        db.session.add_all([
            AccountingHandoverItem(handover_id=active.id, booking_id=b1.id),
            AccountingHandoverItem(handover_id=active.id, transfer_id=t.id),
            AccountingHandoverItem(handover_id=withdrawn.id, booking_id=b2.id),
        ])
        db.session.commit()

        assert handover.handed_over_booking_ids() == {b1.id}
        assert handover.handed_over_booking_ids([b2.id]) == set()
        assert handover.handed_over_booking_ids([]) == set()
        assert handover.handed_over_transfer_ids([t.id]) == {t.id}
        assert handover.active_handover_for(b1).id == active.id
        assert handover.active_handover_for(b2) is None
        assert handover.active_handover_for(t).id == active.id
        assert "03.02.2026" in handover.lock_message(active)

    def test_locked_changes(self, app, account, account2):
        b = Booking(date=date(2026, 1, 5), account_id=account.id, amount=Decimal("10"),
                    description="A", project_id=None, real_account_id=None)
        assert handover.locked_changes(b, {"account_id": account.id, "project_id": None}) == []
        assert handover.locked_changes(b, {"account_id": account2.id}) == ["account_id"]
