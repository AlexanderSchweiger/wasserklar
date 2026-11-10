"""Teilrechnungen (Akonto) mit Jahresabrechnung und Gegenverrechnung.

Jahreszyklus: Plan aus dem Vorjahresverbrauch → Teilrechnungen → versenden →
Jahresabrechnung zieht sie ab → Nachzahlung bzw. Guthaben (verrechnen in der
naechsten Teilrechnung oder auszahlen). Dazu Zahlungs-Split auf Konten/USt,
Storno-Sperren, Entwurfs-Aufraeumen und Datenexport.
"""
import io
import json
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.installments import invoicing, plans, settlement, terms
from app.installments.amounts import round_installment, split_gross
from app.invoices.amounts import line_gross
from app.invoices.charges import build_tariff
from app.invoices.services import create_or_update_open_item
from app.meters.services import save_reading
from app.models import (
    Account, AppSetting, BillingPeriod, BillingRun, Booking, Customer, FiscalYear,
    InstallmentPlan, InstallmentPlanEntry, Invoice, InvoiceItem, Property,
    PropertyOwnership, ReadingCorrection, WaterMeter,
)

D = Decimal
TODAY = date.today()
Y = TODAY.year


@pytest.fixture
def logged_in_admin(client, user):
    from tests.conftest import _ensure_role
    from app.models import User
    role = _ensure_role("Admin")
    u = User(username="inst", email="inst@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "inst", "password": "secret"})
    return client


@pytest.fixture
def setup(app, user):
    """Zwei Perioden (Vorjahr + laufendes Jahr), ein Objekt mit Zaehler,
    Vorjahresverbrauch 100 m³, Tarif 2,00 €/m³ + 40 € Grundgebuehr, 10 % USt
    auf zwei Konten (Wasser/Grundgebuehr)."""
    for year in (Y - 1, Y):
        db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1),
                                  end_date=date(year, 12, 31), is_vat_liable=True))
    prev = BillingPeriod(name=str(Y - 1), start_date=date(Y - 1, 1, 1),
                         end_date=date(Y - 1, 12, 31), active=False)
    cur = BillingPeriod(name=str(Y), start_date=date(Y, 1, 1),
                        end_date=date(Y, 12, 31), active=True)
    water_acc = Account(name="Wasser", code="W01")
    base_acc = Account(name="Grundgebühr", code="G01")
    cust = Customer(name="Muster Max", customer_number=1, email="max@test.at",
                    rechnung_per_email=True)
    prop = Property(object_number="1", object_type="Haus", strasse="Weg", hausnummer="1")
    db.session.add_all([prev, cur, water_acc, base_acc, cust, prop])
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                     valid_from=date(Y - 5, 1, 1)))
    meter = WaterMeter(property_id=prop.id, meter_number="Z1", meter_type="main",
                       initial_value=D("0"))
    db.session.add(meter)
    tariff = build_tariff(name="T", valid_from=Y - 1, water_price=D("2.00"),
                          base_fee=D("40.00"), tax_rate=D("10"),
                          accounts={"water": water_acc.id, "base_fee": base_acc.id})
    db.session.flush()
    save_reading(meter, prev, D("100"), is_estimated=False, reading_date=date(Y - 1, 12, 31))
    db.session.commit()
    return {"prev": prev, "cur": cur, "cust": cust, "prop": prop, "meter": meter,
            "tariff": tariff, "water": water_acc, "base": base_acc, "user": user}


def _plan(s):
    proposals = plans.compute_plans(s["cur"], s["tariff"])
    plans.save_plans(s["cur"], s["tariff"], proposals, created_by_id=s["user"].id)
    db.session.commit()
    return InstallmentPlan.query.one()


def _issue(s, plan, seqs=None):
    entries = [e for e in plan.entries if seqs is None or e.seq in seqs]
    result = invoicing.create_installment_invoices(s["cur"], entries,
                                                   created_by_id=s["user"].id)
    db.session.commit()
    return result


def _send(invoices):
    for inv in invoices:
        inv.status = Invoice.STATUS_SENT
        create_or_update_open_item(inv)
    db.session.commit()


