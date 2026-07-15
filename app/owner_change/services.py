"""Services fuer den Eigentuemerwechsel-Workflow.

Loest das Unterjaehrig-Problem, ohne den ``UniqueConstraint(meter_id,
billing_period_id)`` auf ``meter_readings`` anzutasten: der Stichtags-Stand wird
als normale Perioden-Ablesung geschrieben (``save_reading``), die abgerechnete
Menge wird pro Zaehler in ``OwnerChangeMeterValue`` eingefroren, und der
Massen-Rechnungslauf zieht diese Menge vom Jahresverbrauch des Nachbesitzers ab
(siehe ``deductions_for_property`` + ``app/invoices/routes.py:generate``).

Convention wie ``meters.services``: der Aufrufer ist fuer ``db.session.commit()``
zustaendig — ``execute_owner_change`` committet als einzige Funktion selbst
(ein Wechsel ist eine Transaktion).
"""
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    AppSetting, BillingPeriod, Customer, Invoice, InvoiceItem,
    MeterReading, MeterReplacement, OwnerChange, OwnerChangeMeterValue,
    Property, PropertyOwnership, WaterMeter,
)
from app.accounting.services import default_water_tax_rate, open_fiscal_year_error
from app.meters.estimation import (
    _base_value, apply_corrections_to_invoice, cap_invoice_at_zero,
    customer_correction_balance, estimate_meter_value,
)
from app.meters.services import save_reading
from app.settings_service import is_wassergenossenschaft
from app.utils import next_invoice_number
from app import wg

SETTING_BASE_FEE_MODE = "owner_change.base_fee_mode"
FEE_MODE_NEW_OWNER_FULL = OwnerChange.FEE_MODE_NEW_OWNER_FULL
FEE_MODE_PRO_RATA = OwnerChange.FEE_MODE_PRO_RATA
FEE_MODES = (FEE_MODE_NEW_OWNER_FULL, FEE_MODE_PRO_RATA)

FEE_MODE_LABELS = {
    FEE_MODE_NEW_OWNER_FULL: "Grundgebühr voll beim neuen Eigentümer",
    FEE_MODE_PRO_RATA: "Grundgebühr tagegenau aufteilen",
}


class OwnerChangeError(ValueError):
    """Fachlicher Fehler beim Eigentuemerwechsel (Flash-tauglich)."""


def default_base_fee_mode():
    """Konfigurierter Default-Gebuehrenmodus (AppSetting), robust gefallbackt."""
    raw = AppSetting.get(SETTING_BASE_FEE_MODE, FEE_MODE_NEW_OWNER_FULL)
    return raw if raw in FEE_MODES else FEE_MODE_NEW_OWNER_FULL


# ---------------------------------------------------------------------------
# Perioden- und Zaehler-Auswahl
# ---------------------------------------------------------------------------

def period_for_date(stichtag):
    """Die Abrechnungsperiode, die ``stichtag`` enthaelt (spaetester Start
    zuerst). ``None``, wenn keine passt — der Wizard bietet dann die aktive
    Periode zur Bestaetigung an."""
    if stichtag is None:
        return None
    return (
        BillingPeriod.query
        .filter(BillingPeriod.start_date <= stichtag,
                BillingPeriod.end_date >= stichtag)
        .order_by(BillingPeriod.start_date.desc())
        .first()
    )


def _swapped_out_before(prop, period, stichtag):
    """Zaehlertausche des Objekts in der Periode mit Tauschdatum <= Stichtag.

    Deren ausgebaute Zaehler tragen bereits eine eingefrorene End-Ablesung in
    der Periode — dieser Verbrauch gehoert komplett dem Altbesitzer.
    """
    return (
        MeterReplacement.query
        .filter(
            MeterReplacement.property_id == prop.id,
            MeterReplacement.billing_period_id == period.id,
            MeterReplacement.replacement_date <= stichtag,
        )
        .all()
    )


def _period_reading(meter, period):
    return MeterReading.query.filter_by(
        meter_id=meter.id, billing_period_id=period.id).first()


