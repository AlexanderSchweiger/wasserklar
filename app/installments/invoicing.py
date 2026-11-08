"""Teilrechnungen aus den Faelligkeiten der Teilbetragsplaene erstellen.

Jede Teilrechnung entsteht als Entwurf in einem eigenen Rechnungslauf
(``BillingRun.run_kind = "installment"``) — damit gelten Versenden-Dialog,
Sammel-PDF, Post-Druck, Excel-Export und Lauf-Loeschen unveraendert.

Positionen je (Konto, USt-Satz) aus der Plan-Zusammensetzung: die Zahlung
splittet ``_split_invoice_by_dimensions`` so auf die richtigen Erloeskonten,
und die USt entsteht bei Vereinnahmung (Buchungen).
"""
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.installments import terms
from app.installments.amounts import split_gross
from app.installments.plans import billed_property_ids
from app.invoices import tariff_engine as engine
from app.models import (
    BillingRun, ChargeType, Invoice, InvoiceItem, InstallmentPlanEntry, ReadingCorrection,
)
from app.utils import next_invoice_number

ZERO = Decimal("0")
# Eine verspaetet erstellte Teilrechnung wird fruehestens so viele Tage nach
# dem Rechnungsdatum faellig (Vorabankuendigung bei Lastschrift).
MIN_DUE_DAYS = 14


class InstallmentError(ValueError):
    """Fachlicher Fehler mit deutscher Meldung."""


@dataclass
class CreateResult:
    run: BillingRun = None
    created: list = field(default_factory=list)
    skipped: list = field(default_factory=list)     # (Eintrag, Grund)


def skip_reason(entry, *, billed=None):
    """Warum fuer diese Faelligkeit keine Teilrechnung entstehen kann (oder None)."""
    plan = entry.plan
    if entry.skipped:
        return "Fälligkeit ausgelassen"
    if entry.active_invoice is not None:
        return f"schon ausgestellt ({entry.active_invoice.invoice_number})"
    if billed is not None and plan.property_id in billed:
        return "Periode für das Objekt schon abgerechnet"
    if plan.customer is not None and plan.customer.installment_opt_out:
        return "Kontakt zahlt nur mit der Jahresabrechnung"
    owner = plan.property.current_owner() if plan.property is not None else None
    if owner is None or owner.customer_id != plan.customer_id:
        return "Rechnungsempfänger ist nicht mehr Eigentümer"
    if Decimal(str(entry.amount or 0)) <= 0:
        return "Teilbetrag 0"
    return None


def _groups(plan, vat_liable):
    """Positionsgruppen ``[{account_id, tax_rate, share, label}]`` je (Konto,
    USt-Satz). Konto aus der Tarifposition der Gebuehrenart (Plan-Tarif)."""
    tariff = plan.tariff
    water = tariff.water_component if tariff is not None else None
    rows = plan.composition_rows
    if not rows:
        rate = water.tax_rate if water is not None else None
        rows = [{"charge_key": ChargeType.KEY_WATER, "label": "", "tax_rate": rate,
                 "share": Decimal("1")}]
    merged = OrderedDict()
    for r in rows:
        comp = tariff.component(r["charge_key"]) if tariff is not None else None
        account_id = (comp.account_id if comp is not None else None) or (
            water.account_id if water is not None else None)
        rate = engine.effective_tax(r.get("tax_rate"), vat_liable)
        rate = Decimal(str(rate)) if rate else None
        g = merged.setdefault((account_id, rate), {
            "account_id": account_id, "tax_rate": rate, "share": ZERO, "labels": []})
        g["share"] += Decimal(str(r.get("share") or 0))
        if r.get("label") and r["label"] not in g["labels"]:
            g["labels"].append(r["label"])
    return list(merged.values())


