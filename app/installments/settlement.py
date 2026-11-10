"""Gegenverrechnung der Teilrechnungen in Jahres- und Schlussrechnung.

* ``deduct_into`` spiegelt die Teilbetrags-Zeilen jeder versendeten
  Teilrechnung negativ in die Abrechnung (gleiches Konto, Projekt, USt-Satz,
  ``deducts_invoice_id``). Abgezogen wird der vorgeschriebene Betrag; eine
  unbezahlte Teilrechnung bleibt offen und mahnbar.
* ``handle_credit`` behandelt einen negativen Saldo (Guthaben): verrechnen
  (Kappung auf 0, Uebertrag je USt-Satz als ``ReadingCorrection`` vom Typ
  ``credit_carry``) oder auszahlen (negativer Offener Posten).
* ``summary`` ist die eine Quelle fuer PDF, Word, SaaS-Designs und Detailseite.

„Verrechnet“ gilt nur, solange die verrechnende Rechnung nicht storniert ist —
ein Storno bzw. das Loeschen eines Entwurfs gibt die Teilrechnung wieder frei.
"""
from collections import OrderedDict
from decimal import Decimal

from app.extensions import db
from app.installments import terms
from app.installments.amounts import _net_for_gross
from app.invoices.amounts import line_gross, line_tax
from app.models import Invoice, InvoiceItem, ReadingCorrection

ZERO = Decimal("0")
_SENT = (Invoice.STATUS_SENT, Invoice.STATUS_PAID, Invoice.STATUS_CREDIT)
_SETTLING_KINDS = (Invoice.KIND_STANDARD, Invoice.KIND_FINAL_SETTLEMENT)


# ---------------------------------------------------------------------------
# Abfragen
# ---------------------------------------------------------------------------

def deducting_invoice(installment):
    """Die gueltige Rechnung, die ``installment`` gegenverrechnet, sonst None."""
    return (Invoice.query
            .join(InvoiceItem, InvoiceItem.invoice_id == Invoice.id)
            .filter(InvoiceItem.deducts_invoice_id == installment.id,
                    Invoice.status != Invoice.STATUS_CANCELLED,
                    Invoice.invoice_kind.in_(_SETTLING_KINDS))
            .first())


def _installments(period_id, property_id):
    return (Invoice.query
            .filter(Invoice.invoice_kind == Invoice.KIND_INSTALLMENT,
                    Invoice.billing_period_id == period_id,
                    Invoice.property_id == property_id)
            .order_by(Invoice.due_date, Invoice.id).all())


def deductible_installments(period_id, property_id, customer_id, *, due_before=None):
    """Versendete, noch nicht verrechnete Teilrechnungen an ``customer_id``.

    ``due_before``: nur vor diesem Tag faellige (Schlussrechnung beim
    Eigentuemerwechsel)."""
    out = []
    for inv in _installments(period_id, property_id):
        if inv.customer_id != customer_id or inv.status not in _SENT:
            continue
        due = scheduled_due(inv)
        if due_before is not None and due and due >= due_before:
            continue
        if deducting_invoice(inv) is None:
            out.append(inv)
    return out


def scheduled_due(inv):
    """Planmaessige Faelligkeit (aus dem Plan-Eintrag; eine verspaetet erstellte
    Teilrechnung kann spaeter faellig sein) — massgeblich beim Eigentuemerwechsel."""
    entry = inv.installment_entry
    return entry.due_date if entry is not None else inv.due_date


def foreign_installments(period_id, property_id, customer_id):
    """Versendete, unverrechnete Teilrechnungen des Objekts an einen ANDEREN
    Kontakt (Eigentuemer ohne Wizard gewechselt) — nur Warnung."""
    return [inv for inv in _installments(period_id, property_id)
            if inv.customer_id != customer_id and inv.status in _SENT
            and deducting_invoice(inv) is None]


