"""Teilrechnungen des Folgejahres mit der Jahresabrechnung versenden — wählbar.

Schalter im Versenden-Dialog des Laufs (Vorbelegung aus der Einstellung
``installments.send_with_settlement``), Verknüpfung auch von der Seite
„Teilbeträge“ aus, Guthaben in schon erstellte Teilrechnungen und Duplex-Druck.
"""
from datetime import date

import pytest

from app.extensions import db
from app.installments import invoicing, plans, settlement, terms
from app.invoices import mailing
from app.models import AppSetting, BillingPeriod, BillingRun, FiscalYear, InstallmentPlan, Invoice
from tests.integration.conftest import real_account, user  # noqa: F401  (Fixtures)
from tests.integration.test_installments import (  # noqa: F401  (Fixtures)
    D, Y, _annual, _issue, _plan, _send, _send_paid, logged_in_admin, setup,
)


def _enable():
    AppSetting.set(terms.ENABLED_KEY, "true")
    db.session.commit()


def _next_period():
    nxt = BillingPeriod(name=str(Y + 1), start_date=date(Y + 1, 1, 1),
                        end_date=date(Y + 1, 12, 31), active=False)
    db.session.add(nxt)
    db.session.add(FiscalYear(year=Y + 1, start_date=date(Y + 1, 1, 1),
                              end_date=date(Y + 1, 12, 31), is_vat_liable=True))
    db.session.commit()
    return nxt


def _next_installments(s, nxt, source_run=None):
    plans.save_plans(nxt, s["tariff"], plans.compute_plans(nxt, s["tariff"]))
    nplan = InstallmentPlan.query.filter_by(billing_period_id=nxt.id).one()
    result = invoicing.create_installment_invoices(nxt, nplan.entries, source_run=source_run)
    db.session.commit()
    return sorted(result.created, key=lambda i: i.due_date)


@pytest.fixture
def linked(setup, logged_in_admin):
    """Jahresabrechnung (Entwurf) + vier verknüpfte Teilrechnungen des Folgejahres."""
    _enable()
    s = setup
    _send(_issue(s, _plan(s)).created)
    annual = _annual(s, logged_in_admin, "120")
    nxt = _next_period()
    nexts = _next_installments(s, nxt, source_run=annual.billing_run)
    return {"s": s, "annual": annual, "run": annual.billing_run, "next": nexts,
            "client": logged_in_admin}


def _ids_in_bundles(ctx):
    return {i.id for b in ctx["mail_bundles"] for i in b["invoices"]}


class TestDialogSwitch:
    def _ctx(self, run, choice=None):
        from app.invoices.routes import _billing_run_overview, _run_send_context
        return _run_send_context(run, _billing_run_overview(run), choice)

    def test_default_sends_together(self, linked):
        ctx = self._ctx(linked["run"])
        assert ctx["linked_choice"] and ctx["with_linked"] and ctx["linked_count"] == 4
        assert _ids_in_bundles(ctx) == {linked["annual"].id} | {i.id for i in linked["next"]}
        page = linked["client"].get(f"/invoices/billing-runs/{linked['run'].id}").get_data(as_text=True)
        assert 'id="br-with-linked"' in page and "mitschicken" in page
        assert "with_installments=0" in page                  # Schalter führt zum Ausschalten

    def test_switch_off_sends_settlement_alone(self, linked):
        ctx = self._ctx(linked["run"], "0")
        assert ctx["linked_choice"] and not ctx["with_linked"]
        assert not any(i.id in _ids_in_bundles(ctx) for i in linked["next"])
        html = linked["client"].get(
            f"/invoices/billing-runs/{linked['run'].id}/send-panel?with_installments=0"
        ).get_data(as_text=True)
        assert 'id="br-send-body"' in html and "with_installments=1" in html
        assert linked["annual"].invoice_number in html
        assert not any(i.invoice_number in html for i in linked["next"])
        assert "bleiben Entwürfe" in html

    def test_setting_sets_the_default(self, linked):
        AppSetting.set(terms.SEND_TOGETHER_KEY, "false")
        db.session.commit()
        assert not self._ctx(linked["run"])["with_linked"]
        assert self._ctx(linked["run"], "1")["with_linked"]     # im Dialog wieder einschaltbar

    def test_post_ids_follow_the_switch(self, linked):
        cust = linked["s"]["cust"]
        cust.rechnung_per_email = False
        db.session.commit()
        together = self._ctx(linked["run"])["post_ids"]
        assert together == [linked["annual"].id] + [i.id for i in linked["next"]]   # ein Kuvert
        assert self._ctx(linked["run"], "0")["post_ids"] == [linked["annual"].id]

    def test_no_switch_without_linked_drafts(self, linked):
        for inv in linked["next"]:
            inv.status = Invoice.STATUS_SENT
        db.session.commit()
        ctx = self._ctx(linked["run"])
        assert not ctx["linked_choice"] and not ctx["with_linked"]

    def test_send_panel_writes_nothing(self, linked):
        before = (Invoice.query.filter_by(status=Invoice.STATUS_DRAFT).count(),
                  AppSetting.query.count())
        linked["client"].get(f"/invoices/billing-runs/{linked['run'].id}/send-panel")
        assert (Invoice.query.filter_by(status=Invoice.STATUS_DRAFT).count(),
                AppSetting.query.count()) == before