def build_items(entry, vat_liable, *, project_id=None):
    """Positionen (ungespeichert) fuer die Teilrechnung zu ``entry``."""
    plan = entry.plan
    period = plan.billing_period
    count = len(plan.entries)
    word = terms.installment_word()
    base = f"{entry.seq}. {word} {period.name}" + (f" (von {count})" if count > 1 else "")
    groups = _groups(plan, vat_liable)
    parts = split_gross(Decimal(str(entry.amount)), groups)
    items = []
    for part in parts:
        desc = base
        if len(parts) > 1 and part["labels"]:
            desc = f"{base} – {', '.join(part['labels'])}"
        items.append(InvoiceItem(
            description=desc[:500], quantity=Decimal("1"), unit="Pauschal",
            unit_price=part["net"], amount=part["net"], tax_rate=part["tax_rate"],
            charge_key=terms.KEY_INSTALLMENT, account_id=part["account_id"],
            project_id=project_id, price_decimals=2,
        ))
    return items


def create_installment_invoices(period, entries, *, created_by_id=None, project_id=None,
                                sort_order="customer_name", source_run=None, today=None):
    """Teilrechnungen (Entwuerfe) fuer ``entries`` in einem neuen Lauf.

    Ueberspringt Faelligkeiten, fuer die keine Teilrechnung entstehen darf
    (``skip_reason``). Zieht offene Guthaben-Uebertraege des Kontakts in seine
    Teilrechnungen ein, frueheste Faelligkeit zuerst. Flusht, committet nicht.
    Wirft ``InstallmentError`` (kein offenes Buchungsjahr, nichts zu tun)."""
    from app.accounting.services import is_year_vat_liable, open_fiscal_year_error

    today = today or date.today()
    fy_error = open_fiscal_year_error(today)
    if fy_error:
        raise InstallmentError(f"{fy_error} Teilrechnungen können nicht erstellt werden.")
    vat_liable = is_year_vat_liable(today.year)
    billed = billed_property_ids(period.id)

    result = CreateResult()
    todo = []
    for entry in entries:
        reason = skip_reason(entry, billed=billed)
        if reason:
            result.skipped.append((entry, reason))
        else:
            todo.append(entry)
    if not todo:
        return result

    tariff_names = sorted({e.plan.tariff.name for e in todo if e.plan.tariff is not None})
    run = BillingRun(
        billing_period_id=period.id, created_by_id=created_by_id,
        tariff_name=(", ".join(tariff_names) or terms.installment_title())[:100],
        tariff_price_per_m3=None, run_kind=BillingRun.KIND_INSTALLMENT,
        sort_order=sort_order, project_id=project_id,
        source_run_id=source_run.id if source_run is not None else None,
    )
    db.session.add(run)
    db.session.flush()

    todo.sort(key=lambda e: (_sort_key(e.plan, sort_order), e.due_date, e.seq))
    earliest = today + timedelta(days=MIN_DUE_DAYS)
    by_customer = defaultdict(list)
    for entry in todo:
        plan = entry.plan
        inv = Invoice(
            invoice_number=next_invoice_number(today.year),
            customer_id=plan.customer_id, property_id=plan.property_id,
            billing_run_id=run.id, billing_period_id=period.id,
            invoice_kind=Invoice.KIND_INSTALLMENT, installment_entry_id=entry.id,
            date=today, due_date=max(entry.due_date, earliest),
            status=Invoice.STATUS_DRAFT, created_by_id=created_by_id,
        )
        db.session.add(inv)
        db.session.flush()
        for item in build_items(entry, vat_liable, project_id=project_id):
            inv.items.append(item)
        db.session.flush()
        inv.recalculate_total()
        by_customer[plan.customer_id].append(inv)
        result.created.append(inv)

    # Guthaben aus der letzten Jahresabrechnung: frueheste Teilrechnung zuerst,
    # der Rest wandert (wie bisher) auf die naechste.
    for customer_id, invoices in by_customer.items():
        for inv in sorted(invoices, key=lambda i: (i.due_date, i.id)):
            applied = apply_credit_carries(inv, customer_id)
            if not applied:
                continue
            fallback = next((it.account_id for it in inv.items if it.account_id), None)
            for it in inv.items:
                if it.account_id is None:
                    it.account_id = fallback
                if it.project_id is None:
                    it.project_id = project_id

    run.invoices_created = len(result.created)
    run.invoices_skipped = len(result.skipped)
    db.session.flush()
    result.run = run
    return result


