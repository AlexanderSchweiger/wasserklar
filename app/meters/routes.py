import io
import json
from datetime import date, datetime
from decimal import Decimal

from flask import (
    render_template, redirect, url_for, flash, request, jsonify, session,
    make_response,
)
from flask_login import login_required, current_user
from sqlalchemy import case as sa_case

from app.meters import bp
from app.meters import import_service
from app.meters import swap_import_service
from app.meters.services import save_reading, recompute_meter_chain
from app.extensions import db
from app.models import (
    WaterMeter, MeterReading, Property, PropertyOwnership, Customer,
    BillingPeriod,
)
from app.pagination import paginate_query


# Erlaubte Sort-Keys der Zaehler-Verwaltungstabelle (Mapping URL-Param ->
# ORDER-BY-Logik in ``_apply_meter_sort``).
_SORT_KEYS = {"nr", "object", "owner", "eichjahr", "installed"}
_DEFAULT_SORT = "object"


def _apply_meter_sort(query, sort: str, direction: str):
    """Haengt die ORDER-BY-Klausel passend zum gewaehlten Spalten-Sort an.

    Sekundaer immer nach Property.object_number, Property.ort, damit gleiche
    Werte stabil sortiert sind. NULL-Werte (z.B. fehlender Besitzer, leeres
    Eichjahr) wandern in beiden Richtungen ans Ende — portabel ueber SQLite,
    MySQL/MariaDB und Postgres via "is null"-CASE-Sortier-Praefix (ANSI
    ``NULLS LAST`` wird von MySQL nicht unterstuetzt).
    """
    desc = direction == "desc"

    def order(col):
        return [
            sa_case((col.is_(None), 1), else_=0).asc(),
            col.desc() if desc else col.asc(),
        ]

    if sort == "nr":
        return query.order_by(
            *order(WaterMeter.meter_number),
            Property.object_number.asc(), Property.ort.asc(),
        )
    if sort == "owner":
        return query.order_by(
            *order(Customer.name),
            Property.object_number.asc(), Property.ort.asc(),
        )
    if sort == "eichjahr":
        return query.order_by(
            *order(WaterMeter.eichjahr),
            Property.object_number.asc(), Property.ort.asc(),
        )
    if sort == "installed":
        return query.order_by(
            *order(WaterMeter.installed_from),
            Property.object_number.asc(), Property.ort.asc(),
        )
    # Default und sort == "object"
    return query.order_by(
        *order(Property.object_number),
        *order(Property.ort),
    )


def _build_replacement_map(meters, period):
    """Gibt für jeden Zähler, der in der angegebenen Abrechnungsperiode
    eingebaut wurde, den Vorgänger-Zähler, dessen Abschlussablesung in
    dieser Periode und dessen Ablesung aus der Vorperiode zurück."""
    result = {}
    if period is None:
        return result
    prev_period = _previous_period(period)
    for meter in meters:
        if not (meter.installed_from
                and period.start_date <= meter.installed_from <= period.end_date):
            continue
        old_meter = (
            WaterMeter.query
            .filter_by(property_id=meter.property_id, active=False)
            .filter(WaterMeter.installed_to == meter.installed_from)
            .first()
        )
        if old_meter:
            old_reading = MeterReading.query.filter_by(
                meter_id=old_meter.id, billing_period_id=period.id
            ).first()
            old_prev_reading = (
                MeterReading.query.filter_by(
                    meter_id=old_meter.id, billing_period_id=prev_period.id
                ).first()
                if prev_period else None
            )
            result[meter.id] = {
                "old_meter": old_meter,
                "old_reading": old_reading,
                "old_prev_reading": old_prev_reading,
            }
    return result


def _resolve_period_arg():
    """Liest ``?period_id=`` aus der URL; faellt auf die aktive
    Abrechnungsperiode zurueck (oder ``None``, wenn keine existiert)."""
    pid = request.args.get("period_id", type=int)
    if pid:
        period = db.session.get(BillingPeriod, pid)
        if period is not None:
            return period
    return BillingPeriod.current()


def _previous_period(period):
    """Die chronologisch vorige Abrechnungsperiode (nach ``start_date``)."""
    if period is None:
        return None
    return (
        BillingPeriod.query
        .filter(BillingPeriod.start_date < period.start_date)
        .order_by(BillingPeriod.start_date.desc())
        .first()
    )


def _last_prev_reading(meter_id, period):
    """Ablesung eines Zählers aus der unmittelbar vorherigen Periode."""
    if period is None:
        return None
    prev_period = _previous_period(period)
    if prev_period is None:
        return None
    return MeterReading.query.filter_by(
        meter_id=meter_id, billing_period_id=prev_period.id
    ).first()


