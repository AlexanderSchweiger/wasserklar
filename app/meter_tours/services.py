"""Services fuer Zaehlertausch-Touren.

Faellige Zaehler (Nacheichfrist), Luftlinien-Routing (Nearest-Neighbour +
2-Opt auf Haversine) und der Tour-/Stop-Lebenszyklus. Bewusst ohne externe
Routing-API: die Reihenfolge ist eine Luftlinien-Heuristik, echte
Strassen-Navigation uebernimmt das Navi des Geraets per Deep-Link pro Stopp.
"""
from datetime import date, datetime

from app.extensions import db
from app.models import (
    AppSetting, MeterReplacement, MeterTour, MeterTourStop,
    Property, PropertyOwnership, WaterMeter,
)
from app.network.services import haversine_m

# Oesterreichische Nacheichfrist fuer Kaltwasserzaehler (MEG): 5 Jahre.
CALIBRATION_INTERVAL_DEFAULT = 5

SETTING_INTERVAL = "meter_tours.calibration_interval_years"
SETTING_FEE_DESCRIPTION = "meter_tours.fee_description"
SETTING_FEE_AMOUNT = "meter_tours.fee_amount"
SETTING_FEE_TAX_RATE = "meter_tours.fee_tax_rate"
SETTING_NOTIFY_SUBJECT = "meter_tours.notify_subject"
SETTING_NOTIFY_BODY = "meter_tours.notify_body"

FEE_DESCRIPTION_DEFAULT = "Zählertausch-Pauschale"
FEE_TAX_RATE_DEFAULT = "10"  # AT: Wasser 10 %

NOTIFY_SUBJECT_DEFAULT = "Zählertausch am {datum}"
NOTIFY_BODY_DEFAULT = (
    "{anrede}\n"
    "\n"
    "im Zuge der gesetzlich vorgeschriebenen Nacheichung tauschen wir am "
    "{datum} ({zeitfenster}) den Wasserzähler an folgendem Objekt:\n"
    "\n"
    "{objekt}\n"
    "Zähler: {zaehlernummer}\n"
    "\n"
    "Wir bitten Sie, den Zugang zum Zähler an diesem Tag zu ermöglichen. "
    "Sollten Sie verhindert sein, melden Sie sich bitte bei uns.\n"
    "\n"
    "Vielen Dank!"
)


class TourError(ValueError):
    """Fachlicher Fehler beim Anlegen/Bearbeiten einer Tour (Flash-tauglich)."""


def calibration_interval_years():
    raw = AppSetting.get(SETTING_INTERVAL, str(CALIBRATION_INTERVAL_DEFAULT))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return CALIBRATION_INTERVAL_DEFAULT
    return value if value > 0 else CALIBRATION_INTERVAL_DEFAULT


def open_tour_meter_ids():
    """Zaehler-IDs, die als offener (pending) Stop in einer geplanten oder
    aktiven Tour stecken — die schliesst die Faelligen-Liste aus, damit ein
    Zaehler nicht in zwei Touren gleichzeitig landet."""
    rows = (
        db.session.query(MeterTourStop.meter_id)
        .join(MeterTour, MeterTourStop.tour_id == MeterTour.id)
        .filter(MeterTour.status.in_(
            [MeterTour.STATUS_PLANNED, MeterTour.STATUS_ACTIVE]))
        .filter(MeterTourStop.status == MeterTourStop.STATUS_PENDING)
        .all()
    )
    return {r[0] for r in rows}


def owners_by_property(property_ids):
    """Aktuelle Eigentuemer je Objekt, batch-geladen (kein N+1).

    Mehrere parallele aktive Ownerships pro Objekt sind erlaubt (Ehepaare,
    Erbengemeinschaften) — daher Liste je Property, niemals ``.scalar()``.
    """
    result = {pid: [] for pid in property_ids}
    if not property_ids:
        return result
    rows = (
        PropertyOwnership.query
        .filter(PropertyOwnership.property_id.in_(list(property_ids)))
        .filter(PropertyOwnership.valid_to.is_(None))
        .all()
    )
    for o in rows:
        result.setdefault(o.property_id, []).append(o.customer)
    return result


