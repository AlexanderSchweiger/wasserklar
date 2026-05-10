"""Wiederverwendbare Service-Helpers fuer Zaehlerstaende.

Extrahiert aus ``meters.routes.bulk_read``, damit die SaaS-Self-Service-
Erfassung dieselbe Verbrauchs- und Storage-Logik nutzt — ohne den OSS-
Code zu duplizieren.

Convention: Caller ist fuer ``db.session.commit()`` zustaendig. So kann
ein Bulk-Aufrufer alle Eintraege in einer Transaktion speichern.
"""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import MeterReading, WaterMeter


def save_reading(
    meter: WaterMeter,
    year: int,
    value: Decimal,
    *,
    created_by_id: int = None,
    entered_via_self_service: bool = False,
    self_service_code_id: int = None,
    reading_date: date = None,
) -> MeterReading:
    """Legt einen Zaehlerstand an oder aktualisiert den existierenden
    Eintrag fuer ``(meter, year)``. Berechnet den Verbrauch gegen den
    Vorjahresstand bzw. ``initial_value`` (Pattern aus bulk_read).

    Caller muss ``db.session.commit()`` aufrufen.
    """
    if reading_date is None:
        reading_date = date.today()

    prev = MeterReading.query.filter_by(meter_id=meter.id, year=year - 1).first()
    consumption = None
    if prev:
        consumption = value - prev.value
    elif meter.initial_value is not None:
        consumption = value - meter.initial_value

    existing = MeterReading.query.filter_by(meter_id=meter.id, year=year).first()
    if existing:
        existing.value = value
        existing.consumption = consumption
        existing.reading_date = reading_date
        existing.created_by_id = created_by_id
        existing.entered_via_self_service = entered_via_self_service
        existing.self_service_code_id = self_service_code_id
        return existing

    reading = MeterReading(
        meter_id=meter.id,
        year=year,
        value=value,
        reading_date=reading_date,
        consumption=consumption,
        created_by_id=created_by_id,
        entered_via_self_service=entered_via_self_service,
        self_service_code_id=self_service_code_id,
    )
    db.session.add(reading)
    return reading
