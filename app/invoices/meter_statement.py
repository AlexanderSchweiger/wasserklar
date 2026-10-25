"""Verbrauchsnachweis auf der Rechnung: aus welchen Zaehlerstaenden die
abgerechnete Wassermenge entsteht.

Eine Rechnung friert beim Entstehen je Zaehler Anfangs- und Endstand samt dem
daraus verrechneten Verbrauch ein (``InvoiceMeterReading``) — Ablesungen
bleiben danach aenderbar (Schaetzung → echter Stand ueberschreibt dieselbe
Zeile, der Eigentuemerwechsel ueberschreibt den Periodenstand), die Rechnung
zeigt trotzdem, was sie abgerechnet hat. Einzige Stelle fuer diese Regeln:

- ``rows_for_billing`` (Rechnungslauf) und ``rows_for_settlement``
  (Schlussrechnung) bauen die Zeilen aus genau den Eingaben, aus denen die
  Positionen entstehen; ``freeze`` schreibt sie.
- ``freeze_if_missing`` friert Entwuerfe aus der Zeit vor dem Verbrauchsnachweis
  beim Versenden ein (``invoices.services.create_or_update_open_item``).
- ``statement_for``/``for_document`` liefern Ansicht, PDF und Word den
  Nachweis. **Invariante:** gedruckt wird er nur, wenn seine Summe exakt der
  abgerechneten Wassermenge (``Invoice.consumption``) entspricht — eine
  Handaenderung im Positions-Editor oder eine Null-Kappung laesst ihn weg,
  statt etwas Falsches zu zeigen.

Vor dem Verbrauchsnachweis versendete Rechnungen bekommen nie Zeilen.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import (
    AppSetting, Invoice, InvoiceMeterReading, MeterReading, MeterReplacement,
    OwnerChange, OwnerChangeMeterValue, ReadingCorrection, WaterMeter,
)
from app.meters.estimation import base_reading

SETTING_KEY = "invoice.show_meter_readings"
ZERO = Decimal("0")


def enabled():
    """Mandanteneinstellung — Standard an (fehlender Wert = an)."""
    return AppSetting.get(SETTING_KEY, "true") != "false"


def format_quantity(value):
    """Zaehlerstand/Menge im deutschen Format: ganze Zahlen ohne
    Nachkommastellen (``1.204``), sonst bis zu 3 Stellen (``12,5``)."""
    if value is None:
        return ""
    q = Decimal(str(value))
    if q == q.to_integral_value():
        text = f"{q.quantize(Decimal('1')):,}"
    else:
        text = f"{q.quantize(Decimal('0.001')).normalize():,f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def _dec(value):
    return Decimal(str(value)) if value is not None else None


def _in_period(day, period):
    return day is not None and period.start_date <= day <= period.end_date


def _row(meter, *, start, end, consumption):
    return {
        "meter_id": meter.id,
        "meter_number": meter.meter_number,
        "meter_type": meter.meter_type or "main",
        "start_date": start["date"],
        "start_value": _dec(start["value"]),
        "start_estimated": bool(start["is_estimated"]),
        "start_event": start["event"],
        "end_date": end["date"],
        "end_value": _dec(end["value"]),
        "end_estimated": bool(end["is_estimated"]),
        "end_event": end["event"],
        "consumption": consumption,
    }


def _start_from_chain(meter, period):
    base = base_reading(meter, period)
    installed = base["from_install"] and _in_period(base["date"], period)
    return {**base, "event": InvoiceMeterReading.EVENT_INSTALLED if installed else None}


def _end_from_reading(meter, reading, period):
    removed = _in_period(meter.installed_to, period)
    return {"value": reading.value, "date": reading.reading_date,
            "is_estimated": reading.is_estimated,
            "event": InvoiceMeterReading.EVENT_REMOVED if removed else None}


def _sorted(rows):
    # Hauptzaehler vor Subzaehlern, dann zeitlich (ausgebauter vor eingebautem).
    return sorted(rows, key=lambda r: (r["meter_type"] == "sub",
                                       r["start_date"] or r["end_date"] or date.min,
                                       r["meter_number"] or ""))


# ---------------------------------------------------------------------------
# Zeilen bauen
# ---------------------------------------------------------------------------

def _owner_change_starts(property_id, period_id):
    """Stand beim juengsten Eigentuemerwechsel je Zaehler — nur Wechsel mit
    nicht stornierter Schlussrechnung (wie ``deductions_for_property``) und
    nur Zaehler, von denen die Schlussrechnung Verbrauch verrechnet hat."""
    rows = (
        db.session.query(OwnerChangeMeterValue, OwnerChange)
        .join(OwnerChange, OwnerChange.id == OwnerChangeMeterValue.owner_change_id)
        .join(Invoice, Invoice.id == OwnerChange.settlement_invoice_id)
        .filter(
            OwnerChange.property_id == property_id,
            OwnerChange.billing_period_id == period_id,
            Invoice.status != Invoice.STATUS_CANCELLED,
            OwnerChangeMeterValue.consumption_billed.isnot(None),
        )
        .order_by(OwnerChange.change_date, OwnerChange.id)
        .all()
    )
    starts = {}
    for mv, oc in rows:  # aufsteigend → der juengste Wechsel gewinnt
        starts[mv.meter_id] = {"value": mv.value_at_change, "date": oc.change_date,
                               "is_estimated": mv.is_estimated,
                               "event": InvoiceMeterReading.EVENT_OWNER_CHANGE}
    return starts


def rows_for_billing(prop, period, readings, deductions=None):
    """Zeilen einer Rechnungslauf-Rechnung — dieselben Eingaben wie die
    Verbrauchsposition (``invoices.routes.generate``): je Ablesung der Periode
    der Verbrauch abzueglich der per Schlussrechnung schon verrechneten Menge.

    Nach einem Eigentuemerwechsel beginnt der Zaehler beim Stand des Wechsels;
    ein vor dem Stichtag ausgebauter Zaehler (voll beim Altbesitzer) entfaellt.
    """
    by_meter = deductions["by_meter"] if deductions else {}
    starts = _owner_change_starts(prop.id, period.id) if deductions else {}
    rows = []
    for reading in readings:
        meter = reading.meter
        consumption = _dec(reading.consumption) or ZERO
        deducted = by_meter.get(meter.id, ZERO)
        if deducted > 0:
            consumption -= deducted
            if consumption <= 0:
                continue
        start = (starts[meter.id] if deducted > 0 and meter.id in starts
                 else _start_from_chain(meter, period))
        rows.append(_row(meter, start=start, end=_end_from_reading(meter, reading, period),
                         consumption=consumption))
    return _sorted(rows)


def _prior_change(meter, period, before):
    """Stand des vorigen Wechsels derselben Periode (wie
    ``owner_change.services.settlement_base_value``)."""
    hit = (
        db.session.query(OwnerChangeMeterValue, OwnerChange)
        .join(OwnerChange, OwnerChange.id == OwnerChangeMeterValue.owner_change_id)
        .filter(
            OwnerChangeMeterValue.meter_id == meter.id,
            OwnerChange.billing_period_id == period.id,
            OwnerChange.change_date < before,
        )
        .order_by(OwnerChange.change_date.desc(), OwnerChange.id.desc())
        .first()
    )
    if hit is None:
        return None
    mv, oc = hit
    return {"value": mv.value_at_change, "date": oc.change_date,
            "is_estimated": mv.is_estimated, "event": InvoiceMeterReading.EVENT_OWNER_CHANGE}


def rows_for_settlement(period, stichtag, snapshots):
    """Zeilen der Schlussrechnung aus den Zaehler-Snapshots des Wechsels
    (``owner_change.services.collect_meter_snapshots``): Ende = Stand am
    Stichtag; ein vor dem Stichtag ausgebauter Zaehler endet mit seinem
    Ausbau-Stand. Zaehler ohne Abrechnungsbasis haben auch keine Position."""
    rows = []
    for snap in snapshots:
        if snap["consumption_billed"] is None:
            continue
        meter = snap["meter"]
        if snap["source"] == "fixed":
            reading = MeterReading.query.filter_by(
                meter_id=meter.id, billing_period_id=period.id).first()
            if reading is None:
                continue
            end = _end_from_reading(meter, reading, period)
        else:
            end = {"value": snap["value_at_change"], "date": stichtag,
                   "is_estimated": snap["is_estimated"],
                   "event": InvoiceMeterReading.EVENT_OWNER_CHANGE}
        start = _prior_change(meter, period, stichtag) or _start_from_chain(meter, period)
        rows.append(_row(meter, start=start, end=end,
                         consumption=_dec(snap["consumption_billed"])))
    return _sorted(rows)


def freeze(invoice, rows):
    """Schreibt die Zeilen an die Rechnung (flusht nicht, committet nicht)."""
    for i, row in enumerate(rows):
        invoice.meter_readings.append(InvoiceMeterReading(sort_order=i, **row))


# ---------------------------------------------------------------------------
# Live-Nachbau (nur Entwuerfe ohne eingefrorene Zeilen)
# ---------------------------------------------------------------------------

def _live_rows(invoice):
    prop, period = invoice.property, invoice.billing_period
    if prop is None or period is None or invoice.id is None:
        return []
    if invoice.invoice_kind == Invoice.KIND_FINAL_SETTLEMENT:
        oc = OwnerChange.query.filter_by(settlement_invoice_id=invoice.id).first()
        if oc is None:
            return []
        snapshots = []
        for mv in oc.meter_values:
            rep = MeterReplacement.query.filter_by(
                old_meter_id=mv.meter_id, billing_period_id=period.id).first()
            fixed = rep is not None and rep.replacement_date <= oc.change_date
            snapshots.append({"meter": mv.meter, "value_at_change": mv.value_at_change,
                              "consumption_billed": mv.consumption_billed,
                              "is_estimated": mv.is_estimated,
                              "source": "fixed" if fixed else "editable"})
        return rows_for_settlement(period, oc.change_date, snapshots)
    if invoice.invoice_kind != Invoice.KIND_STANDARD or invoice.billing_run_id is None:
        return []
    from app.owner_change.services import deductions_for_property  # Zyklus vermeiden
    readings = (
        MeterReading.query
        .join(WaterMeter, WaterMeter.id == MeterReading.meter_id)
        .filter(MeterReading.billing_period_id == period.id,
                WaterMeter.property_id == prop.id)
        .all()
    )
    return rows_for_billing(prop, period, readings, deductions_for_property(prop.id, period.id))


def freeze_if_missing(invoice):
    """Altentwurf ohne Zeilen beim Versenden einfrieren — nur, wenn der
    Nachbau die abgerechnete Menge exakt erklaert."""
    if invoice.is_credit_note or invoice.meter_readings:
        return
    rows = _live_rows(invoice)
    total = sum((r["consumption"] for r in rows), ZERO)
    if rows and invoice.consumption is not None and total == Decimal(str(invoice.consumption)):
        freeze(invoice, rows)


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------

@dataclass
class MeterStatement:
    rows: list = field(default_factory=list)
    corrections: list = field(default_factory=list)
    period_name: str = None
    billed: Decimal = None       # abgerechnete Wassermenge der Rechnung
    frozen: bool = False         # Zeilen eingefroren (sonst Live-Nachbau eines Entwurfs)
    enabled: bool = True         # Mandanteneinstellung
    show: bool = False           # auf PDF/Word drucken?

    @property
    def total(self):
        return sum((Decimal(str(r.consumption)) for r in self.rows), ZERO)

    @property
    def matches(self):
        return self.billed is not None and self.total == self.billed

    @staticmethod
    def fmt(value):
        return format_quantity(value)


def _corrections(invoice):
    """Schaetzkorrekturen, die diese Rechnung einzieht (Fussnote) — nur die
    Wasserposition (ein Wassercent-Korrekturposten gehoert zum selben Stand)
    und nur mit m³-Delta (ein Guthaben-Uebertrag hat keins)."""
    ids = [it.reading_correction_id for it in invoice.items if it.reading_correction_id]
    if not ids:
        return []
    out = []
    for corr in (ReadingCorrection.query.filter(ReadingCorrection.id.in_(ids))
                 .order_by(ReadingCorrection.id).all()):
        if corr.delta_m3 is None or corr.charge_key not in (None, "water"):
            continue
        out.append({
            "period_name": corr.billing_period.name if corr.billing_period else "",
            "meter_number": corr.meter.meter_number if corr.meter else "?",
            "estimated": _dec(corr.estimated_consumption),
            "real": _dec(corr.real_consumption),
            "delta": _dec(corr.delta_m3),
        })
    return out


def statement_for(invoice):
    """Nachweis fuer Ansicht, PDF und Word — eingefrorene Zeilen bzw. bei
    einem Entwurf ohne Zeilen der Live-Nachbau (transiente Objekte)."""
    st = MeterStatement(enabled=enabled())
    if invoice is None or invoice.is_credit_note:
        return st
    rows = list(invoice.meter_readings)
    st.frozen = bool(rows)
    if not rows and invoice.status == Invoice.STATUS_DRAFT:
        rows = [InvoiceMeterReading(sort_order=i, **r) for i, r in enumerate(_live_rows(invoice))]
    st.rows = rows
    if not rows:
        return st
    billed = invoice.consumption
    st.billed = Decimal(str(billed)) if billed is not None else None
    st.period_name = invoice.billing_period.name if invoice.billing_period else None
    st.corrections = _corrections(invoice) if invoice.id is not None else []
    return st


def for_document(invoice):
    """Wie ``statement_for``, plus ``show``: Einstellung an, Zeilen vorhanden
    und die Summe erklaert die abgerechnete Menge exakt."""
    st = statement_for(invoice)
    st.show = bool(st.enabled and st.rows and st.matches)
    return st
