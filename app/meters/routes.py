import io
from datetime import date, datetime
from decimal import Decimal

from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.meters import bp
from app.extensions import db
from app.models import WaterMeter, MeterReading, Property


@bp.route("/")
@login_required
def index():
    year = request.args.get("year", date.today().year, type=int)
    q = request.args.get("q", "").strip()

    # Objekt + Zähler-Join, nach Objektnummer / Ort sortiert
    meters_query = (
        WaterMeter.query
        .join(Property)
        .filter(WaterMeter.active == True, Property.active == True)
        .order_by(Property.object_number, Property.ort)
    )
    if q:
        meters_query = meters_query.filter(
            db.or_(
                Property.object_number.ilike(f"%{q}%"),
                Property.strasse.ilike(f"%{q}%"),
                Property.ort.ilike(f"%{q}%"),
            )
        )

    meters = meters_query.all()

    # Ablesungen für gewähltes Jahr vorladen
    readings_map = {}
    for r in MeterReading.query.filter_by(year=year).all():
        readings_map[r.meter_id] = r

    # Vorjahresablesungen vorladen
    prev_readings_map = {}
    for r in MeterReading.query.filter_by(year=year - 1).all():
        prev_readings_map[r.meter_id] = r

    if request.headers.get("HX-Request"):
        return render_template(
            "meters/_table.html",
            meters=meters, readings_map=readings_map,
            prev_readings_map=prev_readings_map, year=year,
        )
    return render_template(
        "meters/index.html",
        meters=meters, readings_map=readings_map,
        prev_readings_map=prev_readings_map, year=year, q=q,
    )