def _build_prev_readings_map(meter_ids, period):
    """Für jeden Zähler die Ablesung aus der unmittelbar vorherigen Periode."""
    if not meter_ids or period is None:
        return {}
    prev_period = _previous_period(period)
    if prev_period is None:
        return {}
    rows = MeterReading.query.filter(
        MeterReading.billing_period_id == prev_period.id,
        MeterReading.meter_id.in_(meter_ids),
    ).all()
    return {r.meter_id: r for r in rows}


def _all_periods():
    """Alle Abrechnungsperioden, neueste zuerst (fuer Auswahl-Dropdowns)."""
    return (
        BillingPeriod.query
        .order_by(BillingPeriod.start_date.desc(), BillingPeriod.id.desc())
        .all()
    )


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
    """Zähler-Verwaltung: Stammdaten anlegen/bearbeiten/tauschen/löschen."""
    q = request.args.get("q", "").strip()
    show_inactive = request.args.get("show_inactive", "0") == "1"
    sort = request.args.get("sort", _DEFAULT_SORT)
    if sort not in _SORT_KEYS:
        sort = _DEFAULT_SORT
    direction = request.args.get("dir", "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"

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
        .filter(Property.active == True)
    )
    if not show_inactive:
        meters_query = meters_query.filter(WaterMeter.active == True)
    if q:
        meters_query = meters_query.filter(
            db.or_(
                Property.object_number.ilike(f"%{q}%"),
                Property.strasse.ilike(f"%{q}%"),
                Property.ort.ilike(f"%{q}%"),
                Customer.name.ilike(f"%{q}%"),
                WaterMeter.meter_number.ilike(f"%{q}%"),
                WaterMeter.location.ilike(f"%{q}%"),
            )
        )

    meters_query = _apply_meter_sort(meters_query, sort, direction)

    pagination = paginate_query(meters_query, page_key="meters")
    meters = pagination.items

    # Ablesungs-Counts pro Zähler — informativ in der Verwaltungstabelle
    readings_count_map = {}
    visible_ids = [m.id for m in meters]
    if visible_ids:
        rows = (
            db.session.query(MeterReading.meter_id, db.func.count(MeterReading.id))
            .filter(MeterReading.meter_id.in_(visible_ids))
            .group_by(MeterReading.meter_id)
            .all()
        )
        readings_count_map = {row[0]: row[1] for row in rows}

    owners_map = _build_owners_map()

    ctx = dict(
        meters=meters,
        readings_count_map=readings_count_map,
        owners_map=owners_map,
        pagination=pagination,
        q=q,
        show_inactive=show_inactive,
        sort=sort,
        dir=direction,
    )
    if request.headers.get("HX-Request"):
        return render_template("meters/_manage_table.html", **ctx)
    return render_template("meters/index.html", **ctx)


@bp.route("/ablesungen")
@login_required
def readings():
    """Zählerablesung: Schnelleingabe, Ablesen pro Zeile, Import."""
    period = _resolve_period_arg()
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "normal")

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

    pagination = paginate_query(meters_query, page_key="meters")
    meters = pagination.items

    visible_ids = [m.id for m in meters]
    readings_map = {}
    prev_readings_map = {}
    if visible_ids and period is not None:
        for r in MeterReading.query.filter(
            MeterReading.billing_period_id == period.id,
            MeterReading.meter_id.in_(visible_ids),
        ).all():
            readings_map[r.meter_id] = r

        prev_readings_map = _build_prev_readings_map(visible_ids, period)

    replacement_map = _build_replacement_map(meters, period)
    owners_map = _build_owners_map()

    ctx = dict(
        meters=meters, readings_map=readings_map,
        prev_readings_map=prev_readings_map, period=period,
        periods=_all_periods(), today=date.today(),
        replacement_map=replacement_map, owners_map=owners_map,
        pagination=pagination,
    )
    if request.headers.get("HX-Request"):
        template = "meters/_table_quick.html" if mode == "quick" else "meters/_table.html"
        return render_template(template, **ctx)
    return render_template("meters/readings.html", q=q, mode=mode, **ctx)


@bp.route("/bulk_read", methods=["POST"])
@login_required
def bulk_read():
    period = db.session.get(
        BillingPeriod, request.form.get("billing_period_id", type=int)
    )
    if period is None:
        flash("Keine Abrechnungsperiode gewählt.", "danger")
        return redirect(url_for("meters.readings"))

    reading_date_str = request.form.get("reading_date", "")
    try:
        reading_date = (
            datetime.strptime(reading_date_str, "%Y-%m-%d").date()
            if reading_date_str else date.today()
        )
    except ValueError:
        reading_date = date.today()

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

        save_reading(meter, period, value, reading_date=reading_date,
                     created_by_id=current_user.id)
        saved += 1

    db.session.commit()
    flash(f"{saved} Ablesung(en) gespeichert.", "success")
    return redirect(url_for("meters.readings", period_id=period.id))