def _annual(s, client, value, credit_mode=None):
    # Zaehlerstand = Vorjahresstand 100 + Verbrauch
    save_reading(s["meter"], s["cur"], D("100") + D(value), is_estimated=False,
                 reading_date=date(Y, 12, 31))
    db.session.commit()
    data = {"billing_period_id": str(s["cur"].id), "tariff_id": str(s["tariff"].id),
            "due_days": "30"}
    if credit_mode:
        data["credit_mode"] = credit_mode
    resp = client.post("/invoices/generate", data=data)
    assert resp.status_code == 302
    return Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD,
                                   billing_period_id=s["cur"].id).one()


# ---------------------------------------------------------------------------
# Reine Regeln
# ---------------------------------------------------------------------------

class TestAmounts:
    def test_round_to_euro(self):
        assert round_installment(D("264.00"), 4, D("1")) == D("66.00")
        assert round_installment(D("265.00"), 4, D("1")) == D("66.00")   # 66,25
        assert round_installment(D("266.00"), 4, D("1")) == D("67.00")   # 66,50
        assert round_installment(D("10.00"), 4, D("10")) == D("2.50")    # Schritt > Betrag
        assert round_installment(D("0"), 4, D("1")) == D("0")

    @pytest.mark.parametrize("gross", ["66.00", "90.00", "0.37", "123.45", "-54.00"])
    def test_split_hits_gross(self, gross):
        groups = [{"share": D("0.8"), "tax_rate": D("10")},
                  {"share": D("0.2"), "tax_rate": D("20")}]
        parts = split_gross(D(gross), groups)
        assert sum(line_gross(p["net"], p["tax_rate"]) for p in parts) == D(gross)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class TestPlan:
    def test_basis_previous_consumption(self, setup):
        plan = _plan(setup)
        # 100 m³ × 2 + 40 = 240 netto → 264 brutto ÷ 4 = 66
        assert plan.basis_consumption == D("100")
        assert plan.basis_source == InstallmentPlan.BASIS_READING
        assert plan.annual_amount == D("264.00")
        assert plan.installment_amount == D("66.00")
        assert [e.due_date for e in plan.entries] == [
            date(Y, 2, 15), date(Y, 5, 15), date(Y, 8, 15), date(Y, 11, 15)]

    def test_new_property_uses_average(self, setup):
        s = setup
        other = Property(object_number="2", object_type="Haus")
        cust2 = Customer(name="Neu Nina", customer_number=2)
        db.session.add_all([other, cust2])
        db.session.flush()
        db.session.add(PropertyOwnership(property_id=other.id, customer_id=cust2.id,
                                         valid_from=date(Y, 1, 1)))
        db.session.add(WaterMeter(property_id=other.id, meter_number="Z2",
                                  meter_type="main", initial_value=D("0")))
        db.session.commit()
        props = {p.prop.id: p for p in plans.compute_plans(s["cur"], s["tariff"])}
        assert props[other.id].basis_source == InstallmentPlan.BASIS_AVERAGE
        assert props[other.id].basis_consumption == D("100")

    def test_opt_out_and_minimum(self, setup):
        s = setup
        s["cust"].installment_opt_out = True
        db.session.commit()
        [p] = plans.compute_plans(s["cur"], s["tariff"])
        assert p.status == plans.STATUS_OPT_OUT
        s["cust"].installment_opt_out = False
        db.session.commit()
        [p] = plans.compute_plans(s["cur"], s["tariff"], min_annual=D("500"))
        assert p.status == plans.STATUS_BELOW_MIN

    def test_manual_amount_survives_recalculation(self, setup):
        s = setup
        plan = _plan(s)
        plans.update_plan(plan, installment_amount=D("80"))
        db.session.commit()
        [p] = plans.compute_plans(s["cur"], s["tariff"])
        assert p.status == plans.STATUS_KEEP
        assert all(e.amount == D("80.00") for e in plan.entries)


# ---------------------------------------------------------------------------
# Teilrechnungen
# ---------------------------------------------------------------------------