@bp.route("/<int:meter_id>/read", methods=["GET", "POST"])
@login_required
def add_reading(meter_id):
    meter = db.get_or_404(WaterMeter, meter_id)
    year = request.args.get("year", date.today().year, type=int)

    existing = MeterReading.query.filter_by(meter_id=meter_id, year=year).first()

    if request.method == "POST":
        year = int(request.form.get("year", year))
        value = Decimal(request.form.get("value", "0").replace(",", "."))
        reading_date_str = request.form.get("reading_date", "")
        reading_date = (
            datetime.strptime(reading_date_str, "%Y-%m-%d").date()
            if reading_date_str else date.today()
        )

        if existing:
            existing.value = value
            existing.reading_date = reading_date
            existing.created_by_id = current_user.id
            reading = existing
        else:
            reading = MeterReading(
                meter_id=meter_id, year=year, value=value,
                reading_date=reading_date, created_by_id=current_user.id,
            )
            db.session.add(reading)

        # Verbrauch berechnen (Vorjahreswert oder Anfangsstand)
        prev = MeterReading.query.filter_by(meter_id=meter_id, year=year - 1).first()
        if prev:
            reading.consumption = value - prev.value
        elif meter.initial_value is not None:
            reading.consumption = value - meter.initial_value

        db.session.commit()
        flash(f"Ablesung für {meter.property.label()} ({year}) gespeichert.", "success")

        if request.headers.get("HX-Request"):
            prev = MeterReading.query.filter_by(meter_id=meter_id, year=year - 1).first()
            return render_template(
                "meters/_row.html", meter=meter, reading=reading, year=year,
                prev_readings_map={meter_id: prev} if prev else {},
            )
        return redirect(url_for("meters.index", year=year))

    return render_template(
        "meters/reading_form.html",
        meter=meter, year=year, existing=existing,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def meter_new():
    properties = Property.query.filter_by(active=True).order_by(
        Property.object_number, Property.ort
    ).all()
    if request.method == "POST":
        m = WaterMeter(
            property_id=int(request.form["property_id"]),
            meter_number=request.form.get("meter_number", "").strip(),
            location=request.form.get("location", "").strip(),
            notes=request.form.get("notes", "").strip(),
        )
        db.session.add(m)
        db.session.commit()
        flash("Zähler angelegt.", "success")
        return redirect(url_for("meters.index"))
    return render_template("meters/meter_form.html", meter=None, properties=properties)


@bp.route("/<int:meter_id>/edit", methods=["GET", "POST"])
@login_required
def meter_edit(meter_id):
    meter = db.get_or_404(WaterMeter, meter_id)
    properties = Property.query.filter_by(active=True).order_by(
        Property.object_number, Property.ort
    ).all()
    if request.method == "POST":
        meter.property_id = int(request.form["property_id"])
        meter.meter_number = request.form.get("meter_number", "").strip()
        meter.location = request.form.get("location", "").strip()
        meter.notes = request.form.get("notes", "").strip()
        db.session.commit()
        flash("Zähler aktualisiert.", "success")
        return redirect(url_for("meters.index"))
    return render_template("meters/meter_form.html", meter=meter, properties=properties)


# ---------------------------------------------------------------------------
# CSV / Excel Import
# ---------------------------------------------------------------------------

@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_readings():
    if request.method == "POST" and "file" in request.files:
        import pandas as pd
        f = request.files["file"]
        filename = f.filename.lower()
        try:
            if filename.endswith(".csv"):
                df = pd.read_csv(f, dtype=str)
            else:
                df = pd.read_excel(f, dtype=str)
        except Exception as e:
            flash(f"Fehler beim Lesen der Datei: {e}", "danger")
            return redirect(url_for("meters.import_readings"))

        # Spaltennamen für Mapping-Dialog zurückgeben
        if "confirm" not in request.form:
            return render_template(
                "meters/import_mapping.html",
                columns=list(df.columns),
                preview=df.head(5).to_dict(orient="records"),
                year=date.today().year,
            )

        # Mapping anwenden
        col_meter = request.form.get("col_meter")
        col_value = request.form.get("col_value")
        col_year = request.form.get("col_year")
        col_date = request.form.get("col_date", "")
        default_year = int(request.form.get("default_year", date.today().year))

        results = {"ok": 0, "skip": 0, "errors": []}
        for _, row in df.iterrows():
            meter_num = str(row.get(col_meter, "")).strip()
            val_raw = str(row.get(col_value, "")).replace(",", ".").strip()
            year = int(row[col_year]) if col_year and row.get(col_year) else default_year

            meter = WaterMeter.query.filter_by(meter_number=meter_num).first()
            if not meter:
                results["errors"].append(f"Zähler '{meter_num}' nicht gefunden")
                results["skip"] += 1
                continue
            try:
                value = Decimal(val_raw)
            except Exception:
                results["errors"].append(f"Ungültiger Wert '{val_raw}' für {meter_num}")
                results["skip"] += 1
                continue

            reading_date = date.today()
            if col_date and row.get(col_date):
                try:
                    reading_date = pd.to_datetime(row[col_date]).date()
                except Exception:
                    pass

            existing = MeterReading.query.filter_by(meter_id=meter.id, year=year).first()
            if existing:
                existing.value = value
                existing.reading_date = reading_date
            else:
                r = MeterReading(
                    meter_id=meter.id, year=year, value=value,
                    reading_date=reading_date, created_by_id=current_user.id,
                )
                db.session.add(r)

            # Verbrauch
            prev = MeterReading.query.filter_by(meter_id=meter.id, year=year - 1).first()
            if existing:
                existing.consumption = value - prev.value if prev else None
            else:
                r.consumption = value - prev.value if prev else None

            results["ok"] += 1

        db.session.commit()
        flash(
            f"Import abgeschlossen: {results['ok']} gespeichert, "
            f"{results['skip']} übersprungen.",
            "success" if not results["errors"] else "warning",
        )
        if results["errors"]:
            for err in results["errors"][:10]:
                flash(err, "warning")
        return redirect(url_for("meters.index"))

    return render_template("meters/import.html")


# ---------------------------------------------------------------------------
# Zählerwechsel
# ---------------------------------------------------------------------------

@bp.route("/<int:meter_id>/replace", methods=["GET", "POST"])
@login_required
def meter_replace(meter_id):
    old_meter = db.get_or_404(WaterMeter, meter_id)
    if not old_meter.active:
        flash("Dieser Zähler ist bereits ausgebaut.", "warning")
        return redirect(url_for("properties.detail", property_id=old_meter.property_id))

    if request.method == "POST":
        replacement_date_str = request.form.get("replacement_date", "")
        replacement_date = (
            datetime.strptime(replacement_date_str, "%Y-%m-%d").date()
            if replacement_date_str else date.today()
        )
        final_value = Decimal(request.form.get("final_value", "0").replace(",", "."))
        new_meter_number = request.form.get("new_meter_number", "").strip()
        new_initial_str = request.form.get("new_initial_value", "0").replace(",", ".")
        new_initial_value = Decimal(new_initial_str) if new_initial_str else Decimal("0")

        year = replacement_date.year

        # 1. Alter Zähler: Ausschlussdatum setzen, deaktivieren
        old_meter.installed_to = replacement_date
        old_meter.active = False

        # 2. Abschlussablesung des alten Zählers speichern
        existing_reading = MeterReading.query.filter_by(meter_id=old_meter.id, year=year).first()
        if existing_reading:
            existing_reading.value = final_value
            existing_reading.reading_date = replacement_date
            prev = MeterReading.query.filter_by(meter_id=old_meter.id, year=year - 1).first()
            if prev:
                existing_reading.consumption = final_value - prev.value
            elif old_meter.initial_value is not None:
                existing_reading.consumption = final_value - old_meter.initial_value
        else:
            prev = MeterReading.query.filter_by(meter_id=old_meter.id, year=year - 1).first()
            if prev:
                consumption = final_value - prev.value
            elif old_meter.initial_value is not None:
                consumption = final_value - old_meter.initial_value
            else:
                consumption = None
            final_reading = MeterReading(
                meter_id=old_meter.id,
                year=year,
                value=final_value,
                reading_date=replacement_date,
                consumption=consumption,
                created_by_id=current_user.id,
            )
            db.session.add(final_reading)

        # 3. Neuen Zähler anlegen
        new_meter = WaterMeter(
            property_id=old_meter.property_id,
            meter_number=new_meter_number,
            location=old_meter.location,
            installed_from=replacement_date,
            initial_value=new_initial_value,
            notes=f"Nachfolger von {old_meter.meter_number}",
        )
        db.session.add(new_meter)
        db.session.commit()

        flash(
            f"Zählerwechsel durchgeführt: Zähler '{old_meter.meter_number}' ausgebaut, "
            f"neuer Zähler '{new_meter_number}' eingebaut.",
            "success",
        )
        return redirect(url_for("properties.detail", property_id=old_meter.property_id))

    return render_template("meters/replace_form.html", meter=old_meter, today=date.today())
