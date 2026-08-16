"""Routen der Plankostenrechnung.

Drei Bereiche:

* ``/cost-planning/`` — Übersicht: Verbrauchsverlauf, Ziele, Gesamtbedarf.
* ``/cost-planning/consumption`` — Jahres-Verbrauchssummen (berechnet + manuell).
* ``/cost-planning/goals/<id>`` — Ergebnisseite mit den drei Tarifpaketen.

Formulare laufen über rohes ``request.form`` (Repo-Konvention, kein WTForms);
Anlage und Bearbeitung teilen sich jeweils einen Formular-Body und laufen als
Modal (``X-From-Modal``-Header → 204 + ``HX-Trigger``), wie bei den Tarifen.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    flash, make_response, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app import consumption
from app.auth.permissions import PERM_RECHNUNGEN, permission_required
from app.cost_planning import bp, services
from app.extensions import db
from app.models import AppSetting, ConsumptionYear, FundingGoal, WaterTariff

SAMPLE_HOUSEHOLD_KEY = "cost_planning.sample_household_m3"
AVG_YEARS_KEY = "cost_planning.avg_years"


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------

def _decimal_or_none(raw):
    """Betragsfeld -> ``Decimal``; leer -> ``None``.

    Verträgt beide Schreibweisen. Der Punkt wird nur dann als Tausenderpunkt
    verworfen, wenn auch ein Komma vorkommt ("500.000,00") — sonst bliebe von
    "1250.5" plötzlich 12505 übrig. Hier geht es um sechsstellige Beträge, da
    tippt der Kassier die Tausenderpunkte durchaus mit.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(".", "")
    return Decimal(raw.replace(",", "."))


def _sample_household_m3():
    raw = AppSetting.get(SAMPLE_HOUSEHOLD_KEY)
    try:
        value = Decimal(raw) if raw else services.DEFAULT_SAMPLE_HOUSEHOLD_M3
    except (InvalidOperation, ValueError):
        value = services.DEFAULT_SAMPLE_HOUSEHOLD_M3
    return value if value > 0 else services.DEFAULT_SAMPLE_HOUSEHOLD_M3


def _avg_years():
    raw = AppSetting.get(AVG_YEARS_KEY)
    try:
        value = int(raw) if raw else 3
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 10))


def _current_tariff():
    """Der aktuell gültige Tarif — jüngstes ``valid_from``, wie überall sonst."""
    return WaterTariff.query.order_by(WaterTariff.valid_from.desc()).first()


def _tariff_from_request():
    """Optional per ``?tariff=`` gewählter Tarif, sonst der aktuelle."""
    raw = request.args.get("tariff") or request.form.get("tariff")
    if raw:
        tariff = db.session.get(WaterTariff, int(raw)) if raw.isdigit() else None
        if tariff is not None:
            return tariff
    return _current_tariff()


def _modal_saved(trigger, payload=None):
    """204 + ``HX-Trigger`` — Modal schließen und Seite nachladen."""
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps({
        f"close{trigger}Modal": True,
        f"{trigger.lower()}Saved": payload or True,
    })
    return resp


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    # Fehlende/veraltete Jahressummen hier nachziehen — das ist die Seite, auf
    # der die Zahlen gebraucht werden. Der Schreibpfad (save_reading) markiert
    # nur, gerechnet wird lazy.
    touched = consumption.ensure_fresh()
    if touched:
        db.session.commit()

    goals = services.active_goals()
    all_goals = FundingGoal.query.order_by(
        FundingGoal.status.asc(), FundingGoal.target_year.asc()).all()
    rows, peak = services.combined_requirement(goals)

    requirements = {}
    for goal in all_goals:
        try:
            requirements[goal.id] = services.annual_requirement(goal)
        except ValueError:
            requirements[goal.id] = None

    return render_template(
        "cost_planning/index.html",
        history=consumption.history(limit=10),
        goals=all_goals,
        requirements=requirements,
        combined_rows=rows,
        peak=peak,
        tariff=_current_tariff(),
        current_year=date.today().year,
    )


