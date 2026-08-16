"""Jahres-Verbrauchssummen — Cache fuer gemessene Jahre, Eingabe fuer historische.

Warum eine eigene Tabelle statt einer Live-Summe: die Jahressumme wird an
mehreren Stellen gebraucht (Dashboard-Kachel, Dashboard-Chart, Plankosten-
rechnung) und ist bei vielen Zaehlern eine teure Aggregation ueber die
komplette ``meter_readings``-Tabelle. ``ConsumptionYear`` haelt das Ergebnis
vor; die Schreibpfade markieren betroffene Jahre nur als ``stale``, gerechnet
wird lazy beim naechsten Oeffnen der Verbrauchsseite.

Zweiter Zweck derselben Tabelle: Jahre VOR der App-Einfuehrung haben keine
Ablesungen. Fuer die Tarifplanung braucht man aber einen mehrjaehrigen Trend,
also kann der Kassier solche Jahre manuell erfassen (``source='manual'``).

**Die Eingabe gewinnt**: ein manuell gesetzter Wert uebersteuert die Messung und
wird von keiner Neuberechnung ueberschrieben — auch dann nicht, wenn es fuer das
Jahr Ablesungen gibt. Gruende dafuer gibt es genug: unvollstaendig erfasstes
Jahr, defekter Zaehler, nachtraeglich bekannt gewordene Korrektur. Die gemessene
Summe wird trotzdem weiter gepflegt (``measured_m3``), bleibt neben der Eingabe
sichtbar und laesst sich per ``revert_to_measured`` wieder aktivieren.

Dieses Modul liegt bewusst top-level (wie ``app/wg.py``, ``app/tax_service.py``)
und importiert nur Models — so koennen ``app.meters`` (Invalidierung) und
``app.cost_planning`` (Auswertung) es beide importieren, ohne Zyklus.

Convention wie in ``app/meters/services.py``: Caller committet.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models import (
    BillingPeriod,
    ConsumptionYear,
    MeterReading,
    WaterMeter,
)


def _periods_by_year():
    """``{Jahr: [BillingPeriod, ...]}`` — gruppiert nach ``start_date.year``.

    Bewusst in Python statt per ``EXTRACT``/``STRFTIME``: die Perioden-Tabelle
    hat eine Handvoll Zeilen, und ein SQL-Datumsausdruck waere ueber SQLite /
    MySQL / Postgres wieder ein Portabilitaets-Risiko.
    """
    out = defaultdict(list)
    for p in BillingPeriod.query.order_by(BillingPeriod.start_date.asc()).all():
        if p.start_date is not None:
            out[p.start_date.year].append(p)
    return out


def _measure(period_ids):
    """Rohdaten fuer ein Jahr: ``(summe_m3, anzahl_ablesungen, anzahl_objekte)``.

    ``count(MeterReading.consumption)`` zaehlt nur Zeilen mit gesetztem
    Verbrauch — eine Ablesung ohne Vorgaenger und ohne ``initial_value`` hat
    ``consumption IS NULL`` und ist keine Abrechnungsbasis.
    """
    total, count = (
        db.session.query(
            func.coalesce(func.sum(MeterReading.consumption), 0),
            func.count(MeterReading.consumption),
        )
        .filter(MeterReading.billing_period_id.in_(period_ids))
        .one()
    )
    units = (
        db.session.query(func.count(func.distinct(WaterMeter.property_id)))
        .select_from(MeterReading)
        .join(WaterMeter, WaterMeter.id == MeterReading.meter_id)
        .filter(MeterReading.billing_period_id.in_(period_ids))
        .scalar()
    ) or 0
    return Decimal(str(total or 0)), int(count or 0), int(units)


def for_year(year):
    """Die Zeile eines Jahres (oder ``None``)."""
    return ConsumptionYear.query.filter_by(year=year).first()


def refresh_year(year, periods=None):
    """Die gemessene Summe eines Jahres neu berechnen.

    Gibt die aktualisierte ``ConsumptionYear`` zurueck, ``None`` wenn es fuer
    das Jahr weder Messdaten noch eine manuelle Eingabe gibt.

    **Eine manuelle Zeile wird nie ueberschrieben.** Ihre ``measured_m3``
    (und Zaehler-Metadaten) werden zwar mitgepflegt, damit die Abweichung
    sichtbar bleibt und ``revert_to_measured`` ohne Neuberechnung funktioniert —
    der wirksame Wert ``total_m3`` bleibt aber die Eingabe des Nutzers.
    """
    if periods is None:
        periods = _periods_by_year().get(year, [])
    row = for_year(year)

    period_ids = [p.id for p in periods]
    total, count, units = (
        _measure(period_ids) if period_ids else (Decimal("0"), 0, 0)
    )

    if count == 0:
        # Keine Messdaten. Manuelle Eingaben bleiben stehen (die Messung, von
        # der sie ggf. abwich, gibt es nicht mehr); eine verwaiste
        # measured-Zeile (Periode geloescht, Ablesungen weg) wird entfernt.
        if row is None:
            return None
        if not row.is_manual:
            db.session.delete(row)
            return None
        row.measured_m3 = None
        row.reading_count = None
        row.stale = False
        return row

    if row is None:
        row = ConsumptionYear(year=year)
        db.session.add(row)

    row.billing_period_id = periods[0].id if len(periods) == 1 else None
    row.measured_m3 = total
    row.reading_count = count
    row.unit_count = units
    row.stale = False
    row.computed_at = datetime.utcnow()
    # Der wirksame Wert folgt der Messung nur, solange nicht uebersteuert wird.
    if not row.is_manual:
        row.total_m3 = total
        row.source = ConsumptionYear.SOURCE_MEASURED
    return row


def refresh_all(force=False):
    """Alle Jahre mit Messdaten neu berechnen.

    ``force=False`` rechnet nur Jahre neu, die als ``stale`` markiert sind oder
    fuer die noch keine Zeile existiert — der Normalfall beim Seitenaufruf.
    ``force=True`` rechnet alles (Button "Neu berechnen"), z.B. nachdem
    Zaehler-Stammdaten ausserhalb der ueblichen Pfade geaendert wurden.
    """
    by_year = _periods_by_year()
    existing = {r.year: r for r in ConsumptionYear.query.all()}
    touched = []
    for year, periods in by_year.items():
        row = existing.get(year)
        if not force and row is not None and not row.stale:
            continue
        result = refresh_year(year, periods=periods)
        if result is not None:
            touched.append(result)
    return touched


def ensure_fresh():
    """Fehlende Jahre anlegen und veraltete neu rechnen. Caller committet."""
    return refresh_all(force=False)


def has_stale():
    """Gibt es Jahre, die auf eine Neuberechnung warten?"""
    return db.session.query(
        ConsumptionYear.query.filter_by(stale=True).exists()
    ).scalar()


def mark_stale(from_year=None):
    """Jahre zur Neuberechnung vormerken. Caller committet.

    Aufgerufen wird das aus ``recompute_meter_chain`` — dem einen Ort, an dem
    sich ``MeterReading.consumption`` ueberhaupt aendert (Einzelerfassung,
    Bulk-Import, Zaehlertausch, Selbstablesung, REST-API und das Loeschen einer
    Ablesung laufen alle dort durch). Dadurch bleibt der Cache konsistent, ohne
    dass jeder Schreibpfad einzeln daran denken muss.

    Ohne ``from_year`` werden alle Jahre markiert — das ist die richtige
    Semantik fuer ``recompute_meter_chain``, das die komplette Kette eines
    Zaehlers ueber alle Perioden neu rechnet.

    Der EXISTS-Vorabcheck haelt Bulk-Importe billig: nach dem ersten Aufruf ist
    ohnehin alles markiert, die folgenden Hunderte Aufrufe kosten dann nur noch
    ein SELECT auf eine Tabelle mit einer Handvoll Zeilen statt je ein UPDATE.

    Auch manuell uebersteuerte Zeilen werden markiert: ihr wirksamer Wert bleibt
    zwar die Eingabe, aber ihre ``measured_m3`` muss weiter mitlaufen, damit die
    angezeigte Abweichung stimmt.
    """
    pending = ConsumptionYear.query.filter(ConsumptionYear.stale.is_(False))
    if from_year is not None:
        pending = pending.filter(ConsumptionYear.year >= from_year)
    if not db.session.query(pending.exists()).scalar():
        return

    q = ConsumptionYear.query
    if from_year is not None:
        q = q.filter(ConsumptionYear.year >= from_year)
    q.update({ConsumptionYear.stale: True}, synchronize_session=False)


class ConsumptionError(Exception):
    """Fachlicher Fehler bei der manuellen Verbrauchserfassung."""


def set_manual(year, total_m3, *, notes=None, unit_count=None,
               created_by_id=None):
    """Jahresmenge manuell setzen — immer erlaubt, auch mit Ablesungen.

    Existieren fuer das Jahr Ablesungen, wird die gemessene Summe **nicht**
    verworfen, sondern als ``measured_m3`` daneben gehalten: die Anzeige kann
    die Abweichung zeigen, und ``revert_to_measured`` stellt den berechneten
    Wert ohne Neuberechnung wieder her.

    Caller committet.
    """
    periods = _periods_by_year().get(year, [])
    measured, count, units = (
        _measure([p.id for p in periods]) if periods else (None, 0, 0)
    )

    row = for_year(year)
    if row is None:
        row = ConsumptionYear(year=year)
        db.session.add(row)

    row.total_m3 = total_m3
    row.source = ConsumptionYear.SOURCE_MANUAL
    row.billing_period_id = periods[0].id if len(periods) == 1 else None
    if count:
        row.measured_m3 = measured
        row.reading_count = count
        # Angegebene Objektzahl gewinnt, sonst die gemessene.
        row.unit_count = unit_count if unit_count is not None else units
    else:
        row.measured_m3 = None
        row.reading_count = None
        row.unit_count = unit_count
    row.stale = False
    row.notes = notes
    if row.created_by_id is None:
        row.created_by_id = created_by_id
    return row


def revert_to_measured(year):
    """Manuelle Uebersteuerung aufheben.

    Gibt es fuer das Jahr Ablesungen, wird wieder die berechnete Summe wirksam;
    gibt es keine, verschwindet die Zeile ganz (ohne Eingabe bliebe nichts
    uebrig). Wirft ``ConsumptionError``, wenn gar nichts zu widerrufen ist.
    """
    row = for_year(year)
    if row is None:
        return False
    if not row.is_manual:
        raise ConsumptionError(
            f"{year} ist bereits die berechnete Jahressumme — es gibt nichts "
            f"aufzuheben."
        )
    if row.measured_m3 is None:
        db.session.delete(row)
        return True
    row.total_m3 = row.measured_m3
    row.source = ConsumptionYear.SOURCE_MEASURED
    row.notes = None
    row.stale = True     # naechster Refresh zieht Metadaten sauber nach
    return True


# Rueckwaerts-kompatibler Name — die Aktion heisst jetzt "Uebersteuerung
# aufheben", weil bei vorhandenen Ablesungen nichts geloescht wird.
delete_manual = revert_to_measured


def history(limit=None):
    """Verbrauchs-Historie chronologisch — gemessene und manuelle Jahre.

    Liefert Dicts (``year``, ``label``, ``value`` als float, ``source``,
    ``is_manual``) fuer Charts und Tabellen. ``limit`` schneidet auf die
    letzten N Jahre.
    """
    rows = ConsumptionYear.query.order_by(ConsumptionYear.year.asc()).all()
    if limit:
        rows = rows[-limit:]
    return [
        {
            "year": r.year,
            "label": r.label,
            "value": float(r.total_m3 or 0),
            "source": r.source,
            "is_manual": r.is_manual,
        }
        for r in rows
    ]


def average(years=3, exclude_current=True):
    """Durchschnittsverbrauch der letzten ``years`` Jahre als Planungsbasis.

    ``exclude_current`` blendet das Jahr der aktiven Abrechnungsperiode aus:
    solange dort erst ein Teil der Zaehler abgelesen ist, ist die Summe
    unvollstaendig und wuerde den Schnitt nach unten ziehen. Gibt es danach
    keine Daten mehr, wird das laufende Jahr doch herangezogen (besser eine
    grobe Basis als gar keine).

    Gibt ``Decimal("0")`` zurueck, wenn ueberhaupt keine Daten vorliegen —
    die Aufrufer behandeln diesen Fall explizit (kein verbrauchsbasiertes
    Tarifpaket moeglich).
    """
    rows = ConsumptionYear.query.order_by(ConsumptionYear.year.desc()).all()
    if not rows:
        return Decimal("0")

    usable = rows
    if exclude_current:
        current = BillingPeriod.current()
        if current is not None and current.start_date is not None:
            filtered = [r for r in rows if r.year != current.start_date.year]
            if filtered:
                usable = filtered

    selected = usable[:years]
    if not selected:
        return Decimal("0")
    total = sum((r.total_m3 or Decimal("0")) for r in selected)
    return (Decimal(str(total)) / Decimal(len(selected))).quantize(
        Decimal("0.001")
    )


def average_basis(years=3, exclude_current=True):
    """Wie ``average``, aber mit den herangezogenen Jahren fuer die Anzeige
    ("Ø aus 2022–2024"). Liefert ``(schnitt, [ConsumptionYear, ...])``."""
    rows = ConsumptionYear.query.order_by(ConsumptionYear.year.desc()).all()
    if not rows:
        return Decimal("0"), []

    usable = rows
    if exclude_current:
        current = BillingPeriod.current()
        if current is not None and current.start_date is not None:
            filtered = [r for r in rows if r.year != current.start_date.year]
            if filtered:
                usable = filtered

    selected = usable[:years]
    if not selected:
        return Decimal("0"), []
    total = sum((r.total_m3 or Decimal("0")) for r in selected)
    avg = (Decimal(str(total)) / Decimal(len(selected))).quantize(
        Decimal("0.001")
    )
    return avg, list(reversed(selected))