def draft_installments(period_id, property_ids=None):
    """Nie versendete Teilrechnungs-Entwuerfe der Periode."""
    query = Invoice.query.filter(Invoice.invoice_kind == Invoice.KIND_INSTALLMENT,
                                 Invoice.billing_period_id == period_id,
                                 Invoice.status == Invoice.STATUS_DRAFT)
    if property_ids is not None:
        query = query.filter(Invoice.property_id.in_(list(property_ids) or [-1]))
    return query.order_by(Invoice.invoice_number).all()


def sent_installment_count(period_id):
    return (Invoice.query.filter(Invoice.invoice_kind == Invoice.KIND_INSTALLMENT,
                                 Invoice.billing_period_id == period_id,
                                 Invoice.status.in_(_SENT)).count())


def delete_drafts(drafts):
    """Entwuerfe loeschen (ihre Faelligkeiten sind danach wieder „geplant“).
    Eingezogene Guthaben-Uebertraege gehen zurueck. Flusht."""
    from app.meters.estimation import reverse_corrections_for_invoice
    for inv in drafts:
        if inv.status != Invoice.STATUS_DRAFT:
            continue
        reverse_corrections_for_invoice(inv)
        if inv.open_item is not None:
            db.session.delete(inv.open_item)
        db.session.delete(inv)
    db.session.flush()


# ---------------------------------------------------------------------------
# Gegenverrechnung
# ---------------------------------------------------------------------------

def installment_items(installment):
    """Die Teilbetrags-Zeilen einer Teilrechnung (ohne eingezogene Guthaben)."""
    return [it for it in installment.items
            if it.charge_key == terms.KEY_INSTALLMENT and not it.is_dunning_fee
            and not it.reading_correction_id]


def deduct_into(invoice, installments):
    """Haengt je Teilrechnung die gespiegelten Teilbetrags-Zeilen an
    ``invoice``. Flusht + rechnet den Betrag neu. Gibt die Anzahl zurueck."""
    count = 0
    for inst in installments:
        rows = installment_items(inst)
        if not rows:
            continue
        when = inst.date.strftime("%d.%m.%Y") if inst.date else ""
        label = f"abzüglich {inst.document_title} {inst.invoice_number}"
        if when:
            label += f" vom {when}"
        for it in rows:
            amount = -Decimal(str(it.amount or 0))
            invoice.items.append(InvoiceItem(
                description=label[:500], quantity=Decimal("1"), unit="Pauschal",
                unit_price=amount, amount=amount, tax_rate=it.tax_rate,
                charge_key=terms.KEY_DEDUCTION, deducts_invoice_id=inst.id,
                account_id=it.account_id, project_id=it.project_id, price_decimals=2,
            ))
        count += 1
    if count:
        db.session.flush()
        invoice.recalculate_total()
    return count


def _installment_gross(inst):
    return _sums(installment_items(inst))["gross"]


def _fully_unpaid(inst, in_collection):
    """Versendet, nichts bezahlt, nicht im Lastschrift-Einzug (die Bank zieht
    sonst trotzdem ein)."""
    if inst.status != Invoice.STATUS_SENT:
        return False
    if Decimal(str(inst.paid_amount or 0)) != 0:
        return False
    op = inst.open_item
    return op is None or op.id not in in_collection


