"""Teilrechnungen beim Eigentuemerwechsel.

Regel (Stichtag S, Periode P, Objekt X):

* Teilrechnungen des Altbesitzers, die **vor** S faellig sind, verrechnet
  seine Schlussrechnung (``settlement.deduct_into`` mit ``due_before=S``,
  Aufruf in ``owner_change.services._build_settlement_invoice``).
* Ab S faellige, bereits versendete werden **storniert** (Gutschrift-Entwurf;
  was schon bezahlt war, geht ueber deren Rueckzahlungs-Posten zurueck).
* Nie versendete Entwuerfe werden geloescht, seine Plan-Faelligkeiten ab S
  entfallen.
* Der Kaeufer uebernimmt die restlichen Faelligkeiten mit demselben Betrag
  (eigener Plan, ``transfer=True``).

Alles im Rahmen der Transaktion des Wechsels (flusht, committet nicht).
"""
from app.extensions import db
from app.installments import settlement
from app.installments import terms
from app.models import Invoice, InstallmentPlan, InstallmentPlanEntry


def preview(prop, period, stichtag, seller_ids):
    """Was der Wechsel mit den Teilrechnungen tut (fuer den Bestaetigen-Schritt)."""
    out = {"deduct": [], "cancel": [], "delete": [], "skip": 0, "plan": None}
    for inv in settlement._installments(period.id, prop.id):
        if inv.customer_id not in seller_ids or inv.status == Invoice.STATUS_CANCELLED:
            continue
        if inv.status == Invoice.STATUS_DRAFT:
            out["delete"].append(inv)
        elif settlement.deducting_invoice(inv) is not None:
            continue
        elif settlement.scheduled_due(inv) and settlement.scheduled_due(inv) >= stichtag:
            out["cancel"].append(inv)
        else:
            out["deduct"].append(inv)
    for plan in InstallmentPlan.query.filter(
            InstallmentPlan.billing_period_id == period.id,
            InstallmentPlan.property_id == prop.id,
            InstallmentPlan.customer_id.in_(list(seller_ids) or [-1])).all():
        out["plan"] = out["plan"] or plan
        out["skip"] += sum(1 for e in plan.entries
                           if not e.skipped and e.due_date >= stichtag
                           and e.active_invoice is None)
    out["any"] = bool(out["deduct"] or out["cancel"] or out["delete"] or out["plan"])
    return out


def cancel_installment(inv, *, reason, created_by_id=None):
    """Storniert eine versendete Teilrechnung wie der Storno-Dialog: Buchungen
    zurueck, Posten erledigt, Mahnungen storniert, Gutschrift als Entwurf."""
    from app.accounting import services as acc_svc
    from app.dunning.services import cancel_dunnings_for_invoice
    from app.invoices.credit_note import create_credit_note, credit_note_blocker
    from app.models import OpenItem
    err = acc_svc.storno_invoice_bookings(inv, reason=reason, created_by_id=created_by_id)
    if err:
        raise ValueError(err)
    inv.status = Invoice.STATUS_CANCELLED
    if inv.open_item is not None:
        inv.open_item.status = OpenItem.STATUS_PAID
    cancel_dunnings_for_invoice(inv)
    if credit_note_blocker(inv) is None:
        return create_credit_note(inv, reason=reason, created_by_id=created_by_id)
    return None


def apply(*, prop, period, stichtag, seller_ids, buyers, settlement_invoice=None,
          transfer=True, created_by_id=None):
    """Teilrechnungen/Plaene nach der Regel oben umstellen. Gibt Hinweise zurueck."""
    title = terms.installment_title()
    info = preview(prop, period, stichtag, seller_ids)
    notes = []
    if not info["any"]:
        return notes

    if info["deduct"] and settlement_invoice is None:
        notes.append(
            f"{len(info['deduct'])} {title}(en) des Altbesitzers wurden nicht verrechnet "
            "(Wechsel ohne Schlussrechnung) – bitte prüfen.")

    if info["delete"]:
        settlement.delete_drafts(info["delete"])
        notes.append(f"{len(info['delete'])} nie versendete {title}-Entwürfe gelöscht.")

    credits = []
    for inv in info["cancel"]:
        credit = cancel_installment(
            inv, reason=f"Eigentümerwechsel zum {stichtag.strftime('%d.%m.%Y')}",
            created_by_id=created_by_id)
        if credit is not None:
            credits.append(credit)
    if info["cancel"]:
        notes.append(
            f"{len(info['cancel'])} nach dem Stichtag fällige {title}(en) storniert"
            + (f" – Gutschriften {', '.join(c.invoice_number for c in credits)} als Entwurf"
               " (bitte versenden)" if credits else "") + ".")

    seller_plan = info["plan"]
    remaining = []
    for plan in InstallmentPlan.query.filter(
            InstallmentPlan.billing_period_id == period.id,
            InstallmentPlan.property_id == prop.id,
            InstallmentPlan.customer_id.in_(list(seller_ids) or [-1])).all():
        for e in plan.entries:
            if e.skipped or e.due_date < stichtag:
                continue
            if e.active_invoice is None:
                remaining.append(e)
                e.skipped = True

    if transfer and seller_plan is not None and buyers:
        buyer = buyers[0]
        if buyer.installment_opt_out:
            notes.append(f"{buyer.name} zahlt nur mit der Jahresabrechnung – keine "
                         f"{terms.installment_words()} übernommen.")
        elif InstallmentPlan.query.filter_by(billing_period_id=period.id, property_id=prop.id,
                                             customer_id=buyer.id).first() is None:
            dues = sorted({(e.seq, e.due_date, e.amount) for e in seller_plan.entries
                           if e.due_date >= stichtag})
            if dues:
                plan = InstallmentPlan(
                    billing_period_id=period.id, property_id=prop.id, customer_id=buyer.id,
                    tariff_id=seller_plan.tariff_id, basis_period_id=seller_plan.basis_period_id,
                    basis_consumption=seller_plan.basis_consumption,
                    basis_source=seller_plan.basis_source,
                    annual_amount=seller_plan.annual_amount,
                    installment_amount=seller_plan.installment_amount,
                    composition=seller_plan.composition, manual=seller_plan.manual,
                    notes=f"Übernommen beim Eigentümerwechsel zum {stichtag.strftime('%d.%m.%Y')}.",
                    created_by_id=created_by_id)
                db.session.add(plan)
                for seq, due, amount in dues:
                    plan.entries.append(InstallmentPlanEntry(seq=seq, due_date=due,
                                                             amount=amount))
                notes.append(f"{buyer.name} übernimmt {len(dues)} {terms.installment_words()} "
                             f"(ab {dues[0][1].strftime('%d.%m.%Y')}) – unter "
                             f"{terms.installment_words()} erstellen.")
    db.session.flush()
    return notes