def wizard_meters(prop, period, stichtag):
    """Zaehler des Objekts fuer den Stichtag, aufgeteilt in:

    - ``editable``: aktive, am Stichtag bereits eingebaute Zaehler -> Stand
      am Stichtag wird abgefragt (mit Schaetz-Vorschlag).
    - ``fixed``: Liste ``(WaterMeter, MeterReading)`` fuer vor dem Stichtag
      ausgebaute Zaehler -> gehen read-only voll in die Schlussrechnung.
    """
    editable = [
        m for m in prop.meters.filter_by(active=True).all()
        if m.installed_from is None or m.installed_from <= stichtag
    ]
    editable.sort(key=lambda m: m.meter_number or "")

    fixed = []
    seen = set()
    for rep in _swapped_out_before(prop, period, stichtag):
        old = rep.old_meter or db.session.get(WaterMeter, rep.old_meter_id)
        if old is None or old.id in seen:
            continue
        reading = _period_reading(old, period)
        if reading is None:
            continue
        seen.add(old.id)
        fixed.append((old, reading))
    return {"editable": editable, "fixed": fixed}


def prefill_value(meter, period):
    """Schaetz-Vorschlag fuer den Stichtags-Stand (oder ``None``)."""
    est = estimate_meter_value(meter, period)
    return est["value"] if est else None


# ---------------------------------------------------------------------------
# Verbrauchs- und Gebuehren-Mathematik
# ---------------------------------------------------------------------------

def settlement_base_value(meter, period, stichtag):
    """Basis-Zaehlerstand, ab dem der Stichtags-Verbrauch zaehlt.

    Bei einem zweiten Wechsel in derselben Periode ist das der Stand des
    vorigen Wechsels (``OwnerChangeMeterValue``) — NICHT die Vorablesung
    (die Unique-Zeile in ``meter_readings`` wurde da schon ueberschrieben).
    Sonst der letzte Stand vor der Periode bzw. ``initial_value``.
    """
    prior = (
        db.session.query(OwnerChangeMeterValue)
        .join(OwnerChange, OwnerChange.id == OwnerChangeMeterValue.owner_change_id)
        .filter(
            OwnerChangeMeterValue.meter_id == meter.id,
            OwnerChange.billing_period_id == period.id,
            OwnerChange.change_date < stichtag,
        )
        .order_by(OwnerChange.change_date.desc(), OwnerChange.id.desc())
        .first()
    )
    if prior is not None:
        return Decimal(str(prior.value_at_change))
    base = _base_value(meter, period)
    return Decimal(str(base)) if base is not None else None


def fee_day_split(period, stichtag):
    """``(old_days, new_days, period_days)`` fuer die tagegenaue Gebuehren-
    aufteilung. Der Stichtag selbst zaehlt zum Nachbesitzer (dessen
    ``valid_from``); Summe old+new ist exakt ``period_days``."""
    period_days = (period.end_date - period.start_date).days + 1
    old_days = (stichtag - period.start_date).days
    new_days = (period.end_date - stichtag).days + 1
    return old_days, new_days, period_days


def collect_meter_snapshots(prop, period, stichtag, meter_inputs):
    """Baut die Zaehler-Snapshots fuer Vorschau UND Ausfuehrung.

    ``meter_inputs`` = ``{meter_id: {"value": Decimal, "is_estimated": bool}}``
    fuer die editierbaren Zaehler. Ergaenzt automatisch die vor dem Stichtag
    ausgebauten (``fixed``) Zaehler. Rueckgabe: Liste von Dicts
    ``{meter, value_at_change, consumption_billed, is_estimated, source}`` sowie
    ``warnings`` (Zaehler ohne Abrechnungsbasis).
    """
    snapshots = []
    warnings = []
    fixed = wizard_meters(prop, period, stichtag)["fixed"]
    fixed_ids = {m.id for m, _ in fixed}

    for meter_id, data in meter_inputs.items():
        if meter_id in fixed_ids:
            continue  # ausgebauter Zaehler kommt ueber ``fixed`` rein
        meter = db.session.get(WaterMeter, meter_id)
        if meter is None:
            continue
        value = Decimal(str(data["value"]))
        base = settlement_base_value(meter, period, stichtag)
        if base is None:
            consumption = None
            warnings.append(
                f"Zähler {meter.meter_number}: kein Vorstand vorhanden — "
                f"keine Verbrauchsposition auf der Schlussrechnung."
            )
        else:
            consumption = value - base
        snapshots.append({
            "meter": meter,
            "value_at_change": value,
            "consumption_billed": consumption,
            "is_estimated": bool(data.get("is_estimated")),
            "source": "editable",
        })

    for meter, reading in fixed:
        snapshots.append({
            "meter": meter,
            "value_at_change": Decimal(str(reading.value)),
            "consumption_billed": (
                Decimal(str(reading.consumption))
                if reading.consumption is not None else None),
            "is_estimated": bool(reading.is_estimated),
            "source": "fixed",
        })
    return snapshots, warnings


