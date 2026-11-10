"""Seite „Teilbeträge“: Plaene je Periode berechnen, bearbeiten, Teilrechnungen erstellen.

Recht wie Rechnungen/OP (``rechnungen_op``). Ist das Verfahren ausgeschaltet,
bleibt die Seite fuer vorhandene Plaene erreichbar (Altbestand), zeigt aber
einen Hinweis; neu berechnen geht nur eingeschaltet.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, abort, flash, make_response, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.auth.permissions import PERM_RECHNUNGEN, require_blueprint_permission
from app.extensions import db
from app.installments import invoicing, plans, settlement, terms
from app.installments.schedule import ScheduleError, dates_for_period, parse_due_dates
from app.models import (
    BillingPeriod, BillingRun, InstallmentPlan, InstallmentPlanEntry, Invoice, Project,
    WaterTariff,
)

bp = Blueprint("installments", __name__, url_prefix="/installments")
bp.before_request(require_blueprint_permission(PERM_RECHNUNGEN))

ZERO = Decimal("0")


def _periods():
    return BillingPeriod.query.order_by(BillingPeriod.start_date.desc(),
                                        BillingPeriod.id.desc()).all()


def _default_tariff(period):
    tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()
    year = period.start_date.year
    for t in tariffs:
        if (t.valid_from or 0) <= year and (t.valid_to is None or t.valid_to >= year):
            return t, tariffs
    return (tariffs[0] if tariffs else None), tariffs


def _parse_money(raw):
    raw = (raw or "").strip().replace("€", "").replace(" ", "")
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise plans.PlanError("Bitte einen gültigen Betrag angeben.")


def _plan_rows(period):
    rows = (InstallmentPlan.query.filter_by(billing_period_id=period.id)
            .join(InstallmentPlan.property).all())
    rows.sort(key=lambda p: ((p.property.object_number or ""), (p.customer.name or "").lower()))
    return rows


def _period_stats(plans_):
    seqs = sorted({e.seq for p in plans_ for e in p.entries})
    by_seq = {}
    for seq in seqs:
        stat = {"seq": seq, "due": None, "planned": 0, "issued": 0, "paid": 0,
                "skipped": 0, "amount": ZERO, "open": ZERO}
        for p in plans_:
            for e in p.entries:
                if e.seq != seq:
                    continue
                stat["due"] = stat["due"] or e.due_date
                inv = e.active_invoice
                if inv is not None:
                    stat["issued"] += 1
                    stat["amount"] += Decimal(str(inv.total_amount or 0))
                    if inv.status == Invoice.STATUS_PAID:
                        stat["paid"] += 1
                    elif inv.status != Invoice.STATUS_DRAFT:
                        stat["open"] += Decimal(str(inv.open_balance or 0))
                elif e.skipped:
                    stat["skipped"] += 1
                else:
                    stat["planned"] += 1
                    stat["amount"] += Decimal(str(e.amount or 0))
        by_seq[seq] = stat
    return [by_seq[s] for s in seqs]


@bp.route("/")
@login_required
def index():
    period_id = request.args.get("period_id", type=int)
    period = db.session.get(BillingPeriod, period_id) if period_id else None
    if period is None:
        current = BillingPeriod.current()
        # Hat die aktive Periode noch keinen Plan, aber die naechste schon:
        # dort hinein (typisch im Jaenner nach der Jahresabrechnung).
        period = current
        if current is not None:
            nxt = plans.next_period(current)
            if (nxt is not None and not InstallmentPlan.query.filter_by(
                    billing_period_id=current.id).first()
                    and InstallmentPlan.query.filter_by(billing_period_id=nxt.id).first()):
                period = nxt
    if period is None:
        flash("Es ist noch keine Abrechnungsperiode angelegt.", "warning")
        return redirect(url_for("periods.index"))
    return redirect(url_for("installments.period", period_id=period.id))


@bp.route("/period/<int:period_id>")
@login_required
def period(period_id):
    period = db.get_or_404(BillingPeriod, period_id)
    rows = _plan_rows(period)
    tariff, tariffs = _default_tariff(period)
    try:
        due_dates = dates_for_period(parse_due_dates(terms.due_dates_text()),
                                     period.start_date, period.end_date)
    except ScheduleError:
        due_dates = []
    open_entries = invoicing.open_entries(period)
    open_seqs = sorted({e.seq for e in open_entries})
    source_run = None
    source_run_id = request.args.get("source_run_id", type=int)
    if source_run_id:
        source_run = db.session.get(BillingRun, source_run_id)
    return render_template(
        "installments/period.html",
        period=period, periods=_periods(), rows=rows, stats=_period_stats(rows),
        tariff=tariff, tariffs=tariffs, due_dates=due_dates,
        open_entries=open_entries, open_seqs=open_seqs,
        open_by_seq={s: [e for e in open_entries if e.seq == s] for s in open_seqs},
        enabled=terms.is_enabled(), rounding=terms.rounding_step(),
        min_annual=terms.min_annual_amount(), source_run=source_run,
        excluded_labels=terms.excluded_charge_labels(),
        projects=Project.query.filter_by(closed=False).order_by(Project.name).all(),
        sort_order_choices=BillingRun.SORT_ORDER_CHOICES,
        word=terms.installment_word(), words=terms.installment_words(),
        title=terms.installment_title(),
        billed=plans.billed_property_ids(period.id),
        previous=plans.previous_periods(period, limit=1),
    )


@bp.route("/period/<int:period_id>/calculate", methods=["GET", "POST"])
@login_required
def calculate(period_id):
    """GET: Vorschau (schreibt nichts). POST: Plaene speichern."""
    period = db.get_or_404(BillingPeriod, period_id)
    if not terms.is_enabled():
        flash("Teilrechnungen sind in den Einstellungen ausgeschaltet.", "warning")
        return redirect(url_for("installments.period", period_id=period.id))
    args = request.form if request.method == "POST" else request.args
    tariff = db.session.get(WaterTariff, args.get("tariff_id", type=int) or 0)
    try:
        proposals = plans.compute_plans(period, tariff)
    except (plans.PlanError, ScheduleError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("installments.period", period_id=period.id))
    if request.method == "POST":
        created, updated = plans.save_plans(period, tariff, proposals,
                                            created_by_id=current_user.id)
        db.session.commit()
        flash(f"Teilbetragspläne gespeichert: {created} neu, {updated} aktualisiert.", "success")
        return redirect(url_for("installments.period", period_id=period.id,
                                source_run_id=args.get("source_run_id", type=int)))
    return render_template(
        "installments/calculate.html", period=period, tariff=tariff, proposals=proposals,
        summary=plans.summarize(proposals), word=terms.installment_word(),
        words=terms.installment_words(),
        source_run_id=args.get("source_run_id", type=int),
        labels=plans.STATUS_LABELS,
    )


def _plan_saved(plan):
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps({
        "closeInstallmentPlanModal": True,
        "installmentPlanSaved": {"id": plan.id},
    })
    return resp


@bp.route("/plan/<int:plan_id>/edit", methods=["GET", "POST"])
@login_required
def plan_edit(plan_id):
    plan = db.get_or_404(InstallmentPlan, plan_id)
    error = None
    if request.method == "POST":
        try:
            amount = _parse_money(request.form.get("installment_amount"))
            skipped = {int(x) for x in request.form.getlist("skip_seq") if x.isdigit()}
            plans.update_plan(plan, installment_amount=amount, skipped_seqs=skipped,
                              notes=request.form.get("notes", ""))
            if request.form.get("reset_manual") == "1":
                plan.manual = False
            db.session.commit()
            return _plan_saved(plan)
        except plans.PlanError as exc:
            db.session.rollback()
            error = str(exc)
    return render_template("installments/_plan_form_body.html", plan=plan, error=error,
                           word=terms.installment_word())


@bp.route("/plan/<int:plan_id>/row")
@login_required
def plan_row(plan_id):
    plan = db.get_or_404(InstallmentPlan, plan_id)
    return render_template("installments/_plan_row.html", plan=plan,
                           billed=plans.billed_property_ids(plan.billing_period_id))


@bp.route("/plan/<int:plan_id>/delete", methods=["POST"])
@login_required
def plan_delete(plan_id):
    plan = db.get_or_404(InstallmentPlan, plan_id)
    period_id = plan.billing_period_id
    try:
        plans.delete_plan(plan)
        db.session.commit()
        flash("Teilbetragsplan gelöscht.", "success")
    except plans.PlanError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("installments.period", period_id=period_id))


@bp.route("/period/<int:period_id>/invoices", methods=["POST"])
@login_required
def create_invoices(period_id):
    """Teilrechnungen fuer die gewaehlten Faelligkeiten erstellen (Entwuerfe)."""
    period = db.get_or_404(BillingPeriod, period_id)
    seqs = {int(x) for x in request.form.getlist("seq") if x.isdigit()}
    if not seqs:
        flash("Bitte mindestens eine Fälligkeit auswählen.", "warning")
        return redirect(url_for("installments.period", period_id=period.id))
    entries = invoicing.open_entries(period, seqs=seqs)
    source_run = db.session.get(BillingRun, request.form.get("source_run_id", type=int) or 0)
    sort_order = request.form.get("sort_order", "customer_name")
    if sort_order not in {k for k, _ in BillingRun.SORT_ORDER_CHOICES}:
        sort_order = "customer_name"
    try:
        result = invoicing.create_installment_invoices(
            period, entries, created_by_id=current_user.id,
            project_id=request.form.get("project_id", type=int) or None,
            sort_order=sort_order, source_run=source_run)
    except invoicing.InstallmentError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("installments.period", period_id=period.id))
    if result.run is None:
        db.session.rollback()
        reasons = sorted({r for _e, r in result.skipped})
        flash("Keine Teilrechnung erstellt" + (f" ({'; '.join(reasons)})" if reasons else "")
              + ".", "warning")
        return redirect(url_for("installments.period", period_id=period.id))
    db.session.commit()
    title = terms.installment_title()
    flash(f"{len(result.created)} {title}en als Entwurf erstellt"
          + (f", {len(result.skipped)} übersprungen" if result.skipped else "") + ".", "success")
    target = source_run or result.run
    return redirect(url_for("invoices.billing_run_detail", run_id=target.id))