# ---------------------------------------------------------------------------
# Verbrauchs-Jahressummen
# ---------------------------------------------------------------------------

@bp.route("/consumption")
@login_required
def consumption_index():
    touched = consumption.ensure_fresh()
    if touched:
        db.session.commit()

    rows = ConsumptionYear.query.order_by(ConsumptionYear.year.desc()).all()
    avg, basis = consumption.average_basis(_avg_years())
    return render_template(
        "cost_planning/consumption.html",
        rows=rows,
        average=avg,
        basis_years=basis,
        avg_years=_avg_years(),
        sample_household_m3=_sample_household_m3(),
        current_year=date.today().year,
    )


@bp.route("/consumption/recalculate", methods=["POST"])
@login_required
def consumption_recalculate():
    touched = consumption.refresh_all(force=True)
    db.session.commit()
    flash(f"{len(touched)} Jahressumme(n) neu berechnet.", "success")
    return redirect(url_for("cost_planning.consumption_index"))


def _manual_body(row, form):
    return render_template(
        "cost_planning/_manual_year_form_body.html", row=row, form=form,
        current_year=date.today().year)


def _parse_manual_form():
    """``(data, error)`` für die manuelle Jahreserfassung."""
    try:
        year = int(request.form.get("year", "").strip())
    except (TypeError, ValueError):
        return None, "Bitte ein gültiges Jahr angeben."
    if not (1950 <= year <= date.today().year + 1):
        return None, "Das Jahr liegt außerhalb des zulässigen Bereichs."
    try:
        total = _decimal_or_none(request.form.get("total_m3"))
    except (InvalidOperation, ValueError):
        return None, "Ungültige Menge — bitte eine Zahl eingeben."
    if total is None:
        return None, "Bitte die Jahresmenge in m³ angeben."
    if total < 0:
        return None, "Die Jahresmenge kann nicht negativ sein."
    try:
        units = request.form.get("unit_count", "").strip()
        units = int(units) if units else None
    except (TypeError, ValueError):
        return None, "Die Anzahl der Objekte muss eine ganze Zahl sein."
    return {
        "year": year,
        "total_m3": total,
        "unit_count": units,
        "notes": request.form.get("notes", "").strip() or None,
    }, None


@bp.route("/consumption/manual", methods=["GET", "POST"])
@bp.route("/consumption/manual/<int:year>", methods=["GET", "POST"])
@login_required
def consumption_manual(year=None):
    row = consumption.for_year(year) if year is not None else None
    is_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        data, err = _parse_manual_form()
        if err:
            flash(err, "danger")
            return _manual_body(row, request.form) if is_modal else \
                redirect(url_for("cost_planning.consumption_index"))
        saved = consumption.set_manual(
            data["year"], data["total_m3"],
            notes=data["notes"], unit_count=data["unit_count"],
            created_by_id=current_user.id,
        )
        overrides = saved.is_override
        db.session.commit()
        if overrides:
            flash(f"Verbrauch {data['year']} manuell übersteuert — die "
                  f"berechnete Summe bleibt zum Vergleich sichtbar.", "warning")
        else:
            flash(f"Verbrauch {data['year']} gespeichert.", "success")
        if is_modal:
            return _modal_saved("ManualYear", {"year": data["year"]})
        return redirect(url_for("cost_planning.consumption_index"))

    if is_modal:
        return _manual_body(row, None)
    return redirect(url_for("cost_planning.consumption_index"))


@bp.route("/consumption/manual/<int:year>/delete", methods=["POST"])
@login_required
def consumption_manual_delete(year):
    """Manuelle Eingabe zuruecknehmen.

    Gibt es Ablesungen fuer das Jahr, wird wieder die berechnete Summe wirksam;
    sonst verschwindet die Zeile.
    """
    row = consumption.for_year(year)
    had_measurement = row is not None and row.measured_m3 is not None
    try:
        changed = consumption.revert_to_measured(year)
    except consumption.ConsumptionError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("cost_planning.consumption_index"))
    if changed:
        db.session.commit()
        if had_measurement:
            flash(f"Übersteuerung für {year} aufgehoben — es gilt wieder die "
                  f"berechnete Summe.", "success")
        else:
            flash(f"Verbrauchsjahr {year} gelöscht.", "success")
    return redirect(url_for("cost_planning.consumption_index"))