def _effective_fee(prop, customer, tariff_fee_attr, tariff, override_attr):
    """Effektive Gebuehr: Objekt-Override > Kunden-Override > Tarif.
    ``None`` bedeutet: keine Gebuehr / keine Position."""
    prop_ov = getattr(prop, override_attr)
    if prop_ov is not None:
        return prop_ov
    cust_ov = getattr(customer, override_attr)
    if cust_ov is not None:
        return cust_ov
    return getattr(tariff, tariff_fee_attr)


def _settlement_lines(prop, period, stichtag, tariff, fee_mode, recipient,
                      snapshots, water_tax):
    """Positionsliste (Dicts) der Schlussrechnung — geteilt von Vorschau und
    Rechnungsbau. Verbrauch je Zaehler + (nur pro_rata) anteilige Gebuehren."""
    lines = []
    price = Decimal(str(tariff.price_per_m3))
    for snap in snapshots:
        cons = snap["consumption_billed"]
        if cons is None:
            continue
        meter = snap["meter"]
        amount = (cons * price).quantize(Decimal("0.01"))
        lines.append({
            "description": (
                f"Wasserverbrauch {period.name} bis "
                f"{stichtag.strftime('%d.%m.%Y')} – Zähler {meter.meter_number}"
                f" ({cons.quantize(Decimal('1'))} m³)"),
            "quantity": cons,
            "unit": "m³",
            "unit_price": price,
            "amount": amount,
            "tax_rate": water_tax,
            "is_estimated": snap["is_estimated"],
        })

    if fee_mode == FEE_MODE_PRO_RATA:
        old_days, _new_days, period_days = fee_day_split(period, stichtag)
        if old_days > 0 and period_days > 0:
            last_day = stichtag - timedelta(days=1)
            span = (f"{period.start_date.strftime('%d.%m.%Y')} – "
                    f"{last_day.strftime('%d.%m.%Y')}")
            for fee_attr, override_attr, default_label in (
                ("base_fee", "base_fee_override", "Grundgebühr"),
                ("additional_fee", "additional_fee_override", "Zusatzgebühr"),
            ):
                fee = _effective_fee(prop, recipient, fee_attr, tariff, override_attr)
                if fee is None:
                    continue
                label = getattr(tariff, fee_attr + "_label", None) or default_label
                amount = (Decimal(str(fee)) * Decimal(old_days)
                          / Decimal(period_days)).quantize(Decimal("0.01"))
                lines.append({
                    "description": f"{label} anteilig {old_days}/{period_days} Tage ({span})",
                    "quantity": Decimal("1"),
                    "unit": "Pauschal",
                    "unit_price": amount,
                    "amount": amount,
                    "tax_rate": water_tax,
                    "is_estimated": False,
                })
    return lines


