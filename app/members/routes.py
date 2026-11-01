"""Routen: Anteilsbuch buchen/loeschen und Mitgliederliste (Seite, CSV, PDF)."""
import csv
import io
import json
from datetime import date

from flask import (abort, flash, make_response, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required

from app import wg_shares
from app.extensions import db
from app.members import bp
from app.members import member_list
from app.models import Customer, Property, WgShareEntry
from app.settings_service import shares_on_person


def _require_person_mode():
    if not shares_on_person():
        flash("Das Anteilsbuch gibt es nur, wenn die Anteile am Mitglied geführt "
              "werden (Einstellungen → Mandant).", "warning")
        return redirect(url_for("members.index"))
    return None


def _parse_date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Anteilsbuch
# ---------------------------------------------------------------------------
@bp.route("/<int:customer_id>/shares/new", methods=["GET", "POST"])
@login_required
def share_entry_new(customer_id):
    """Bewegung buchen (Erwerb, Uebertragung, Rueckgabe, Korrektur,
    Anfangsbestand). Modal-Fragment ueber ``X-From-Modal``; Erfolg = 204 +
    HX-Trigger, Fehler = Fragment mit Meldung (bleibt im Modal)."""
    blocked = _require_person_mode()
    if blocked:
        return blocked
    customer = db.get_or_404(Customer, customer_id)
    if not customer.is_customer:
        abort(404)
    is_modal = bool(request.headers.get("X-From-Modal"))
    has_entries = customer.wg_share_entries.first() is not None
    actions = [a for a in wg_shares.ACTION_LABELS
               if a != wg_shares.ACTION_OPENING or not has_entries]
    # GET: Vorgang aus dem Menue (?action=), POST: aus dem Formular — nie
    # request.values (dort gewaenne der URL-Parameter gegen die Auswahl).
    source = request.form if request.method == "POST" else request.args
    action = source.get("action", wg_shares.ACTION_ACQUISITION)
    if action not in actions:
        action = wg_shares.ACTION_ACQUISITION

    detail_url = url_for("customers.detail", customer_id=customer.id) + "#anteilsbuch"

    def _render(error=None, form=None):
        # Gebucht wird nur im Modal der Kontaktseite (Modal-first); ohne
        # Modal-Header geht es dorthin zurueck.
        if not is_modal:
            if error:
                flash(error, "danger")
            return redirect(detail_url)
        recipients = (Customer.query
                      .filter(Customer.active.is_(True), Customer.is_customer.is_(True),
                              Customer.id != customer.id)
                      .order_by(Customer.name).all())
        # Bezug zu Liegenschaft: eigene (aktuell besessene) zuerst, dann alle.
        own_ids = {o.property_id for o in customer.ownerships if o.valid_to is None}
        properties = sorted(
            Property.query.filter(Property.active.is_(True)).all(),
            key=lambda p: (p.id not in own_ids, p.object_number or "", p.id))
        return render_template(
            "members/_share_entry_form_body.html", customer=customer, action=action,
            actions=actions, action_labels=wg_shares.ACTION_LABELS,
            holding=customer.wg_share_count(), recipients=recipients,
            properties=properties, own_property_ids=own_ids, form=form,
            error=error, today=date.today())

    if request.method == "GET":
        return _render()

    form = request.form
    on = _parse_date(form.get("entry_date"))
    note = form.get("note", "")
    ref = form.get("property_id", type=int) or None
    try:
        if on is None:
            raise wg_shares.ShareError("Bitte ein gültiges Datum angeben.")
        if action == wg_shares.ACTION_TRANSFER:
            target = db.session.get(Customer, form.get("to_customer_id", type=int) or 0)
            wg_shares.record_transfer(customer, target, form.get("quantity"), on,
                                      note=note, user_id=current_user.id, property_ref=ref)
            touched = [customer, target]
        elif action == wg_shares.ACTION_RETURN:
            wg_shares.record_return(customer, form.get("quantity"), on, note=note,
                                    user_id=current_user.id, property_ref=ref)
            touched = [customer]
        elif action == wg_shares.ACTION_CORRECTION:
            wg_shares.record_correction(customer, form.get("quantity"), on, note,
                                        user_id=current_user.id, property_ref=ref)
            touched = [customer]
        else:
            kind = (wg_shares.KIND_OPENING if action == wg_shares.ACTION_OPENING
                    else wg_shares.KIND_ACQUISITION)
            wg_shares.record_acquisition(customer, form.get("quantity"), on, kind=kind,
                                         note=note, user_id=current_user.id,
                                         property_ref=ref)
            touched = [customer]
        # Mitgliedschaft passend zum neuen Bestand (Vorschlag aus dem Formular).
        changed = []
        if form.get("adjust_membership") == "1":
            for c in touched:
                if wg_shares.apply_membership(c, on, wg_shares.holding(c.id, on)):
                    changed.append(c.name)
        db.session.commit()
    except wg_shares.ShareError as exc:
        db.session.rollback()
        return _render(error=str(exc), form=form)

    if changed:
        flash("Mitgliedschaft angepasst: " + ", ".join(changed) + ".", "info")
    if is_modal:
        resp = make_response("", 204)
        resp.headers["HX-Trigger"] = json.dumps({
            "closeShareEntryModal": True,
            "shareEntrySaved": {"customer_id": customer.id},
        })
        return resp
    flash("Anteile gebucht.", "success")
    return redirect(detail_url)


@bp.route("/shares/<int:entry_id>/delete", methods=["POST"])
@login_required
def share_entry_delete(entry_id):
    blocked = _require_person_mode()
    if blocked:
        return blocked
    entry = db.get_or_404(WgShareEntry, entry_id)
    customer_id = entry.customer_id
    try:
        affected = wg_shares.delete_entry(entry)
        db.session.commit()
    except wg_shares.ShareError as exc:
        db.session.rollback()
        flash(f"Nicht gelöscht: {exc}", "danger")
    else:
        flash("Eintrag gelöscht." if len(affected) == 1
              else "Übertragung gelöscht (beide Seiten).", "info")
    return redirect(url_for("customers.detail", customer_id=customer_id) + "#anteilsbuch")


# ---------------------------------------------------------------------------
# Mitgliederliste
# ---------------------------------------------------------------------------
def _list_args(args):
    at = _parse_date(args.get("at")) or date.today()
    scope = args.get("scope", member_list.SCOPE_MEMBERS)
    if scope not in member_list.SCOPES:
        scope = member_list.SCOPE_MEMBERS
    sort = args.get("sort", "name")
    if sort not in member_list.SORTS:
        sort = "name"
    direction = "desc" if args.get("dir") == "desc" else "asc"
    return dict(at=at, scope=scope, q=(args.get("q") or "").strip(), sort=sort,
                direction=direction)


def _build(args):
    f = _list_args(args)
    person = shares_on_person()
    rows, totals = member_list.build(f["at"], f["scope"], f["q"], f["sort"],
                                     f["direction"], shares_on_person=person)
    return f, rows, totals, person


@bp.route("/")
@login_required
def index():
    f, rows, totals, person = _build(request.args)
    ctx = dict(rows=rows, totals=totals, f=f, person_mode=person,
               is_today=f["at"] == date.today())
    if request.headers.get("HX-Request"):
        return render_template("members/_table.html", **ctx)
    return render_template("members/index.html", **ctx)


@bp.route("/export.csv")
@login_required
def export_csv():
    f, rows, totals, person = _build(request.args)
    output = io.StringIO()
    output.write("﻿")  # UTF-8-BOM fuer Excel
    writer = csv.writer(output, delimiter=";")
    for line in member_list.csv_rows(rows, totals, person):
        writer.writerow(line)
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="mitgliederliste_{f["at"].isoformat()}.csv"')
    return resp


def _print_ctx(args):
    f, rows, totals, person = _build(args)
    return dict(rows=rows, totals=totals, f=f, person_mode=person,
                today_de=date.today().strftime("%d.%m.%Y"))


@bp.route("/print")
@login_required
def print_view():
    return render_template("members/print.html", **_print_ctx(request.args))


@bp.route("/list.pdf")
@login_required
def list_pdf():
    try:
        from app.pdf_html import HTML
    except (ImportError, OSError):
        flash("PDF-Export ist nur im Docker-Container verfügbar. "
              "Die Druckansicht steht als Alternative bereit.", "warning")
        return redirect(url_for("members.print_view", **request.args))
    ctx = _print_ctx(request.args)
    html = render_template("members/pdf_list.html", **ctx)
    pdf = HTML(string=html, base_url=request.url_root).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        f'inline; filename="mitgliederliste_{ctx["f"]["at"].isoformat()}.pdf"')
    return resp