# ---------------------------------------------------------------------------
# Ziele
# ---------------------------------------------------------------------------

def _goal_body(goal, form):
    return render_template(
        "cost_planning/_goal_form_body.html", goal=goal, form=form,
        current_year=date.today().year, goal_model=FundingGoal)


def _parse_goal_form():
    """``(data, error)`` für das Ziel-Formular."""
    name = request.form.get("name", "").strip()
    if not name:
        return None, "Bitte eine Bezeichnung für das Ziel angeben."

    goal_type = request.form.get("goal_type", FundingGoal.TYPE_INVESTMENT)
    if goal_type not in (FundingGoal.TYPE_INVESTMENT, FundingGoal.TYPE_LOAN):
        return None, "Unbekannte Zielart."

    try:
        start_year = int(request.form.get("start_year", "").strip())
        target_year = int(request.form.get("target_year", "").strip())
    except (TypeError, ValueError):
        return None, "Start- und Zieljahr müssen Jahreszahlen sein."
    if target_year < start_year:
        return None, "Das Zieljahr darf nicht vor dem Startjahr liegen."

    try:
        target_amount = _decimal_or_none(request.form.get("target_amount"))
        reserve = _decimal_or_none(request.form.get("existing_reserve")) \
            or Decimal("0")
        rate = _decimal_or_none(request.form.get("interest_rate"))
    except (InvalidOperation, ValueError):
        return None, "Ungültiger Betrag — bitte Zahlen eingeben."

    if target_amount is None or target_amount <= 0:
        return None, "Bitte einen Zielbetrag größer als null angeben."
    if reserve < 0:
        return None, "Die vorhandene Rücklage kann nicht negativ sein."
    if rate is not None and rate < 0:
        return None, "Der Zinssatz kann nicht negativ sein."

    # Beim Kredit ist der Zielbetrag die Restschuld — eine "vorhandene
    # Ruecklage" gibt es dort nicht, das Feld ist im Formular ausgeblendet.
    if goal_type == FundingGoal.TYPE_LOAN:
        reserve = Decimal("0")

    status = request.form.get("status", FundingGoal.STATUS_DRAFT)
    if status not in dict(FundingGoal.STATUS_CHOICES):
        status = FundingGoal.STATUS_DRAFT

    return {
        "name": name,
        "goal_type": goal_type,
        "target_amount": target_amount,
        "existing_reserve": reserve,
        "interest_rate": rate,
        "start_year": start_year,
        "target_year": target_year,
        "status": status,
        "notes": request.form.get("notes", "").strip() or None,
    }, None


@bp.route("/goals/new", methods=["GET", "POST"])
@bp.route("/goals/<int:goal_id>/edit", methods=["GET", "POST"])
@login_required
def goal_form(goal_id=None):
    goal = db.get_or_404(FundingGoal, goal_id) if goal_id else None
    is_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        data, err = _parse_goal_form()
        if err:
            flash(err, "danger")
            return _goal_body(goal, request.form) if is_modal else \
                redirect(url_for("cost_planning.index"))
        if goal is None:
            goal = FundingGoal(created_by_id=current_user.id)
            db.session.add(goal)
        for field, value in data.items():
            setattr(goal, field, value)
        db.session.commit()
        flash("Finanzierungsziel gespeichert.", "success")
        if is_modal:
            return _modal_saved("Goal", {"goal_id": goal.id})
        return redirect(url_for("cost_planning.goal_detail", goal_id=goal.id))

    if is_modal:
        return _goal_body(goal, None)
    return redirect(url_for("cost_planning.index"))


