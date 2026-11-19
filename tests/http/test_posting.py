"""Festschreiben je Voranmeldungszeitraum (``app/accounting/posting.py``) über die Oberfläche.

* Der nächtliche Lauf verbucht laut Einstellung (je Zeitraum / täglich / aus); „Aus“ gilt nicht,
  solange eine Übergabe an die Steuerberatung eingeschaltet ist.
* Einstellungskarte unter Buchhaltung speichert nur mit Marker; die Länder-Vorgabe bleibt ohne Eintrag.
* Buchungsliste: kein „Jetzt verbuchen“ mehr, stattdessen Info-Leiste und „änderbar bis“.
* USt-Voranmeldung: „Zeitraum abschließen“ verbucht alles bis zum Zeitraumende.
* Protokoll: Änderungen/Löschungen offener Buchungen ab dem Tag nach der Erfassung.
* Betrag-Schutz: Zahlungs- und Bankimport-Buchungen behalten ihren Betrag, Löschen → Storno.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.accounting import handover, posting
from app.accounting import services as acc_svc
from app.extensions import db
from app.models import (Account, AppSetting, BankStatement, BankStatementLine, Booking,
                        BookingChange, BookingGroup, BookingRemoval, FiscalYear, OpenItem,
                        RealAccount, User)
from tests.conftest import _ensure_role

HX = {"HX-Request": "true"}
TODAY = date.today()
YEAR = TODAY.year
YESTERDAY_TS = datetime.utcnow() - timedelta(days=2)


@pytest.fixture
def setup(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    fy = FiscalYear(year=YEAR, start_date=date(YEAR, 1, 1), end_date=date(YEAR, 12, 31),
                    closed=False, is_vat_liable=True)
    acc = Account(name="Strom", code="STR")
    acc2 = Account(name="Wasser", code="WAS")
    ra = RealAccount(name="Giro", opening_balance=Decimal("0"))
    db.session.add_all([u, fy, acc, acc2, ra])
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return dict(u=u, fy=fy, acc=acc, acc2=acc2, ra=ra)


def _booking(s, d, *, created=None, status=Booking.STATUS_OFFEN, **kw):
    b = Booking(date=d, account_id=s["acc"].id, amount=kw.pop("amount", Decimal("-50")),
                description=kw.pop("description", "Strom"), reference=kw.pop("reference", "B-1"),
                status=status, real_account_id=s["ra"].id, created_at=created or datetime.utcnow(),
                **kw)
    db.session.add(b)
    db.session.commit()
    return b


def _status(booking_id):
    db.session.expire_all()
    return db.session.get(Booking, booking_id).status


def _setting(key, value):
    AppSetting.set(key, value)
    db.session.commit()
    posting.invalidate()      # pytest-flask: der Test läuft in einem Request-Kontext


@pytest.fixture
def test_format(app):
    handover.register_format(app, "test", "Testformat")
    yield
    app.extensions.get(handover.FORMATS_EXTENSION, {}).pop("test", None)


# ---------------------------------------------------------------------------
# Nächtlicher Lauf
# ---------------------------------------------------------------------------

class TestAutoPost:
    def test_period_mode_is_default_and_waits_for_lock_day(self, app, setup):
        q1 = _booking(setup, date(2027, 2, 3))
        q2 = _booking(setup, date(2027, 4, 2))
        if True:  # ohne Request: posting.settings() liest direkt
            # AT: Q1 bis zum Ende des 15.05. (Abgabefrist), am 15. selbst noch offen
            assert acc_svc.auto_post_bookings(today=date(2027, 5, 15)) == 0
            assert _status(q1.id) == Booking.STATUS_OFFEN
            assert acc_svc.auto_post_bookings(today=date(2027, 5, 16)) == 1
        assert _status(q1.id) == Booking.STATUS_VERBUCHT
        assert _status(q2.id) == Booking.STATUS_OFFEN

    def test_monthly_period_and_custom_day(self, app, setup):
        _setting("tax.vat_return_period", "month")
        _setting(posting.LOCK_DAY_KEY, "10")
        _setting(posting.LOCK_OFFSET_KEY, "2")
        jan = _booking(setup, date(2027, 1, 20))
        if True:  # ohne Request: posting.settings() liest direkt
            acc_svc.auto_post_bookings(today=date(2027, 3, 10))
            assert _status(jan.id) == Booking.STATUS_OFFEN
            acc_svc.auto_post_bookings(today=date(2027, 3, 11))
        assert _status(jan.id) == Booking.STATUS_VERBUCHT

    def test_daily_mode(self, app, setup):
        _setting(posting.MODE_KEY, "daily")
        b = _booking(setup, date(2027, 4, 15))
        if True:  # ohne Request: posting.settings() liest direkt
            acc_svc.auto_post_bookings(today=date(2027, 4, 16))
        assert _status(b.id) == Booking.STATUS_VERBUCHT

    def test_off_mode_posts_nothing(self, app, setup):
        _setting(posting.MODE_KEY, "off")
        b = _booking(setup, date(2026, 1, 15))
        if True:  # ohne Request: posting.settings() liest direkt
            assert acc_svc.auto_post_bookings(today=date(2027, 4, 16)) == 0
        assert _status(b.id) == Booking.STATUS_OFFEN

    def test_off_falls_back_to_period_with_active_handover(self, app, setup, test_format):
        _setting(posting.MODE_KEY, "off")
        _setting(handover.SETTING_KEY, "test")
        b = _booking(setup, date(2027, 2, 3))
        if True:  # ohne Request: posting.settings() liest direkt
            assert posting.settings().off_blocked
            acc_svc.auto_post_bookings(today=date(2027, 5, 16))
        assert _status(b.id) == Booking.STATUS_VERBUCHT

    def test_de_profile_default_is_tenth(self, app, setup):
        _setting("org.country", "DE")
        b = _booking(setup, date(2027, 3, 3))
        if True:  # ohne Request: posting.settings() liest direkt
            cfg = posting.settings()
            assert (cfg.lock_day, cfg.month_offset) == (10, 1)
            acc_svc.auto_post_bookings(today=date(2027, 4, 11))
        assert _status(b.id) == Booking.STATUS_VERBUCHT

    def test_post_bookings_for_handover(self, setup):
        a = _booking(setup, date(2027, 2, 3))
        b = _booking(setup, date(2027, 2, 4))
        assert acc_svc.post_bookings([a.id]) == 1
        db.session.commit()
        assert _status(a.id) == Booking.STATUS_VERBUCHT
        assert _status(b.id) == Booking.STATUS_OFFEN


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

def _settings_post(client, **fields):
    data = {"active_tab": "tax"}
    data.update(fields)
    return client.post("/einstellungen/", data=data)


class TestSettingsCard:
    def test_card_is_shown(self, client, setup):
        html = client.get("/einstellungen/?tab=tax").get_data(as_text=True)
        assert "Buchungen festschreiben" in html
        assert 'name="posting_fields" value="1"' in html
        assert "wird am Ende des" in html

    def test_saves_with_marker(self, client, setup):
        _settings_post(client, posting_fields="1", posting_mode="daily", posting_lock_day="20",
                       posting_lock_month_offset="1", posting_lock_day_default="15",
                       posting_lock_month_offset_default="2")
        assert AppSetting.get(posting.MODE_KEY) == "daily"
        assert AppSetting.get(posting.LOCK_DAY_KEY) == "20"
        assert AppSetting.get(posting.LOCK_OFFSET_KEY) == "1"

    def test_default_values_are_not_stored(self, client, setup):
        _settings_post(client, posting_fields="1", posting_mode="period", posting_lock_day="15",
                       posting_lock_month_offset="2", posting_lock_day_default="15",
                       posting_lock_month_offset_default="2")
        assert AppSetting.get(posting.LOCK_DAY_KEY) is None
        assert AppSetting.get(posting.LOCK_OFFSET_KEY) is None

    def test_without_marker_nothing_changes(self, client, setup):
        _setting(posting.MODE_KEY, "daily")
        _settings_post(client, posting_mode="off", posting_lock_day="3")
        assert AppSetting.get(posting.MODE_KEY) == "daily"
        assert AppSetting.get(posting.LOCK_DAY_KEY) is None

    def test_invalid_day_is_rejected(self, client, setup):
        _settings_post(client, posting_fields="1", posting_mode="period", posting_lock_day="31")
        assert AppSetting.get(posting.LOCK_DAY_KEY) is None

    def test_off_rejected_with_active_handover(self, client, setup, test_format):
        _setting(handover.SETTING_KEY, "test")
        _settings_post(client, posting_fields="1", posting_mode="off",
                       accounting_handover_format="test")
        assert AppSetting.get(posting.MODE_KEY) == "period"
        html = client.get("/einstellungen/?tab=tax").get_data(as_text=True)
        assert "Nicht möglich, solange eine Übergabe" in html

    def test_off_allowed_without_handover(self, client, setup):
        _settings_post(client, posting_fields="1", posting_mode="off")
        assert AppSetting.get(posting.MODE_KEY) == "off"


# ---------------------------------------------------------------------------
# Buchungsliste + USt-Voranmeldung
# ---------------------------------------------------------------------------

class TestBookingsList:
    def test_no_post_now_button_but_info(self, client, setup):
        _booking(setup, TODAY)
        html = client.get(f"/accounting/bookings?year={YEAR}").get_data(as_text=True)
        assert "Jetzt verbuchen" not in html
        assert "postOpenModal" not in html
        assert 'id="bookings-open-hint"' in html and "1 offen" in html
        assert "Offen – änderbar bis" in html

    def test_daily_mode_text(self, client, setup):
        _setting(posting.MODE_KEY, "daily")
        _booking(setup, TODAY)
        html = client.get(f"/accounting/bookings?year={YEAR}").get_data(as_text=True)
        assert "werden über Nacht verbucht" in html

    def test_route_is_gone(self, client, setup):
        assert client.post("/accounting/bookings/post-open").status_code in (404, 405)


class TestClosePeriod:
    def test_close_posts_until_period_end(self, client, setup):
        q1 = _booking(setup, date(YEAR, 2, 3))
        q2 = _booking(setup, date(YEAR, 4, 2)) if TODAY >= date(YEAR, 4, 2) else None
        html = client.get(f"/accounting/ust?year={YEAR}&period=q1").get_data(as_text=True)
        assert "Zeitraum abschließen" in html
        r = client.post("/accounting/ust/close-period", data={"year": YEAR, "period": "q1"})
        assert r.status_code == 302
        assert _status(q1.id) == Booking.STATUS_VERBUCHT
        if q2 is not None:
            assert _status(q2.id) == Booking.STATUS_OFFEN
        html = client.get(f"/accounting/ust?year={YEAR}&period=q1").get_data(as_text=True)
        assert 'id="ust-close-period"' not in html

    def test_year_is_not_closable(self, client, setup):
        b = _booking(setup, date(YEAR, 1, 3))
        client.post("/accounting/ust/close-period", data={"year": YEAR, "period": "year"})
        assert _status(b.id) == Booking.STATUS_OFFEN


# ---------------------------------------------------------------------------
# Protokoll
# ---------------------------------------------------------------------------

def _edit_payload(s, b, **over):
    data = {"account_id": b.account_id, "description": b.description, "project_id": "",
            "customer_id": "", "real_account_id": s["ra"].id, "action": "",
            "date": b.date.isoformat(), "amount": str(b.amount), "reference": b.reference or "",
            "tax_rate": ""}
    data.update(over)
    return data


class TestChangeLog:
    def test_change_after_entry_day_is_logged(self, client, setup):
        b = _booking(setup, date(YEAR, 1, 2), created=YESTERDAY_TS)
        client.post(f"/accounting/bookings/{b.id}/edit", headers=HX,
                    data=_edit_payload(setup, b, amount="-60", account_id=setup["acc2"].id))
        changes = {c.field: (c.old_value, c.new_value) for c in BookingChange.query.all()}
        assert changes["amount"] == ("-50,00", "-60,00")
        assert changes["account_id"] == ("STR Strom", "WAS Wasser")
        assert "date" not in changes
        assert db.session.get(Booking, b.id).amount == Decimal("-60")
        html = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX).get_data(as_text=True)
        assert "Änderungsprotokoll" in html and "Betrag -50,00 → -60,00" in html

    def test_same_day_change_is_not_logged(self, client, setup):
        b = _booking(setup, TODAY)
        client.post(f"/accounting/bookings/{b.id}/edit", headers=HX,
                    data=_edit_payload(setup, b, amount="-60"))
        assert BookingChange.query.count() == 0
        assert db.session.get(Booking, b.id).amount == Decimal("-60")

    def test_unchanged_save_logs_nothing(self, client, setup):
        b = _booking(setup, date(YEAR, 1, 2), created=YESTERDAY_TS)
        client.post(f"/accounting/bookings/{b.id}/edit", headers=HX, data=_edit_payload(setup, b))
        assert BookingChange.query.count() == 0


class TestRemovalLog:
    def test_delete_after_entry_day_keeps_snapshot(self, client, setup):
        b = _booking(setup, date(YEAR, 1, 2), created=YESTERDAY_TS, description="Strom Jänner")
        bid = b.id
        client.post(f"/accounting/bookings/{bid}/delete")
        assert db.session.get(Booking, bid) is None
        r = BookingRemoval.query.one()
        assert r.original_booking_id == bid and r.reason == BookingRemoval.REASON_DELETED
        assert r.amount == Decimal("-50") and r.description == "Strom Jänner"
        assert r.snapshot_data["account_id"] == "STR Strom"
        html = client.get(f"/accounting/bookings/removed?year={YEAR}").get_data(as_text=True)
        assert "Strom Jänner" in html and "gelöscht" in html

    def test_same_day_delete_is_not_logged(self, client, setup):
        b = _booking(setup, TODAY)
        client.post(f"/accounting/bookings/{b.id}/delete")
        assert BookingRemoval.query.count() == 0

    def test_group_delete_is_logged(self, client, setup):
        g = BookingGroup(date=date(YEAR, 1, 5), description="Rechnung Mix", total_amount=Decimal("-30"),
                         created_at=YESTERDAY_TS)
        db.session.add(g)
        db.session.flush()
        for amount in ("-10", "-20"):
            db.session.add(Booking(date=g.date, account_id=setup["acc"].id, amount=Decimal(amount),
                                   description="Mix", status=Booking.STATUS_OFFEN, group_id=g.id,
                                   created_at=YESTERDAY_TS))
        db.session.commit()
        gid = g.id
        client.post(f"/accounting/booking-groups/{gid}/delete")
        r = BookingRemoval.query.one()
        assert r.original_group_id == gid and r.amount == Decimal("-30")
        assert len(r.snapshot_data["children"]) == 2


# ---------------------------------------------------------------------------
# Betrag-Schutz
# ---------------------------------------------------------------------------

class TestPaymentLock:
    def _open_item_booking(self, setup):
        from app.models import Customer
        cust = Customer(name="Huber Hans")
        db.session.add(cust)
        db.session.flush()
        oi = OpenItem(description="Beitrag", amount=Decimal("50"), date=date(YEAR, 1, 1),
                      customer_id=cust.id)
        db.session.add(oi)
        db.session.flush()
        return _booking(setup, date(YEAR, 1, 3), amount=Decimal("50"), open_item_id=oi.id)

    def test_open_item_payment_amount_is_fixed(self, client, setup):
        b = self._open_item_booking(setup)
        html = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX).get_data(as_text=True)
        assert "gleicht eine Rechnung bzw. einen Offenen Posten aus" in html
        client.post(f"/accounting/bookings/{b.id}/edit", headers=HX,
                    data=_edit_payload(setup, b, amount="70", description="Beitrag 2027"))
        b = db.session.get(Booking, b.id)
        assert b.amount == Decimal("50") and b.description == "Beitrag 2027"

    def test_open_item_payment_cannot_be_deleted(self, client, setup):
        b = self._open_item_booking(setup)
        client.post(f"/accounting/bookings/{b.id}/delete")
        assert db.session.get(Booking, b.id) is not None
        html = client.get(f"/accounting/bookings?year={YEAR}").get_data(as_text=True)
        assert f"/accounting/bookings/{b.id}/stornieren" in html
        assert f"/accounting/bookings/{b.id}/delete" not in html

    def test_bank_import_booking_keeps_amount_and_account(self, client, setup):
        other = RealAccount(name="Kassa", opening_balance=Decimal("0"), account_type="cash")
        db.session.add(other)
        b = _booking(setup, date(YEAR, 1, 3))
        stmt = BankStatement(format="camt053", filename="a.xml", file_hash="y" * 64,
                             real_account_id=setup["ra"].id)
        db.session.add(stmt)
        db.session.flush()
        db.session.add(BankStatementLine(statement_id=stmt.id, line_index=1, booking_date=b.date,
                                         amount=b.amount, purpose="Strom", booking_id=b.id))
        db.session.commit()
        assert acc_svc.payment_lock(b)[1] is True
        client.post(f"/accounting/bookings/{b.id}/edit", headers=HX,
                    data=_edit_payload(setup, b, amount="-99", real_account_id=other.id))
        b = db.session.get(Booking, b.id)
        assert b.amount == Decimal("-50") and b.real_account_id == setup["ra"].id

    def test_plain_booking_is_free(self, setup):
        b = _booking(setup, date(YEAR, 1, 3))
        assert acc_svc.payment_lock(b) is None


# ---------------------------------------------------------------------------
# Nachbuchung in einen abgeschlossenen Zeitraum: erlaubt, mit Hinweis
# ---------------------------------------------------------------------------

LATE_TEXT = "ist bereits abgeschlossen"
# Q3 des Vorjahrs ist an jedem Tag des laufenden Jahres abgeschlossen (AT-Sperrtag: 15.11. des Vorjahrs).
LATE_DATE = date(YEAR - 1, 9, 1)


@pytest.fixture
def prev_year(setup):
    fy = FiscalYear(year=YEAR - 1, start_date=date(YEAR - 1, 1, 1),
                    end_date=date(YEAR - 1, 12, 31), closed=False, is_vat_liable=True)
    db.session.add(fy)
    db.session.commit()
    return fy


def _new_payload(s, d, **over):
    data = {"account_id": s["acc"].id, "description": "Nachtrag", "project_id": "",
            "customer_id": "", "real_account_id": s["ra"].id, "action": "",
            "date": d.isoformat(), "amount": "-20", "reference": "N-1", "tax_rate": ""}
    data.update(over)
    return data


class TestLateEntry:
    def test_closed_through_rule_and_marker(self, app, setup):
        cfg = posting.settings()
        today = date(2027, 5, 16)                      # AT: Q1 ist seit dem Ende des 15.05. gesperrt
        assert posting.closed_through(today, cfg) == date(2027, 3, 31)
        assert posting.closed_through(date(2027, 5, 15), cfg) == date(2026, 12, 31)
        posting.mark_closed_until(date(2027, 6, 30))
        posting.mark_closed_until(date(2027, 3, 31))   # ein älterer Zeitraum setzt nicht zurück
        db.session.commit()
        assert posting.closed_until() == date(2027, 6, 30)
        assert posting.closed_through(today, cfg) == date(2027, 6, 30)

    def test_rule_applies_in_daily_mode_too(self, app, setup):
        _setting(posting.MODE_KEY, posting.MODE_DAILY)
        assert posting.closed_through(date(2027, 5, 16)) == date(2027, 3, 31)

    def test_form_carries_hint(self, client, setup):
        html = client.get("/accounting/bookings/new", headers=HX).get_data(as_text=True)
        assert "data-late-entry-hint" in html
        assert f'data-vat-years="{YEAR}"' in html

    def test_no_hint_without_vat_year(self, client, setup):
        setup["fy"].is_vat_liable = False
        db.session.commit()
        html = client.get("/accounting/bookings/new", headers=HX).get_data(as_text=True)
        assert "data-late-entry-hint" not in html

    def test_late_booking_is_saved_with_warning(self, client, setup, prev_year):
        r = client.post("/accounting/bookings/new", data=_new_payload(setup, LATE_DATE),
                        follow_redirects=True)
        html = r.get_data(as_text=True)
        assert f"Q3/{YEAR - 1} {LATE_TEXT}" in html
        b = Booking.query.filter_by(description="Nachtrag").one()
        assert b.status == Booking.STATUS_OFFEN and b.date == LATE_DATE

    def test_current_booking_has_no_warning(self, client, setup):
        r = client.post("/accounting/bookings/new", data=_new_payload(setup, TODAY),
                        follow_redirects=True)
        assert LATE_TEXT not in r.get_data(as_text=True)

    def test_no_warning_for_non_vat_year(self, client, setup, prev_year):
        prev_year.is_vat_liable = False
        db.session.commit()
        r = client.post("/accounting/bookings/new", data=_new_payload(setup, LATE_DATE),
                        follow_redirects=True)
        assert LATE_TEXT not in r.get_data(as_text=True)

    def test_closing_a_period_marks_it(self, client, setup, prev_year):
        # Q4 des Vorjahrs ist an jedem Tag des laufenden Jahres vorbei, also abschließbar;
        # der Merker gilt danach unabhängig davon, ob der Sperrtag (15.02.) schon war.
        q4_end = date(YEAR - 1, 12, 31)
        client.post("/accounting/ust/close-period", data={"year": YEAR - 1, "period": "q4"})
        assert posting.closed_until() == q4_end
        r = client.post("/accounting/bookings/new", data=_new_payload(setup, date(YEAR - 1, 12, 20)),
                        follow_redirects=True)
        assert LATE_TEXT in r.get_data(as_text=True)

    def test_running_period_cannot_be_closed(self, client, setup):
        """Ein laufender Zeitraum hat noch Tage vor sich — kein Abschluss, kein Merker."""
        b = _booking(setup, TODAY)
        quarter = (TODAY.month - 1) // 3 + 1
        html = client.get(f"/accounting/ust?year={YEAR}&period=q{quarter}").get_data(as_text=True)
        assert 'id="ust-close-period"' not in html
        r = client.post("/accounting/ust/close-period",
                        data={"year": YEAR, "period": f"q{quarter}"}, follow_redirects=True)
        assert "läuft noch" in r.get_data(as_text=True)
        assert _status(b.id) == Booking.STATUS_OFFEN
        assert posting.closed_until() is None

    def test_late_entry_text_in_off_mode(self, app, setup, prev_year):
        _setting(posting.MODE_KEY, "off")
        with app.test_request_context():
            posting.invalidate()
            text = posting.late_entry_warning(LATE_DATE)
        assert "automatisches Verbuchen ist ausgeschaltet" in text
        assert "nächsten Nacht" not in text

    def test_edit_warns_only_when_date_moves_in(self, client, setup, prev_year):
        b = _booking(setup, TODAY)
        r = client.post(f"/accounting/bookings/{b.id}/edit", data=_edit_payload(setup, b),
                        follow_redirects=True)
        assert LATE_TEXT not in r.get_data(as_text=True)
        r = client.post(f"/accounting/bookings/{b.id}/edit",
                        data=_edit_payload(setup, b, date=LATE_DATE.isoformat()),
                        follow_redirects=True)
        assert LATE_TEXT in r.get_data(as_text=True)

    def test_list_bar_counts_late_entries_for_tonight(self, client, setup, prev_year):
        _booking(setup, LATE_DATE)
        _booking(setup, TODAY)
        html = client.get("/accounting/bookings").get_data(as_text=True)
        assert "1 Nachtrag wird heute Nacht verbucht" in html
        assert "2 offen" in html      # die heutige zählt mit, aber nicht als Nachtrag
