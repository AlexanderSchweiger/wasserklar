"""Teilbetragsplaene berechnen und speichern.

Grundlage ist der Verbrauch der Liegenschaft in der juengsten Vorperiode mit
Ablesung, bewertet mit dem Tarif der Zielperiode — gerechnet ausschliesslich
ueber die Tarif-Engine (``resolve_charges`` + ``build_lines``), also mit
individuellen Gebuehren, Staffeln, Mindestabnahme und Wassercent. Bei
unveraendertem Tarif ist das die Jahresabrechnung der Vorperiode ÷ Anzahl,
ohne deren Einmalposten. Ohne Vorjahresverbrauch gilt der Durchschnitt der
Objekte gleicher Art (markiert), sonst nur die Pauschalen.

``compute_plans`` schreibt nichts (Vorschau); ``save_plans`` flusht nur.
"""
import json
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.installments import schedule as sched
from app.installments import terms
from app.installments.amounts import round_installment
from app.invoices import tariff_engine as engine
from app.invoices.amounts import line_gross
from app.models import (
    BillingPeriod, ChargeType, Customer, InstallmentPlan, InstallmentPlanEntry, Invoice,
    MeterReading, Property, PropertyOwnership, WaterMeter,
)

ZERO = Decimal("0")
# So weit zurueck wird nach einer Ablesung gesucht (Vorperioden).
_BASIS_LOOKBACK = 3

STATUS_NEW = "new"              # wird angelegt
STATUS_UPDATE = "update"        # vorhandener Plan wird neu berechnet
STATUS_KEEP = "keep"            # vorhandener Plan bleibt (von Hand / schon ausgestellt)
STATUS_OPT_OUT = "opt_out"      # Kontakt zahlt nur jaehrlich
STATUS_BELOW_MIN = "below_min"  # Jahresbetrag unter dem Mindestbetrag
STATUS_BILLED = "billed"        # Periode fuer das Objekt schon abgerechnet
STATUS_EMPTY = "empty"          # Jahresbetrag 0

STATUS_LABELS = {
    STATUS_NEW: "neu",
    STATUS_UPDATE: "wird aktualisiert",
    STATUS_KEEP: "bleibt unverändert",
    STATUS_OPT_OUT: "ausgenommen (nur Jahresabrechnung)",
    STATUS_BELOW_MIN: "unter dem Mindestbetrag",
    STATUS_BILLED: "Periode schon abgerechnet",
    STATUS_EMPTY: "kein Betrag",
}


class PlanError(ValueError):
    """Fachlicher Fehler mit deutscher Meldung."""


@dataclass
class Proposal:
    prop: Property
    customer: Customer
    status: str
    basis_consumption: Decimal = None
    basis_period: BillingPeriod = None
    basis_source: str = InstallmentPlan.BASIS_READING
    annual_amount: Decimal = ZERO
    installment_amount: Decimal = ZERO
    composition: list = field(default_factory=list)
    due_dates: list = field(default_factory=list)
    plan: InstallmentPlan = None
    note: str = ""

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def will_write(self):
        return self.status in (STATUS_NEW, STATUS_UPDATE)


# ---------------------------------------------------------------------------
# Grundlage
# ---------------------------------------------------------------------------

def previous_periods(period, limit=_BASIS_LOOKBACK):
    """Die Perioden vor ``period`` (juengste zuerst)."""
    return (BillingPeriod.query
            .filter(BillingPeriod.end_date < period.start_date)
            .order_by(BillingPeriod.start_date.desc(), BillingPeriod.id.desc())
            .limit(limit).all())


def next_period(period):
    """Die Periode direkt nach ``period`` oder ``None``."""
    return (BillingPeriod.query
            .filter(BillingPeriod.start_date > period.end_date)
            .order_by(BillingPeriod.start_date, BillingPeriod.id)
            .first())


def consumption_by_property(period_id):
    """``{property_id: m³}`` aus den Ablesungen einer Periode (alle Zaehler,
    auch ausgebaute — wie der Rechnungslauf)."""
    rows = (db.session.query(WaterMeter.property_id, func.sum(MeterReading.consumption))
            .join(MeterReading, MeterReading.meter_id == WaterMeter.id)
            .filter(MeterReading.billing_period_id == period_id)
            .group_by(WaterMeter.property_id).all())
    return {pid: Decimal(str(total or 0)) for pid, total in rows}