def build_settlement_preview(*, prop, period, stichtag, tariff, fee_mode,
                             recipient, meter_inputs):
    """Reine Rechenvorschau der Schlussrechnung (keine DB-Writes)."""
    water_tax = default_water_tax_rate(date.today().year)
    snapshots, warnings = collect_meter_snapshots(prop, period, stichtag, meter_inputs)
    lines = _settlement_lines(prop, period, stichtag, tariff, fee_mode,
                              recipient, snapshots, water_tax)
    net = sum((Decimal(str(l["amount"])) for l in lines), Decimal("0"))
    gross = Decimal("0")
    for l in lines:
        n = Decimal(str(l["amount"]))
        gross += n
        if l["tax_rate"]:
            gross += (n * Decimal(str(l["tax_rate"])) / Decimal("100")).quantize(Decimal("0.01"))
    old_days, new_days, period_days = fee_day_split(period, stichtag)
    return {
        "lines": lines,
        "net_total": net,
        "gross_total": gross,
        "water_tax": water_tax,
        "fee_mode": fee_mode,
        "old_days": old_days,
        "new_days": new_days,
        "period_days": period_days,
        "correction_balance": customer_correction_balance(recipient.id),
        "warnings": warnings,
        "has_lines": bool(lines),
    }


# ---------------------------------------------------------------------------
# Deduktion fuer den Massen-Rechnungslauf
# ---------------------------------------------------------------------------

def deductions_for_property(property_id, period_id):
    """Bereits per Schlussrechnung verrechnete Mengen/Gebuehren-Tage fuer
    ``(property, period)`` — vom Massen-Rechnungslauf abzuziehen.

    INNER JOIN auf einen NICHT-stornierten ``Invoice``: eine geloeschte oder
    stornierte Schlussrechnung erzeugt so keinen Abzug (self-healing; SQLite
    feuert das FK-``SET NULL`` nicht). ``None``, wenn nichts abzuziehen ist.
    """
    changes = (
        OwnerChange.query
        .join(Invoice, Invoice.id == OwnerChange.settlement_invoice_id)
        .filter(
            OwnerChange.property_id == property_id,
            OwnerChange.billing_period_id == period_id,
            Invoice.status != Invoice.STATUS_CANCELLED,
        )
        .all()
    )
    if not changes:
        return None
    by_meter = {}
    total = Decimal("0")
    fee_days = 0
    numbers = []
    for oc in changes:
        if oc.settlement_invoice is not None:
            numbers.append(oc.settlement_invoice.invoice_number)
        if oc.fee_days_billed:
            fee_days += oc.fee_days_billed
        for mv in oc.meter_values:
            if mv.consumption_billed is None:
                continue
            amt = Decimal(str(mv.consumption_billed))
            by_meter[mv.meter_id] = by_meter.get(mv.meter_id, Decimal("0")) + amt
            total += amt
    return {
        "by_meter": by_meter,
        "total": total,
        "fee_days": fee_days,
        "invoice_numbers": numbers,
    }


# ---------------------------------------------------------------------------
# Ausfuehrung
# ---------------------------------------------------------------------------

def _build_settlement_invoice(*, prop, period, stichtag, tariff, fee_mode,
                              recipient, snapshots, due_days, created_by_id):
    """Erzeugt die Schlussrechnung (Entwurf) + Positionen. Flusht, committet
    nicht. Gibt ``(invoice, old_days_or_None)`` zurueck."""
    water_tax = default_water_tax_rate(date.today().year)
    lines = _settlement_lines(prop, period, stichtag, tariff, fee_mode,
                              recipient, snapshots, water_tax)
    if not lines:
        raise OwnerChangeError(
            "Die Schlussrechnung hätte keine Positionen (kein abrechenbarer "
            "Verbrauch und keine anteilige Gebühr). Bitte Stände prüfen oder "
            "den Wechsel ohne Schlussrechnung durchführen.")

    inv = Invoice(
        invoice_number=next_invoice_number(date.today().year),
        customer_id=recipient.id,
        property_id=prop.id,
        billing_run_id=None,
        billing_period_id=period.id,
        invoice_kind=Invoice.KIND_FINAL_SETTLEMENT,
        date=date.today(),
        due_date=date.today() + timedelta(days=due_days),
        status=Invoice.STATUS_DRAFT,
        created_by_id=created_by_id,
    )
    db.session.add(inv)
    db.session.flush()

    billed_meter_ids = []
    for l in lines:
        db.session.add(InvoiceItem(
            invoice_id=inv.id,
            description=l["description"],
            quantity=l["quantity"],
            unit=l["unit"],
            unit_price=l["unit_price"],
            amount=l["amount"],
            tax_rate=l["tax_rate"],
            is_estimated=l.get("is_estimated", False),
        ))
    for snap in snapshots:
        if snap["consumption_billed"] is not None:
            billed_meter_ids.append(snap["meter"].id)

    db.session.flush()
    inv.recalculate_total()
    # Offene Schaetz-Korrekturen des Altbesitzers in die Schlussrechnung ziehen.
    apply_corrections_to_invoice(inv, recipient.id)
    # Nie negativ (z.B. Gutschrift > Rechnung): auf 0 kappen, Rest vertagen.
    if billed_meter_ids:
        cap_invoice_at_zero(
            inv, customer_id=recipient.id, meter_id=billed_meter_ids[0],
            period_id=period.id, tax_rate=water_tax, created_by_id=created_by_id)

    old_days = None
    if fee_mode == FEE_MODE_PRO_RATA:
        od, _nd, pd = fee_day_split(period, stichtag)
        # Gebuehr-Tage nur, wenn tatsaechlich eine Gebuehrenposition entstand.
        if any(l["unit"] == "Pauschal" for l in lines):
            old_days = od
    return inv, old_days