def split_for_credit(invoice, installments):
    """Welche Teilrechnungen abgezogen und welche storniert werden.

    Abgezogen wird der vorgeschriebene Betrag (Soll). Ergaebe das ein
    Guthaben, waeren darin auch Teilrechnungen enthalten, die nie bezahlt
    wurden — der Kunde muesste sie zahlen und bekaeme dasselbe Geld als
    Guthaben zurueck. Deshalb werden ganz unbezahlte Teilrechnungen (spaeteste
    Faelligkeit zuerst) nicht abgezogen, sondern storniert, solange der Saldo
    negativ ist; die letzte darf ihn ins Plus drehen (dann eine Nachzahlung
    statt offener Teilrechnung + Guthaben). Teilweise bezahlte bleiben im Abzug.

    Gibt ``(abziehen, stornieren)`` zurueck. Schreibt nichts."""
    invoice.recalculate_total()
    balance = Decimal(str(invoice.total_amount or 0)) - sum(
        (_installment_gross(i) for i in installments), ZERO)
    if balance >= 0:
        return list(installments), []
    from app.direct_debit.services import open_item_ids_in_collection
    in_collection = set(open_item_ids_in_collection())
    unpaid = sorted((i for i in installments if _fully_unpaid(i, in_collection)),
                    key=lambda i: (scheduled_due(i) or i.date, i.id), reverse=True)
    cancel = []
    for inst in unpaid:
        if balance >= 0:
            break
        cancel.append(inst)
        balance += _installment_gross(inst)
    cancel_ids = {i.id for i in cancel}
    return [i for i in installments if i.id not in cancel_ids], cancel


def cancel_into(invoice, installments, *, created_by_id=None):
    """Storniert ``installments`` (Gutschrift-Entwurf ohne Rueckzahlung, weil
    nichts bezahlt war) und vermerkt sie auf ``invoice`` als Nullzeile mit
    ``deducts_invoice_id`` — so zeigt die Gegenverrechnung „storniert“, und
    ``deducting_invoice`` kennt die erledigende Abrechnung. Flusht. Gibt die
    Gutschriften zurueck."""
    from app.installments.owner_change import cancel_installment
    if not installments:
        return []
    for inst in installments:
        rate = next((it.tax_rate for it in installment_items(inst)), None)
        invoice.items.append(InvoiceItem(
            description=(f"{inst.document_title} {inst.invoice_number} storniert – "
                         "mit dieser Abrechnung erledigt")[:500],
            quantity=Decimal("1"), unit="Pauschal", unit_price=ZERO, amount=ZERO,
            tax_rate=rate, charge_key=terms.KEY_DEDUCTION, deducts_invoice_id=inst.id,
            price_decimals=2,
        ))
    db.session.flush()
    invoice.recalculate_total()
    # Titel erst jetzt: mit den Vermerkzeilen ist es eine Jahres-/Schlussrechnung.
    title = invoice.document_title
    credits = []
    for inst in installments:
        credit = cancel_installment(
            inst, reason=f"Mit der {title} {invoice.invoice_number} erledigt.",
            created_by_id=created_by_id)
        if credit is not None:
            credits.append(credit)
    db.session.flush()
    return credits


def settle_installments(invoice, installments, *, credit_mode, meter_id, period_id,
                        created_by_id=None):
    """Gegenverrechnung einer Abrechnung in einem Schritt: unbezahlte
    Teilrechnungen bei Guthaben stornieren (``split_for_credit``), den Rest
    abziehen, ein verbleibendes Guthaben verrechnen/auszahlen.

    Gibt ``(abgezogen, gutschriften, modus)`` zurueck. Flusht."""
    deduct, cancel = split_for_credit(invoice, installments)
    count = deduct_into(invoice, deduct)
    credits = cancel_into(invoice, cancel, created_by_id=created_by_id)
    mode = handle_credit(invoice, credit_mode, meter_id=meter_id, period_id=period_id,
                         created_by_id=created_by_id)
    return count, credits, mode


def companion_credit_notes(invoices):
    """Gutschriften der Teilrechnungen, die eine der ``invoices`` storniert hat
    (Nullzeilen aus ``cancel_into``) — sie gehoeren in dieselbe E-Mail bzw.
    dasselbe Kuvert wie die Abrechnung."""
    ids = [inv.id for inv in invoices if inv.id is not None]
    if not ids:
        return []
    inst_ids = [r[0] for r in db.session.query(InvoiceItem.deducts_invoice_id).filter(
        InvoiceItem.invoice_id.in_(ids), InvoiceItem.deducts_invoice_id.isnot(None),
        InvoiceItem.amount == 0).distinct()]
    if not inst_ids:
        return []
    return (Invoice.query.filter(Invoice.invoice_kind == Invoice.KIND_CREDIT_NOTE,
                                 Invoice.cancels_invoice_id.in_(inst_ids),
                                 Invoice.status != Invoice.STATUS_CANCELLED)
            .order_by(Invoice.invoice_number).all())