class TestInstallmentInvoices:
    def test_lines_per_account_and_exact_amount(self, setup):
        s = setup
        plan = _plan(s)
        result = _issue(s, plan)
        assert len(result.created) == 4
        run = result.run
        assert run.run_kind == BillingRun.KIND_INSTALLMENT
        inv = result.created[0]
        assert inv.is_installment and inv.total_amount == D("66.00")
        assert {i.account_id for i in inv.items} == {s["water"].id, s["base"].id}
        assert all(i.charge_key == terms.KEY_INSTALLMENT for i in inv.items)
        assert inv.document_title == "Teilrechnung"
        assert inv.installment_entry.seq == 1

    def test_no_second_invoice_for_entry(self, setup):
        s = setup
        plan = _plan(s)
        _issue(s, plan, seqs={1})
        again = _issue(s, plan, seqs={1})
        assert again.created == [] and again.skipped

    def test_payment_splits_per_account(self, setup, real_account):
        from app.accounting.services import booking_group_from_invoice_payment
        s = setup
        plan = _plan(s)
        [inv] = _issue(s, plan, seqs={1}).created
        _send([inv])
        group, children = booking_group_from_invoice_payment(
            inv, D("66.00"), TODAY, real_account.id, s["user"].id, open_item=inv.open_item)
        db.session.commit()
        by_acc = {b.account_id: b.amount for b in children}
        assert sum(by_acc.values()) == D("66.00")
        assert set(by_acc) == {s["water"].id, s["base"].id}


# ---------------------------------------------------------------------------
# Jahresabrechnung
# ---------------------------------------------------------------------------

class TestExcludedCharges:
    """Nur ausgewaehlte Gebuehrenarten im Teilbetrag, der Rest in der
    Jahresabrechnung."""

    def _exclude_base_fee(self):
        AppSetting.set(terms.EXCLUDED_CHARGES_KEY, "base_fee")
        db.session.commit()

    def test_plan_only_water(self, setup):
        self._exclude_base_fee()
        plan = _plan(setup)
        # 100 m³ × 2 = 200 netto → 220 brutto ÷ 4 = 55
        assert plan.annual_amount == D("220.00")
        assert plan.installment_amount == D("55.00")
        assert {r["charge_key"] for r in plan.composition_rows} == {"water"}

    def test_minimum_checked_against_included_amount(self, setup):
        self._exclude_base_fee()
        [p] = plans.compute_plans(setup["cur"], setup["tariff"], min_annual=D("250"))
        assert p.status == plans.STATUS_BELOW_MIN     # 220 < 250 (mit Grundgebühr 264)

    def test_water_never_excluded(self, setup):
        AppSetting.set(terms.EXCLUDED_CHARGES_KEY, "water,base_fee")
        db.session.commit()
        assert terms.excluded_charge_keys() == frozenset({"base_fee"})
        [p] = plans.compute_plans(setup["cur"], setup["tariff"], excluded={"water"})
        assert p.annual_amount == D("264.00")

    def test_items_only_on_water_account(self, setup):
        self._exclude_base_fee()
        plan = _plan(setup)
        [inv] = _issue(setup, plan, seqs={1}).created
        assert {i.account_id for i in inv.items} == {setup["water"].id}
        assert inv.total_amount == D("55.00")

    def test_annual_settlement_bills_base_fee_in_full(self, setup, logged_in_admin):
        self._exclude_base_fee()
        s = setup
        plan = _plan(s)
        _send(_issue(s, plan).created)
        inv = _annual(s, logged_in_admin, "100")   # 200 + 40 = 240 → 264 brutto
        summ = settlement.summary(inv)
        assert summ["regular"]["gross"] == D("264.00")
        assert summ["deducted"]["gross"] == D("220.00")
        # Nachzahlung = Grundgebühr brutto (Verbrauch unverändert)
        assert inv.total_amount == D("44.00")
        assert any(i.charge_key == "base_fee" for i in inv.items)


def _pay(s, inv, real_account):
    from app.accounting.services import booking_group_from_invoice_payment
    booking_group_from_invoice_payment(inv, inv.total_amount, TODAY, real_account.id,
                                       s["user"].id, open_item=inv.open_item)
    db.session.commit()


def _send_paid(s, invoices, real_account):
    """Versenden und voll bezahlen."""
    _send(invoices)
    for inv in invoices:
        _pay(s, inv, real_account)