def execute_owner_change(*, prop, period, stichtag, new_customer_ids,
                         meter_inputs, create_settlement, settlement_recipient_id,
                         tariff, due_days, fee_mode, resign_customer_ids=None,
                         new_member_updates=None, note=None, created_by_id=None):
    """Fuehrt den Eigentuemerwechsel in EINER Transaktion aus (committet selbst).

    Rueckgabe: ``(OwnerChange, warnings)``. Wirft ``OwnerChangeError`` bei
    fachlichen Verstoessen (Aufrufer flasht + bleibt im Wizard).
    """
    warnings = []
    fee_mode = fee_mode if fee_mode in FEE_MODES else FEE_MODE_NEW_OWNER_FULL

    # --- Validierungen -----------------------------------------------------
    if not (period.start_date <= stichtag <= period.end_date):
        raise OwnerChangeError(
            f"Der Stichtag muss innerhalb der Periode {period.name} liegen "
            f"({period.start_date.strftime('%d.%m.%Y')} – "
            f"{period.end_date.strftime('%d.%m.%Y')}).")

    active_ownerships = (
        PropertyOwnership.query
        .filter_by(property_id=prop.id, valid_to=None).all())
    if not active_ownerships:
        raise OwnerChangeError("Für dieses Objekt ist kein aktueller Eigentümer hinterlegt.")
    for o in active_ownerships:
        if o.valid_from is not None and o.valid_from >= stichtag:
            raise OwnerChangeError(
                "Der Stichtag muss nach dem Beginn des aktuellen "
                "Besitzverhältnisses liegen.")

    new_customer_ids = [cid for cid in dict.fromkeys(new_customer_ids or []) if cid]
    if not new_customer_ids:
        raise OwnerChangeError("Bitte mindestens einen neuen Eigentümer wählen.")
    new_customers = []
    for cid in new_customer_ids:
        c = db.session.get(Customer, cid)
        if c is None or not c.active:
            raise OwnerChangeError("Ein gewählter neuer Eigentümer existiert nicht (mehr).")
        new_customers.append(c)

    active_owner_ids = {o.customer_id for o in active_ownerships}
    if set(new_customer_ids) == active_owner_ids:
        raise OwnerChangeError(
            "Die neuen Eigentümer sind identisch mit den aktuellen — kein Wechsel.")

    recipient = None
    if create_settlement:
        if settlement_recipient_id not in active_owner_ids:
            raise OwnerChangeError("Der Schlussrechnungs-Empfänger ist kein aktueller Eigentümer.")
        recipient = db.session.get(Customer, settlement_recipient_id)
        fy_error = open_fiscal_year_error(date.today())
        if fy_error:
            raise OwnerChangeError(f"{fy_error} Schlussrechnung nicht möglich.")
        if tariff is None:
            raise OwnerChangeError("Bitte einen Tarif für die Schlussrechnung wählen.")
        existing_std = (
            Invoice.query
            .filter(
                Invoice.property_id == prop.id,
                Invoice.billing_period_id == period.id,
                Invoice.invoice_kind == Invoice.KIND_STANDARD,
                Invoice.status != Invoice.STATUS_CANCELLED,
            ).first())
        if existing_std is not None:
            raise OwnerChangeError(
                f"Für {period.name} existiert bereits eine reguläre Rechnung "
                f"({existing_std.invoice_number}). Eine Schlussrechnung würde "
                f"doppelt verrechnen. Bitte den Wechsel ohne Schlussrechnung "
                f"durchführen oder die bestehende Rechnung prüfen.")

    # --- Stichtags-Ablesungen schreiben -----------------------------------
    fixed = wizard_meters(prop, period, stichtag)["fixed"]
    fixed_ids = {m.id for m, _ in fixed}
    for meter_id, data in meter_inputs.items():
        if meter_id in fixed_ids:
            continue
        meter = db.session.get(WaterMeter, meter_id)
        if meter is None:
            continue
        existing = _period_reading(meter, period)
        if (existing is not None and not existing.is_estimated
                and existing.reading_date is not None
                and existing.reading_date >= stichtag):
            warnings.append(
                f"Zähler {meter.meter_number}: bestehende echte Ablesung vom "
                f"{existing.reading_date.strftime('%d.%m.%Y')} wurde NICHT "
                f"überschrieben — Schlussrechnung nutzt den eingegebenen Stand.")
            continue
        save_reading(
            meter, period, Decimal(str(data["value"])),
            created_by_id=created_by_id, reading_date=stichtag,
            is_estimated=bool(data.get("is_estimated")))

    # --- Snapshots + OwnerChange ------------------------------------------
    snapshots, snap_warnings = collect_meter_snapshots(
        prop, period, stichtag, meter_inputs)
    warnings.extend(snap_warnings)

    oc = OwnerChange(
        property_id=prop.id,
        billing_period_id=period.id,
        change_date=stichtag,
        base_fee_mode=fee_mode,
        note=(note or None),
        created_by_id=created_by_id,
    )
    db.session.add(oc)
    db.session.flush()
    for snap in snapshots:
        db.session.add(OwnerChangeMeterValue(
            owner_change_id=oc.id,
            meter_id=snap["meter"].id,
            value_at_change=snap["value_at_change"],
            consumption_billed=snap["consumption_billed"],
            is_estimated=snap["is_estimated"],
        ))

    # --- Schlussrechnung ---------------------------------------------------
    if create_settlement:
        inv, old_days = _build_settlement_invoice(
            prop=prop, period=period, stichtag=stichtag, tariff=tariff,
            fee_mode=fee_mode, recipient=recipient, snapshots=snapshots,
            due_days=due_days, created_by_id=created_by_id)
        oc.settlement_invoice_id = inv.id
        oc.fee_days_billed = old_days

    # --- Ownership umschreiben --------------------------------------------
    prev_day = stichtag - timedelta(days=1)
    for o in active_ownerships:
        o.valid_to = prev_day
    for c in new_customers:
        db.session.add(PropertyOwnership(
            property_id=prop.id, customer_id=c.id,
            valid_from=stichtag, valid_to=None))

    # --- WG-Status ---------------------------------------------------------
    if is_wassergenossenschaft():
        for cid in (resign_customer_ids or []):
            cust = db.session.get(Customer, cid)
            if cust is None:
                continue
            profile = cust.ensure_wg_profile()
            profile.status = wg.STATUS_RESIGNED
            profile.member_until = prev_day
        for cid, upd in (new_member_updates or {}).items():
            cust = db.session.get(Customer, cid)
            if cust is None:
                continue
            profile = cust.ensure_wg_profile()
            status = upd.get("status")
            if status in wg.STATUS_LABELS:
                profile.status = status
            member_since = upd.get("member_since")
            if member_since is not None and cust.member_since is None:
                cust.member_since = member_since

    db.session.commit()
    return oc, warnings
