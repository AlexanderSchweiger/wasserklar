"""Seiten „Lastschrift-Einzug“: Uebersicht, neuer Einzug, Laufdetail, Downloads."""
from datetime import date, timedelta

from flask import Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app.direct_debit import bp, export_csv, pain008
from app.direct_debit import services as dd
from app.direct_debit.notice import incomplete_clause
from app.extensions import db
from app.models import (
    Account, BillingRun, Customer, DirectDebitItem, DirectDebitRun, RealAccount,
)


def _eur(value):
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _run_stats(run):
    states = {"pending": [0, 0], "paid": [0, 0], "returned": [0, 0]}
    for item in run.items:
        st = states[item.state]
        st[0] += 1
        st[1] += item.amount
    return states


def _run_status(run, stats):
    if run.is_cancelled:
        return "cancelled"
    if stats["pending"][0] == 0:
        return "done"
    return "open"


@bp.route("/")
@login_required
def index():
    runs = DirectDebitRun.query.order_by(DirectDebitRun.created_at.desc()).limit(200).all()
    rows = []
    for run in runs:
        stats = _run_stats(run)
        rows.append({"run": run, "stats": stats, "status": _run_status(run, stats)})
    debit_count = Customer.query.filter(
        Customer.payment_method == Customer.PAYMENT_DIRECT_DEBIT,
        Customer.active.isnot(False)).count()
    incomplete_count = Customer.query.filter(incomplete_clause(),
                                             Customer.active.isnot(False)).count()
    return render_template(
        "direct_debit/index.html",
        rows=rows, readiness=dd.readiness(),
        debit_count=debit_count, incomplete_count=incomplete_count,
    )


@bp.route("/new")
@login_required
def new():
    billing_run_id = request.args.get("billing_run", type=int)
    due_raw = request.args.get("due_until")
    due_until = _parse_date(due_raw) if due_raw is not None else date.today() + timedelta(days=14)
    ready, excluded = dd.candidates(due_until=due_until, billing_run_id=billing_run_id)
    billing_runs = BillingRun.query.order_by(BillingRun.created_at.desc()).limit(30).all()
    ctx = dict(
        ready=ready, excluded=excluded, readiness=dd.readiness(),
        not_listed=dd.not_listed(due_until=due_until, billing_run_id=billing_run_id),
        billing_runs=billing_runs, billing_run_id=billing_run_id,
        due_until=due_until, earliest=dd.earliest_collection_date(),
        prenotification_days=dd.PRENOTIFICATION_DAYS,
    )
    if request.headers.get("HX-Request"):
        return render_template("direct_debit/_candidates.html", **ctx)
    return render_template("direct_debit/new.html", **ctx)


@bp.route("/create", methods=["POST"])
@login_required
def create():
    ids = [i for i in request.form.getlist("open_item_id") if i.isdigit()]
    if not ids:
        flash("Bitte mindestens einen Posten auswählen.", "warning")
        return redirect(url_for("direct_debit.new"))
    try:
        run = dd.create_run(ids, user_id=current_user.id,
                            notes=(request.form.get("notes") or "").strip()[:500] or None)
        db.session.commit()
    except dd.DirectDebitError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("direct_debit.new"))
    skipped = len(ids) - run.item_count
    msg = (f"Einzugslauf angelegt: {run.item_count} Lastschrift(en). Laden Sie jetzt die "
           "Datei für die Bank herunter und lesen Sie sie im Online-Banking ein.")
    if skipped > 0:
        msg += f" {skipped} Posten konnten nicht übernommen werden."
    flash(msg, "success")
    return redirect(url_for("direct_debit.detail", run_id=run.id))


def _run_or_404(run_id):
    run = db.session.get(DirectDebitRun, run_id)
    if run is None:
        abort(404)
    return run