def basis_for(period):
    """``(je_objekt, durchschnitt_je_art)`` fuer die Zielperiode.

    ``je_objekt``: ``{property_id: (m³, Periode)}`` aus der juengsten
    Vorperiode, in der das Objekt eine Ablesung hat. ``durchschnitt_je_art``:
    ``{object_type: (m³, Periode)}`` aus der juengsten Vorperiode mit
    Ablesungen ueberhaupt (Rueckfall fuer neue Objekte)."""
    per_prop = {}
    averages = {}
    for prev in previous_periods(period):
        data = consumption_by_property(prev.id)
        if not data:
            continue
        for pid, qty in data.items():
            per_prop.setdefault(pid, (qty, prev))
        if not averages:
            types = dict(db.session.query(Property.id, Property.object_type)
                         .filter(Property.id.in_(list(data))).all())
            buckets = defaultdict(list)
            for pid, qty in data.items():
                if qty >= 0:
                    buckets[types.get(pid)].append(qty)
            for kind, values in buckets.items():
                averages[kind] = ((sum(values, ZERO) / len(values)).quantize(Decimal("1")), prev)
    return per_prop, averages


# ---------------------------------------------------------------------------
# Berechnung
# ---------------------------------------------------------------------------

def _vat_liable(period, today):
    from app.accounting.services import is_year_vat_liable
    year = max(today, period.start_date).year
    return is_year_vat_liable(year)


def estimate_lines(tariff, prop, customer, period, consumption, *, vat_liable, wg_mode=None):
    """Jahreszeilen (Engine-Dicts) fuer ``consumption`` m³ in ``period``."""
    charges = engine.resolve_charges(tariff, prop=prop, customer=customer, wg_mode=wg_mode)
    period_days = (period.end_date - period.start_date).days + 1
    case = engine.BillingCase(
        consumption=consumption or ZERO, vat_liable=vat_liable, period_name=period.name,
        usage_start=period.start_date, usage_end=period.end_date,
        fee_start=period.start_date, fee_end=period.end_date, period_days=period_days,
    )
    return engine.build_lines(charges, case)


def composition_from_lines(lines):
    """Anteile je (Gebuehrenart, USt-Satz) am Brutto der Jahreszeilen."""
    groups = OrderedDict()
    labels = {}
    for line in lines:
        key = line.get("charge_key") or "other"
        rate = line.get("tax_rate")
        rate = Decimal(str(rate)) if rate else None
        gross = line_gross(line["amount"], rate)
        groups[(key, rate)] = groups.get((key, rate), ZERO) + gross
        labels.setdefault(key, (line.get("description") or "").split(" (")[0].split(" –")[0])
    total = sum(groups.values(), ZERO)
    if total <= 0:
        return [], ZERO
    rows = []
    for (key, rate), gross in groups.items():
        if gross == 0:
            continue
        rows.append({"charge_key": key, "label": labels.get(key) or key,
                     "tax_rate": (str(rate) if rate is not None else None),
                     "share": str((gross / total).quantize(Decimal("0.000001")))})
    return rows, total


def _owners(prop_ids):
    owners = {}
    for pid, cust in (db.session.query(PropertyOwnership.property_id, Customer)
                      .join(Customer, Customer.id == PropertyOwnership.customer_id)
                      .filter(PropertyOwnership.property_id.in_(prop_ids or [-1]),
                              PropertyOwnership.valid_to.is_(None))
                      .order_by(PropertyOwnership.id).all()):
        owners.setdefault(pid, cust)
    return owners


def billed_property_ids(period_id):
    """Objekte, die fuer die Periode schon eine Jahres- oder Schlussrechnung
    haben (nicht storniert) — dort entstehen keine Teilrechnungen mehr."""
    return {pid for (pid,) in db.session.query(Invoice.property_id).filter(
        Invoice.billing_period_id == period_id,
        Invoice.property_id.isnot(None),
        Invoice.invoice_kind == Invoice.KIND_STANDARD,
        Invoice.status != Invoice.STATUS_CANCELLED).distinct()}


def candidate_properties():
    """Aktive Objekte mit mindestens einem aktiven Zaehler."""
    return (Property.query
            .join(WaterMeter, WaterMeter.property_id == Property.id)
            .filter(Property.active.is_(True), WaterMeter.active.is_(True))
            .distinct()
            .order_by(Property.object_number, Property.strasse, Property.id)
            .all())