@bp.route("/<int:meter_id>/read", methods=["GET", "POST"])
@login_required
def add_reading(meter_id):
    meter = db.get_or_404(WaterMeter, meter_id)
    period = _resolve_period_arg()
    is_modal = bool(request.headers.get("X-From-Modal"))

    if request.method == "POST":
        period = db.session.get(
            BillingPeriod, request.form.get("billing_period_id", type=int)
        )
        if period is None:
            flash("Bitte eine Abrechnungsperiode wählen.", "danger")
            return redirect(url_for("meters.add_reading", meter_id=meter_id))
        value = Decimal(request.form.get("value", "0").replace(",", "."))
        reading_date_str = request.form.get("reading_date", "")
        try:
            reading_date = (
                datetime.strptime(reading_date_str, "%Y-%m-%d").date()
                if reading_date_str else date.today()
            )
        except ValueError:
            reading_date = date.today()

        reading = save_reading(
            meter, period, value, reading_date=reading_date,
            created_by_id=current_user.id,
        )
        db.session.commit()
        flash(
            f"Ablesung für {meter.property.label()} ({period.name}) gespeichert.",
            "success",
        )

        if is_modal:
            # Aktualisiertes Subtable direkt in das offene Panel schreiben,
            # dann Modal per HX-Trigger schließen.
            readings = (
                MeterReading.query
                .filter_by(meter_id=meter_id)
                .join(BillingPeriod, MeterReading.billing_period_id == BillingPeriod.id)
                .order_by(BillingPeriod.start_date.desc(), MeterReading.reading_date.desc())
                .all()
            )
            resp = make_response(render_template(
                "meters/_readings_subtable.html",
                meter=meter, readings=readings,
            ))
            resp.headers["HX-Retarget"] = f"#readings-content-{meter_id}"
            resp.headers["HX-Reswap"] = "innerHTML"
            resp.headers["HX-Trigger"] = json.dumps({"closeReadingModal": True})
            return resp

        if request.headers.get("HX-Request"):
            prev = _last_prev_reading(meter_id, period)
            repl_map = _build_replacement_map([meter], period)
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
                "meters/_row.html", meter=meter, reading=reading, period=period,
                prev_readings_map={meter_id: prev} if prev else {},
                replacement_map=repl_map,
                owners_map={meter.property_id: owner} if owner else {},
            )
        return redirect(url_for("meters.readings", period_id=period.id))

    existing = (
        MeterReading.query.filter_by(
            meter_id=meter_id, billing_period_id=period.id
        ).first()
        if period else None
    )

    # Vorperiodenwert oder Anfangsstand als Basis
    prev = _last_prev_reading(meter_id, period) if period else None
    prev_value = (
        int(prev.value) if prev
        else (int(meter.initial_value) if meter.initial_value is not None else None)
    )

    # Durchschnittsverbrauch der letzten 5 Ablesungen (mind. 3 Werte nötig)
    past_readings = (
        MeterReading.query
        .filter(
            MeterReading.meter_id == meter_id,
            MeterReading.consumption.isnot(None),
        )
        .order_by(MeterReading.reading_date.desc())
        .limit(5)
        .all()
    )
    avg_consumption = None
    avg_years = 0
    if len(past_readings) >= 3:
        avg_years = len(past_readings)
        avg_consumption = round(
            sum(float(r.consumption) for r in past_readings) / avg_years
        )

    # Bei Zählertausch: Verbrauch des alten Zählers (Abschlussablesung)
    # mitanzeigen — analog zur Ablesungstabelle (alt + neu = gesamt).
    repl_map = _build_replacement_map([meter], period) if period else {}
    repl = repl_map.get(meter.id)
    old_consumption = None
    if repl and repl["old_reading"] and repl["old_reading"].consumption is not None:
        old_consumption = int(repl["old_reading"].consumption)

    form_ctx = dict(
        meter=meter, period=period, periods=_all_periods(), existing=existing,
        prev_value=prev_value, avg_consumption=avg_consumption, avg_years=avg_years,
        old_consumption=old_consumption, today=date.today(),
    )

    # HTMX-GET (vom Edit-Button im Subtable): nur den Form-Body zurückgeben,
    # der dann via HTMX in den Modal-Body geswappt wird.
    if request.headers.get("HX-Request"):
        return render_template("meters/_reading_form_body.html", **form_ctx)

    return render_template("meters/reading_form.html", **form_ctx)