class TestCreditCancelsUnpaid:
    """Guthaben + unbezahlte Teilrechnung: storniert statt abgezogen."""

    def _sent(self, s):
        insts = sorted(_issue(s, _plan(s)).created, key=lambda i: i.due_date)
        _send(insts)
        return insts

    def test_unpaid_cancelled_credit_reduced(self, setup, logged_in_admin, real_account):
        s = setup
        insts = self._sent(s)
        for inv in insts[:3]:
            _pay(s, inv, real_account)
        # 50 m³ → 100 + 40 = 140 → 154 brutto; Soll-Abzug 264 → Guthaben 110,
        # davon 66 nie bezahlt → 4. storniert, Guthaben 154 − 198 = −44
        inv = _annual(s, logged_in_admin, "50", credit_mode="offset")
        db.session.refresh(insts[3])
        assert insts[3].status == Invoice.STATUS_CANCELLED
        credit = insts[3].active_credit_note
        assert credit is not None and credit.status == Invoice.STATUS_DRAFT
        assert inv.invoice_number in (credit.notes or "")
        summ = settlement.summary(inv)
        assert summ["deducted"]["gross"] == D("198.00")
        assert summ["credit"] == D("44.00")
        assert [d["invoice"].id for d in summ["cancelled_installments"]] == [insts[3].id]
        assert summ["open_installments"] == []
        carry = ReadingCorrection.query.filter_by(
            source_invoice_id=inv.id, kind=ReadingCorrection.KIND_CREDIT_CARRY).all()
        # Uebertrag netto, negativ = Guthaben: 44 brutto / 1,1
        assert sum((c.amount for c in carry), D("0")) == D("-40.00")
        assert settlement.deducting_invoice(insts[3]).id == inv.id
        assert insts[3].open_item.status != "Offen"

    def test_partial_cover_turns_into_back_payment(self, setup, logged_in_admin,
                                                   real_account):
        s = setup
        insts = self._sent(s)
        _pay(s, insts[0], real_account)
        # Guthaben 110 bei 3 unbezahlten: 4. storniert (−44), 3. storniert (+22) → Stopp
        inv = _annual(s, logged_in_admin, "50")
        db.session.refresh(insts[2])
        db.session.refresh(insts[3])
        assert insts[3].status == Invoice.STATUS_CANCELLED
        assert insts[2].status == Invoice.STATUS_CANCELLED
        assert insts[1].status == Invoice.STATUS_SENT
        assert inv.total_amount == D("22.00")
        summ = settlement.summary(inv)
        assert len(summ["cancelled_installments"]) == 2
        assert [d["invoice"].id for d in summ["open_installments"]] == [insts[1].id]
        assert summ["due_overall"] == D("88.00")

    def test_back_payment_cancels_nothing(self, setup, logged_in_admin):
        s = setup
        insts = self._sent(s)
        inv = _annual(s, logged_in_admin, "120")
        assert all(db.session.get(Invoice, i.id).status == Invoice.STATUS_SENT for i in insts)
        assert settlement.summary(inv)["cancelled_installments"] == []

    def test_credit_note_travels_with_settlement(self, app, setup, logged_in_admin,
                                                 real_account):
        from app.invoices import mailing
        s = setup
        insts = self._sent(s)
        for inv in insts[:3]:
            _pay(s, inv, real_account)
        inv = _annual(s, logged_in_admin, "50")
        credit = db.session.get(Invoice, insts[3].id).active_credit_note
        listed = mailing.run_invoices_with_linked(inv.billing_run)
        assert credit.id in {i.id for i in listed}
        [bundle] = [b for b in mailing.bundles(listed) if b["customer"].id == s["cust"].id]
        assert [i.id for i in bundle["invoices"]] == [inv.id, credit.id]
        # Post-Druck (OSS-Route + SaaS-Job): Gutschrift mit ins Kuvert, fremde IDs nicht
        printed = mailing.post_invoices_for_run(inv.billing_run, [inv.id, credit.id, insts[0].id])
        assert [i.id for i in printed] == [inv.id, credit.id]
        html = TestDocuments._html(None, app, inv)
        assert "entfällt – nicht zu zahlen" in html and "storniert" in html
        assert "nicht mehr zu bezahlen" in html


