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


def _act(text, kind="info"):
    """Eine Aktion in der Vorschau. ``kind``: 'new' (wird angelegt),
    'update' (wird aktualisiert) oder 'info' (neutral)."""
    return {"text": text, "kind": kind}


def _plan_entry(idx, cnum, name, objekt, meter, category, label, actions):
    """Baut einen serialisierbaren Vorschau-Eintrag für eine CSV-Zeile."""
    return {
        "row": idx,
        "customer_number": str(cnum),
        "customer_name": name,
        "object_number": objekt,
        "meter_number": meter,
        "category": category,      # "import" | "exists" | "skip"
        "label": label,
        "actions": actions,        # Liste von Beschreibungs-Strings
    }


def _run_import(df, col_map: dict, stand_columns: list, duplicate_mode: str,
                dry_run: bool = False) -> dict:
    """
    Analysiert/importiert die CSV.

    **Additives Verhalten:** Jede Zeile ergänzt Daten, statt sie zu verwerfen.

    - Ein Kunde, der mehrfach in der CSV vorkommt oder bereits in der Datenbank
      liegt, wird wiederverwendet – seine zusätzlichen Objekte/Zähler/Ablesungen
      werden ergänzt.
    - ``duplicate_mode`` steuert nur, was mit bereits vorhandenen Daten geschieht:
      ``"overwrite"`` aktualisiert die Stammdaten eines bestehenden Kunden und
      vorhandene Ablesungen; ``"skip"`` lässt Bestehendes unverändert. Neue
      Objekte/Zähler/Ablesungen werden in **beiden** Modi ergänzt.

    Bei ``dry_run=True`` läuft der komplette Import durch, wird am Ende aber
    vollständig zurückgerollt – es bleibt nichts in der DB. So entspricht die
    Vorschau exakt dem späteren echten Import.

    Gibt dict mit Statistiken und – unter ``plan`` – einem Eintrag je Zeile zurück.
    """
    results = {
        "customers_created": 0,
        "customers_updated": 0,
        "properties_created": 0,
        "properties_reused": 0,
        "meters_created": 0,
        "meters_reused": 0,
        "ownerships_created": 0,
        "readings_created": 0,
        "readings_updated": 0,
        "rows_skipped": 0,
        "rows_unchanged": 0,
        "warnings": [],
        "errors": [],
        "plan": [],
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

    # Kunden-Nr. → Customer, die in DIESEM Lauf bereits angefasst wurden.
    seen_customers = {}

    # Äußere Transaktionsklammer um den gesamten Lauf. Notwendig, damit die
    # Zeilen-Savepoints darin geschachtelt sind: SQLite committet andernfalls
    # beim RELEASE des *äußersten* SAVEPOINT die Transaktion – ein dry_run
    # könnte dann nicht mehr sauber zurückgerollt werden.
    outer = db.session.begin_nested()

    for idx, row in enumerate(rows, start=2):  # Zeile 2 = erste Datenzeile nach Header
        sp = db.session.begin_nested()
        try:
            # --- Skip-Guard: Kunden-Nr. leer oder "Ergebnis"-Zeile ---
            raw_cnum = _get_cell(row, col_cnum)
            if not raw_cnum:
                results["rows_skipped"] += 1
                results["plan"].append(_plan_entry(
                    idx, "", "", "", "", "skip", "Nicht importiert",
                    [_act("Keine Kunden-Nr. – Zeile ignoriert")]))
                sp.rollback()
                continue

            # "Ergebnis"-Zeile am Ende überspringen
            first_val = str(list(row.values())[0]).strip() if row else ""
            if "ergebnis" in first_val.lower() or "ergebnis" in raw_cnum.lower():
                results["rows_skipped"] += 1
                results["plan"].append(_plan_entry(
                    idx, raw_cnum, "", "", "", "skip", "Nicht importiert",
                    [_act("Ergebnis-/Summenzeile – kein Datensatz")]))
                sp.rollback()
                continue

            try:
                cnum = int(float(raw_cnum))
            except (ValueError, TypeError):
                results["errors"].append(f"Zeile {idx}: Ungültige Kunden-Nr. '{raw_cnum}'")
                results["rows_skipped"] += 1
                results["plan"].append(_plan_entry(
                    idx, raw_cnum, "", "", "", "skip", "Fehler",
                    [_act(f"Ungültige Kunden-Nr. '{raw_cnum}'")]))
                sp.rollback()
                continue

            # --- Storno-Zeilen überspringen ---
            notes_val = _get_cell(row, col_notes)
            info_val = _get_cell(row, "Info")
            if "storno" in notes_val.lower() or "storno" in info_val.lower():
                results["rows_skipped"] += 1
                results["plan"].append(_plan_entry(
                    idx, cnum, "", "", "", "skip", "Nicht importiert",
                    [_act("Storno-Zeile – wird übersprungen")]))
                sp.rollback()
                continue

            # --- Kunden-Name ---
            raw_name = _get_cell(row, col_name)
            if not raw_name:
                last = _get_cell(row, col_last)
                first = _get_cell(row, col_first)
                raw_name = f"{last} {first}".strip() if (last or first) else f"Kunde {cnum}"

            raw_objekt = _get_cell(row, col_prop) or f"Objekt-{cnum}"
            raw_meter = _get_cell(row, col_meter)

            actions = []
            new_count = 0       # Anzahl neu angelegter Datensätze in dieser Zeile
            updated = False     # wurde Bestehendes aktualisiert?

            cust_fields = dict(
                name=raw_name,
                strasse=_get_cell(row, col_strasse),
                plz=_get_cell(row, col_plz),
                ort=_get_cell(row, col_ort),
                land=_get_cell(row, col_land) or "Österreich",
                email=_get_cell(row, col_email),
                rechnung_per_email=bool(_get_cell(row, col_email)),
                phone=_get_cell(row, col_phone),
                notes=_get_cell(row, col_notes),
            )

            # --- Kunde bestimmen (neu / aus DB / aus diesem Lauf) ---
            if cnum in seen_customers:
                customer = seen_customers[cnum]
                actions.append(_act(
                    f"Kunde Nr. {cnum} – kam in dieser Datei bereits vor"))
            else:
                customer = Customer.query.filter_by(customer_number=cnum).first()
                if customer is not None:
                    seen_customers[cnum] = customer
                    if duplicate_mode == "overwrite":
                        for key, val in cust_fields.items():
                            setattr(customer, key, val)
                        db.session.flush()
                        results["customers_updated"] += 1
                        updated = True
                        actions.append(_act(
                            f"Kunde Nr. {cnum} »{customer.name}« – bereits vorhanden, "
                            f"Stammdaten aktualisiert", "update"))
                    else:
                        actions.append(_act(
                            f"Kunde Nr. {cnum} »{customer.name}« – bereits vorhanden, "
                            f"Stammdaten unverändert"))
                else:
                    customer = Customer(customer_number=cnum, active=True, **cust_fields)
                    db.session.add(customer)
                    db.session.flush()
                    seen_customers[cnum] = customer
                    results["customers_created"] += 1
                    new_count += 1
                    actions.append(_act(
                        f"Kunde Nr. {cnum} »{raw_name}« – neu angelegt", "new"))

            # --- Property (Objekt) ---
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
                new_count += 1
                actions.append(_act(f"Objekt »{raw_objekt}« – neu angelegt", "new"))
            else:
                results["properties_reused"] += 1
                actions.append(_act(
                    f"Objekt »{raw_objekt}« – bereits vorhanden, wird verwendet"))

            # --- PropertyOwnership (Kunde ↔ Objekt) ---
            existing_ownership = PropertyOwnership.query.filter_by(
                property_id=prop.id, customer_id=customer.id, valid_to=None
            ).first()
            if existing_ownership is None:
                db.session.add(PropertyOwnership(
                    property_id=prop.id,
                    customer_id=customer.id,
                    valid_from=date.today(),
                    valid_to=None,
                ))
                db.session.flush()
                results["ownerships_created"] += 1
                new_count += 1
                actions.append(_act(
                    f"Objekt »{raw_objekt}« wird dem Kunden zugeordnet", "new"))
            else:
                actions.append(_act(
                    f"Zuordnung Kunde ↔ Objekt »{raw_objekt}« besteht bereits"))

            # --- WaterMeter + Ablesungen ---
            if not raw_meter:
                results["warnings"].append(
                    f"Zeile {idx} (Kunden-Nr. {cnum}): Keine Zählernummer – "
                    f"Zähler und Ablesungen übersprungen."
                )
                actions.append(_act(
                    "Keine Zählernummer – kein Zähler/keine Ablesungen"))
            else:
                raw_eichjahr = _get_cell(row, col_eichjahr)
                eichjahr = None
                if raw_eichjahr:
                    try:
                        eichjahr = int(float(raw_eichjahr))
                    except (ValueError, TypeError):
                        pass

                existing_meter = WaterMeter.query.filter_by(meter_number=raw_meter).first()
                if existing_meter:
                    meter = existing_meter
                    results["meters_reused"] += 1
                    actions.append(_act(
                        f"Zähler »{raw_meter}« – bereits vorhanden, "
                        f"wird wiederverwendet"))
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
                    new_count += 1
                    actions.append(_act(f"Zähler »{raw_meter}« – neu angelegt", "new"))

                # --- MeterReadings für alle Stand-Spalten ---
                previous_value = None
                readings_new = 0
                readings_upd = 0
                for col_stand, year in stand_columns:
                    raw_val = _get_cell(row, col_stand)
                    if not raw_val:
                        continue

                    parsed = _parse_austrian_number(raw_val)
                    if parsed is None:
                        results["warnings"].append(
                            f"Zeile {idx}: Ungültiger Ablesewert '{raw_val}' "
                            f"für {col_stand} – übersprungen."
                        )
                        continue

                    # Null-Wert ohne Vorjahr → neue Anschlüsse ohne Ablesung überspringen
                    if parsed == 0 and previous_value is None:
                        continue

                    # Verbrauch berechnen
                    consumption = None
                    if previous_value is not None:
                        consumption = parsed - previous_value

                    existing_reading = MeterReading.query.filter_by(
                        meter_id=meter.id, year=year
                    ).first()
                    if existing_reading is not None:
                        if duplicate_mode == "overwrite":
                            existing_reading.value = parsed
                            existing_reading.consumption = consumption
                            results["readings_updated"] += 1
                            readings_upd += 1
                            updated = True
                        # skip-Modus: vorhandene Ablesung unverändert lassen
                    else:
                        db.session.add(MeterReading(
                            meter_id=meter.id,
                            year=year,
                            value=parsed,
                            consumption=consumption,
                            reading_date=date(year, 12, 31),
                        ))
                        results["readings_created"] += 1
                        readings_new += 1
                        new_count += 1

                    previous_value = parsed

                if readings_new:
                    actions.append(_act(
                        f"{readings_new} Ablesung(en) werden angelegt", "new"))
                if readings_upd:
                    actions.append(_act(
                        f"{readings_upd} vorhandene Ablesung(en) werden aktualisiert",
                        "update"))

            # --- Zeile klassifizieren ---
            if new_count > 0 or updated:
                category, label = "import", "Wird importiert"
            else:
                category, label = "exists", "Bereits vorhanden"
                results["rows_unchanged"] += 1

            results["plan"].append(_plan_entry(
                idx, cnum, raw_name, raw_objekt, raw_meter, category, label, actions))

            sp.commit()

        except Exception as exc:
            sp.rollback()
            results["errors"].append(f"Zeile {idx}: Unerwarteter Fehler – {exc}")
            results["plan"].append(_plan_entry(
                idx, "", "", "", "", "skip", "Fehler",
                [_act(f"Unerwarteter Fehler: {exc}")]))

    if dry_run:
        outer.rollback()
        db.session.rollback()
    else:
        outer.commit()
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
    session.pop("import_col_map", None)
    session.pop("import_duplicate_mode", None)

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
        # Beim Zurück-Navigieren aus der Vorschau die zuvor getroffene
        # Zuordnung beibehalten, sonst automatisch vorschlagen.
        stored = session.get("import_col_map")
        suggestions = stored if stored else _build_suggestions(columns)
        preview = df.head(5).to_dict(orient="records")
        return render_template(
            "import_csv/mapping.html",
            columns=columns,
            suggestions=suggestions,
            stand_columns=stand_columns,
            preview=preview,
            duplicate_mode=session.get("import_duplicate_mode", "skip"),
        )

    # POST: Zuordnung übernehmen und zur Vorschau weiterleiten
    col_map = {}
    for key in _COLUMN_HINTS:
        val = request.form.get(f"col_{key}", "")
        col_map[key] = val if val else ""

    session["import_col_map"] = col_map
    session["import_duplicate_mode"] = request.form.get("duplicate_mode", "skip")
    return redirect(url_for("import_csv.preview"))


@bp.route("/preview", methods=["GET", "POST"])
@login_required
def preview():
    filename = session.get("import_file")
    col_map = session.get("import_col_map")
    if not filename or col_map is None:
        flash("Keine Import-Daten gefunden. Bitte neu hochladen.", "warning")
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

    stand_columns = _detect_stand_columns(df.columns.tolist())
    duplicate_mode = session.get("import_duplicate_mode", "skip")

    if request.method == "GET":
        # Probelauf: zeigt exakt, was der echte Import tun würde, ohne zu schreiben.
        plan = _run_import(df, col_map, stand_columns, duplicate_mode, dry_run=True)
        return render_template(
            "import_csv/preview.html",
            results=plan,
            duplicate_mode=duplicate_mode,
        )

    # POST: "Jetzt importieren" – echter Import
    results = _run_import(df, col_map, stand_columns, duplicate_mode, dry_run=False)

    # Aufräumen
    try:
        os.remove(filepath)
    except OSError:
        pass
    session.pop("import_file", None)
    session.pop("import_col_map", None)
    session.pop("import_duplicate_mode", None)

    # plan-Liste nicht in die (Cookie-)Session legen – kann sehr groß werden.
    results.pop("plan", None)
    session["import_result"] = results
    return redirect(url_for("import_csv.result"))


@bp.route("/result")
@login_required
def result():
    res = session.pop("import_result", None)
    if res is None:
        return redirect(url_for("import_csv.upload"))
    return render_template("import_csv/result.html", results=res)