def _read_type_and_parent(meter_id: int | None) -> tuple[str, int | None]:
    """Liest meter_type und parent_meter_id aus dem Form, validiert und gibt
    ein bereinigtes Tupel zurueck.

    Regeln:
      - meter_type defaulted auf 'main' (akzeptiert nur 'main'|'sub')
      - bei meter_type='main' wird parent_meter_id zwingend auf NULL gesetzt
      - Self-Reference (parent == eigener id) wird gekappt + flash-Warnung
      - parent muss meter_type='main' sein (sonst gekappt + flash-Warnung)
    """
    mt = (request.form.get("meter_type") or "main").strip()
    if mt not in ("main", "sub"):
        mt = "main"
    pid_raw = (request.form.get("parent_meter_id") or "").strip()
    pid: int | None = None
    if mt == "sub" and pid_raw:
        try:
            pid = int(pid_raw)
        except ValueError:
            pid = None
        if pid and meter_id and pid == meter_id:
            flash("Ein Zähler kann nicht sein eigener Hauptzähler sein.", "warning")
            pid = None
        if pid:
            parent = db.session.get(WaterMeter, pid)
            if not parent or parent.meter_type != "main":
                flash("Übergeordneter Zähler muss ein Hauptzähler sein.", "warning")
                pid = None
    return mt, pid