def due_meters(*, due_until_year=None, q="", include_toured=False):
    """Faellige Zaehlertausche: aktive Hauptdaten-Zaehler mit
    ``eichjahr + Intervall <= due_until_year``.

    Reiner Integer-Vergleich auf ``eichjahr`` — dialekt-portabel. Rueckgabe:
    Liste von Dicts ``{meter, property, owners, due_year}``, sortiert nach
    Faelligkeit, dann Adresse. ``q`` filtert Freitext ueber Zaehlernummer,
    Objekt (Nummer/Adresse) und Eigentuemernamen (Python-seitig — die
    Ergebnismenge ist klein und der Owner-Join waere sonst dialektheikel).
    """
    interval = calibration_interval_years()
    if due_until_year is None:
        due_until_year = date.today().year
    threshold = due_until_year - interval

    query = (
        WaterMeter.query
        .join(Property, WaterMeter.property_id == Property.id)
        .filter(WaterMeter.active.is_(True))
        .filter(WaterMeter.eichjahr.isnot(None))
        .filter(WaterMeter.eichjahr <= threshold)
    )
    meters = query.all()

    if not include_toured:
        blocked = open_tour_meter_ids()
        meters = [m for m in meters if m.id not in blocked]

    owners = owners_by_property({m.property_id for m in meters})

    rows = []
    for m in meters:
        rows.append({
            "meter": m,
            "property": m.property,
            "owners": owners.get(m.property_id, []),
            "due_year": (m.eichjahr or 0) + interval,
        })

    needle = (q or "").strip().lower()
    if needle:
        def _matches(row):
            hay = [
                row["meter"].meter_number or "",
                row["property"].object_number or "",
                row["property"].address_display() or "",
            ]
            hay.extend(c.name or "" for c in row["owners"])
            return any(needle in h.lower() for h in hay)
        rows = [r for r in rows if _matches(r)]

    rows.sort(key=lambda r: (
        r["due_year"],
        (r["property"].ort or ""),
        (r["property"].strasse or ""),
        (r["property"].hausnummer or ""),
    ))
    return rows


# ---------------------------------------------------------------------------
# Luftlinien-Routing (Nearest-Neighbour + 2-Opt auf Haversine)
# ---------------------------------------------------------------------------

def nearest_neighbour_order(start, points):
    """Greedy-Reihenfolge ab ``start``: immer zum naechsten noch offenen Punkt.

    ``start`` = (lat, lng); ``points`` = {id: (lat, lng)}. Liefert die IDs in
    Besuchsreihenfolge.
    """
    remaining = dict(points)
    order = []
    cur = start
    while remaining:
        nid = min(
            remaining,
            key=lambda i: haversine_m(cur[0], cur[1],
                                      remaining[i][0], remaining[i][1]),
        )
        order.append(nid)
        cur = remaining.pop(nid)
    return order


def route_length_m(order, start, points):
    """Gesamt-Luftlinienlaenge der Route Start -> Stops in Reihenfolge."""
    total = 0.0
    cur = start
    for i in order:
        p = points[i]
        total += haversine_m(cur[0], cur[1], p[0], p[1])
        cur = p
    return total


def two_opt(order, start, points, max_rounds=10):
    """Klassisches 2-Opt: Kanten-Paare tauschen, solange die Gesamtlaenge
    sinkt (max. ``max_rounds`` Durchlaeufe). O(n²) pro Runde — fuer Touren
    mit Dutzenden Stops serverseitig voellig unkritisch."""
    if len(order) < 3:
        return list(order)
    best = list(order)
    best_len = route_length_m(best, start, points)
    for _ in range(max_rounds):
        improved = False
        for i in range(len(best) - 1):
            for k in range(i + 1, len(best)):
                candidate = best[:i] + best[i:k + 1][::-1] + best[k + 1:]
                cand_len = route_length_m(candidate, start, points)
                if cand_len < best_len - 1e-9:
                    best, best_len = candidate, cand_len
                    improved = True
        if not improved:
            break
    return best


