from datetime import date, timedelta

from flask import render_template, redirect, url_for, flash, request, abort
from flask_login import login_required

from app.periods import bp
from app.extensions import db
from app.models import (
    BillingPeriod, MeterReading, BillingRun, Invoice, MeterReadingAccessCode,
)
from app.pagination import paginate_query


@bp.route("/")
@login_required
def index():
    """Liste aller Abrechnungsperioden, neueste zuerst."""
    query = BillingPeriod.query.order_by(
        BillingPeriod.start_date.desc(), BillingPeriod.id.desc()
    )
    pagination = paginate_query(query, page_key="periods")
    return render_template(
        "periods/index.html",
        periods=pagination.items,
        pagination=pagination,
    )


def _parse_form():
    """Liest und validiert das Periodenformular.

    Gibt ``(data, error)`` zurueck — ``data`` ist ein dict fuer den
    Konstruktor bzw. die Zuweisung, ``error`` eine deutsche Fehlermeldung
    oder ``None``.
    """
    name = request.form.get("name", "").strip()
    start_raw = request.form.get("start_date", "").strip()
    end_raw = request.form.get("end_date", "").strip()
    notes = request.form.get("notes", "").strip() or None
    if not name:
        return None, "Bitte einen Namen für die Periode angeben."
    if not start_raw or not end_raw:
        return None, "Start- und Enddatum sind erforderlich."
    try:
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    except ValueError:
        return None, "Ungültiges Datumsformat."
    if end_date < start_date:
        return None, "Das Enddatum darf nicht vor dem Startdatum liegen."
    return dict(name=name, start_date=start_date, end_date=end_date, notes=notes), None


def _validate_timeline(name, start_date, end_date, exclude_id=None):
    """Prueft, dass die Periode mit allen anderen eine lueckenlose,
    ueberschneidungsfreie Zeitachse bildet.

    Erlaubt sind beide gaengigen Konventionen am Periodenrand: ein
    gemeinsamer Ablesetag (Ende == naechster Start) ODER taggenaues
    Anschliessen (naechster Start == Ende + 1 Tag). Echte Ueberlappungen
    und echte Luecken werden abgelehnt. Gibt eine deutsche Fehlermeldung
    zurueck oder ``None``.
    """
    q = BillingPeriod.query
    if exclude_id is not None:
        q = q.filter(BillingPeriod.id != exclude_id)
    spans = [(o.name, o.start_date, o.end_date) for o in q.all()]
    spans.append((name, start_date, end_date))
    spans.sort(key=lambda s: (s[1], s[2]))

    for (pname, pstart, pend), (nname, nstart, _nend) in zip(spans, spans[1:]):
        if nstart < pend:
            return (
                f"Die Periode überschneidet sich mit '{pname}' "
                f"({pstart.strftime('%d.%m.%Y')}–{pend.strftime('%d.%m.%Y')}). "
                "Abrechnungsperioden dürfen sich zeitlich nicht überlappen."
            )
        if nstart > pend + timedelta(days=1):
            return (
                f"Zwischen '{pname}' (endet {pend.strftime('%d.%m.%Y')}) und "
                f"'{nname}' (beginnt {nstart.strftime('%d.%m.%Y')}) entsteht "
                "eine Lücke. Abrechnungsperioden müssen lückenlos aneinander "
                "anschließen."
            )
    return None


