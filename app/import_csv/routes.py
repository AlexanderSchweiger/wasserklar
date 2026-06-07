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
from app.imports.common import resolve_contact_name, split_street_number
from app.imports.relations import OwnerConflictTracker, MeterObjectTracker
from app.models import (
    Customer, Property, PropertyOwnership, WaterMeter, MeterReading,
    BillingPeriod,
)

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
    "name_last": ["nachname", "familienname"],
    "name_first": ["vorname"],
    "salutation": ["anrede"],
    "is_company": [
        "firma", "kontaktart", "kontakttyp", "kundenart", "kundentyp",
        "rechtsform", "unternehmen", "company", "geschäftskunde",
    ],
    "property_name": ["objekt"],
    "property_type": ["typ"],
    "meter_number": ["zählernummer", "zahlernummer", "zähler-nr", "zaehlernummer", "zähler nr"],
    "meter_eichjahr": ["eichjahr"],
    "strasse": ["strasse", "straße"],
    "hausnummer": ["hausnummer", "hausnr", "haus-nr", "haus nr", "nr."],
    "plz": ["plz"],
    "ort": ["ort"],
    "land": ["land"],
    "phone": ["telefon", "tel"],
    "email": ["e-mail", "email"],
    "notes": ["kommentar", "bemerkung", "notiz", "info"],
    # WG-spezifisch (nur im Genossenschafts-Modus gemappt/angewendet)
    "wg_status": ["status", "mitgliedsstatus", "mitglieds-status"],
    "member_since": ["mitglied seit", "mitglied-seit", "beitritt", "beitrittsdatum", "eintritt"],
    "member_until": ["mitglied bis", "mitglied-bis", "austritt", "austrittsdatum"],
    "property_shares": ["anteile", "anteil"],
    "property_area": ["fläche", "flaeche", "quadratmeter", "m2", "fläche m2", "fläche (m2)"],
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
    'update' (wird aktualisiert), 'info' (neutral) oder 'warn' (Warnung)."""
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


def _parse_import_date(raw: str):
    """Parst ein Import-Datum (de/iso/Excel) zu einem date-Objekt, sonst None."""
    from app.imports.common import parse_date
    return parse_date(raw, "de") if raw else None


def _parse_import_int(raw: str):
    """Parst eine (oesterr. formatierte) Zahl zu int, sonst None."""
    from app.imports.common import parse_number
    n = parse_number(raw, "at_de") if raw else None
    return int(n) if n is not None else None


def _apply_wg_customer(customer, row, cols, actions):
    """Setzt im WG-Modus Mitglied-seit (Customer) sowie Status und Mitglied-bis
    (CustomerWgProfile). Nur gemappte Spalten werden angefasst."""
    from app.wg import parse_status
    col_status, col_since, col_until = cols
    if col_since:
        d = _parse_import_date(_get_cell(row, col_since))
        if d is not None:
            customer.member_since = d
    if col_status or col_until:
        profile = customer.ensure_wg_profile()
        if col_status:
            st = parse_status(_get_cell(row, col_status))
            if st:
                profile.status = st
                actions.append(_act(f"WG-Status »{st}« gesetzt", "update"))
        if col_until:
            raw_until = _get_cell(row, col_until)
            if raw_until:
                profile.member_until = _parse_import_date(raw_until)


def _apply_wg_property(prop, row, cols, actions):
    """Setzt im WG-Modus Anteile + Fläche (PropertyWgProfile)."""
    col_shares, col_area = cols
    shares = _parse_import_int(_get_cell(row, col_shares)) if col_shares else None
    area = _parse_import_int(_get_cell(row, col_area)) if col_area else None
    if shares is None and area is None:
        return
    profile = prop.ensure_wg_profile()
    if shares is not None:
        profile.shares = shares
    if area is not None:
        profile.area_m2 = area
    bits = []
    if shares is not None:
        bits.append(f"{shares} Anteil(e)")
    if area is not None:
        bits.append(f"{area} m²")
    actions.append(_act("WG-Liegenschaft: " + ", ".join(bits), "update"))


def _run_import(df, col_map: dict, stand_columns: list, duplicate_mode: str,
                dry_run: bool = False, is_wg: bool = False) -> dict:
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
        "periods_created": 0,
        "readings_created": 0,
        "readings_updated": 0,
        "rows_skipped": 0,
        "rows_unchanged": 0,
        "warnings": [],
        "errors": [],
        "plan": [],
    }

    owner_tracker = OwnerConflictTracker()
    meter_tracker = MeterObjectTracker()

    col_cnum = col_map.get("customer_number", "")
    col_name = col_map.get("customer_name", "")
    col_last = col_map.get("name_last", "")
    col_first = col_map.get("name_first", "")
    col_salutation = col_map.get("salutation", "")
    col_is_company = col_map.get("is_company", "")
    col_prop = col_map.get("property_name", "")
    col_typ = col_map.get("property_type", "")
    col_meter = col_map.get("meter_number", "")
    col_eichjahr = col_map.get("meter_eichjahr", "")
    col_strasse = col_map.get("strasse", "")
    col_hausnummer = col_map.get("hausnummer", "")
    col_plz = col_map.get("plz", "")
    col_ort = col_map.get("ort", "")
    col_land = col_map.get("land", "")
    col_phone = col_map.get("phone", "")
    col_email = col_map.get("email", "")
    col_notes = col_map.get("notes", "")
    # WG-spezifisch (nur im Genossenschafts-Modus angewendet)
    wg_customer_cols = (
        col_map.get("wg_status", ""),
        col_map.get("member_since", ""),
        col_map.get("member_until", ""),
    )
    wg_property_cols = (
        col_map.get("property_shares", ""),
        col_map.get("property_area", ""),
    )

    # Äußere Transaktionsklammer vor alle DB-Schreibzugriffe ziehen — auch
    # vor die Period-Erstellung, damit dry_run alles sauber zurückrollt.
    outer = db.session.begin_nested()

    # Jede "Stand YYYY"-Spalte einer Abrechnungsperiode zuordnen.
    # Existiert keine passende Periode, wird automatisch eine Kalender-Periode
    # (01.01.YYYY – 31.12.YYYY) angelegt.  Falls vor dem Import noch keine
    # aktive Periode existierte, wird die neueste auto-erstellte aktiviert.
    had_active = BillingPeriod.query.filter_by(active=True).first() is not None
    auto_created: list[tuple[int, BillingPeriod]] = []

    period_by_year: dict[int, int] = {}
    for _col, _year in stand_columns:
        if _year in period_by_year:
            continue
        _eoy = date(_year, 12, 31)
        _p = (
            BillingPeriod.query
            .filter(BillingPeriod.start_date <= _eoy,
                    BillingPeriod.end_date >= _eoy)
            .order_by(BillingPeriod.start_date.desc())
            .first()
        )
        if _p is None:
            _p = BillingPeriod(
                name=str(_year),
                start_date=date(_year, 1, 1),
                end_date=date(_year, 12, 31),
            )
            db.session.add(_p)
            db.session.flush()
            auto_created.append((_year, _p))
            results["periods_created"] += 1
            results["warnings"].append(
                f"Keine Abrechnungsperiode für Jahr {_year} gefunden – "
                f"Periode '{_year}' (01.01.{_year}–31.12.{_year}) automatisch angelegt."
            )
        period_by_year[_year] = _p.id

    if not had_active and auto_created:
        newest = max(auto_created, key=lambda x: x[0])
        newest[1].activate()

    rows = df.to_dict(orient="records")

    # Kunden-Nr. → Customer, die in DIESEM Lauf bereits angefasst wurden.
    seen_customers = {}

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

            # --- Kunden-Name (Firma/Person + Anrede + Vor-/Nachname) ---
            # Gemeinsamer Resolver: kombinierter Name = "Nachname Vorname";
            # ein einzelnes, nicht aufgespaltenes Namensfeld → Nachname.
            resolved = resolve_contact_name(
                combined=_get_cell(row, col_name),
                last=_get_cell(row, col_last),
                first=_get_cell(row, col_first),
                salutation=_get_cell(row, col_salutation),
                company=_get_cell(row, col_is_company),
            )
            raw_name = resolved["name"] or f"Kunde {cnum}"
            name_mapped = bool(col_name or col_last or col_first)

            raw_objekt = _get_cell(row, col_prop) or f"Objekt-{cnum}"
            raw_meter = _get_cell(row, col_meter)

            actions = []
            new_count = 0       # Anzahl neu angelegter Datensätze in dieser Zeile
            updated = False     # wurde Bestehendes aktualisiert?

            # Straße + Hausnummer ggf. aus einem kombinierten Feld trennen
            # (number-last); eine eigene Hausnummer-Spalte hat Vorrang.
            _strasse, _hausnummer = split_street_number(
                _get_cell(row, col_strasse), _get_cell(row, col_hausnummer)
            )

            # Adress-/Kontaktfelder werden im overwrite-Modus immer gesetzt;
            # die Namens-/Typ-Felder weiter unten nur, wenn ihre Spalte gemappt ist.
            cust_fields = dict(
                strasse=_strasse,
                hausnummer=_hausnummer,
                plz=_get_cell(row, col_plz),
                ort=_get_cell(row, col_ort),
                land=_get_cell(row, col_land) or "Österreich",
                email=_get_cell(row, col_email),
                rechnung_per_email=bool(_get_cell(row, col_email)),
                phone=_get_cell(row, col_phone),
                notes=_get_cell(row, col_notes),
            )

            def _apply_name_fields(cust, *, is_new):
                """Setzt Name-Aufspaltung + Firma/Person aus ``resolved``.

                Bei Neuanlage werden alle Werte übernommen (Firma/Person inkl. der
                Anrede-'Firma'-Heuristik). Im overwrite-Modus nur, was gemappt ist
                — Namensfelder bei gemappter Namensspalte, ``is_company`` bei
                gemappter Firma-Spalte. Eine Firma räumt Anrede/Vor-/Nachnamen ab,
                damit letter_name/salutation_line den Firmennamen nutzen."""
                if is_new:
                    cust.name = raw_name
                    cust.is_company = resolved["is_company"]
                    cust.salutation = resolved["salutation"] or None
                    cust.first_name = resolved["first_name"] or None
                    cust.last_name = resolved["last_name"] or None
                else:
                    if name_mapped:
                        cust.name = raw_name
                        cust.salutation = resolved["salutation"] or None
                        cust.first_name = resolved["first_name"] or None
                        cust.last_name = resolved["last_name"] or None
                    if col_is_company:
                        cust.is_company = resolved["is_company"]
                if cust.is_company:
                    cust.salutation = None
                    cust.first_name = None
                    cust.last_name = None

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
                        _apply_name_fields(customer, is_new=False)
                        db.session.flush()
                        if is_wg:
                            _apply_wg_customer(customer, row, wg_customer_cols, actions)
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
                    _apply_name_fields(customer, is_new=True)
                    db.session.add(customer)
                    db.session.flush()
                    seen_customers[cnum] = customer
                    if is_wg:
                        _apply_wg_customer(customer, row, wg_customer_cols, actions)
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
                if is_wg:
                    _apply_wg_property(prop, row, wg_property_cols, actions)
                results["properties_created"] += 1
                new_count += 1
                actions.append(_act(f"Objekt »{raw_objekt}« – neu angelegt", "new"))
            else:
                if is_wg and duplicate_mode == "overwrite":
                    _apply_wg_property(prop, row, wg_property_cols, actions)
                    db.session.flush()
                results["properties_reused"] += 1
                actions.append(_act(
                    f"Objekt »{raw_objekt}« – bereits vorhanden, wird verwendet"))

            # --- PropertyOwnership (Kunde ↔ Objekt) ---
            existing_ownership = PropertyOwnership.query.filter_by(
                property_id=prop.id, customer_id=customer.id, valid_to=None
            ).first()
            if existing_ownership is None:
                existing_owner_keys = [
                    o.customer_id for o in
                    PropertyOwnership.query.filter_by(
                        property_id=prop.id, valid_to=None
                    ).all()
                ]
                warn = owner_tracker.check_and_register(
                    prop.id, customer.id, existing_owner_keys
                )
                if warn:
                    results["warnings"].append(f"Zeile {idx}: {warn}")
                    actions.append(_act(warn, "warn"))
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
                    existing_object_key = existing_meter.property_id
                    warn = meter_tracker.check_and_register(
                        raw_meter, prop.id, existing_object_key
                    )
                    if warn:
                        results["warnings"].append(f"Zeile {idx}: {warn}")
                        actions.append(_act(warn, "warn"))
                    results["meters_reused"] += 1
                    actions.append(_act(
                        f"Zähler »{raw_meter}« – bereits vorhanden, "
                        f"wird wiederverwendet"))
                else:
                    warn = meter_tracker.check_and_register(
                        raw_meter, prop.id, None
                    )
                    if warn:
                        results["warnings"].append(f"Zeile {idx}: {warn}")
                        actions.append(_act(warn, "warn"))
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

                    bp_id = period_by_year[year]

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
                        meter_id=meter.id, billing_period_id=bp_id
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
                            billing_period_id=bp_id,
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
    from app.settings_service import is_wassergenossenschaft
    is_wg = is_wassergenossenschaft()

    if request.method == "GET":
        # Probelauf: zeigt exakt, was der echte Import tun würde, ohne zu schreiben.
        plan = _run_import(df, col_map, stand_columns, duplicate_mode,
                           dry_run=True, is_wg=is_wg)
        return render_template(
            "import_csv/preview.html",
            results=plan,
            duplicate_mode=duplicate_mode,
        )

    # POST: "Jetzt importieren" – echter Import
    results = _run_import(df, col_map, stand_columns, duplicate_mode,
                          dry_run=False, is_wg=is_wg)

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
