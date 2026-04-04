import io
from datetime import date, datetime
from decimal import Decimal

from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.meters import bp
from app.extensions import db
from app.models import WaterMeter, MeterReading, Property, PropertyOwnership, Customer


def _build_replacement_map(meters, year):
    """Gibt für jeden Zähler, der im angegebenen Jahr eingebaut wurde,
    den Vorgänger-Zähler und dessen Abschlussablesung zurück."""
    result = {}
    for meter in meters:
        if not (meter.installed_from and meter.installed_from.year == year):
            continue
        old_meter = (
            WaterMeter.query
            .filter_by(property_id=meter.property_id, active=False)
            .filter(WaterMeter.installed_to == meter.installed_from)
            .first()
        )
        if old_meter:
            old_reading = MeterReading.query.filter_by(
                meter_id=old_meter.id, year=year
            ).first()
            result[meter.id] = {"old_meter": old_meter, "old_reading": old_reading}
    return result


def _build_owners_map():
    """Gibt ein Dict {property_id: customer_name} für alle aktuellen Eigentümer zurück."""
    rows = (
        db.session.query(PropertyOwnership.property_id, Customer.name)
        .join(Customer, Customer.id == PropertyOwnership.customer_id)
        .filter(PropertyOwnership.valid_to == None)
        .all()
    )
    return {row.property_id: row.name for row in rows}


