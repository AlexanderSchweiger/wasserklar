"""Seiten „Teilbeträge“, Einstellungskarte und Rechnungslauf-Hinweis."""
from decimal import Decimal

from app.extensions import db
from app.installments import terms
from app.models import AppSetting, InstallmentPlan, Invoice
from tests.integration.conftest import real_account, user  # noqa: F401  (Fixtures)
from tests.integration.test_installments import (  # noqa: F401  (Fixtures)
    D, _issue, _plan, _send, logged_in_admin, setup,
)


def _enable():
    AppSetting.set(terms.ENABLED_KEY, "true")
    db.session.commit()


def test_nav_hidden_when_off(setup, logged_in_admin):
    html = logged_in_admin.get("/invoices/").get_data(as_text=True)
    assert "/installments/" not in html
    _enable()
    html = logged_in_admin.get("/invoices/").get_data(as_text=True)
    assert "/installments/" in html


def test_calculate_preview_writes_nothing_then_save(setup, logged_in_admin):
    _enable()
    s = setup
    resp = logged_in_admin.get(f"/installments/period/{s['cur'].id}")
    assert resp.status_code == 200
    resp = logged_in_admin.get(f"/installments/period/{s['cur'].id}/calculate"
                               f"?tariff_id={s['tariff'].id}")
    assert resp.status_code == 200
    assert "66,00" in resp.get_data(as_text=True)
    assert InstallmentPlan.query.count() == 0
    resp = logged_in_admin.post(f"/installments/period/{s['cur'].id}/calculate",
                                data={"tariff_id": s["tariff"].id})
    assert resp.status_code == 302
    assert InstallmentPlan.query.count() == 1


def test_edit_plan_modal_and_row(setup, logged_in_admin):
    _enable()
    plan = _plan(setup)
    url = f"/installments/plan/{plan.id}/edit"
    assert logged_in_admin.get(url, headers={"X-From-Modal": "1"}).status_code == 200
    resp = logged_in_admin.post(url, data={"installment_amount": "70,00", "skip_seq": ["4"]},
                                headers={"X-From-Modal": "1"})
    assert resp.status_code == 204
    assert "installmentPlanSaved" in resp.headers["HX-Trigger"]
    db.session.expire_all()
    plan = db.session.get(InstallmentPlan, plan.id)
    assert plan.manual and plan.installment_amount == D("70.00")
    assert [e.skipped for e in plan.entries] == [False, False, False, True]
    assert logged_in_admin.get(f"/installments/plan/{plan.id}/row").status_code == 200


def test_create_invoices_redirects_to_run(setup, logged_in_admin):
    _enable()
    s = setup
    _plan(s)
    resp = logged_in_admin.post(f"/installments/period/{s['cur'].id}/invoices",
                                data={"seq": ["1", "2"]})
    assert resp.status_code == 302 and "/billing-runs/" in resp.headers["Location"]
    assert Invoice.query.filter_by(invoice_kind=Invoice.KIND_INSTALLMENT).count() == 2
    run_page = logged_in_admin.get(resp.headers["Location"])
    assert run_page.status_code == 200


def test_settings_card_saves_with_marker(setup, logged_in_admin):
    resp = logged_in_admin.get("/einstellungen/")
    assert "Teilrechnungen (Akonto)" in resp.get_data(as_text=True)
    resp = logged_in_admin.post("/einstellungen/", data={
        "installment_fields": "1", "installments_enabled": "1",
        "installments_due_dates": "15.03.; 15.09.", "installments_rounding": "5",
        "installments_min_annual": "50,00", "installments_credit_mode": "refund",
        "installments_title": "Vorschreibung"})
    assert resp.status_code == 302
    assert terms.is_enabled()
    assert terms.due_dates_text() == "15.03.;15.09."
    assert terms.rounding_step() == Decimal("5")
    assert terms.min_annual_amount() == Decimal("50.00")
    assert terms.credit_mode() == terms.CREDIT_REFUND
    assert terms.installment_title() == "Vorschreibung"
    # ohne Marker bleibt alles stehen
    logged_in_admin.post("/einstellungen/", data={})
    assert terms.is_enabled()


def test_settings_card_excluded_charges(setup, logged_in_admin):
    html = logged_in_admin.get("/einstellungen/").get_data(as_text=True)
    assert 'name="installments_charges"' in html
    base = {"installment_fields": "1", "installments_enabled": "1",
            "installments_due_dates": "15.02.;15.05.;15.08.;15.11."}
    # Nur Wasser angehakt (water kommt ohnehin über das Hidden-Feld) → Grundgebühr raus
    logged_in_admin.post("/einstellungen/", data={
        **base, "installments_charge_fields": "1", "installments_charges": ["water"]})
    assert "base_fee" in terms.excluded_charge_keys()
    assert "water" not in terms.excluded_charge_keys()
    # Karte ohne Untermarker (z. B. ältere Seite) lässt die Auswahl stehen
    logged_in_admin.post("/einstellungen/", data=base)
    assert "base_fee" in terms.excluded_charge_keys()
    # Periodenseite nennt die ausgeschlossene Gebühr
    html = logged_in_admin.get(f"/installments/period/{setup['cur'].id}").get_data(as_text=True)
    assert "ohne Grundgebühr" in html
    # wieder alle angehakt
    logged_in_admin.post("/einstellungen/", data={
        **base, "installments_charge_fields": "1",
        "installments_charges": ["water", "base_fee", "additional_fee", "water_levy"]})
    assert terms.excluded_charge_keys() == frozenset()