class TestAnnualSettlement:
    def test_back_payment(self, setup, logged_in_admin):
        s = setup
        plan = _plan(s)
        insts = _issue(s, plan).created
        _send(insts)
        inv = _annual(s, logged_in_admin, "120")   # 120 m³ → 240+40 = 280 → 308 brutto
        assert inv.has_deductions
        assert inv.document_title == "Jahresabrechnung"
        assert inv.total_amount == D("308.00") - 4 * D("66.00")
        summ = settlement.summary(inv)
        assert summ["regular"]["gross"] == D("308.00")
        assert summ["deducted"]["gross"] == D("264.00")
        assert len(summ["deductions"]) == 4
        assert summ["open_total"] == D("264.00")       # keine bezahlt
        for t in insts:
            assert settlement.deducting_invoice(t).id == inv.id

    def test_credit_offset_into_next_installment(self, setup, logged_in_admin, real_account):
        s = setup
        plan = _plan(s)
        _send_paid(s, _issue(s, plan).created, real_account)
        inv = _annual(s, logged_in_admin, "50", credit_mode="offset")  # 140 → 154 brutto
        assert inv.total_amount == D("0.00")
        carries = settlement.credit_carries(inv)
        assert sum(c.amount for c in carries) < 0
        summ = settlement.summary(inv)
        assert summ["is_credit"] and summ["credit"] == D("110.00")   # 264 - 154
        # Ansicht + Liste zeigen das vorgetragene Guthaben statt nur 0,00 €
        assert inv.carried_credit == D("110.00")
        detail = logged_in_admin.get(f"/invoices/{inv.id}").get_data(as_text=True)
        assert "Guthaben" in detail and "110,00" in detail
        listing = logged_in_admin.get("/invoices/").get_data(as_text=True)
        assert "Guthaben 110,00" in listing
        # Naechste Periode: Plan + erste Teilrechnung zieht das Guthaben ein.
        nxt = BillingPeriod(name=str(Y + 1), start_date=date(Y + 1, 1, 1),
                            end_date=date(Y + 1, 12, 31))
        db.session.add(nxt)
        db.session.add(FiscalYear(year=Y + 1, start_date=date(Y + 1, 1, 1),
                                  end_date=date(Y + 1, 12, 31), is_vat_liable=True))
        db.session.commit()
        proposals = plans.compute_plans(nxt, s["tariff"])
        plans.save_plans(nxt, s["tariff"], proposals)
        db.session.commit()
        nplan = InstallmentPlan.query.filter_by(billing_period_id=nxt.id).one()
        assert nplan.basis_consumption == D("50")
        created = invoicing.create_installment_invoices(nxt, nplan.entries).created
        db.session.commit()
        # Teilbetrag (50×2+40)×1,1/4 = 38,50 → 39; Guthaben 110 → 1.–2. auf 0, 3. 7
        totals = [c.total_amount for c in sorted(created, key=lambda i: i.due_date)]
        assert totals[0] == D("0.00") and totals[1] == D("0.00")
        assert totals[2] == D("39.00") * 3 - D("110.00")
        assert totals[3] == D("39.00")

    def test_credit_refund_keeps_negative(self, setup, logged_in_admin, real_account):
        s = setup
        plan = _plan(s)
        _send_paid(s, _issue(s, plan).created, real_account)
        inv = _annual(s, logged_in_admin, "50", credit_mode="refund")
        assert inv.total_amount == D("154.00") - D("264.00")
        create_or_update_open_item(inv)
        db.session.commit()
        assert inv.open_item.amount == D("-110.00")

    def test_drafts_deleted_and_not_deducted(self, setup, logged_in_admin):
        s = setup
        plan = _plan(s)
        insts = _issue(s, plan).created
        _send(insts[:2])
        inv = _annual(s, logged_in_admin, "120")
        assert len(settlement.summary(inv)["deductions"]) == 2
        assert Invoice.query.filter_by(invoice_kind=Invoice.KIND_INSTALLMENT).count() == 2
        assert [e.state for e in plan.entries][2:] == [InstallmentPlanEntry.STATE_PLANNED] * 2

    def test_no_installments_after_settlement(self, setup, logged_in_admin):
        s = setup
        plan = _plan(s)
        _send(_issue(s, plan, seqs={1}).created)
        _annual(s, logged_in_admin, "120")
        result = _issue(s, plan, seqs={2})
        assert result.created == []
        assert "abgerechnet" in result.skipped[0][1]

    def test_cancel_blocked_while_settled(self, setup, logged_in_admin):
        s = setup
        plan = _plan(s)
        insts = _issue(s, plan, seqs={1}).created
        _send(insts)
        inv = _annual(s, logged_in_admin, "120")
        assert "verrechnet" in settlement.cancel_blocker(insts[0])
        inv.status = Invoice.STATUS_CANCELLED
        db.session.commit()
        assert settlement.cancel_blocker(insts[0]) is None
        assert settlement.deductible_installments(s["cur"].id, s["prop"].id, s["cust"].id)

    def test_back_payment_books_per_account(self, setup, logged_in_admin, real_account):
        from app.accounting.services import booking_group_from_invoice_payment
        s = setup
        plan = _plan(s)
        _send(_issue(s, plan).created)
        inv = _annual(s, logged_in_admin, "120")
        create_or_update_open_item(inv)
        db.session.commit()
        _g, children = booking_group_from_invoice_payment(
            inv, inv.total_amount, TODAY, real_account.id, s["user"].id,
            open_item=inv.open_item)
        by_acc = {b.account_id: b.amount for b in children}
        # Wasser: 240 netto Jahr − 4 × Anteil; Grundgebuehr: 40 − 4 × Anteil
        assert sum(by_acc.values()) == inv.total_amount
        water_gross = line_gross(D("240"), D("10")) - sum(
            line_gross(i.amount, i.tax_rate) for t in Invoice.query.filter_by(
                invoice_kind=Invoice.KIND_INSTALLMENT).all()
            for i in t.items if i.account_id == s["water"].id)
        assert by_acc[s["water"].id] == water_gross