# ---------------------------------------------------------------------------
# Guthaben
# ---------------------------------------------------------------------------

def _gross_by_rate(invoice):
    groups = OrderedDict()
    for it in invoice.items:
        if it.is_dunning_fee:
            continue
        rate = Decimal(str(it.tax_rate)) if it.tax_rate else None
        groups[rate] = groups.get(rate, ZERO) + line_gross(it.amount, rate)
    return groups


def carry_credit_forward(invoice, *, meter_id, period_id, created_by_id=None):
    """Kappt eine negative Abrechnung auf 0 und traegt das Guthaben je
    USt-Satz als ``ReadingCorrection(kind=credit_carry)`` vor — die naechste
    Teilrechnung (bzw. Rechnung) des Kontakts zieht es ein. Gibt die Posten."""
    invoice.recalculate_total()
    total = Decimal(str(invoice.total_amount or 0))
    if total >= 0:
        return []
    groups = _gross_by_rate(invoice)
    negative = {r: g for r, g in groups.items() if g < 0}
    neg_sum = sum(negative.values(), ZERO)
    need = -total
    offsets = []
    remaining = need
    keys = list(negative)
    for i, rate in enumerate(keys):
        target = remaining if i == len(keys) - 1 else (need * negative[rate] / neg_sum).quantize(
            Decimal("0.01"))
        net, actual = _net_for_gross(target, rate)
        remaining -= actual
        offsets.append((rate, net))
    title = invoice.document_title
    items = []
    for rate, net in offsets:
        if net == 0:
            continue
        item = InvoiceItem(
            description="Guthaben – Verrechnung mit der nächsten Teilrechnung",
            quantity=Decimal("1"), unit="Pauschal", unit_price=net, amount=net,
            tax_rate=rate, charge_key=terms.KEY_CREDIT_CARRY, price_decimals=2,
        )
        invoice.items.append(item)
        items.append(item)
    db.session.flush()
    invoice.recalculate_total()
    # Rundung (USt je Position): nie unter 0 bleiben.
    guard = 0
    while items and Decimal(str(invoice.total_amount or 0)) < 0 and guard < 5:
        items[0].amount += Decimal("0.01")
        items[0].unit_price = items[0].amount
        db.session.flush()
        invoice.recalculate_total()
        guard += 1
    corrections = []
    for item in items:
        carried = -Decimal(str(item.amount))
        corr = ReadingCorrection(
            customer_id=invoice.customer_id, meter_id=meter_id, billing_period_id=period_id,
            source_invoice_id=invoice.id, estimated_consumption=None, real_consumption=None,
            delta_m3=None, unit_price=ZERO, tax_rate=item.tax_rate,
            label=f"Guthaben aus {title} {invoice.invoice_number}"[:100],
            kind=ReadingCorrection.KIND_CREDIT_CARRY,
            amount=carried, remaining_amount=carried,
            status=ReadingCorrection.STATUS_OPEN, created_by_id=created_by_id,
        )
        db.session.add(corr)
        corrections.append(corr)
    db.session.flush()
    return corrections