def test_generate_form_shows_installments(setup, logged_in_admin):
    _enable()
    s = setup
    _send(_issue(s, _plan(s), seqs={1}).created)
    html = logged_in_admin.get("/invoices/generate").get_data(as_text=True)
    assert "Teilrechnungen" in html and "1 versendet" in html


def test_invoice_pages_render(setup, logged_in_admin):
    s = setup
    [inv] = _issue(s, _plan(s), seqs={1}).created
    assert logged_in_admin.get(f"/invoices/{inv.id}").status_code == 200
    assert logged_in_admin.get("/invoices/").status_code == 200
    assert logged_in_admin.get(f"/invoices/period/{s['cur'].id}").status_code == 200


def test_editor_keeps_deduction_lines(setup, logged_in_admin):
    from tests.integration.test_installments import _annual
    s = setup
    _send(_issue(s, _plan(s)).created)
    inv = _annual(s, logged_in_admin, "120")
    total = inv.total_amount
    html = logged_in_admin.get(f"/invoices/{inv.id}").get_data(as_text=True)
    assert "Gegenverrechnung" in html
    # Editor sendet nur die regulaeren Zeilen (wie der Browser) — die Abzuege bleiben.
    regular = [i for i in inv.items if not i.deducts_invoice_id]
    data = {"row_item_id[]": [], "row_type[]": [], "row_description[]": [],
            "row_quantity[]": [], "row_unit[]": [], "row_unit_price[]": [],
            "row_tax_rate[]": [], "row_account_id[]": [], "row_project_id[]": [],
            "row_tariff_id[]": [], "row_consumption_m3[]": []}
    for it in regular:
        data["row_item_id[]"].append(str(it.id))
        data["row_type[]"].append("free")
        data["row_description[]"].append(it.description)
        data["row_quantity[]"].append(str(it.quantity))
        data["row_unit[]"].append(it.unit)
        data["row_unit_price[]"].append(str(it.unit_price))
        data["row_tax_rate[]"].append(str(it.tax_rate or ""))
        data["row_account_id[]"].append(str(it.account_id or ""))
        data["row_project_id[]"].append("")
        data["row_tariff_id[]"].append("")
        data["row_consumption_m3[]"].append("")
    resp = logged_in_admin.post(f"/invoices/{inv.id}/items/save", data=data)
    assert resp.status_code == 302
    db.session.expire_all()
    inv = db.session.get(Invoice, inv.id)
    assert len(inv.deduction_items) == 8          # 4 Teilrechnungen × 2 Konten
    assert inv.total_amount == total


def test_cancel_settled_installment_blocked(setup, logged_in_admin):
    from tests.integration.test_installments import _annual
    s = setup
    insts = _issue(s, _plan(s), seqs={1}).created
    _send(insts)
    _annual(s, logged_in_admin, "120")
    logged_in_admin.post(f"/invoices/{insts[0].id}/status", data={"status": "Storniert"})
    db.session.expire_all()
    assert db.session.get(Invoice, insts[0].id).status == Invoice.STATUS_SENT


def test_bundle_mail_one_message_per_customer(setup, logged_in_admin, monkeypatch):
    """Jahresabrechnung + verknuepfte Teilrechnungen: eine E-Mail mit allen Anhaengen."""
    from datetime import date as _date
    from app.installments import invoicing, plans as inst_plans
    from app.models import BillingPeriod, BillingRun, FiscalYear
    from tests.integration.test_installments import Y, _annual
    sent = []
    monkeypatch.setattr("app.settings_service.send_mail", lambda msg: sent.append(msg))
    monkeypatch.setattr("app.invoices.pdf_service.render_invoice_pdf",
                        lambda inv, **kw: b"%PDF-1.4 test")
    _enable()
    s = setup
    _send(_issue(s, _plan(s)).created)
    annual = _annual(s, logged_in_admin, "120")
    nxt = BillingPeriod(name=str(Y + 1), start_date=_date(Y + 1, 1, 1), end_date=_date(Y + 1, 12, 31))
    db.session.add(nxt)
    db.session.add(FiscalYear(year=Y + 1, start_date=_date(Y + 1, 1, 1),
                              end_date=_date(Y + 1, 12, 31), is_vat_liable=True))
    db.session.commit()
    inst_plans.save_plans(nxt, s["tariff"], inst_plans.compute_plans(nxt, s["tariff"]))
    run = annual.billing_run
    nplan = InstallmentPlan.query.filter_by(billing_period_id=nxt.id).one()
    invoicing.create_installment_invoices(nxt, nplan.entries, source_run=run)
    db.session.commit()
    page = logged_in_admin.get(f"/invoices/billing-runs/{run.id}").get_data(as_text=True)
    assert "Eine E-Mail je Kontakt" in page
    ids = [annual.id] + [i.id for i in Invoice.query.filter_by(
        invoice_kind=Invoice.KIND_INSTALLMENT, billing_period_id=nxt.id).all()]
    resp = logged_in_admin.post("/invoices/send-email-bundle",
                                data={"invoice_ids": ",".join(map(str, ids))})
    assert resp.get_json()["ok"], resp.get_json()
    assert len(sent) == 1 and len(sent[0].attachments) == 5
    db.session.expire_all()
    annual = db.session.get(Invoice, annual.id)
    assert annual.status == Invoice.STATUS_SENT and annual.mailed_with_invoice_id is None
    companions = Invoice.query.filter(Invoice.id.in_(ids[1:])).all()
    assert all(c.status == Invoice.STATUS_SENT and c.mailed_with_invoice_id == annual.id
               for c in companions)
    assert all(c.mail_status_invoice.id == annual.id for c in companions)
