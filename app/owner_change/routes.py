"""Eigentuemerwechsel-Wizard (Session-State, 5 Schritte).

Schritt 1 Stichtag + neue Eigentuemer -> 2 Zaehlerstaende am Stichtag ->
3 Mitgliedschaft (nur WG-Modus) -> 4 Schlussrechnung (optional, ``rechnungen_op``)
-> 5 Bestaetigung. Der Zustand liegt JSON-serialisierbar unter einem einzigen
Session-Key; bei abgelaufener/fremder Session leitet ``_require_state`` zurueck
auf Schritt 1.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    render_template, redirect, url_for, flash, request, session, abort,
)
from flask_login import login_required, current_user

from app.owner_change import bp
from app.owner_change import services as svc
from app.auth.permissions import permission_required, PERM_RECHNUNGEN
from app.extensions import db
from app.models import (
    BillingPeriod, Customer, Property, PropertyOwnership, WaterTariff,
)
from app.settings_service import is_wassergenossenschaft
from app import wg

_KEY = "owner_change_wizard"


# ---------------------------------------------------------------------------
# Parsing / Session-Helfer
# ---------------------------------------------------------------------------

def _parse_decimal(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _parse_date(raw):
    try:
        return date.fromisoformat(str(raw).strip())
    except (ValueError, AttributeError):
        return None


def _can_bill():
    return current_user.has_permission(PERM_RECHNUNGEN)


def _abort_to_start(property_id,
                    reason="Sitzung abgelaufen — bitte den Wechsel erneut starten.",
                    category="warning"):
    session.pop(_KEY, None)
    flash(reason, category)
    return redirect(url_for("owner_change.start", property_id=property_id))


def _require_state(property_id):
    """Liefert den Wizard-Zustand oder ``None`` (Aufrufer ruft dann
    ``_abort_to_start``)."""
    state = session.get(_KEY)
    if not state or state.get("property_id") != property_id:
        return None
    return state


def _active_ownerships(prop):
    return (PropertyOwnership.query
            .filter_by(property_id=prop.id, valid_to=None)
            .all())


def _period_from_state(state):
    return db.session.get(BillingPeriod, state["period_id"])


def _meter_inputs(state):
    out = {}
    for mid, d in (state.get("meters") or {}).items():
        val = _parse_decimal(d.get("value"))
        if val is None:
            continue
        out[int(mid)] = {"value": val, "is_estimated": bool(d.get("is_estimated"))}
    return out


# ---------------------------------------------------------------------------
# Schritt 1: Stichtag + neue Eigentuemer
# ---------------------------------------------------------------------------

@bp.route("/<int:property_id>/start", methods=["GET", "POST"])
@login_required
def start(property_id):
    prop = db.get_or_404(Property, property_id)
    active = _active_ownerships(prop)
    if not active:
        flash("Für dieses Objekt ist kein aktueller Eigentümer hinterlegt — "
              "bitte zuerst einen Besitzer zuweisen.", "warning")
        return redirect(url_for("properties.detail", property_id=prop.id))

    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    periods = BillingPeriod.query.order_by(BillingPeriod.start_date.desc()).all()
    state = session.get(_KEY) if session.get(_KEY, {}).get("property_id") == property_id else None

    if request.method == "POST":
        stichtag = _parse_date(request.form.get("stichtag"))
        if stichtag is None:
            flash("Bitte einen gültigen Stichtag angeben.", "danger")
            return _render_start(prop, active, customers, periods, request.form)

        # Periode: explizit gewaehlt (Bestaetigung) oder aus dem Stichtag abgeleitet.
        period_id = request.form.get("period_id", type=int)
        period = (db.session.get(BillingPeriod, period_id) if period_id
                  else svc.period_for_date(stichtag) or BillingPeriod.current())
        if period is None:
            flash("Keine Abrechnungsperiode gefunden — bitte zuerst eine Periode anlegen.", "danger")
            return _render_start(prop, active, customers, periods, request.form)
        if not (period.start_date <= stichtag <= period.end_date):
            flash(f"Der Stichtag liegt außerhalb der Periode {period.name}. "
                  f"Bitte Stichtag oder Periode anpassen.", "danger")
            return _render_start(prop, active, customers, periods, request.form)

        new_ids = [int(x) for x in request.form.getlist("new_customer_ids") if x]
        if not new_ids:
            flash("Bitte mindestens einen neuen Eigentümer wählen.", "danger")
            return _render_start(prop, active, customers, periods, request.form)

        create_settlement = bool(request.form.get("create_settlement")) and _can_bill()
        recipient_id = request.form.get("settlement_recipient_id", type=int)
        if recipient_id is None and len(active) == 1:
            recipient_id = active[0].customer_id
        if create_settlement and recipient_id not in {o.customer_id for o in active}:
            flash("Bitte einen gültigen Schlussrechnungs-Empfänger wählen.", "danger")
            return _render_start(prop, active, customers, periods, request.form)

        session[_KEY] = {
            "property_id": prop.id,
            "stichtag": stichtag.isoformat(),
            "period_id": period.id,
            "new_customer_ids": new_ids,
            "create_settlement": create_settlement,
            "settlement_recipient_id": recipient_id,
            "note": (request.form.get("note") or "").strip(),
            "meters": {},
            "wg": {},
            "tariff_id": None,
            "due_days": 30,
            "fee_mode": svc.default_base_fee_mode(),
        }
        return redirect(url_for("owner_change.meters", property_id=prop.id))

    return _render_start(prop, active, customers, periods,
                         _start_defaults(state))


def _start_defaults(state):
    if not state:
        return {"stichtag": date.today().isoformat()}
    return {
        "stichtag": state.get("stichtag", date.today().isoformat()),
        "period_id": state.get("period_id"),
        "new_customer_ids": state.get("new_customer_ids", []),
        "create_settlement": state.get("create_settlement"),
        "settlement_recipient_id": state.get("settlement_recipient_id"),
        "note": state.get("note", ""),
    }


def _render_start(prop, active, customers, periods, form):
    # ``form`` ist entweder request.form (MultiDict, POST-Re-Render) oder ein
    # Defaults-Dict (GET). Auf ein einheitliches ``sel`` normalisieren.
    if hasattr(form, "getlist"):
        raw_new = form.getlist("new_customer_ids")
    else:
        raw_new = form.get("new_customer_ids", []) or []
    sel = {
        "stichtag": form.get("stichtag") or date.today().isoformat(),
        "period_id": (int(form.get("period_id")) if form.get("period_id") else None),
        "new_customer_ids": [int(x) for x in raw_new if x],
        "create_settlement": bool(form.get("create_settlement")),
        "settlement_recipient_id": (int(form.get("settlement_recipient_id"))
                                    if form.get("settlement_recipient_id") else None),
        "note": form.get("note", "") or "",
    }
    suggested = svc.period_for_date(_parse_date(sel["stichtag"])) if sel["stichtag"] else None
    return render_template(
        "owner_change/step_start.html",
        step=1, property=prop, active_ownerships=active,
        customers=customers, periods=periods, sel=sel,
        suggested_period=suggested, can_bill=_can_bill(),
        is_wg=is_wassergenossenschaft(),
    )


# ---------------------------------------------------------------------------
# Schritt 2: Zaehlerstaende am Stichtag
# ---------------------------------------------------------------------------

@bp.route("/<int:property_id>/meters", methods=["GET", "POST"])
@login_required
def meters(property_id):
    prop = db.get_or_404(Property, property_id)
    state = _require_state(property_id)
    if state is None:
        return _abort_to_start(property_id)
    period = _period_from_state(state)
    stichtag = _parse_date(state["stichtag"])
    if period is None or stichtag is None:
        return _abort_to_start(property_id)

    groups = svc.wizard_meters(prop, period, stichtag)

    if request.method == "POST":
        meters_state = {}
        for m in groups["editable"]:
            val = _parse_decimal(request.form.get(f"value_{m.id}"))
            if val is None:
                flash(f"Bitte für Zähler {m.meter_number} einen Stand angeben.", "danger")
                return _render_meters(prop, period, stichtag, groups, state)
            meters_state[str(m.id)] = {
                "value": str(val),
                "is_estimated": bool(request.form.get(f"estimated_{m.id}")),
            }
        state["meters"] = meters_state
        session[_KEY] = state
        return redirect(_next_after_meters(prop, state))

    return _render_meters(prop, period, stichtag, groups, state)


def _render_meters(prop, period, stichtag, groups, state):
    prefills = {m.id: svc.prefill_value(m, period) for m in groups["editable"]}
    saved = state.get("meters", {})
    return render_template(
        "owner_change/step_meters.html",
        step=2, property=prop, period=period, stichtag=stichtag,
        editable=groups["editable"], fixed=groups["fixed"],
        prefills=prefills, saved=saved, state=state,
        is_wg=is_wassergenossenschaft(),
    )


def _next_after_meters(prop, state):
    if is_wassergenossenschaft():
        return url_for("owner_change.member", property_id=prop.id)
    if state.get("create_settlement"):
        return url_for("owner_change.settlement", property_id=prop.id)
    return url_for("owner_change.confirm", property_id=prop.id)


# ---------------------------------------------------------------------------
# Schritt 3: Mitgliedschaft (nur WG-Modus)
# ---------------------------------------------------------------------------

@bp.route("/<int:property_id>/member", methods=["GET", "POST"])
@login_required
def member(property_id):
    prop = db.get_or_404(Property, property_id)
    state = _require_state(property_id)
    if state is None:
        return _abort_to_start(property_id)
    if not is_wassergenossenschaft():
        # Kein WG-Modus -> Schritt ueberspringen.
        return redirect(_next_after_member(prop, state))

    stichtag = _parse_date(state["stichtag"])
    active = _active_ownerships(prop)
    old_customers = [o.customer for o in active]
    new_customers = [db.session.get(Customer, cid) for cid in state["new_customer_ids"]]
    new_customers = [c for c in new_customers if c is not None]

    if request.method == "POST":
        resign_ids = [int(x) for x in request.form.getlist("resign_ids") if x]
        new_updates = {}
        for c in new_customers:
            status = request.form.get(f"new_status_{c.id}")
            ms = request.form.get(f"new_member_since_{c.id}")
            new_updates[str(c.id)] = {
                "status": status if status in wg.STATUS_LABELS else None,
                "member_since": ms.strip() if ms else None,
            }
        state["wg"] = {"resign_ids": resign_ids, "new": new_updates}
        session[_KEY] = state
        return redirect(_next_after_member(prop, state))

    # Vorschlag: Altbesitzer als "ausgeschieden", wenn er kein weiteres aktives
    # Besitzverhaeltnis (auf anderen Objekten) behaelt.
    resign_suggest = {}
    for c in old_customers:
        others = (PropertyOwnership.query
                  .filter(PropertyOwnership.customer_id == c.id,
                          PropertyOwnership.valid_to.is_(None),
                          PropertyOwnership.property_id != prop.id)
                  .count())
        resign_suggest[c.id] = (others == 0)

    return render_template(
        "owner_change/step_member.html",
        step=3, property=prop, stichtag=stichtag,
        old_customers=old_customers, new_customers=new_customers,
        resign_suggest=resign_suggest, state=state,
        status_labels=wg.STATUS_LABELS, wg=wg,
        create_settlement=state.get("create_settlement"),
    )


def _next_after_member(prop, state):
    if state.get("create_settlement"):
        return url_for("owner_change.settlement", property_id=prop.id)
    return url_for("owner_change.confirm", property_id=prop.id)


# ---------------------------------------------------------------------------
# Schritt 4: Schlussrechnung (optional, rechnungen_op)
# ---------------------------------------------------------------------------

@bp.route("/<int:property_id>/settlement", methods=["GET", "POST"])
@login_required
@permission_required(PERM_RECHNUNGEN)
def settlement(property_id):
    prop = db.get_or_404(Property, property_id)
    state = _require_state(property_id)
    if state is None:
        return _abort_to_start(property_id)
    if not state.get("create_settlement"):
        return redirect(url_for("owner_change.confirm", property_id=prop.id))

    period = _period_from_state(state)
    stichtag = _parse_date(state["stichtag"])
    recipient = db.session.get(Customer, state.get("settlement_recipient_id"))
    if period is None or stichtag is None or recipient is None:
        return _abort_to_start(property_id)

    tariffs = WaterTariff.query.order_by(WaterTariff.valid_from.desc()).all()

    if request.method == "POST":
        tariff_id = request.form.get("tariff_id", type=int)
        tariff = db.session.get(WaterTariff, tariff_id) if tariff_id else None
        due_days = request.form.get("due_days", type=int) or 30
        fee_mode = request.form.get("fee_mode")
        if fee_mode not in svc.FEE_MODES:
            fee_mode = svc.default_base_fee_mode()
        # Eingaben immer zwischenspeichern (auch fuer die Vorschau).
        state["tariff_id"] = tariff.id if tariff else None
        state["due_days"] = due_days
        state["fee_mode"] = fee_mode
        session[_KEY] = state

        action = request.form.get("action", "continue")
        if action == "continue":
            if tariff is None:
                flash("Bitte einen Tarif für die Schlussrechnung wählen.", "danger")
                return _render_settlement(prop, period, stichtag, recipient, tariffs, state)
            if request.form.get("save_default"):
                from app.models import AppSetting
                AppSetting.set(svc.SETTING_BASE_FEE_MODE, fee_mode)
                db.session.commit()
            return redirect(url_for("owner_change.confirm", property_id=prop.id))
        # action == "preview" -> auf der Seite bleiben, Vorschau anzeigen.
        return _render_settlement(prop, period, stichtag, recipient, tariffs, state)

    return _render_settlement(prop, period, stichtag, recipient, tariffs, state)


def _render_settlement(prop, period, stichtag, recipient, tariffs, state):
    tariff = db.session.get(WaterTariff, state.get("tariff_id")) if state.get("tariff_id") else None
    preview = None
    if tariff is not None:
        preview = svc.build_settlement_preview(
            prop=prop, period=period, stichtag=stichtag, tariff=tariff,
            fee_mode=state.get("fee_mode", svc.default_base_fee_mode()),
            recipient=recipient, meter_inputs=_meter_inputs(state))
    return render_template(
        "owner_change/step_settlement.html",
        step=4, property=prop, period=period, stichtag=stichtag,
        recipient=recipient, tariffs=tariffs, state=state,
        preview=preview, fee_modes=svc.FEE_MODES,
        fee_mode_labels=svc.FEE_MODE_LABELS,
        selected_fee_mode=state.get("fee_mode", svc.default_base_fee_mode()),
        selected_tariff_id=state.get("tariff_id"),
        is_wg=is_wassergenossenschaft(),
    )


# ---------------------------------------------------------------------------
# Schritt 5: Bestaetigung + Ausfuehrung
# ---------------------------------------------------------------------------

@bp.route("/<int:property_id>/confirm", methods=["GET", "POST"])
@login_required
def confirm(property_id):
    prop = db.get_or_404(Property, property_id)
    state = _require_state(property_id)
    if state is None:
        return _abort_to_start(property_id)
    period = _period_from_state(state)
    stichtag = _parse_date(state["stichtag"])
    if period is None or stichtag is None:
        return _abort_to_start(property_id)

    active = _active_ownerships(prop)
    old_customers = [o.customer for o in active]
    new_customers = [db.session.get(Customer, cid) for cid in state["new_customer_ids"]]
    new_customers = [c for c in new_customers if c is not None]
    recipient = db.session.get(Customer, state.get("settlement_recipient_id"))
    tariff = db.session.get(WaterTariff, state.get("tariff_id")) if state.get("tariff_id") else None

    create_settlement = bool(state.get("create_settlement")) and _can_bill()
    preview = None
    if create_settlement and tariff is not None and recipient is not None:
        preview = svc.build_settlement_preview(
            prop=prop, period=period, stichtag=stichtag, tariff=tariff,
            fee_mode=state.get("fee_mode", svc.default_base_fee_mode()),
            recipient=recipient, meter_inputs=_meter_inputs(state))

    if request.method == "POST":
        if create_settlement and (tariff is None or recipient is None):
            flash("Für die Schlussrechnung fehlen Tarif oder Empfänger.", "danger")
            return redirect(url_for("owner_change.settlement", property_id=prop.id))
        wg_state = state.get("wg") or {}
        new_updates = {}
        for cid_str, upd in (wg_state.get("new") or {}).items():
            new_updates[int(cid_str)] = {
                "status": upd.get("status"),
                "member_since": _parse_date(upd.get("member_since")),
            }
        try:
            oc, warnings = svc.execute_owner_change(
                prop=prop, period=period, stichtag=stichtag,
                new_customer_ids=state["new_customer_ids"],
                meter_inputs=_meter_inputs(state),
                create_settlement=create_settlement,
                settlement_recipient_id=state.get("settlement_recipient_id"),
                tariff=tariff, due_days=state.get("due_days", 30),
                fee_mode=state.get("fee_mode", svc.default_base_fee_mode()),
                resign_customer_ids=wg_state.get("resign_ids") or [],
                new_member_updates=new_updates,
                note=state.get("note"),
                created_by_id=current_user.id,
            )
        except svc.OwnerChangeError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("owner_change.confirm", property_id=prop.id))

        session.pop(_KEY, None)
        flash("Eigentümerwechsel durchgeführt.", "success")
        for w in warnings:
            flash(w, "warning")
        return redirect(url_for("owner_change.result", property_id=prop.id, change_id=oc.id))

    return render_template(
        "owner_change/step_confirm.html",
        step=5, property=prop, period=period, stichtag=stichtag,
        old_customers=old_customers, new_customers=new_customers,
        recipient=recipient, tariff=tariff, state=state,
        create_settlement=create_settlement, preview=preview,
        fee_mode_labels=svc.FEE_MODE_LABELS, is_wg=is_wassergenossenschaft(),
        meter_inputs=_meter_inputs(state),
    )


@bp.route("/<int:property_id>/result/<int:change_id>")
@login_required
def result(property_id, change_id):
    prop = db.get_or_404(Property, property_id)
    from app.models import OwnerChange
    oc = db.session.get(OwnerChange, change_id)
    if oc is None or oc.property_id != property_id:
        abort(404)
    new_owners = [o.customer for o in _active_ownerships(prop)]
    return render_template(
        "owner_change/result.html",
        property=prop, change=oc, new_owners=new_owners,
        settlement=oc.settlement_invoice,
    )