def plan_route(start_lat, start_lng, items):
    """Reihenfolge fuer eine Stop-Menge planen.

    ``items`` = Liste ``(id, lat, lng)`` — lat/lng duerfen None sein.
    Rueckgabe ``(ordered_ids, ungeocoded_ids)``: geocodete Stops in
    NN+2-Opt-Reihenfolge, Stops ohne Koordinaten ans Ende (im UI markiert;
    ein spaeterer BEV-Abgleich heilt sie, weil Koordinaten live vom Objekt
    kommen).
    """
    points = {i: (lat, lng) for i, lat, lng in items
              if lat is not None and lng is not None}
    ungeocoded = [i for i, lat, lng in items
                  if lat is None or lng is None]
    if not points:
        return [], ungeocoded

    if start_lat is None or start_lng is None:
        # Ohne Startpunkt: erster geocodeter Stop als Anker.
        first_id = next(iter(points))
        start = points[first_id]
    else:
        start = (start_lat, start_lng)

    order = nearest_neighbour_order(start, points)
    order = two_opt(order, start, points)
    return order, ungeocoded


# ---------------------------------------------------------------------------
# Tour-Lebenszyklus
# ---------------------------------------------------------------------------

def create_tour(*, name, planned_date=None, time_window=None, start_lat=None,
                start_lng=None, start_address=None, meter_ids, created_by_id=None,
                notes=None):
    """Tour + Stops anlegen; Positionen aus ``plan_route``. Flusht, committet
    nicht. Wirft ``TourError`` bei fachlichen Problemen."""
    ids = [int(i) for i in meter_ids]
    if not ids:
        raise TourError("Keine Zähler ausgewählt.")

    meters = WaterMeter.query.filter(WaterMeter.id.in_(ids)).all()
    found = {m.id for m in meters}
    missing = [i for i in ids if i not in found]
    if missing:
        raise TourError("Mindestens ein gewählter Zähler existiert nicht mehr.")
    inactive = [m for m in meters if not m.active]
    if inactive:
        raise TourError(
            "Zähler " + ", ".join(m.meter_number for m in inactive)
            + " ist nicht mehr aktiv (bereits getauscht?).")
    blocked = open_tour_meter_ids() & found
    if blocked:
        nums = [m.meter_number for m in meters if m.id in blocked]
        raise TourError(
            "Zähler " + ", ".join(nums)
            + " steckt bereits in einer offenen Tour.")

    tour = MeterTour(
        name=name.strip() or f"Tour {date.today().strftime('%d.%m.%Y')}",
        planned_date=planned_date,
        time_window=(time_window or "").strip() or None,
        start_lat=start_lat,
        start_lng=start_lng,
        start_address=(start_address or "").strip() or None,
        notes=(notes or "").strip() or None,
        created_by_id=created_by_id,
    )
    db.session.add(tour)
    db.session.flush()

    by_id = {m.id: m for m in meters}
    items = [(m.id, m.property.lat if m.property else None,
              m.property.lng if m.property else None) for m in meters]
    ordered, ungeocoded = plan_route(start_lat, start_lng, items)

    position = 0
    for mid in list(ordered) + list(ungeocoded):
        position += 1
        m = by_id[mid]
        db.session.add(MeterTourStop(
            tour_id=tour.id,
            meter_id=m.id,
            property_id=m.property_id,
            position=position,
        ))
    db.session.flush()
    return tour


def reorder_pending_stops(tour, start_lat, start_lng):
    """Offene (pending) Stops ab neuem Startpunkt neu nummerieren; erledigte/
    uebersprungene Stops behalten ihre Relativreihenfolge dahinter."""
    pending = [s for s in tour.stops if s.status == MeterTourStop.STATUS_PENDING]
    others = [s for s in tour.stops if s.status != MeterTourStop.STATUS_PENDING]

    items = [(s.id, s.property.lat if s.property else None,
              s.property.lng if s.property else None) for s in pending]
    ordered, ungeocoded = plan_route(start_lat, start_lng, items)

    by_id = {s.id: s for s in pending}
    position = 0
    for sid in list(ordered) + list(ungeocoded):
        position += 1
        by_id[sid].position = position
    for s in sorted(others, key=lambda s: s.position):
        position += 1
        s.position = position

    if start_lat is not None and start_lng is not None:
        tour.start_lat = start_lat
        tour.start_lng = start_lng