class TestDataTransfer:
    def test_roundtrip(self, setup, tmp_path):
        from flask import g
        from app.data_transfer.services import (
            export_to_zip, extract_to_temp, import_from_zip, validate_manifest)
        s = setup
        plan = _plan(s)
        _send(_issue(s, plan, seqs={1}).created)
        buf = io.BytesIO()
        export_to_zip({"stammdaten": True, "buchungen": True, "mahnwesen": False,
                       "einstellungen": False, "include_pdfs": False, "years": []},
                      buf, exported_by="test")
        extract_dir, manifest = extract_to_temp(io.BytesIO(buf.getvalue()), str(tmp_path))
        assert validate_manifest(manifest, extract_dir)["errors"] == []
        import_from_zip(extract_dir, manifest, mode="replace")
        db.session.remove()
        for attr in ("_login_user", "_flask_login_user"):
            g.pop(attr, None)
        plan = InstallmentPlan.query.one()
        inv = Invoice.query.filter_by(invoice_kind=Invoice.KIND_INSTALLMENT).one()
        assert inv.installment_entry.plan_id == plan.id
        assert len(plan.entries) == 4


class TestDocuments:
    """Beleg-HTML (OSS-Vorlage) fuer Teilrechnung und Jahresabrechnung."""

    def _html(self, app, inv):
        from app.invoices.pdf_service import render_invoice_html
        with app.test_request_context():
            return render_invoice_html(inv)

    def test_installment_document(self, app, setup):
        s = setup
        [inv] = _issue(s, _plan(s), seqs={2}).created
        html = self._html(app, inv)
        assert "Teilrechnung" in html and "2 von 4" in html
        assert "Weitere Teilbetrage" not in html
        assert "Weitere Teilbeträge" in html and "15.02." in html

    def test_settlement_document(self, app, setup, logged_in_admin):
        s = setup
        _send(_issue(s, _plan(s)).created)
        inv = _annual(s, logged_in_admin, "120")
        html = self._html(app, inv)
        assert "Jahresabrechnung" in html
        assert "Gegenverrechnung" in html and "Nachzahlung" in html
        assert "Summe Jahresabrechnung" in html and "308,00" in html
        # Gegenverrechnungs-Zeilen stehen nicht in der Positionstabelle
        assert html.count("abzüglich Teilrechnung") == 4
        # Zahlstand steht in der Tabelle, offene Beträge laufen bis „Insgesamt offen“
        assert "<th>Zahlung</th>" in html
        assert html.count('<span class="inst-open">offen</span>') == 4
        assert html.count("zuzüglich noch offen") == 4
        # Nachzahlung 44 + 4 × 66 offen = 308
        assert settlement.summary(inv)["due_overall"] == D("308.00")
        assert "Insgesamt offen" in html
        assert "Noch nicht bezahlt" not in html

    def test_settlement_document_paid_installment(self, app, setup, logged_in_admin,
                                                  real_account):
        s = setup
        insts = _issue(s, _plan(s)).created
        _send(insts)
        from app.accounting.services import booking_group_from_invoice_payment
        first = min(insts, key=lambda i: i.due_date)
        booking_group_from_invoice_payment(first, first.total_amount, TODAY, real_account.id,
                                           s["user"].id, open_item=first.open_item)
        db.session.commit()
        inv = _annual(s, logged_in_admin, "120")
        summ = settlement.summary(inv)
        states = [d["pay_state"] for d in summ["deductions"]]
        assert states.count("paid") == 1 and states.count("open") == 3
        html = self._html(app, inv)
        assert html.count("zuzüglich noch offen") == 3
        assert ">bezahlt<" in html

    def test_credit_document(self, app, setup, logged_in_admin, real_account):
        s = setup
        _send_paid(s, _issue(s, _plan(s)).created, real_account)
        inv = _annual(s, logged_in_admin, "50", credit_mode="offset")
        html = self._html(app, inv)
        assert "Guthaben" in html and "110,00" in html
        assert "verrechnen wir" in html
        assert "Verrechnung mit der nächsten Teilrechnung" not in html  # Uebertragszeile verborgen

    def test_docx_settlement_and_credit(self, app, setup, logged_in_admin, real_account):
        import io as _io
        from docx import Document as _Docx
        from app.invoices.document_service import generate_docx
        from app.settings_service import wg_settings
        s = setup
        _send_paid(s, _issue(s, _plan(s)).created, real_account)
        inv = _annual(s, logged_in_admin, "50", credit_mode="offset")
        with app.test_request_context():
            data = generate_docx(inv, wg_settings())
        doc = _Docx(_io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        cells = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
        assert "Gegenverrechnung" in text and "Guthaben" in cells
        assert "verrechnen wir" in cells
        assert "Summe Jahresabrechnung" in cells


class TestOwnerChange:
    def test_seller_settlement_and_buyer_takes_over(self, setup, logged_in_admin):
        from app.owner_change import services as oc_svc
        s = setup
        plan = _plan(s)
        insts = sorted(_issue(s, plan).created, key=lambda i: i.due_date)
        _send(insts)                                   # alle vier im Jänner versendet
        buyer = Customer(name="Käufer Kurt", customer_number=9)
        db.session.add(buyer)
        db.session.commit()
        stichtag = date(Y, 7, 1)
        oc, warnings = oc_svc.execute_owner_change(
            prop=s["prop"], period=s["cur"], stichtag=stichtag,
            new_customer_ids=[buyer.id],
            meter_inputs={s["meter"].id: {"value": D("160"), "is_estimated": False}},
            create_settlement=True, settlement_recipient_id=s["cust"].id,
            tariff=s["tariff"], due_days=30, fee_mode=oc_svc.FEE_MODE_PRO_RATA,
            created_by_id=s["user"].id)
        settlement_inv = db.session.get(Invoice, oc.settlement_invoice_id)
        summ = settlement.summary(settlement_inv)
        # Feb + Mai verrechnet, Aug + Nov storniert
        assert {d["invoice"].id for d in summ["deductions"]} == {insts[0].id, insts[1].id}
        assert insts[2].status == Invoice.STATUS_CANCELLED
        assert insts[3].status == Invoice.STATUS_CANCELLED
        assert all(i.active_credit_note is not None for i in insts[2:])
        # Kaeufer hat einen Plan mit den Faelligkeiten ab Stichtag
        bplan = InstallmentPlan.query.filter_by(customer_id=buyer.id).one()
        assert [e.due_date for e in bplan.entries] == [date(Y, 8, 15), date(Y, 11, 15)]
        assert any("übernimmt" in w for w in warnings)