def _active_main_meters_excluding(exclude_id: int | None = None) -> list[WaterMeter]:
    q = WaterMeter.query.filter(
        WaterMeter.active.is_(True),
        WaterMeter.meter_type == "main",
    )
    if exclude_id:
        q = q.filter(WaterMeter.id != exclude_id)
    return q.order_by(WaterMeter.meter_number.asc()).all()


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
        meter_type, parent_id = _read_type_and_parent(meter_id=None)
        new_meter_number = request.form.get("meter_number", "").strip()
        if WaterMeter.query.filter_by(meter_number=new_meter_number).first():
            flash(f"Zählernummer '{new_meter_number}' ist bereits vergeben.", "danger")
            selected_property_id = request.form.get("property_id", type=int)
            return render_template(
                "meters/meter_form.html",
                meter=None, properties=properties,
                selected_property_id=selected_property_id,
                main_meters=_active_main_meters_excluding(),
            )
        m = WaterMeter(
            property_id=int(request.form["property_id"]),
            meter_number=new_meter_number,
            location=request.form.get("location", "").strip(),
            notes=request.form.get("notes", "").strip(),
            installed_from=(
                datetime.strptime(installed_from_str, "%Y-%m-%d").date()
                if installed_from_str else None
            ),
            initial_value=Decimal(initial_value_str) if initial_value_str else None,
            eichjahr=int(eichjahr_str) if eichjahr_str else None,
            meter_type=meter_type,
            parent_meter_id=parent_id,
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
        main_meters=_active_main_meters_excluding(),
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
        meter_type, parent_id = _read_type_and_parent(meter_id=meter.id)
        new_meter_number = request.form.get("meter_number", "").strip()
        if WaterMeter.query.filter(
            WaterMeter.meter_number == new_meter_number,
            WaterMeter.id != meter.id,
        ).first():
            flash(f"Zählernummer '{new_meter_number}' ist bereits vergeben.", "danger")
            return render_template(
                "meters/meter_form.html", meter=meter, properties=properties,
                selected_property_id=None,
                main_meters=_active_main_meters_excluding(exclude_id=meter.id),
            )
        meter.property_id = int(request.form["property_id"])
        meter.meter_number = new_meter_number
        meter.location = request.form.get("location", "").strip()
        meter.notes = request.form.get("notes", "").strip()
        meter.installed_from = (
            datetime.strptime(installed_from_str, "%Y-%m-%d").date()
            if installed_from_str else None
        )
        meter.initial_value = Decimal(initial_value_str) if initial_value_str else None
        meter.eichjahr = int(eichjahr_str) if eichjahr_str else None
        meter.meter_type = meter_type
        meter.parent_meter_id = parent_id
        db.session.commit()
        flash("Zähler aktualisiert.", "success")
        return redirect(url_for("meters.index"))
    return render_template(
        "meters/meter_form.html", meter=meter, properties=properties,
        selected_property_id=None,
        main_meters=_active_main_meters_excluding(exclude_id=meter.id),
    )


# ---------------------------------------------------------------------------
# CSV / Excel Import — 3-stufiger Wizard
# ---------------------------------------------------------------------------
#
# Architektur (siehe import_service.py fuer das Heavy Lifting):
#   /meters/import           GET = Upload-Form, POST = Datei -> Pickle -> /preview
#   /meters/import/preview   GET = Resolve + Vorschau-Editor
#                            POST mit action=refresh -> Mapping aendern, neu rendern
#                            POST mit action=confirm  -> User-Edits einlesen,
#                                                       commit_import, /result
#   /meters/import/result    GET = Stats anzeigen
#
# Persistenz: hochgeladenes DataFrame liegt als Pickle in instance/, der
# Session-Cookie haelt nur den Dateipfad und die Mapping-Config -- die
# resolved-Liste wird bei jedem GET frisch aus DataFrame+Config aufgebaut.

_SESSION_FILE_KEY = "meter_import_file"
_SESSION_CFG_KEY = "meter_import_cfg"
_SESSION_RESULT_KEY = "meter_import_result"


def _abort_to_upload(reason: str = "Sitzung abgelaufen — bitte erneut hochladen.",
                    category: str = "warning"):
    flash(reason, category)
    # Pickle aufraeumen, falls Pfad noch in Session
    path = session.pop(_SESSION_FILE_KEY, None)
    if path:
        import_service.delete_dataframe(path)
    session.pop(_SESSION_CFG_KEY, None)
    return redirect(url_for("meters.import_upload"))


@bp.route("/import", methods=["GET", "POST"])
@login_required
def import_upload():
    """Step 1: Datei + Mapping-Modus + Spalten + Duplikat-Strategie."""
    if request.method == "POST":
        import pandas as pd

        f = request.files.get("file")
        if not f or not f.filename:
            flash("Bitte eine Datei auswählen.", "warning")
            return redirect(url_for("meters.import_upload"))

        filename = f.filename.lower()
        try:
            if filename.endswith(".csv"):
                df = pd.read_csv(f, dtype=str, sep=None, engine="python")
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(f, dtype=str)
            else:
                flash("Nicht unterstütztes Dateiformat. Bitte CSV oder Excel.", "danger")
                return redirect(url_for("meters.import_upload"))
        except Exception as e:
            flash(f"Fehler beim Lesen der Datei: {e}", "danger")
            return redirect(url_for("meters.import_upload"))

        # Alten Pickle-Stand bereinigen, falls noch einer rumliegt
        old_path = session.pop(_SESSION_FILE_KEY, None)
        if old_path:
            import_service.delete_dataframe(old_path)

        path = import_service.save_dataframe(df)
        session[_SESSION_FILE_KEY] = path

        # Mapping-Konfig: nimm was im Form steht; spaeter im /preview
        # nochmal anpassbar.
        cfg = import_service.MappingConfig.from_form(request.form)
        session[_SESSION_CFG_KEY] = cfg.to_dict()

        return redirect(url_for("meters.import_preview"))

    return render_template(
        "meters/import.html",
        modes=import_service.MAPPING_MODES,
        duplicate_modes=import_service.DUPLICATE_MODES,
        periods=_all_periods(),
        active_period=BillingPeriod.current(),
    )


@bp.route("/import/preview", methods=["GET", "POST"])
@login_required
def import_preview():
    """Step 2: Vorschau-Editor.

    GET = Vorschau rendern.
    POST mit action=refresh = Mapping-Config aktualisieren, neu rendern
        (User-Edits gehen verloren -- wird im Template kommuniziert).
    POST mit action=confirm  = wird an import_confirm() weitergeleitet.
    """
    path = session.get(_SESSION_FILE_KEY)
    df = import_service.load_dataframe(path) if path else None
    if df is None:
        return _abort_to_upload()

    cfg = import_service.MappingConfig.from_dict(session.get(_SESSION_CFG_KEY))
    columns = list(df.columns)

    # Auto-suggest col_lookup beim ersten Aufruf, falls leer.
    if not cfg.col_lookup:
        cfg.col_lookup = _suggest_lookup_column(columns, cfg.mode)
    if not cfg.col_value:
        cfg.col_value = _suggest_value_column(columns)
    if not cfg.col_date:
        cfg.col_date = _suggest_date_column(columns)
    if not cfg.col_consumption:
        cfg.col_consumption = _suggest_consumption_column(columns)
    if not cfg.billing_period_id:
        _active = BillingPeriod.current()
        if _active is not None:
            cfg.billing_period_id = _active.id

    if request.method == "POST":
        # Beide Aktionen (refresh + confirm) muessen die Mapping-Config aus dem
        # Form uebernehmen. Sonst geht der State der Spalten-Selects verloren,
        # wenn der User direkt auf "Import ausfuehren" klickt ohne vorher
        # "Vorschau aktualisieren" gedrueckt zu haben.
        cfg = import_service.MappingConfig.from_form(request.form)
        session[_SESSION_CFG_KEY] = cfg.to_dict()
        if request.form.get("action") == "confirm":
            return _do_confirm(df, cfg)

    rows = import_service.build_resolved_rows(df, cfg)
    detected_value_fmt, detected_date_fmt = import_service.detect_formats_for_config(df, cfg)

    counts = {"ok": 0, "warn": 0, "err": 0}
    for r in rows:
        if r.status in (import_service.STATUS_OK,
                        import_service.STATUS_OK_PREFERRED_MAIN):
            counts["ok"] += 1
        elif r.status == import_service.STATUS_AMBIGUOUS:
            counts["warn"] += 1
        else:
            counts["err"] += 1

    # Meter-Pool fuer Dropdowns: alle aktiven Meter (fuer not_found-Faelle).
    # Pro Zeile mit candidate_meter_ids wird das Dropdown im Template
    # entsprechend gefiltert -- meters_by_id ist das O(1)-Lookup dafuer.
    all_meters = import_service.all_active_meters()
    meters_by_id = {m.id: m for m in all_meters}
    owners_by_meter = {m.id: import_service.owner_name_for(m) for m in all_meters}

    return render_template(
        "meters/import_preview.html",
        cfg=cfg,
        columns=columns,
        rows=rows,
        counts=counts,
        all_meters=all_meters,
        meters_by_id=meters_by_id,
        owners_by_meter=owners_by_meter,
        modes=import_service.MAPPING_MODES,
        duplicate_modes=import_service.DUPLICATE_MODES,
        periods=_all_periods(),
        detected_value_fmt=detected_value_fmt,
        detected_date_fmt=detected_date_fmt,
        format_value_de=import_service.format_value_de,
        status_row_class=import_service.status_row_class,
        status_badge=import_service.status_badge,
        STATUS_OK=import_service.STATUS_OK,
        STATUS_OK_PREFERRED_MAIN=import_service.STATUS_OK_PREFERRED_MAIN,
        STATUS_AMBIGUOUS=import_service.STATUS_AMBIGUOUS,
        STATUS_NOT_FOUND=import_service.STATUS_NOT_FOUND,
        STATUS_PARSE_ERROR=import_service.STATUS_PARSE_ERROR,
    )


def _do_confirm(df, cfg):
    """Hilfsfunktion: User-Edits aus dem Form lesen, committen, weiterleiten."""
    period = (
        db.session.get(BillingPeriod, cfg.billing_period_id)
        if cfg.billing_period_id else None
    )
    if period is None:
        flash("Bitte eine Abrechnungsperiode für den Import wählen.", "warning")
        return redirect(url_for("meters.import_preview"))
    baseline = import_service.build_resolved_rows(df, cfg)
    merged = import_service.parse_form_edits(request.form, baseline)
    stats = import_service.commit_import(
        merged, user_id=current_user.id, billing_period=period,
        duplicate_mode=cfg.duplicate_mode,
    )

    # Pickle aufraeumen
    path = session.pop(_SESSION_FILE_KEY, None)
    if path:
        import_service.delete_dataframe(path)
    session.pop(_SESSION_CFG_KEY, None)
    session[_SESSION_RESULT_KEY] = stats.to_dict()

    # User-feedback ueber flash, Detail in Result-Page
    total = stats.created + stats.updated
    if stats.errors:
        category = "warning"
    elif total == 0:
        category = "warning"
    else:
        category = "success"
    flash(
        f"Import abgeschlossen: {stats.created} angelegt, {stats.updated} aktualisiert, "
        f"{stats.skipped} übersprungen, {stats.skipped_dup} Duplikate, "
        f"{stats.skipped_unmapped} nicht gemappt.",
        category,
    )
    return redirect(url_for("meters.import_result"))


@bp.route("/import/result")
@login_required
def import_result():
    stats = session.pop(_SESSION_RESULT_KEY, None)
    if not stats:
        return redirect(url_for("meters.readings"))
    return render_template("meters/import_result.html", stats=stats)


# ---- Spalten-Auto-Suggestion -------------------------------------------------

def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _suggest_lookup_column(columns: list[str], mode: str) -> str:
    hints_by_mode = {
        "meter_number": ["zählernummer", "zaehlernummer", "zähler-nr", "zähler nr", "zaehler"],
        "customer_number": ["kunden-nr", "kundennr", "kundennummer", "kunden nr", "kunden_nr"],
        "customer_name": ["kombinierter name", "kunde", "name", "kundenname"],
    }
    hints = hints_by_mode.get(mode, [])
    for col in columns:
        n = _norm(col)
        for h in hints:
            if h in n:
                return col
    return ""


def _suggest_value_column(columns: list[str]) -> str:
    hints = ["stand", "zählerstand", "zaehlerstand", "wert", "value"]
    for col in columns:
        n = _norm(col)
        for h in hints:
            if h in n:
                return col
    return ""


def _suggest_date_column(columns: list[str]) -> str:
    hints = ["datum", "ablesedatum", "date"]
    for col in columns:
        n = _norm(col)
        for h in hints:
            if h in n:
                return col
    return ""


def _suggest_consumption_column(columns: list[str]) -> str:
    hints = ["verbrauch", "konsum", "consumption"]
    for col in columns:
        n = _norm(col)
        for h in hints:
            if h in n:
                return col
    return ""


# ---------------------------------------------------------------------------
# Zählerwechsel
# ---------------------------------------------------------------------------

@bp.route("/<int:meter_id>/replace", methods=["GET", "POST"])
@login_required
def meter_replace(meter_id):
    old_meter = db.get_or_404(WaterMeter, meter_id)
    from_view = (
        request.form.get("from") if request.method == "POST"
        else request.args.get("from", "property")
    )
    if not old_meter.active:
        flash("Dieser Zähler ist bereits ausgebaut.", "warning")
        if from_view == "list":
            return redirect(url_for("meters.index"))
        return redirect(url_for("properties.detail", property_id=old_meter.property_id))

    if request.method == "POST":
        period = db.session.get(
            BillingPeriod, request.form.get("billing_period_id", type=int)
        )
        if period is None:
            flash("Bitte eine Abrechnungsperiode für den Zählertausch wählen.", "danger")
            return redirect(url_for("meters.meter_replace", meter_id=meter_id,
                                    **{"from": from_view}))

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

        if WaterMeter.query.filter_by(meter_number=new_meter_number).first():
            flash(f"Zählernummer '{new_meter_number}' ist bereits vergeben.", "danger")
            return redirect(url_for("meters.meter_replace", meter_id=meter_id,
                                    **{"from": from_view}))

        # 1. Alter Zähler: Ausbaudatum setzen, deaktivieren
        old_meter.installed_to = replacement_date
        old_meter.active = False

        # 2. Abschlussablesung des alten Zählers anlegen/aktualisieren —
        #    Verbrauch wird unten ueber recompute_meter_chain gesetzt.
        existing_reading = MeterReading.query.filter_by(
            meter_id=old_meter.id, billing_period_id=period.id
        ).first()
        if existing_reading:
            existing_reading.value = final_value
            existing_reading.reading_date = replacement_date
            existing_reading.created_by_id = current_user.id
        else:
            db.session.add(MeterReading(
                meter_id=old_meter.id,
                billing_period_id=period.id,
                value=final_value,
                reading_date=replacement_date,
                created_by_id=current_user.id,
            ))

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
        db.session.flush()
        recompute_meter_chain(old_meter)
        db.session.commit()

        flash(
            f"Zählerwechsel durchgeführt: Zähler '{old_meter.meter_number}' ausgebaut, "
            f"neuer Zähler '{new_meter_number}' eingebaut.",
            "success",
        )
        if from_view == "list":
            return redirect(url_for("meters.index"))
        return redirect(url_for("properties.detail", property_id=old_meter.property_id))

    return render_template(
        "meters/replace_form.html",
        meter=old_meter, today=date.today(), from_view=from_view,
        periods=_all_periods(), active_period=BillingPeriod.current(),
    )


# ---------------------------------------------------------------------------
# Zählertausch-Import — CSV / Excel Bulk-Import
# ---------------------------------------------------------------------------
#
#   /meters/swap-import          GET = Upload-Form, POST = Datei -> Pickle -> /preview
#   /meters/swap-import/preview  GET = Vorschau-Editor
#                                POST mit action=confirm -> commit, /result
#   /meters/swap-import/result   GET = Stats anzeigen
#
# Persistenz wie beim Ablesungs-Import: das DataFrame liegt als Pickle in
# instance/, die Session haelt nur den Pfad. Die Vorschau-Zeilen werden bei
# jedem Aufruf frisch aus dem DataFrame aufgebaut (DB-Lookups inklusive).

_SESSION_SWAP_FILE_KEY = "meter_swap_file"
_SESSION_SWAP_RESULT_KEY = "meter_swap_result"


def _abort_to_swap_upload(
    reason: str = "Sitzung abgelaufen — bitte erneut hochladen.",
    category: str = "warning",
):
    flash(reason, category)
    path = session.pop(_SESSION_SWAP_FILE_KEY, None)
    if path:
        swap_import_service.delete_dataframe(path)
    return redirect(url_for("meters.swap_import_upload"))


@bp.route("/swap-import", methods=["GET", "POST"])
@login_required
def swap_import_upload():
    """Step 1: CSV/Excel mit Zählertäuschen hochladen."""
    if request.method == "POST":
        import pandas as pd

        f = request.files.get("file")
        if not f or not f.filename:
            flash("Bitte eine Datei auswählen.", "warning")
            return redirect(url_for("meters.swap_import_upload"))

        filename = f.filename.lower()
        try:
            if filename.endswith(".csv"):
                df = pd.read_csv(f, dtype=str, sep=None, engine="python")
            elif filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(f, dtype=str)
            else:
                flash("Nicht unterstütztes Dateiformat. Bitte CSV oder Excel.", "danger")
                return redirect(url_for("meters.swap_import_upload"))
        except Exception as e:
            flash(f"Fehler beim Lesen der Datei: {e}", "danger")
            return redirect(url_for("meters.swap_import_upload"))

        cols = swap_import_service.detect_columns(list(df.columns))
        missing = swap_import_service.missing_required_columns(cols)
        if missing:
            flash(
                "Pflicht-Spalten fehlen in der Datei: " + ", ".join(missing)
                + ". Bitte Spaltenüberschriften prüfen.",
                "danger",
            )
            return redirect(url_for("meters.swap_import_upload"))

        old_path = session.pop(_SESSION_SWAP_FILE_KEY, None)
        if old_path:
            swap_import_service.delete_dataframe(old_path)

        session[_SESSION_SWAP_FILE_KEY] = swap_import_service.save_dataframe(df)
        return redirect(url_for("meters.swap_import_preview"))

    return render_template("meters/swap_import.html")


@bp.route("/swap-import/preview", methods=["GET", "POST"])
@login_required
def swap_import_preview():
    """Step 2: Vorschau der Täusche/Neuanlagen, POST=confirm committet."""
    path = session.get(_SESSION_SWAP_FILE_KEY)
    df = swap_import_service.load_dataframe(path) if path else None
    if df is None:
        return _abort_to_swap_upload()

    if request.method == "POST" and request.form.get("action") == "confirm":
        period = db.session.get(
            BillingPeriod, request.form.get("billing_period_id", type=int)
        )
        if period is None:
            flash("Bitte eine Abrechnungsperiode für den Import wählen.", "warning")
            return redirect(url_for("meters.swap_import_preview"))
        baseline, _cols = swap_import_service.build_swap_rows(df)
        merged = swap_import_service.parse_swap_form_edits(request.form, baseline)
        stats = swap_import_service.commit_swap_import(
            merged, user_id=current_user.id, billing_period=period,
        )

        session.pop(_SESSION_SWAP_FILE_KEY, None)
        if path:
            swap_import_service.delete_dataframe(path)
        session[_SESSION_SWAP_RESULT_KEY] = stats.to_dict()

        done = stats.swapped + stats.created
        category = "warning" if (stats.errors or done == 0) else "success"
        flash(
            f"Import abgeschlossen: {stats.swapped} Tausch(e), "
            f"{stats.created} Neuanlage(n), {stats.skipped} übersprungen, "
            f"{stats.skipped_error} fehlerhaft.",
            category,
        )
        return redirect(url_for("meters.swap_import_result"))

    rows, cols = swap_import_service.build_swap_rows(df)

    counts = {"tausch": 0, "neuanlage": 0, "fehler": 0}
    for r in rows:
        if r.status == swap_import_service.STATUS_TAUSCH:
            counts["tausch"] += 1
        elif r.status == swap_import_service.STATUS_NEUANLAGE:
            counts["neuanlage"] += 1
        else:
            counts["fehler"] += 1

    return render_template(
        "meters/swap_import_preview.html",
        rows=rows,
        cols=cols,
        counts=counts,
        properties=swap_import_service.active_properties(),
        periods=_all_periods(),
        active_period=BillingPeriod.current(),
        format_value_de=swap_import_service.format_value_de,
        status_row_class=swap_import_service.status_row_class,
        status_badge=swap_import_service.status_badge,
        STATUS_TAUSCH=swap_import_service.STATUS_TAUSCH,
        STATUS_NEUANLAGE=swap_import_service.STATUS_NEUANLAGE,
        STATUS_FEHLER=swap_import_service.STATUS_FEHLER,
    )


@bp.route("/swap-import/result")
@login_required
def swap_import_result():
    stats = session.pop(_SESSION_SWAP_RESULT_KEY, None)
    if not stats:
        return redirect(url_for("meters.index"))
    return render_template("meters/swap_import_result.html", stats=stats)


@bp.route("/<int:meter_id>/readings-partial")
@login_required
def readings_partial(meter_id):
    """HTMX-Fragment: alle Ablesungen eines Zählers chronologisch."""
    meter = db.get_or_404(WaterMeter, meter_id)
    readings = (
        MeterReading.query
        .filter_by(meter_id=meter_id)
        .join(BillingPeriod, MeterReading.billing_period_id == BillingPeriod.id)
        .order_by(BillingPeriod.start_date.desc(), MeterReading.reading_date.desc())
        .all()
    )
    return render_template(
        "meters/_readings_subtable.html",
        meter=meter,
        readings=readings,
    )


@bp.route("/<int:meter_id>/delete", methods=["POST"])
@login_required
def meter_delete(meter_id):
    meter = db.get_or_404(WaterMeter, meter_id)
    if MeterReading.query.filter_by(meter_id=meter.id).count() > 0:
        flash(
            f"Zähler '{meter.meter_number}' kann nicht gelöscht werden — "
            "es existieren bereits Ablesungen. Stattdessen Zählertausch verwenden "
            "oder den Zähler manuell ausbauen.",
            "danger",
        )
        return redirect(url_for("meters.index"))
    number = meter.meter_number
    db.session.delete(meter)
    db.session.commit()
    flash(f"Zähler '{number}' wurde gelöscht.", "success")
    return redirect(url_for("meters.index"))