def handle_credit(invoice, mode, *, meter_id, period_id, created_by_id=None):
    """Negativer Saldo nach der Gegenverrechnung: ``offset`` traegt ihn vor,
    ``refund`` laesst die Rechnung negativ (Rueckzahlung ueber den Offenen
    Posten). Ohne Zaehler oder bei ausgenommenem Kontakt wird ausgezahlt.
    Gibt den angewandten Modus oder None (kein Guthaben) zurueck."""
    invoice.recalculate_total()
    if Decimal(str(invoice.total_amount or 0)) >= 0:
        return None
    customer = invoice.customer
    if (mode == terms.CREDIT_OFFSET and meter_id is not None
            and not (customer is not None and customer.installment_opt_out)):
        carry_credit_forward(invoice, meter_id=meter_id, period_id=period_id,
                             created_by_id=created_by_id)
        return terms.CREDIT_OFFSET
    return terms.CREDIT_REFUND


def credit_carries(invoice):
    return ReadingCorrection.query.filter_by(
        source_invoice_id=invoice.id, kind=ReadingCorrection.KIND_CREDIT_CARRY).all()


# ---------------------------------------------------------------------------
# Storno-Sperren
# ---------------------------------------------------------------------------

def cancel_blocker(invoice):
    """Grund, warum ``invoice`` nicht storniert werden darf (oder None)."""
    if invoice.is_installment:
        settling = deducting_invoice(invoice)
        if settling is not None:
            return (f"Die {invoice.document_title} ist in der {settling.document_title} "
                    f"{settling.invoice_number} verrechnet. Bitte zuerst diese stornieren.")
    for corr in credit_carries(invoice):
        if corr.status != ReadingCorrection.STATUS_OPEN:
            target = corr.applied_invoice
            ref = f" ({target.invoice_number})" if target is not None else ""
            return ("Das Guthaben dieser Abrechnung ist bereits mit einer Teilrechnung"
                    f"{ref} verrechnet. Bitte zuerst diese stornieren.")
    return None


def release_after_cancel(invoice):
    """Nach dem Storno einer Abrechnung: noch nicht eingezogene
    Guthaben-Uebertraege entfernen (sonst wuerde ein Guthaben verrechnet, das
    es nicht mehr gibt). Die Teilrechnungen werden durch das Storno von selbst
    wieder frei. Flusht."""
    for corr in credit_carries(invoice):
        if corr.status == ReadingCorrection.STATUS_OPEN:
            db.session.delete(corr)
    db.session.flush()


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------

def _sums(items):
    net = sum((Decimal(str(i.amount or 0)) for i in items), ZERO)
    taxes = OrderedDict()
    for i in items:
        if i.tax_rate and Decimal(str(i.tax_rate)) > 0:
            rate = Decimal(str(i.tax_rate))
            taxes[rate] = taxes.get(rate, ZERO) + line_tax(i.amount, rate)
    tax = sum(taxes.values(), ZERO)
    return {"net": net, "tax": tax, "gross": net + tax,
            "taxes": [{"rate": r, "tax": t} for r, t in taxes.items()]}


def display_items(invoice):
    """Positionen fuer die Positionstabelle: ohne Mahngebuehren,
    Gegenverrechnungs- und Guthaben-Uebertragszeilen."""
    if not invoice.has_deductions and not any(
            i.charge_key == terms.KEY_CREDIT_CARRY for i in invoice.items):
        return [i for i in invoice.items if not i.is_dunning_fee]
    return [i for i in invoice.items
            if not i.is_dunning_fee and not i.deducts_invoice_id
            and i.charge_key != terms.KEY_CREDIT_CARRY]


