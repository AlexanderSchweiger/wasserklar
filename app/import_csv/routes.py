import os
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    render_template, request, redirect, url_for,
    flash, session, current_app
)
from flask_login import login_required

from app.import_csv import bp
from app.extensions import db
from app.models import Customer, Property, PropertyOwnership, WaterMeter, MeterReading

# ---------------------------------------------------------------------------
# Bekannte Spalten-Zuordnungen (normalisierter Spaltenname → Ziel-Feld)
# ---------------------------------------------------------------------------

_COLUMN_HINTS = {
    "customer_number": [
        "kunden-nr.", "kundennr", "kunden nr", "kundennummer",
        # BOM-varianten
        'ï»¿""kunden-nr."""', "kunden-nr",
    ],
    "customer_name": [
        "kombinierter name", "kombinierter_name", "name",
    ],
    "name_last": ["nachname"],
    "name_first": ["vorname"],
    "property_name": ["objekt"],
    "property_type": ["typ"],
    "meter_number": ["zählernummer", "zahlernummer", "zähler-nr", "zaehlernummer", "zähler nr"],
    "meter_eichjahr": ["eichjahr"],
    "strasse": ["strasse", "straße"],
    "plz": ["plz"],
    "ort": ["ort"],
    "land": ["land"],
    "phone": ["telefon", "tel"],
    "email": ["e-mail", "email"],
    "notes": ["kommentar", "bemerkung", "notiz", "info"],
}


def _suggest_column(columns: list, target_key: str) -> str:
    """Gibt den besten CSV-Spaltennamen für ein Zielfeld zurück, oder ''."""
    candidates = _COLUMN_HINTS.get(target_key, [])
    for col in columns:
        normalized = col.strip().lower()
        for candidate in candidates:
            if normalized == candidate or candidate in normalized:
                return col
    return ""


def _build_suggestions(columns: list) -> dict:
    return {key: _suggest_column(columns, key) for key in _COLUMN_HINTS}


def _detect_stand_columns(columns: list) -> list:
    """Gibt Liste von (spaltenname, jahr) für alle 'Stand YYYY'-Spalten zurück."""
    result = []
    for col in columns:
        m = re.match(r"^Stand\s+(\d{4})$", col.strip(), re.IGNORECASE)
        if m:
            result.append((col, int(m.group(1))))
    return sorted(result, key=lambda x: x[1])


def _parse_austrian_number(raw: str):
    """Parst österreichisches Zahlenformat (z.B. '1.139,00' oder '1 139,00') zu Decimal."""
    if not raw or raw.strip().lower() in ("", "nan", "none"):
        return None
    raw = raw.strip()
    # Leerzeichen entfernen (Tausendertrennzeichen)
    raw = raw.replace(" ", "")
    # Wenn Komma UND Punkt: Punkt ist Tausendertrennzeichen → entfernen
    if "," in raw and "." in raw:
        raw = raw.replace(".", "")
    # Komma → Dezimalpunkt
    raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _get_cell(row_dict: dict, col_name: str) -> str:
    """Liest einen Zellwert aus dem Zeilen-Dict und gibt immer einen String zurück."""
    if not col_name:
        return ""
    val = row_dict.get(col_name, "")
    if val is None:
        return ""
    return str(val).strip()


def _delete_customer_cascade(customer, warnings: list) -> bool:
    """
    Löscht Kunden und alle abhängigen Daten (Objekte, Zähler, Ablesungen).
    Gibt True zurück wenn erfolgreich, False wenn Rechnungen vorhanden (→ nicht löschen).
    """
    if customer.invoices.count() > 0:
        warnings.append(
            f"Kunden-Nr. {customer.customer_number} ({customer.name}) hat Rechnungen "
            f"und kann nicht überschrieben werden – übersprungen."
        )
        return False

    # Ownerships + Properties (ohne Rechnungen)
    for ownership in customer.ownerships.all():
        prop = ownership.property
        if prop and prop.invoices.count() == 0:
            # Alle Zähler/Ablesungen via cascade löschen
            for meter in prop.meters.all():
                db.session.delete(meter)
            db.session.delete(prop)
        db.session.delete(ownership)

    db.session.delete(customer)
    db.session.flush()
    return True


