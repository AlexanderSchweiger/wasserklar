"""Wiederverwendbare Service-Helpers fuer Zaehlerstaende.

Extrahiert aus ``meters.routes.bulk_read``, damit die SaaS-Self-Service-
Erfassung dieselbe Verbrauchs- und Storage-Logik nutzt — ohne den OSS-
Code zu duplizieren.

Verbrauch (``consumption``) ist ein Pro-Ablesung-Delta: ``value`` minus dem
Wert der vorigen Ablesung desselben Zaehlers (chronologisch nach
``reading_date``), bzw. minus ``initial_value`` des Zaehlers, wenn es keine
Vorablesung gibt. Der Wert wird beim Speichern eingefroren.

Convention: Caller ist fuer ``db.session.commit()`` zustaendig. So kann
ein Bulk-Aufrufer alle Eintraege in einer Transaktion speichern.
"""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import MeterReading, WaterMeter, BillingPeriod


def previous_reading(meter, reading_date, *, exclude_id=None):
    """Die juengste Ablesung des Zaehlers VOR ``reading_date``.

    Tiebreak ueber ``id`` absteigend, falls zwei Ablesungen auf dasselbe
    Datum fallen. ``exclude_id`` blendet eine bestimmte Ablesung aus (z.B.
    die gerade bearbeitete).
    """
    q = MeterReading.query.filter(
        MeterReading.meter_id == meter.id,
        MeterReading.reading_date < reading_date,
    )
    if exclude_id is not None:
        q = q.filter(MeterReading.id != exclude_id)
    return q.order_by(
        MeterReading.reading_date.desc(), MeterReading.id.desc()
    ).first()


def compute_consumption_for(reading):
    """Verbrauch fuer ``reading`` berechnen — ``value`` minus Vorablesung
    (nach Datum) bzw. minus ``initial_value`` des Zaehlers.

    Gibt einen ``Decimal`` oder ``None`` zurueck; weist NICHTS zu.
    """
    if reading.value is None:
        return None
    meter = reading.meter or db.session.get(WaterMeter, reading.meter_id)
    if meter is None:
        return None
    prev = previous_reading(
        meter, reading.reading_date, exclude_id=reading.id
    )
    if prev is not None:
        return reading.value - prev.value
    if meter.initial_value is not None:
        return reading.value - meter.initial_value
    return None


def recompute_meter_chain(meter):
    """Verbrauch ALLER Ablesungen eines Zaehlers neu berechnen.

    Laedt die Ablesungen in Abrechnungsperioden-Reihenfolge (nach
    ``BillingPeriod.start_date``) und setzt ``consumption`` jeder Ablesung
    auf die Differenz zur vorigen (bzw. zu ``initial_value`` bei der ersten
    Ablesung). Robuste Variante fuer Bulk-Importe, nachtraegliche Eingaben
    und ``save_reading`` — immer korrekt, unabhaengig vom tatsaechlichen
    Eingabedatum (``reading_date``). Caller committet.
    """
    rows = (
        MeterReading.query
        .join(BillingPeriod, BillingPeriod.id == MeterReading.billing_period_id)
        .filter(MeterReading.meter_id == meter.id)
        .order_by(BillingPeriod.start_date.asc(), MeterReading.reading_date.asc(), MeterReading.id.asc())
        .all()
    )
    prev_value = meter.initial_value
    for r in rows:
        r.consumption = (
            (r.value - prev_value) if (prev_value is not None and r.value is not None)
            else None
        )
        if r.value is not None:
            prev_value = r.value
    return rows


def save_reading(
    meter: WaterMeter,
    billing_period: BillingPeriod,
    value: Decimal,
    *,
    created_by_id: int = None,
    entered_via_self_service: bool = False,
    self_service_code_id: int = None,
    reading_date: date = None,
    is_estimated: bool = False,
) -> MeterReading:
    """Legt einen Zaehlerstand an oder aktualisiert den existierenden
    Eintrag fuer ``(meter, billing_period)``. Berechnet den Verbrauch der
    gesamten Zaehlerkette neu (gegen die vorige Ablesung nach Datum bzw.
    ``initial_value``).

    ``is_estimated`` markiert den Stand als Schaetzung. Ersetzt ein echter
    Stand (``is_estimated=False``) eine zuvor *abgerechnete* Schaetzung, wird
    automatisch ein ``ReadingCorrection`` (Gutschrift/Nachforderung) angelegt
    — zentral hier, damit alle Eingabewege (OSS-Einzel/-Bulk, SaaS-Self-
    Service) den Abgleich ohne Mehraufwand bekommen.

    Caller muss ``db.session.commit()`` aufrufen.
    """
    if reading_date is None:
        reading_date = date.today()

    # Vorzustand merken, um den Schaetzungs-Abgleich zu erkennen.
    existing = MeterReading.query.filter_by(
        meter_id=meter.id, billing_period_id=billing_period.id
    ).first()
    was_estimated = bool(existing.is_estimated) if existing else False
    est_consumption = existing.consumption if existing else None

    if existing:
        existing.value = value
        existing.reading_date = reading_date
        existing.created_by_id = created_by_id
        existing.entered_via_self_service = entered_via_self_service
        existing.self_service_code_id = self_service_code_id
        existing.is_estimated = is_estimated
        reading = existing
    else:
        reading = MeterReading(
            meter_id=meter.id,
            billing_period_id=billing_period.id,
            value=value,
            reading_date=reading_date,
            created_by_id=created_by_id,
            entered_via_self_service=entered_via_self_service,
            self_service_code_id=self_service_code_id,
            is_estimated=is_estimated,
        )
        db.session.add(reading)

    db.session.flush()
    recompute_meter_chain(meter)

    # Echter Stand ersetzt abgerechnete Schaetzung -> Korrekturposten anlegen.
    if existing is not None and was_estimated and not is_estimated:
        from app.meters.estimation import build_correction
        build_correction(reading, est_consumption, created_by_id=created_by_id)

    return reading