def compute_plans(period, tariff, *, due_spec=None, step=None, min_annual=None, today=None,
                  excluded=None):
    """Vorschlaege fuer alle Objekte (schreibt nichts).

    ``due_spec`` = Ergebnis von ``schedule.parse_due_dates`` (Standard aus der
    Einstellung). ``excluded`` = Gebuehrenart-Schluessel, die nicht in den
    Teilbetrag eingehen (Standard aus der Einstellung; der Wasserverbrauch ist
    nie ausgeschlossen) — die Jahresabrechnung verrechnet sie dann voll.
    Wirft ``PlanError``, wenn die Faelligkeiten nicht in die Periode passen
    oder der Tarif keinen Wasserpreis hat."""
    today = today or date.today()
    if tariff is None:
        raise PlanError("Bitte einen Tarif wählen.")
    water = tariff.water_component
    if water is None or water.amount is None:
        raise PlanError("Der Tarif hat keinen Verbrauchspreis (Position „Wasserverbrauch“).")
    if due_spec is None:
        due_spec = sched.parse_due_dates(terms.due_dates_text())
    due_dates = sched.dates_for_period(due_spec, period.start_date, period.end_date)
    if not due_dates:
        raise PlanError("Keine der Fälligkeiten liegt in der Abrechnungsperiode.")
    step = terms.rounding_step() if step is None else Decimal(str(step))
    min_annual = terms.min_annual_amount() if min_annual is None else Decimal(str(min_annual))
    excluded = set(terms.excluded_charge_keys() if excluded is None else excluded)
    excluded.discard(ChargeType.KEY_WATER)

    props = candidate_properties()
    owners = _owners([p.id for p in props])
    existing = {(pl.property_id, pl.customer_id): pl for pl in
                InstallmentPlan.query.filter_by(billing_period_id=period.id).all()}
    billed = billed_property_ids(period.id)
    per_prop, averages = basis_for(period)
    vat_liable = _vat_liable(period, today)
    wg_mode = engine.wg_mode_active()

    proposals = []
    for prop in props:
        customer = owners.get(prop.id)
        if customer is None:
            continue
        plan = existing.get((prop.id, customer.id))
        p = Proposal(prop=prop, customer=customer, status=STATUS_NEW, plan=plan,
                     due_dates=list(due_dates))
        if prop.id in billed:
            p.status = STATUS_BILLED
            proposals.append(p)
            continue
        if customer.installment_opt_out:
            p.status = STATUS_OPT_OUT
            proposals.append(p)
            continue
        if plan is not None and (plan.manual or plan.has_issued):
            p.status = STATUS_KEEP
            p.note = ("Betrag von Hand festgelegt" if plan.manual
                      else "Teilrechnungen bereits ausgestellt")
            p.annual_amount = plan.annual_amount
            p.installment_amount = plan.installment_amount
            p.basis_consumption = plan.basis_consumption
            p.basis_period = plan.basis_period
            p.basis_source = plan.basis_source
            proposals.append(p)
            continue

        if prop.id in per_prop:
            qty, src_period = per_prop[prop.id]
            p.basis_source = InstallmentPlan.BASIS_READING
        elif prop.object_type in averages:
            qty, src_period = averages[prop.object_type]
            p.basis_source = InstallmentPlan.BASIS_AVERAGE
            p.note = f"kein Vorjahresverbrauch – Durchschnitt „{prop.object_type}“"
        else:
            qty, src_period = ZERO, None
            p.basis_source = InstallmentPlan.BASIS_NONE
            p.note = "kein Verbrauch bekannt – nur Pauschalen"
        if qty < 0:
            qty = ZERO
        p.basis_consumption, p.basis_period = qty, src_period

        lines = estimate_lines(tariff, prop, customer, period, qty,
                               vat_liable=vat_liable, wg_mode=wg_mode)
        if excluded:
            # Vor der Zusammensetzung filtern: Jahresbetrag, Mindestbetrag und
            # Konto-/USt-Gruppen beziehen sich dann nur auf die eingerechneten
            # Gebuehren. Auffuellzeilen (Mindestabnahme) tragen den Schluessel
            # ihrer m³-Position und folgen ihr.
            lines = [ln for ln in lines if ln.get("charge_key") not in excluded]
        p.composition, p.annual_amount = composition_from_lines(lines)
        if p.annual_amount <= 0:
            p.status = STATUS_EMPTY
        elif min_annual and p.annual_amount < min_annual:
            p.status = STATUS_BELOW_MIN
        else:
            p.installment_amount = round_installment(p.annual_amount, len(due_dates), step)
            p.status = STATUS_UPDATE if plan is not None else STATUS_NEW
        proposals.append(p)
    return proposals