def _run_import(df, col_map: dict, stand_columns: list, duplicate_mode: str) -> dict:
    """
    Führt den eigentlichen Import durch.
    Gibt dict mit Statistiken zurück.
    """
    results = {
        "customers_created": 0,
        "properties_created": 0,
        "meters_created": 0,
        "readings_created": 0,
        "rows_skipped": 0,
        "warnings": [],
        "errors": [],
    }

    col_cnum = col_map.get("customer_number", "")
    col_name = col_map.get("customer_name", "")
    col_last = col_map.get("name_last", "")
    col_first = col_map.get("name_first", "")
    col_prop = col_map.get("property_name", "")
    col_typ = col_map.get("property_type", "")
    col_meter = col_map.get("meter_number", "")
    col_eichjahr = col_map.get("meter_eichjahr", "")
    col_strasse = col_map.get("strasse", "")
    col_plz = col_map.get("plz", "")
    col_ort = col_map.get("ort", "")
    col_land = col_map.get("land", "")
    col_phone = col_map.get("phone", "")
    col_email = col_map.get("email", "")
    col_notes = col_map.get("notes", "")

    rows = df.to_dict(orient="records")

    for idx, row in enumerate(rows, start=2):  # Zeile 2 = erste Datenzeile nach Header
        sp = db.session.begin_nested()
        try:
            # --- Skip-Guard: Kunden-Nr. leer oder "Ergebnis"-Zeile ---
            raw_cnum = _get_cell(row, col_cnum)
            if not raw_cnum:
                results["rows_skipped"] += 1
                sp.rollback()
                continue

            # "Ergebnis"-Zeile am Ende überspringen
            first_val = str(list(row.values())[0]).strip() if row else ""
            if "ergebnis" in first_val.lower() or "ergebnis" in raw_cnum.lower():
                results["rows_skipped"] += 1
                sp.rollback()
                continue

            try:
                cnum = int(float(raw_cnum))
            except (ValueError, TypeError):
                results["errors"].append(f"Zeile {idx}: Ungültige Kunden-Nr. '{raw_cnum}'")
                results["rows_skipped"] += 1
                sp.rollback()
                continue

            # --- Storno-Zeilen überspringen ---
            notes_val = _get_cell(row, col_notes)
            info_val = _get_cell(row, "Info")
            if "storno" in notes_val.lower() or "storno" in info_val.lower():
                results["rows_skipped"] += 1
                sp.rollback()
                continue

            # --- Duplikat-Prüfung ---
            existing = Customer.query.filter_by(customer_number=cnum).first()
            if existing:
                if duplicate_mode == "skip":
                    results["warnings"].append(
                        f"Kunden-Nr. {cnum} ({existing.name}) bereits vorhanden – übersprungen."
                    )
                    results["rows_skipped"] += 1
                    sp.rollback()
                    continue
                else:  # overwrite
                    ok = _delete_customer_cascade(existing, results["warnings"])
                    if not ok:
                        results["rows_skipped"] += 1
                        sp.rollback()
                        continue

            # --- Kunden-Name ---
            raw_name = _get_cell(row, col_name)
            if not raw_name:
                last = _get_cell(row, col_last)
                first = _get_cell(row, col_first)
                raw_name = f"{last} {first}".strip() if (last or first) else f"Kunde {cnum}"

            # --- Customer erstellen ---
            customer = Customer(
                customer_number=cnum,
                name=raw_name,
                strasse=_get_cell(row, col_strasse),
                plz=_get_cell(row, col_plz),
                ort=_get_cell(row, col_ort),
                land=_get_cell(row, col_land) or "Österreich",
                email=_get_cell(row, col_email),
                phone=_get_cell(row, col_phone),
                notes=_get_cell(row, col_notes),
                active=True,
            )
            db.session.add(customer)
            db.session.flush()
            results["customers_created"] += 1

            # --- Property (Objekt) ---
            raw_objekt = _get_cell(row, col_prop)
            if not raw_objekt:
                raw_objekt = f"Objekt-{cnum}"

            raw_typ = _get_cell(row, col_typ).lower()
            object_type = "Sonstiges" if raw_typ in ("stall", "garten") else "Haus"

            prop = Property.query.filter_by(object_number=raw_objekt).first()
            if prop is None:
                prop = Property(
                    object_number=raw_objekt,
                    object_type=object_type,
                    strasse=raw_objekt,
                    active=True,
                )
                db.session.add(prop)
                db.session.flush()
                results["properties_created"] += 1

            # --- PropertyOwnership ---
            ownership = PropertyOwnership(
                property_id=prop.id,
                customer_id=customer.id,
                valid_from=date.today(),
                valid_to=None,
            )
            db.session.add(ownership)

            # --- WaterMeter ---
            raw_meter = _get_cell(row, col_meter)
            if not raw_meter:
                results["warnings"].append(
                    f"Zeile {idx} (Kunden-Nr. {cnum}): Keine Zählernummer – Zähler und Ablesungen übersprungen."
                )
                sp.commit()
                continue

            raw_eichjahr = _get_cell(row, col_eichjahr)
            eichjahr = None
            if raw_eichjahr:
                try:
                    eichjahr = int(float(raw_eichjahr))
                except (ValueError, TypeError):
                    pass

            existing_meter = WaterMeter.query.filter_by(meter_number=raw_meter).first()
            if existing_meter:
                results["warnings"].append(
                    f"Zeile {idx}: Zähler '{raw_meter}' existiert bereits – Zähler wird wiederverwendet."
                )
                meter = existing_meter
            else:
                meter = WaterMeter(
                    property_id=prop.id,
                    meter_number=raw_meter,
                    eichjahr=eichjahr,
                    active=True,
                )
                db.session.add(meter)
                db.session.flush()
                results["meters_created"] += 1

            # --- MeterReadings für alle Stand-Spalten ---
            previous_value = None
            for col_stand, year in stand_columns:
                raw_val = _get_cell(row, col_stand)
                if not raw_val:
                    continue

                parsed = _parse_austrian_number(raw_val)
                if parsed is None:
                    results["warnings"].append(
                        f"Zeile {idx}: Ungültiger Ablesewert '{raw_val}' für {col_stand} – übersprungen."
                    )
                    continue

                # Null-Wert ohne Vorjahr → neue Anschlüsse ohne Ablesung überspringen
                if parsed == 0 and previous_value is None:
                    continue

                # Verbrauch berechnen
                consumption = None
                if previous_value is not None:
                    consumption = parsed - previous_value

                # Vorhandene Ablesung überschreiben oder neu anlegen
                existing_reading = MeterReading.query.filter_by(
                    meter_id=meter.id, year=year
                ).first()
                if existing_reading:
                    existing_reading.value = parsed
                    existing_reading.consumption = consumption
                else:
                    reading = MeterReading(
                        meter_id=meter.id,
                        year=year,
                        value=parsed,
                        consumption=consumption,
                        reading_date=date(year, 12, 31),
                    )
                    db.session.add(reading)
                    results["readings_created"] += 1

                previous_value = parsed

            sp.commit()

        except Exception as exc:
            sp.rollback()
            results["errors"].append(f"Zeile {idx}: Unerwarteter Fehler – {exc}")

    db.session.commit()
    return results


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