@bp.route("/<int:run_id>")
@login_required
def detail(run_id):
    run = _run_or_404(run_id)
    stats = _run_stats(run)
    real_accounts = (RealAccount.query.filter_by(active=True)
                     .order_by(RealAccount.name).all())
    real_accounts = [ra for ra in real_accounts if not ra.is_cash]
    default_ra = dd.default_real_account()
    needs_account = any(
        item.invoice is not None and item.state == DirectDebitItem.STATE_PENDING
        and _missing_account(item.invoice) for item in run.items)
    return render_template(
        "direct_debit/detail.html",
        run=run, stats=stats, status=_run_status(run, stats),
        cancel_blocker=dd.cancel_blocker(run),
        real_accounts=real_accounts, default_real_account=default_ra,
        accounts=Account.query.filter_by(active=True).order_by(Account.name).all(),
        needs_account=needs_account, today=date.today(),
    )


def _missing_account(invoice):
    from app.accounting.services import invoice_missing_account
    return invoice_missing_account(invoice)


@bp.route("/<int:run_id>/pain008.xml")
@login_required
def download_xml(run_id):
    run = _run_or_404(run_id)
    return Response(
        pain008.build(run), mimetype="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{pain008.filename(run)}"',
                 "X-Content-Type-Options": "nosniff"},
    )


@bp.route("/<int:run_id>/export.csv")
@login_required
def download_csv(run_id):
    run = _run_or_404(run_id)
    return Response(
        export_csv.build(run), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export_csv.filename(run)}"'},
    )


@bp.route("/<int:run_id>/cancel", methods=["POST"])
@login_required
def cancel(run_id):
    run = _run_or_404(run_id)
    try:
        dd.cancel_run(run)
        db.session.commit()
    except dd.DirectDebitError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("direct_debit.detail", run_id=run.id))
    flash("Einzugslauf verworfen — die Posten können erneut eingezogen werden.", "success")
    return redirect(url_for("direct_debit.detail", run_id=run.id))


def _real_account_from_form():
    ra_id = request.form.get("real_account_id", type=int)
    ra = db.session.get(RealAccount, ra_id) if ra_id else None
    if ra is None or not ra.active:
        return None
    return ra


@bp.route("/<int:run_id>/book", methods=["POST"])
@login_required
def book(run_id):
    run = _run_or_404(run_id)
    ra = _real_account_from_form()
    if ra is None:
        flash("Bitte ein Bankkonto wählen.", "danger")
        return redirect(url_for("direct_debit.detail", run_id=run.id))
    fallback = request.form.get("account_id", type=int) or None
    try:
        stats = dd.book_run_paid(run, ra.id, current_user.id, fallback_account_id=fallback)
        db.session.commit()
    except dd.DirectDebitError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("direct_debit.detail", run_id=run.id))
    if stats["booked"]:
        flash(f"{stats['booked']} Lastschrift(en) über {_eur(stats['amount'])} als bezahlt "
              "verbucht.", "success")
    for err in stats["errors"][:10]:
        flash(err, "warning")
    if not stats["booked"] and not stats["errors"]:
        flash("Es war nichts mehr zu verbuchen.", "info")
    return redirect(url_for("direct_debit.detail", run_id=run.id))


@bp.route("/items/<int:item_id>/return", methods=["POST"])
@login_required
def item_return(item_id):
    item = db.session.get(DirectDebitItem, item_id)
    if item is None:
        abort(404)
    ra = _real_account_from_form()
    returned_on = _parse_date(request.form.get("returned_on")) or date.today()
    if ra is None:
        flash("Bitte ein Bankkonto wählen.", "danger")
        return redirect(url_for("direct_debit.detail", run_id=item.run_id))
    try:
        amount, _ = dd.record_return(item, returned_on, ra.id, current_user.id,
                                     reason=request.form.get("reason"))
        db.session.commit()
    except dd.DirectDebitError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("direct_debit.detail", run_id=item.run_id))
    number = item.invoice.invoice_number if item.invoice else ""
    if amount > 0:
        flash(f"Rücklastschrift zu {number} erfasst, Zahlung über {_eur(amount)} "
              "zurückgebucht. Der Posten ist wieder offen.", "success")
    else:
        flash(f"Rücklastschrift zu {number} erfasst. Der Posten ist wieder offen.", "success")
    return redirect(url_for("direct_debit.detail", run_id=item.run_id))