def summarize(proposals):
    counts = defaultdict(int)
    total = ZERO
    for p in proposals:
        counts[p.status] += 1
        if p.status in (STATUS_NEW, STATUS_UPDATE, STATUS_KEEP):
            total += p.installment_amount * len(p.due_dates or [None])
    return {"counts": dict(counts), "total": total,
            "write": counts[STATUS_NEW] + counts[STATUS_UPDATE]}


# ---------------------------------------------------------------------------
# Speichern / Bearbeiten
# ---------------------------------------------------------------------------

def save_plans(period, tariff, proposals, *, created_by_id=None):
    """Legt die Vorschlaege mit Status neu/aktualisieren an. Flusht, committet
    nicht. Gibt ``(angelegt, aktualisiert)`` zurueck."""
    created = updated = 0
    for p in proposals:
        if not p.will_write:
            continue
        plan = p.plan
        if plan is None:
            plan = InstallmentPlan(billing_period_id=period.id, property_id=p.prop.id,
                                   customer_id=p.customer.id, created_by_id=created_by_id)
            db.session.add(plan)
            created += 1
        else:
            updated += 1
        plan.tariff_id = tariff.id if tariff is not None else None
        plan.basis_period_id = p.basis_period.id if p.basis_period else None
        plan.basis_consumption = p.basis_consumption
        plan.basis_source = p.basis_source
        plan.annual_amount = p.annual_amount
        plan.installment_amount = p.installment_amount
        plan.composition = json.dumps(p.composition)
        plan.manual = False
        _sync_entries(plan, p.due_dates, p.installment_amount)
    db.session.flush()
    return created, updated


def _sync_entries(plan, due_dates, amount):
    """Eintraege an Faelligkeiten/Betrag angleichen — ausgestellte bleiben,
    wie sie sind; neue Faelligkeiten kommen dazu."""
    by_seq = {e.seq: e for e in plan.entries}
    for seq, due in enumerate(due_dates, start=1):
        entry = by_seq.get(seq)
        if entry is None:
            plan.entries.append(InstallmentPlanEntry(seq=seq, due_date=due, amount=amount))
            continue
        if entry.active_invoice is None:
            entry.due_date = due
            entry.amount = amount
    for seq, entry in by_seq.items():
        if seq > len(due_dates) and entry.active_invoice is None and not entry.invoices:
            plan.entries.remove(entry)


def update_plan(plan, *, installment_amount=None, skipped_seqs=None, notes=None):
    """Plan von Hand aendern: neuer Teilbetrag fuer alle noch nicht
    ausgestellten Faelligkeiten (macht den Plan „manuell“), Faelligkeiten
    auslassen. Flusht."""
    if installment_amount is not None:
        installment_amount = Decimal(str(installment_amount)).quantize(Decimal("0.01"))
        if installment_amount < 0:
            raise PlanError("Der Teilbetrag darf nicht negativ sein.")
        if installment_amount != plan.installment_amount:
            plan.installment_amount = installment_amount
            plan.manual = True
            for e in plan.entries:
                if e.active_invoice is None:
                    e.amount = installment_amount
    if skipped_seqs is not None:
        for e in plan.entries:
            if e.active_invoice is None:
                e.skipped = e.seq in skipped_seqs
    if notes is not None:
        plan.notes = notes.strip() or None
    db.session.flush()


def delete_plan(plan):
    if any(e.invoices for e in plan.entries):
        raise PlanError("Zu diesem Plan gibt es schon Teilrechnungen – er kann nicht "
                        "gelöscht werden. Einzelne Fälligkeiten lassen sich auslassen.")
    db.session.delete(plan)
    db.session.flush()


def plan_for(period_id, property_id, customer_id):
    return InstallmentPlan.query.filter_by(billing_period_id=period_id,
                                           property_id=property_id,
                                           customer_id=customer_id).first()