def move_stop(tour, stop, direction):
    """Manuelles Umsortieren: tauscht die Position mit dem Nachbarn in der
    aktuellen Reihenfolge (``direction`` = 'up' | 'down'). Die automatische
    Luftlinien-Reihenfolge ist nur ein Startvorschlag — der Nutzer kennt die
    Strassen und darf selbst umordnen. Liefert False am Rand (kein Nachbar)."""
    ordered = sorted(tour.stops, key=lambda s: s.position)
    idx = ordered.index(stop)
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if swap_idx < 0 or swap_idx >= len(ordered):
        return False
    other = ordered[swap_idx]
    stop.position, other.position = other.position, stop.position
    return True


def complete_stop_from_replacement(stop):
    """Stop als erledigt markieren, wenn zu seinem Zaehler ein Tausch-Event
    existiert. Der Lookup laeuft ueber den UNIQUE ``old_meter_id`` — der
    Server vertraut nie einer vom Client gelieferten Replacement-ID.
    Idempotent; liefert True, wenn der Stop (jetzt) erledigt ist."""
    if stop.status == MeterTourStop.STATUS_DONE:
        return True
    repl = MeterReplacement.query.filter_by(old_meter_id=stop.meter_id).first()
    if repl is None:
        return False
    stop.replacement_id = repl.id
    stop.status = MeterTourStop.STATUS_DONE
    stop.completed_at = stop.completed_at or datetime.utcnow()
    return True


def sync_tour_completions(tour):
    """Self-Heal: pending Stops abhaken, deren Zaehler ausserhalb des
    Tour-Kontexts getauscht wurde (z.B. ueber die Zaehlerliste oder den
    409-Pfad von ``meter_replace``). Liefert die Anzahl neu erledigter."""
    healed = 0
    for stop in tour.stops:
        if stop.status != MeterTourStop.STATUS_PENDING:
            continue
        if complete_stop_from_replacement(stop):
            healed += 1
    return healed


# ---------------------------------------------------------------------------
# Ankuendigung (Vorab-Info an die Ziele)
# ---------------------------------------------------------------------------

def notify_defaults():
    return {
        "subject": AppSetting.get(SETTING_NOTIFY_SUBJECT, NOTIFY_SUBJECT_DEFAULT),
        "body": AppSetting.get(SETTING_NOTIFY_BODY, NOTIFY_BODY_DEFAULT),
    }


def render_notify_text(template, *, customer, stops, tour):
    """Platzhalter fuellen: {anrede} {name} {adresse} {objekt}
    {zaehlernummer} {datum} {zeitfenster}. Mehrere Stops desselben Kunden
    werden kommagetrennt zusammengefasst."""
    datum = (tour.planned_date.strftime("%d.%m.%Y")
             if tour.planned_date else "(Termin folgt)")
    objekte = ", ".join(
        s.property.label() if s.property else "?" for s in stops)
    zaehler = ", ".join(
        s.meter.meter_number if s.meter else "?" for s in stops)
    adresse = ", ".join(
        s.property.address_display() if s.property else "?" for s in stops)
    values = {
        "anrede": customer.salutation_line,
        "name": customer.letter_name,
        "adresse": adresse,
        "objekt": objekte,
        "zaehlernummer": zaehler,
        "datum": datum,
        "zeitfenster": tour.time_window or "ganztägig",
    }
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val or ""))
    return out


def send_stop_notification(customer, subject, body, channel="email"):
    """Kanal-agnostischer Versand der Vorab-Info.

    Aktuell ist nur ``email`` implementiert; ``sms`` ist der vorgesehene
    Erweiterungspunkt (eigener Task) — neue Kanaele hier ergaenzen, die
    Routen bleiben unveraendert. Wirft bei Versandfehlern (Aufrufer faengt
    und meldet pro Empfaenger)."""
    if channel != "email":
        raise ValueError(f"Unbekannter Benachrichtigungskanal: {channel}")
    from flask_mail import Message
    from app.settings_service import send_mail

    msg = Message(subject=subject, recipients=[customer.email], body=body)
    send_mail(msg)
