from datetime import date, datetime

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import or_

from app.dunning import bp
from app.extensions import db
from app.models import (
    Customer, DunningNotice, DunningPolicy, DunningStage, Invoice,
)
from app.dunning.services import (
    cancel_dunnings_for_invoice, compute_fee, create_dunning_notice,
    current_dunning_level, defer_dunning_notice, dunning_summary,
    eligible_invoices_for_stage, reset_dunning_notice,
)


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    """Mahnwesen-Dashboard: Zusammenfassung und fällige Mahnungen."""
    policy = DunningPolicy.query.filter_by(is_default=True, active=True).first()
    eligible = eligible_invoices_for_stage(policy) if policy else []

    # Aktive Mahnungen nach Status
    active_count = DunningNotice.query.filter_by(status=DunningNotice.STATUS_AKTIV).count()
    reset_count = DunningNotice.query.filter_by(status=DunningNotice.STATUS_ZURUECKGESETZT).count()
    total_count = DunningNotice.query.count()

    return render_template(
        "dunning/index.html",
        eligible=eligible,
        active_count=active_count,
        reset_count=reset_count,
        total_count=total_count,
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Mahnlauf
# ---------------------------------------------------------------------------

@bp.route("/run")
@login_required
def run():
    """Mahnlauf: zeigt fällige Vorschläge, Operator wählt aus."""
    policy = DunningPolicy.query.filter_by(is_default=True, active=True).first()
    if not policy:
        flash("Keine aktive Standard-Mahnvorlage konfiguriert.", "danger")
        return redirect(url_for("dunning.index"))

    eligible = eligible_invoices_for_stage(policy)
    return render_template("dunning/run.html", eligible=eligible, policy=policy)


@bp.route("/run/execute", methods=["POST"])
@login_required
def run_execute():
    """Ausgewählte Mahnungen erzeugen (Bulk)."""
    invoice_ids = request.form.getlist("invoice_ids")
    if not invoice_ids:
        flash("Keine Rechnungen ausgewählt.", "danger")
        return redirect(url_for("dunning.run"))

    policy = DunningPolicy.query.filter_by(is_default=True, active=True).first()
    if not policy:
        flash("Keine aktive Standard-Mahnvorlage konfiguriert.", "danger")
        return redirect(url_for("dunning.run"))

    eligible = eligible_invoices_for_stage(policy)
    eligible_map = {str(inv.id): stage for inv, stage in eligible}

    created = 0
    for inv_id in invoice_ids:
        stage = eligible_map.get(inv_id)
        if not stage:
            continue
        invoice = db.session.get(Invoice, int(inv_id))
        if not invoice:
            continue
        create_dunning_notice(invoice, stage, current_user.id)
        created += 1

    db.session.commit()
    flash(f"{created} Mahnung(en) erzeugt.", "success")
    return redirect(url_for("dunning.notices"))


# ---------------------------------------------------------------------------
# Notices (Liste)
# ---------------------------------------------------------------------------

@bp.route("/notices")
@login_required
def notices():
    """Alle Mahnungen auflisten mit Filtern."""
    status_filter = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    query = (
        DunningNotice.query
        .join(Invoice, DunningNotice.invoice_id == Invoice.id)
        .join(Customer, Invoice.customer_id == Customer.id)
        .order_by(DunningNotice.issued_date.desc(), DunningNotice.id.desc())
    )

    if status_filter:
        query = query.filter(DunningNotice.status == status_filter)
    if q:
        query = query.filter(or_(
            Customer.name.ilike(f"%{q}%"),
            Invoice.invoice_number.ilike(f"%{q}%"),
        ))

    notices_list = query.all()

    if request.headers.get("HX-Request"):
        return render_template("dunning/_notices_table.html", notices=notices_list)

    return render_template(
        "dunning/notices.html",
        notices=notices_list,
        statuses=DunningNotice.ALL_STATUSES,
        status_filter=status_filter,
        q=q,
    )


# ---------------------------------------------------------------------------
# Notice Detail
# ---------------------------------------------------------------------------

@bp.route("/notices/<int:notice_id>")
@login_required
def notice_detail(notice_id):
    """Detailseite einer Mahnung."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    summary = dunning_summary(notice.invoice)
    return render_template("dunning/notice_detail.html", notice=notice, summary=summary)


@bp.route("/notices/<int:notice_id>/reset", methods=["POST"])
@login_required
def notice_reset(notice_id):
    """Mahnung zurücksetzen (eine Stufe)."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    if notice.status != DunningNotice.STATUS_AKTIV:
        flash("Nur aktive Mahnungen können zurückgesetzt werden.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    reason = request.form.get("reason", "").strip()
    reset_dunning_notice(notice, current_user, reason)
    db.session.commit()
    flash(f"Mahnung Stufe {notice.level_snapshot} zurückgesetzt.", "success")
    return redirect(url_for("dunning.notice_detail", notice_id=notice_id))


@bp.route("/notices/<int:notice_id>/defer", methods=["POST"])
@login_required
def notice_defer(notice_id):
    """Nachfrist einer Mahnung verlängern."""
    notice = db.session.get(DunningNotice, notice_id)
    if not notice:
        flash("Mahnung nicht gefunden.", "danger")
        return redirect(url_for("dunning.notices"))

    if notice.status != DunningNotice.STATUS_AKTIV:
        flash("Nur aktive Mahnungen können verlängert werden.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    new_due = request.form.get("new_due_date", "").strip()
    if not new_due:
        flash("Bitte ein neues Fälligkeitsdatum angeben.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    try:
        new_due_date = date.fromisoformat(new_due)
    except ValueError:
        flash("Ungültiges Datum.", "danger")
        return redirect(url_for("dunning.notice_detail", notice_id=notice_id))

    defer_dunning_notice(notice, new_due_date, current_user)
    db.session.commit()
    flash(f"Nachfrist auf {new_due_date.strftime('%d.%m.%Y')} verlängert.", "success")
    return redirect(url_for("dunning.notice_detail", notice_id=notice_id))


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------

@bp.route("/policies")
@login_required
def policies():
    """Alle Mahnvorlagen auflisten."""
    all_policies = DunningPolicy.query.order_by(DunningPolicy.name).all()
    return render_template("dunning/policies.html", policies=all_policies)


@bp.route("/policies/neu", methods=["GET", "POST"])
@login_required
def policy_new():
    """Neue Mahnvorlage anlegen."""
    if not current_user.is_admin:
        flash("Nur Administratoren können Mahnvorlagen anlegen.", "danger")
        return redirect(url_for("dunning.policies"))

    if request.method == "POST":
        return _save_policy(None)

    return render_template("dunning/policy_form.html", policy=None)


@bp.route("/policies/<int:policy_id>/bearbeiten", methods=["GET", "POST"])
@login_required
def policy_edit(policy_id):
    """Mahnvorlage bearbeiten (inkl. Stufen)."""
    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    if not current_user.is_admin:
        flash("Nur Administratoren können Mahnvorlagen bearbeiten.", "danger")
        return redirect(url_for("dunning.policies"))

    if request.method == "POST":
        return _save_policy(policy)

    return render_template("dunning/policy_form.html", policy=policy)


@bp.route("/policies/<int:policy_id>/loeschen", methods=["POST"])
@login_required
def policy_delete(policy_id):
    """Mahnvorlage löschen (nur wenn keine Notices darauf verweisen)."""
    if not current_user.is_admin:
        flash("Nur Administratoren können Mahnvorlagen löschen.", "danger")
        return redirect(url_for("dunning.policies"))

    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    stage_ids = [s.id for s in policy.stages]
    if stage_ids:
        notice_count = DunningNotice.query.filter(
            DunningNotice.stage_id.in_(stage_ids)
        ).count()
        if notice_count > 0:
            flash(f"Vorlage kann nicht gelöscht werden – {notice_count} Mahnung(en) verknüpft.", "danger")
            return redirect(url_for("dunning.policies"))

    db.session.delete(policy)
    db.session.commit()
    flash(f'Mahnvorlage "{policy.name}" gelöscht.', "success")
    return redirect(url_for("dunning.policies"))


@bp.route("/policies/<int:policy_id>/default", methods=["POST"])
@login_required
def policy_set_default(policy_id):
    """Mahnvorlage als Standard setzen."""
    if not current_user.is_admin:
        flash("Nur Administratoren können den Standard ändern.", "danger")
        return redirect(url_for("dunning.policies"))

    policy = db.session.get(DunningPolicy, policy_id)
    if not policy:
        flash("Mahnvorlage nicht gefunden.", "danger")
        return redirect(url_for("dunning.policies"))

    DunningPolicy.query.filter(DunningPolicy.id != policy_id).update({"is_default": False})
    policy.is_default = True
    db.session.commit()
    flash(f'"{policy.name}" als Standard-Mahnvorlage gesetzt.', "success")
    return redirect(url_for("dunning.policies"))


# ---------------------------------------------------------------------------
# Hilfsfunktion: Policy + Stages speichern
# ---------------------------------------------------------------------------

def _save_policy(policy):
    """Speichert eine Policy (neu oder bestehend) inkl. ihrer Stages aus dem Formular."""
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()

    if not name:
        flash("Name ist ein Pflichtfeld.", "danger")
        if policy:
            return redirect(url_for("dunning.policy_edit", policy_id=policy.id))
        return redirect(url_for("dunning.policy_new"))

    is_new = policy is None
    if is_new:
        policy = DunningPolicy(name=name, description=description)
        db.session.add(policy)
        db.session.flush()
    else:
        policy.name = name
        policy.description = description

    levels = request.form.getlist("stage_level")
    names = request.form.getlist("stage_name")
    days_list = request.form.getlist("stage_days_after_due")
    fees_fixed = request.form.getlist("stage_fee_fixed")
    fees_percent = request.form.getlist("stage_fee_percent")
    fees_min = request.form.getlist("stage_fee_min")
    fees_max = request.form.getlist("stage_fee_max")
    new_due_days_list = request.form.getlist("stage_new_due_days")
    print_titles = request.form.getlist("stage_print_title")
    colors = request.form.getlist("stage_color")
    icons = request.form.getlist("stage_icon")
    stage_ids = request.form.getlist("stage_id")

    existing = {s.id: s for s in policy.stages}
    seen_ids = set()

    from decimal import Decimal, InvalidOperation

    for i in range(len(levels)):
        s_name = names[i].strip() if i < len(names) else ""
        if not s_name:
            continue

        try:
            level = int(levels[i]) if i < len(levels) else i + 1
            days = int(days_list[i]) if i < len(days_list) else 14
            fee_fixed = Decimal(fees_fixed[i].replace(",", ".")) if i < len(fees_fixed) and fees_fixed[i].strip() else Decimal("0")
            fee_pct = Decimal(fees_percent[i].replace(",", ".")) if i < len(fees_percent) and fees_percent[i].strip() else Decimal("0")
            f_min = Decimal(fees_min[i].replace(",", ".")) if i < len(fees_min) and fees_min[i].strip() else None
            f_max = Decimal(fees_max[i].replace(",", ".")) if i < len(fees_max) and fees_max[i].strip() else None
            ndd = int(new_due_days_list[i]) if i < len(new_due_days_list) and new_due_days_list[i].strip() else 14
        except (ValueError, InvalidOperation):
            flash(f"Ungültige Eingabe in Stufe {i + 1}.", "danger")
            return redirect(url_for("dunning.policy_edit", policy_id=policy.id))

        pt = print_titles[i].strip() if i < len(print_titles) else s_name
        color = colors[i].strip() if i < len(colors) else ""
        icon = icons[i].strip() if i < len(icons) else ""

        sid_raw = stage_ids[i] if i < len(stage_ids) else ""
        sid = int(sid_raw) if sid_raw.strip() else None

        if sid and sid in existing:
            stage = existing[sid]
            seen_ids.add(sid)
        else:
            stage = DunningStage(policy_id=policy.id)
            db.session.add(stage)

        stage.level = level
        stage.name = s_name
        stage.days_after_due = days
        stage.fee_fixed = fee_fixed
        stage.fee_percent = fee_pct
        stage.fee_min = f_min
        stage.fee_max = f_max
        stage.new_due_days = ndd
        stage.print_title = pt or s_name
        stage.color = color or None
        stage.icon = icon or None

    for sid, stage in existing.items():
        if sid not in seen_ids:
            notice_count = DunningNotice.query.filter_by(stage_id=sid).count()
            if notice_count == 0:
                db.session.delete(stage)
            else:
                stage.active = False

    db.session.commit()

    if is_new:
        flash(f'Mahnvorlage "{policy.name}" angelegt.', "success")
    else:
        flash(f'Mahnvorlage "{policy.name}" gespeichert.', "success")
    return redirect(url_for("dunning.policies"))