@bp.route("/neu", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        data, err = _parse_form()
        if err:
            flash(err, "danger")
            return render_template("periods/form.html", period=None, form=request.form)
        if BillingPeriod.query.filter_by(name=data["name"]).first():
            flash(f"Eine Periode mit dem Namen '{data['name']}' existiert bereits.", "danger")
            return render_template("periods/form.html", period=None, form=request.form)
        terr = _validate_timeline(data["name"], data["start_date"], data["end_date"])
        if terr:
            flash(terr, "danger")
            return render_template("periods/form.html", period=None, form=request.form)

        # Erste Periode ueberhaupt wird automatisch aktiv — es muss immer
        # genau eine aktive Periode geben.
        make_active = (
            request.form.get("active") == "1"
            or BillingPeriod.query.count() == 0
        )
        period = BillingPeriod(**data)
        db.session.add(period)
        db.session.flush()
        if make_active:
            period.activate()
        db.session.commit()
        flash(f"Abrechnungsperiode '{period.name}' angelegt.", "success")
        return redirect(url_for("periods.index"))
    return render_template("periods/form.html", period=None, form=None)


@bp.route("/<int:period_id>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit(period_id):
    period = db.session.get(BillingPeriod, period_id) or abort(404)
    if request.method == "POST":
        data, err = _parse_form()
        if err:
            flash(err, "danger")
            return render_template("periods/form.html", period=period, form=request.form)
        existing = BillingPeriod.query.filter_by(name=data["name"]).first()
        if existing and existing.id != period.id:
            flash(f"Eine Periode mit dem Namen '{data['name']}' existiert bereits.", "danger")
            return render_template("periods/form.html", period=period, form=request.form)
        terr = _validate_timeline(
            data["name"], data["start_date"], data["end_date"], exclude_id=period.id
        )
        if terr:
            flash(terr, "danger")
            return render_template("periods/form.html", period=period, form=request.form)
        period.name = data["name"]
        period.start_date = data["start_date"]
        period.end_date = data["end_date"]
        period.notes = data["notes"]
        db.session.commit()
        flash(f"Abrechnungsperiode '{period.name}' gespeichert.", "success")
        return redirect(url_for("periods.index"))
    return render_template("periods/form.html", period=period, form=None)


@bp.route("/<int:period_id>/aktivieren", methods=["POST"])
@login_required
def activate(period_id):
    period = db.session.get(BillingPeriod, period_id) or abort(404)
    period.activate()
    db.session.commit()
    flash(f"Abrechnungsperiode '{period.name}' ist jetzt aktiv.", "success")
    return redirect(url_for("periods.index"))


@bp.route("/<int:period_id>/loeschen", methods=["POST"])
@login_required
def delete(period_id):
    period = db.session.get(BillingPeriod, period_id) or abort(404)
    if period.active:
        flash(
            "Die aktive Periode kann nicht gelöscht werden. "
            "Bitte zuerst eine andere Periode aktiv setzen.",
            "danger",
        )
        return redirect(url_for("periods.index"))
    refs = (
        MeterReading.query.filter_by(billing_period_id=period.id).count()
        + BillingRun.query.filter_by(billing_period_id=period.id).count()
        + Invoice.query.filter_by(billing_period_id=period.id).count()
        + MeterReadingAccessCode.query.filter_by(billing_period_id=period.id).count()
    )
    if refs > 0:
        flash(
            f"Periode '{period.name}' kann nicht gelöscht werden — "
            f"es sind noch {refs} Datensätze (Ablesungen/Rechnungen) zugeordnet.",
            "danger",
        )
        return redirect(url_for("periods.index"))
    has_earlier = BillingPeriod.query.filter(
        BillingPeriod.id != period.id,
        BillingPeriod.start_date < period.start_date,
    ).count() > 0
    has_later = BillingPeriod.query.filter(
        BillingPeriod.id != period.id,
        BillingPeriod.start_date > period.start_date,
    ).count() > 0
    if has_earlier and has_later:
        flash(
            f"Periode '{period.name}' liegt zwischen anderen Perioden und kann "
            "nicht gelöscht werden — dadurch entstünde eine Lücke in der "
            "Zeitachse. Es lassen sich nur die älteste oder die neueste Periode "
            "löschen.",
            "danger",
        )
        return redirect(url_for("periods.index"))
    name = period.name
    db.session.delete(period)
    db.session.commit()
    flash(f"Abrechnungsperiode '{name}' gelöscht.", "success")
    return redirect(url_for("periods.index"))