def apply_credit_carries(inv, customer_id):
    """Guthaben-Uebertraege des Kontakts in die Teilrechnung ``inv`` einziehen.

    Wie ``apply_corrections_to_invoice`` (nie unter 0, Rest wandert weiter),
    aber cent-genau: deckt das Guthaben die Teilrechnung ganz, werden deren
    Teilbetrags-Zeilen desselben USt-Satzes gespiegelt — so steht sie exakt
    auf 0 statt auf einem Rundungs-Cent. Flusht. Gibt die Posten zurueck."""
    from app.meters.estimation import (
        _add_correction_item, _item_gross, _safe_partial_credit_net,
        open_corrections_for_customer)
    corrs = [c for c in open_corrections_for_customer(
        customer_id, kinds={ReadingCorrection.KIND_CREDIT_CARRY})
        if Decimal(str(c.remaining_amount)) < 0]
    applied = []
    for corr in corrs:
        inv.recalculate_total()
        headroom = Decimal(str(inv.total_amount or 0))
        if headroom <= 0:
            break
        remaining = Decimal(str(corr.remaining_amount))       # negativ, netto
        rate = Decimal(str(corr.tax_rate)) if corr.tax_rate else None
        pieces = None                            # Netto je Korrekturzeile
        # Brutto, das von diesem Guthaben noch offen ist — aus dem Ursprung
        # abzueglich der schon gebuchten Zeilen (sonst kostet die USt-Rundung
        # je Zeile ueber mehrere Teilrechnungen Cents).
        done = InvoiceItem.query.filter_by(reading_correction_id=corr.id).all()
        open_gross = abs(_item_gross(corr.amount, rate or 0)) - sum(
            (abs(_item_gross(it.amount, rate or 0)) for it in done), ZERO)
        if open_gross <= headroom:
            apply_net = remaining
            if done:
                from app.installments.amounts import _net_for_gross
                pieces = [-_net_for_gross(open_gross, rate)[0]]
        else:
            same_rate = [it for it in inv.items
                         if (Decimal(str(it.tax_rate)) if it.tax_rate else None) == rate
                         and not it.is_dunning_fee]
            rate_gross = sum((_item_gross(it.amount, rate or 0) for it in same_rate), ZERO)
            rate_net = sum((Decimal(str(it.amount)) for it in same_rate), ZERO)
            if rate_gross == headroom and rate_net > 0 and rate_net <= -remaining:
                # genau die Zeilen dieses Satzes spiegeln (je Zeile — die USt
                # rundet je Position, eine Summenzeile laege um Cents daneben)
                pieces = [-Decimal(str(it.amount)) for it in same_rate]
                apply_net = -rate_net
            else:
                part = _safe_partial_credit_net(headroom, rate)
                if part <= 0:
                    break
                apply_net = -part
        partial = apply_net != remaining
        for net in (pieces or [apply_net]):
            _add_correction_item(inv, corr, net, partial=partial)
        corr.remaining_amount = Decimal("0.00") if not partial else remaining - apply_net
        corr.status = (ReadingCorrection.STATUS_PARTIAL if partial
                       else ReadingCorrection.STATUS_APPLIED)
        corr.applied_invoice_id = inv.id
        applied.append(corr)
    inv.recalculate_total()
    return applied


def _sort_key(plan, sort_order):
    prop, cust = plan.property, plan.customer
    if sort_order == "customer_number":
        return (cust.customer_number or 0, cust.name or "")
    if sort_order == "object_number":
        return (prop.object_number or "", prop.id)
    if sort_order == "address":
        return ((prop.strasse or "").lower(), prop.hausnummer or "", prop.id)
    return ((cust.name or "").lower(), prop.id)


def open_entries(period, *, seqs=None, due_until=None):
    """Noch nicht ausgestellte Faelligkeiten der Periode (Auswahl im Dialog)."""
    from app.models import InstallmentPlan
    query = (InstallmentPlanEntry.query
             .join(InstallmentPlan, InstallmentPlan.id == InstallmentPlanEntry.plan_id)
             .filter(InstallmentPlan.billing_period_id == period.id,
                     InstallmentPlanEntry.skipped.is_(False)))
    if seqs:
        query = query.filter(InstallmentPlanEntry.seq.in_(list(seqs)))
    if due_until:
        query = query.filter(InstallmentPlanEntry.due_date <= due_until)
    return [e for e in query.order_by(InstallmentPlanEntry.due_date).all()
            if e.active_invoice is None]