class TestSetting:
    def _post(self, client, **extra):
        data = {"installment_fields": "1", "installments_enabled": "1",
                "installments_due_dates": terms.due_dates_text(),
                "installments_rounding": "1", "installments_credit_mode": "offset"}
        data.update(extra)
        return client.post("/einstellungen/", data=data)

    def test_card_shows_switch_checked_by_default(self, setup, logged_in_admin):
        html = logged_in_admin.get("/einstellungen/").get_data(as_text=True)
        assert 'name="installments_send_together"' in html
        assert terms.send_with_settlement()

    def test_unchanged_save_writes_nothing_then_off(self, setup, logged_in_admin):
        self._post(logged_in_admin, installments_send_together="1")
        assert AppSetting.query.filter_by(key=terms.SEND_TOGETHER_KEY).first() is None
        self._post(logged_in_admin)                            # Haken entfernt
        assert not terms.send_with_settlement()
        self._post(logged_in_admin, installments_send_together="1")
        assert terms.send_with_settlement()


class TestLinkFromInstallmentsPage:
    def test_create_form_offers_and_links_the_run(self, setup, logged_in_admin):
        _enable()
        s = setup
        _send(_issue(s, _plan(s)).created)
        annual = _annual(s, logged_in_admin, "120")
        nxt = _next_period()
        plans.save_plans(nxt, s["tariff"], plans.compute_plans(nxt, s["tariff"]))
        db.session.commit()
        run = annual.billing_run
        assert invoicing.linkable_runs(nxt) == [run]
        html = logged_in_admin.get(f"/installments/period/{nxt.id}").get_data(as_text=True)
        assert "Gemeinsam versenden mit" in html
        assert f'<option value="{run.id}" selected' in html
        resp = logged_in_admin.post(f"/installments/period/{nxt.id}/invoices",
                                    data={"seq": ["1"], "source_run_id": str(run.id)})
        assert resp.status_code == 302 and f"/billing-runs/{run.id}" in resp.headers["Location"]
        new_run = BillingRun.query.filter_by(run_kind=BillingRun.KIND_INSTALLMENT,
                                             billing_period_id=nxt.id).one()
        assert new_run.source_run_id == run.id

    def test_foreign_run_is_not_linked(self, setup, logged_in_admin):
        _enable()
        s = setup
        inst_run = _issue(s, _plan(s)).run              # ein Teilrechnungslauf, kein Abrechnungslauf
        nxt = _next_period()
        plans.save_plans(nxt, s["tariff"], plans.compute_plans(nxt, s["tariff"]))
        db.session.commit()
        logged_in_admin.post(f"/installments/period/{nxt.id}/invoices",
                             data={"seq": ["1"], "source_run_id": str(inst_run.id)})
        created = BillingRun.query.filter_by(run_kind=BillingRun.KIND_INSTALLMENT,
                                             billing_period_id=nxt.id).one()
        assert created.source_run_id is None


class TestCreditIntoEarlierDrafts:
    def test_credit_reaches_first_draft_created_before_the_run(self, setup, logged_in_admin,
                                                               real_account):
        """Teilrechnungen des Folgejahres VOR der Jahresabrechnung erstellt: das Guthaben
        landet trotzdem in der frühesten — wie beim Erstellen danach."""
        _enable()
        s = setup
        _send_paid(s, _issue(s, _plan(s)).created, real_account)
        nxt = _next_period()
        nexts = _next_installments(s, nxt)
        before = nexts[0].total_amount
        inv = _annual(s, logged_in_admin, "50", credit_mode="offset")
        summ = settlement.summary(inv)
        assert summ["is_credit"] and summ["credit"] == D("110.00")
        db.session.expire_all()
        first = db.session.get(Invoice, nexts[0].id)
        assert any(it.reading_correction_id for it in first.items)
        # Guthaben 110 > Teilbetrag: die erste steht auf 0, der Rest geht in die zweite
        assert first.total_amount == D("0.00") and before > 0
        second = db.session.get(Invoice, nexts[1].id)
        assert second.total_amount == before - (D("110.00") - before)
        assert all(it.account_id for it in first.items + second.items)


class TestNotice:
    def test_next_plan_says_each_installment_has_its_invoice(self, linked):
        np_ = settlement.summary(linked["annual"])["next_plan"]
        assert np_["notice"] == "Für jeden Teilbetrag erhalten Sie eine eigene Teilrechnung."


def test_start_on_new_sheet_pads_odd_page_counts():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    mailing.start_on_new_sheet(writer)
    assert len(writer.pages) == 2
    mailing.start_on_new_sheet(writer)
    assert len(writer.pages) == 2