@bp.route("/", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("import_csv/upload.html")

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Bitte eine Datei auswählen.", "warning")
        return redirect(url_for("import_csv.upload"))

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".csv", ".xlsx", ".xls"):
        flash("Ungültiges Dateiformat. Bitte CSV oder Excel (.xlsx/.xls) hochladen.", "danger")
        return redirect(url_for("import_csv.upload"))

    try:
        import pandas as pd
    except ImportError:
        flash("pandas ist nicht installiert. Bitte 'pip install pandas openpyxl' ausführen.", "danger")
        return redirect(url_for("import_csv.upload"))

    try:
        if ext == ".csv":
            df = pd.read_csv(
                file,
                sep=";",
                encoding="utf-8-sig",
                dtype=str,
                keep_default_na=False,
            )
        else:
            df = pd.read_excel(file, dtype=str, keep_default_na=False)
    except Exception as exc:
        flash(f"Fehler beim Lesen der Datei: {exc}", "danger")
        return redirect(url_for("import_csv.upload"))

    # Alle Spaltennamen und Zellwerte bereinigen
    df.columns = [str(c).strip() for c in df.columns]
    df = df.apply(lambda col: col.map(lambda x: x.strip() if isinstance(x, str) else x))

    # Pickle in instance/ speichern
    filename = f"import_{uuid.uuid4().hex}.pkl"
    filepath = os.path.join(current_app.instance_path, filename)
    df.to_pickle(filepath)
    session["import_file"] = filename

    return redirect(url_for("import_csv.mapping"))


@bp.route("/mapping", methods=["GET", "POST"])
@login_required
def mapping():
    filename = session.get("import_file")
    if not filename:
        flash("Keine hochgeladene Datei gefunden. Bitte neu hochladen.", "warning")
        return redirect(url_for("import_csv.upload"))

    filepath = os.path.join(current_app.instance_path, filename)
    if not os.path.exists(filepath):
        flash("Die hochgeladene Datei ist nicht mehr verfügbar. Bitte neu hochladen.", "warning")
        session.pop("import_file", None)
        return redirect(url_for("import_csv.upload"))

    try:
        import pandas as pd
        df = pd.read_pickle(filepath)
    except Exception as exc:
        flash(f"Fehler beim Laden der Datei: {exc}", "danger")
        return redirect(url_for("import_csv.upload"))

    columns = df.columns.tolist()
    stand_columns = _detect_stand_columns(columns)

    if request.method == "GET":
        suggestions = _build_suggestions(columns)
        preview = df.head(5).to_dict(orient="records")
        return render_template(
            "import_csv/mapping.html",
            columns=columns,
            suggestions=suggestions,
            stand_columns=stand_columns,
            preview=preview,
        )

    # POST: Import ausführen
    col_map = {}
    for key in _COLUMN_HINTS:
        val = request.form.get(f"col_{key}", "")
        col_map[key] = val if val else ""

    duplicate_mode = request.form.get("duplicate_mode", "skip")

    results = _run_import(df, col_map, stand_columns, duplicate_mode)

    # Aufräumen
    try:
        os.remove(filepath)
    except OSError:
        pass
    session.pop("import_file", None)

    session["import_result"] = results
    return redirect(url_for("import_csv.result"))


@bp.route("/result")
@login_required
def result():
    res = session.pop("import_result", None)
    if res is None:
        return redirect(url_for("import_csv.upload"))
    return render_template("import_csv/result.html", results=res)