@bp.route("/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def goal_delete(goal_id):
    goal = db.get_or_404(FundingGoal, goal_id)
    db.session.delete(goal)
    db.session.commit()
    flash("Finanzierungsziel gelöscht.", "success")
    return redirect(url_for("cost_planning.index"))


def _goal_context(goal):
    """Gemeinsamer Rechen-Context für Detailseite, Druckansicht und PDF."""
    tariff = _tariff_from_request()
    rounding = request.values.get("rounding", "exact")
    if rounding not in dict(services.ROUNDING_CHOICES):
        rounding = "exact"
    try:
        avg_years = int(request.values.get("avg_years", _avg_years()))
    except (TypeError, ValueError):
        avg_years = _avg_years()
    avg_years = max(1, min(avg_years, 10))

    base_info = services.baseline(tariff, avg_years=avg_years)
    error = None
    try:
        requirement = services.annual_requirement(goal)
    except ValueError as exc:
        requirement = None
        error = str(exc)

    sample = _sample_household_m3()
    scenarios = (
        services.build_scenarios(requirement["amount"], base_info,
                                 rounding=rounding, sample_household_m3=sample)
        if requirement is not None else []
    )
    projections = {
        sc.key: services.projection(goal, sc) for sc in scenarios
    }

    # Das im Detail gezeigte Paket: das beschlossene, sonst das erste.
    active_key = (
        goal.chosen_scenario if goal.chosen_scenario in projections
        else (scenarios[0].key if scenarios else None)
    )
    active_rows = projections.get(active_key, [])
    chart = {
        "labels": [r["year"] for r in active_rows],
        "balance": [float(r["balance"]) for r in active_rows],
        "target": [float(r["target"]) for r in active_rows],
        "is_loan": goal.is_loan,
    }

    return {
        "goal": goal,
        "tariff": tariff,
        "tariffs": WaterTariff.query.order_by(
            WaterTariff.valid_from.desc()).all(),
        "baseline": base_info,
        "requirement": requirement,
        "requirement_error": error,
        "scenarios": scenarios,
        "projections": projections,
        "active_key": active_key,
        "active_rows": active_rows,
        "chart": chart,
        "rounding": rounding,
        "avg_years": avg_years,
        "sample_household_m3": sample,
        "generated_at": date.today(),
    }


@bp.route("/goals/<int:goal_id>")
@login_required
def goal_detail(goal_id):
    goal = db.get_or_404(FundingGoal, goal_id)
    touched = consumption.ensure_fresh()
    if touched:
        db.session.commit()
    return render_template("cost_planning/goal_detail.html",
                           **_goal_context(goal))


@bp.route("/goals/<int:goal_id>/choose", methods=["POST"])
@login_required
def goal_choose(goal_id):
    goal = db.get_or_404(FundingGoal, goal_id)
    key = request.form.get("scenario")
    if key not in services.SCENARIO_LABELS:
        flash("Unbekanntes Tarifpaket.", "danger")
        return redirect(url_for("cost_planning.goal_detail", goal_id=goal.id))
    goal.chosen_scenario = key
    goal.status = FundingGoal.STATUS_ACTIVE
    db.session.commit()
    flash(f"Paket „{services.SCENARIO_LABELS[key]}“ als beschlossen vermerkt.",
          "success")
    return redirect(url_for("cost_planning.goal_detail", goal_id=goal.id))


@bp.route("/goals/<int:goal_id>/apply-tariff", methods=["POST"])
@permission_required(PERM_RECHNUNGEN)
def goal_apply_tariff(goal_id):
    """Aus einem Paket einen echten Tarif machen.

    Bewusst KEIN stiller Insert: der Nutzer landet im normalen Tarifformular
    mit vorbefüllten Werten und entscheidet dort selbst über Name, Gültigkeit
    und Feinschliff. Der Tarif ist ein abrechnungsrelevanter Stammsatz — den
    legt man nicht als Nebenwirkung eines Planungs-Klicks an.
    """
    goal = db.get_or_404(FundingGoal, goal_id)
    key = request.form.get("scenario") or goal.chosen_scenario
    ctx = _goal_context(goal)
    scenario = next((s for s in ctx["scenarios"] if s.key == key), None)
    if scenario is None:
        flash("Bitte zuerst ein Tarifpaket auswählen.", "warning")
        return redirect(url_for("cost_planning.goal_detail", goal_id=goal.id))
    if not scenario.feasible:
        flash("Dieses Paket ist mit den vorhandenen Daten nicht umsetzbar.",
              "danger")
        return redirect(url_for("cost_planning.goal_detail", goal_id=goal.id))

    params = {
        "name": f"Tarif ab {goal.start_year}",
        "valid_from": goal.start_year,
        "price_per_m3": scenario.price_per_m3,
    }
    if scenario.base_fee is not None:
        params["base_fee"] = scenario.base_fee
    if scenario.additional_fee is not None:
        params["additional_fee"] = scenario.additional_fee
    if ctx["tariff"] is not None:
        params["base_fee_label"] = ctx["tariff"].base_fee_label or "Grundgebühr"
        params["additional_fee_label"] = (
            ctx["tariff"].additional_fee_label or "Zusatzgebühr")
    params["notes"] = (
        f"Aus der Plankostenrechnung „{goal.name}“ "
        f"(Paket {scenario.label}, Bedarf "
        f"{ctx['requirement']['amount']} € pro Jahr)."
    )
    return redirect(url_for("invoices.tariff_new", **params))


# ---------------------------------------------------------------------------
# Beschlussvorlage (Druck / PDF)
# ---------------------------------------------------------------------------

@bp.route("/goals/<int:goal_id>/print")
@login_required
def goal_print(goal_id):
    goal = db.get_or_404(FundingGoal, goal_id)
    return render_template("cost_planning/report_print.html",
                           **_goal_context(goal))


@bp.route("/goals/<int:goal_id>/report.pdf")
@login_required
def goal_report_pdf(goal_id):
    goal = db.get_or_404(FundingGoal, goal_id)
    try:
        from weasyprint import HTML
    except (ImportError, OSError):
        # WeasyPrint importiert auch ohne GTK3 und stirbt erst beim Laden der
        # Bibliotheken (OSError) — deshalb beide Fehler abfangen.
        flash("PDF-Export ist nur im Docker-Container verfügbar. "
              "Die Druckansicht steht als Alternative bereit.", "warning")
        return redirect(url_for("cost_planning.goal_print", goal_id=goal.id,
                                **request.args))
    ctx = _goal_context(goal)
    html = render_template("cost_planning/report_pdf.html", **ctx)
    pdf = HTML(string=html, base_url=request.url_root).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        f'inline; filename="plankosten_{goal.id}.pdf"')
    return resp


# ---------------------------------------------------------------------------
# Einstellungen (Musterhaushalt / Ø-Jahre)
# ---------------------------------------------------------------------------

@bp.route("/settings", methods=["POST"])
@login_required
def settings_save():
    raw = request.form.get("sample_household_m3", "").strip().replace(",", ".")
    try:
        value = Decimal(raw) if raw else services.DEFAULT_SAMPLE_HOUSEHOLD_M3
        if value <= 0:
            raise InvalidOperation
        AppSetting.set(SAMPLE_HOUSEHOLD_KEY, str(value))
    except (InvalidOperation, ValueError):
        flash("Ungültiger Musterverbrauch — bitte eine Zahl größer null.",
              "danger")
        return redirect(url_for("cost_planning.consumption_index"))

    try:
        years = int(request.form.get("avg_years", "3"))
        AppSetting.set(AVG_YEARS_KEY, str(max(1, min(years, 10))))
    except (TypeError, ValueError):
        pass

    db.session.commit()
    flash("Einstellungen gespeichert.", "success")
    return redirect(url_for("cost_planning.consumption_index"))