def summary(invoice):
    """Alles, was Beleg und Detailseite zum Verfahren zeigen. ``None``, wenn
    die Rechnung weder Teilrechnung noch Abrechnung mit Gegenverrechnung ist."""
    if invoice is None or invoice.is_credit_note:
        return None
    if invoice.is_installment:
        return _installment_summary(invoice)
    if not invoice.has_deductions:
        return None
    items = [i for i in invoice.items if not i.is_dunning_fee]
    regular = [i for i in items if not i.deducts_invoice_id
               and i.charge_key != terms.KEY_CREDIT_CARRY]
    carry_items = [i for i in items if i.charge_key == terms.KEY_CREDIT_CARRY]
    deductions = OrderedDict()
    for it in items:
        if it.deducts_invoice_id:
            deductions.setdefault(it.deducts_invoice_id, []).append(it)
    rows = []
    for inst_id, its in deductions.items():
        inst = db.session.get(Invoice, inst_id)
        sums = _sums(its)
        open_balance = Decimal(str(inst.open_balance)) if inst is not None else ZERO
        open_amount = open_balance if open_balance > 0 else ZERO
        gross = -sums["gross"]
        # Zahlstand fuer die Spalte „Zahlung“ der Gegenverrechnung. Eine mit
        # dieser Abrechnung stornierte Teilrechnung (Nullzeile) ist erledigt.
        if inst is not None and inst.status == Invoice.STATUS_CANCELLED:
            open_amount = ZERO
            pay_state = "cancelled"
        elif open_amount <= 0:
            pay_state = "paid"
        elif open_amount < gross:
            pay_state = "partial"
        else:
            pay_state = "open"
        rows.append({"invoice": inst, "net": -sums["net"], "tax": -sums["tax"],
                     "gross": gross, "open": open_amount, "pay_state": pay_state})
    deducted = {
        "net": sum((r["net"] for r in rows), ZERO),
        "tax": sum((r["tax"] for r in rows), ZERO),
        "gross": sum((r["gross"] for r in rows), ZERO),
    }
    reg = _sums(regular)
    balance_before_carry = reg["gross"] - deducted["gross"]
    carry = _sums(carry_items)["gross"]
    total = Decimal(str(invoice.total_amount or 0))
    open_installments = [r for r in rows if r["open"] > 0]
    open_total = sum((r["open"] for r in open_installments), ZERO)
    return {
        "kind": "settlement",
        "regular": reg,
        "deductions": rows,
        "deducted": deducted,
        "balance": balance_before_carry,
        "is_credit": balance_before_carry < 0,
        "credit": -balance_before_carry if balance_before_carry < 0 else ZERO,
        "credit_mode": (terms.CREDIT_OFFSET if carry > 0 else
                        (terms.CREDIT_REFUND if total < 0 else None)),
        "carry": carry,
        "total": total,
        "open_installments": open_installments,
        "cancelled_installments": [r for r in rows if r["pay_state"] == "cancelled"],
        "open_total": open_total,
        "due_overall": (total if total > 0 else ZERO) + open_total,
        "next_plan": next_plan_for(invoice),
        "word": terms.installment_word(),
        "words": terms.installment_words(),
    }


def next_plan_for(invoice):
    """Teilbetragsplan der Folgeperiode fuer Objekt + Empfaenger (oder None)."""
    from app.installments.plans import next_period, plan_for
    if invoice.billing_period is None or invoice.property_id is None:
        return None
    nxt = next_period(invoice.billing_period)
    if nxt is None:
        return None
    plan = plan_for(nxt.id, invoice.property_id, invoice.customer_id)
    if plan is None or not plan.active_entries:
        return None
    return {"plan": plan, "period": nxt,
            "entries": [{"seq": e.seq, "due": e.due_date, "amount": e.amount}
                        for e in plan.active_entries]}


def _installment_summary(invoice):
    entry = invoice.installment_entry
    plan = entry.plan if entry is not None else None
    others = []
    if plan is not None:
        others = [{"seq": e.seq, "due": e.due_date, "amount": e.amount}
                  for e in plan.active_entries if e.id != entry.id]
    settling = deducting_invoice(invoice)
    return {
        "kind": "installment",
        "entry": entry,
        "plan": plan,
        "seq": entry.seq if entry is not None else None,
        "count": len(plan.entries) if plan is not None else None,
        "others": others,
        "settled_by": settling,
        "word": terms.installment_word(),
        "words": terms.installment_words(),
        "period": invoice.billing_period,
    }