@bp.route("/")
@login_required
def index():
    year = request.args.get("year", date.today().year, type=int)
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "normal")

    # Objekt + Zähler-Join + aktueller Eigentümer (für Suche), nach Objektnummer / Ort sortiert
    meters_query = (
        WaterMeter.query
        .join(Property)
        .outerjoin(
            PropertyOwnership,
            db.and_(
                PropertyOwnership.property_id == Property.id,
                PropertyOwnership.valid_to == None,
            ),
        )
        .outerjoin(Customer, Customer.id == PropertyOwnership.customer_id)
        .filter(WaterMeter.active == True, Property.active == True)
        .order_by(Property.object_number, Property.ort)
    )
    if q:
        meters_query = meters_query.filter(
            db.or_(
                Property.object_number.ilike(f"%{q}%"),
                Property.strasse.ilike(f"%{q}%"),
                Property.ort.ilike(f"%{q}%"),
                Customer.name.ilike(f"%{q}%"),
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

    # Zählertausch-Info: Zähler die in diesem Jahr eingebaut wurden
    replacement_map = _build_replacement_map(meters, year)

    # Eigentümer-Map für Anzeige
    owners_map = _build_owners_map()

    if request.headers.get("HX-Request"):
        template = "meters/_table_quick.html" if mode == "quick" else "meters/_table.html"
        return render_template(
            template,
            meters=meters, readings_map=readings_map,
            prev_readings_map=prev_readings_map, year=year,
            replacement_map=replacement_map, owners_map=owners_map,
        )
    return render_template(
        "meters/index.html",
        meters=meters, readings_map=readings_map,
        prev_readings_map=prev_readings_map, year=year, q=q,
        replacement_map=replacement_map, owners_map=owners_map,
        mode=mode,
    )


@bp.route("/bulk_read", methods=["POST"])
@login_required
def bulk_read():
    year = int(request.form.get("year", date.today().year))
    saved = 0
    for key, value_str in request.form.items():
        if not key.startswith("value_"):
            continue
        meter_id = int(key[len("value_"):])
        value_str = value_str.strip().replace(",", ".")
        if not value_str:
            continue
        try:
            value = Decimal(value_str)
        except Exception:
            continue

        meter = db.session.get(WaterMeter, meter_id)
        if not meter:
            continue

        existing = MeterReading.query.filter_by(meter_id=meter_id, year=year).first()
        prev = MeterReading.query.filter_by(meter_id=meter_id, year=year - 1).first()
        consumption = None
        if prev:
            consumption = value - prev.value
        elif meter.initial_value is not None:
            consumption = value - meter.initial_value

        if existing:
            existing.value = value
            existing.consumption = consumption
            existing.created_by_id = current_user.id
        else:
            reading = MeterReading(
                meter_id=meter_id, year=year, value=value,
                reading_date=date.today(),
                consumption=consumption,
                created_by_id=current_user.id,
            )
            db.session.add(reading)
        saved += 1

    db.session.commit()
    flash(f"{saved} Ablesung(en) gespeichert.", "success")
    return redirect(url_for("meters.index", year=year))


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
            repl_map = _build_replacement_map([meter], year)
            owner = (
                db.session.query(Customer.name)
                .join(PropertyOwnership, PropertyOwnership.customer_id == Customer.id)
                .filter(
                    PropertyOwnership.property_id == meter.property_id,
                    PropertyOwnership.valid_to == None,
                )
                .scalar()
            )
            return render_template(
                "meters/_row.html", meter=meter, reading=reading, year=year,
                prev_readings_map={meter_id: prev} if prev else {},
                replacement_map=repl_map,
                owners_map={meter.property_id: owner} if owner else {},
            )
        return redirect(url_for("meters.index", year=year))

    # Vorjahreswert oder Anfangsstand als Basis
    prev = MeterReading.query.filter_by(meter_id=meter_id, year=year - 1).first()
    prev_value = int(prev.value) if prev else (int(meter.initial_value) if meter.initial_value is not None else None)

    # Durchschnittsverbrauch der letzten 5 Jahre (mind. 3 Werte nötig)
    past_readings = (
        MeterReading.query
        .filter(
            MeterReading.meter_id == meter_id,
            MeterReading.year < year,
            MeterReading.year >= year - 5,
            MeterReading.consumption.isnot(None),
        )
        .all()
    )
    avg_consumption = None
    avg_years = 0
    if len(past_readings) >= 3:
        avg_years = len(past_readings)
        avg_consumption = round(sum(float(r.consumption) for r in past_readings) / avg_years)

    return render_template(
        "meters/reading_form.html",
        meter=meter, year=year, existing=existing,
        prev_value=prev_value, avg_consumption=avg_consumption, avg_years=avg_years,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def meter_new():
    properties = Property.query.filter_by(active=True).order_by(
        Property.object_number, Property.ort
    ).all()
    if request.method == "POST":
        installed_from_str = request.form.get("installed_from", "")
        initial_value_str = request.form.get("initial_value", "").replace(",", ".")
        eichjahr_str = request.form.get("eichjahr", "").strip()
        m = WaterMeter(
            property_id=int(request.form["property_id"]),
            meter_number=request.form.get("meter_number", "").strip(),
            location=request.form.get("location", "").strip(),
            notes=request.form.get("notes", "").strip(),
            installed_from=(
                datetime.strptime(installed_from_str, "%Y-%m-%d").date()
                if installed_from_str else None
            ),
            initial_value=Decimal(initial_value_str) if initial_value_str else None,
            eichjahr=int(eichjahr_str) if eichjahr_str else None,
        )
        db.session.add(m)
        db.session.commit()
        flash("Zähler angelegt.", "success")
        return redirect(url_for("meters.index"))
    selected_property_id = request.args.get("property_id", type=int)
    return render_template(
        "meters/meter_form.html",
        meter=None, properties=properties,
        selected_property_id=selected_property_id,
    )


@bp.route("/<int:meter_id>/edit", methods=["GET", "POST"])
@login_required
def meter_edit(meter_id):
    meter = db.get_or_404(WaterMeter, meter_id)
    properties = Property.query.filter_by(active=True).order_by(
        Property.object_number, Property.ort
    ).all()
    if request.method == "POST":
        installed_from_str = request.form.get("installed_from", "")
        initial_value_str = request.form.get("initial_value", "").replace(",", ".")
        eichjahr_str = request.form.get("eichjahr", "").strip()
        meter.property_id = int(request.form["property_id"])
        meter.meter_number = request.form.get("meter_number", "").strip()
        meter.location = request.form.get("location", "").strip()
        meter.notes = request.form.get("notes", "").strip()
        meter.installed_from = (
            datetime.strptime(installed_from_str, "%Y-%m-%d").date()
            if installed_from_str else None
        )
        meter.initial_value = Decimal(initial_value_str) if initial_value_str else None
        meter.eichjahr = int(eichjahr_str) if eichjahr_str else None
        db.session.commit()
        flash("Zähler aktualisiert.", "success")
        return redirect(url_for("meters.index"))
    return render_template(
        "meters/meter_form.html", meter=meter, properties=properties,
        selected_property_id=None,
    )


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
            val_raw = str(row.get(col_value, "")).strip()
            year = int(row[col_year]) if col_year and row.get(col_year) else default_year

            meter = WaterMeter.query.filter_by(meter_number=meter_num).first()
            if not meter:
                results["errors"].append(f"Zähler '{meter_num}' nicht gefunden")
                results["skip"] += 1
                continue
            # Österreichisches Zahlenformat: Komma = Dezimal, Punkt = Tausender
            val_raw_at = val_raw
            if "," in val_raw_at and "." in val_raw_at:
                val_raw_at = val_raw_at.replace(".", "")
            val_raw_at = val_raw_at.replace(",", ".")
            try:
                value = Decimal(val_raw_at)
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
        new_eichjahr_str = request.form.get("new_eichjahr", "").strip()
        new_eichjahr = int(new_eichjahr_str) if new_eichjahr_str else None

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
            eichjahr=new_eichjahr,
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
