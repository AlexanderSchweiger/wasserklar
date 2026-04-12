"""
Service-Layer für das Mahnwesen (ADR-003).

Alle DB-Mutationen ausschließlich hier, Routen orchestrieren nur.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    DunningNotice, DunningPolicy, DunningStage,
    Invoice, InvoiceItem,
)


# ---------------------------------------------------------------------------
# Gebührenberechnung
# ---------------------------------------------------------------------------

def compute_fee(stage, principal_amount):
    """Berechnet die Mahngebühr aus Stage-Parametern und Hauptforderung.

    Formel: fee_fixed + (principal_amount * fee_percent / 100),
    begrenzt durch fee_min / fee_max (falls gesetzt).
    """
    fixed = Decimal(str(stage.fee_fixed or 0))
    percent = Decimal(str(stage.fee_percent or 0))
    principal = Decimal(str(principal_amount or 0))

    fee = fixed
    if percent > 0:
        pct_fee = (principal * percent / Decimal("100")).quantize(Decimal("0.01"))
        fee_min = Decimal(str(stage.fee_min)) if stage.fee_min is not None else None
        fee_max = Decimal(str(stage.fee_max)) if stage.fee_max is not None else None
        if fee_min is not None:
            pct_fee = max(pct_fee, fee_min)
        if fee_max is not None:
            pct_fee = min(pct_fee, fee_max)
        fee += pct_fee

    return fee.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Fälligkeitsermittlung
# ---------------------------------------------------------------------------

def current_dunning_level(invoice):
    """Höchste aktive Mahnstufe einer Rechnung (0 = keine Mahnung)."""
    from sqlalchemy import func
    result = (
        db.session.query(func.max(DunningNotice.level_snapshot))
        .filter(
            DunningNotice.invoice_id == invoice.id,
            DunningNotice.status == DunningNotice.STATUS_AKTIV,
        )
        .scalar()
    )
    return result or 0


def eligible_invoices_for_stage(policy, today=None):
    """Ermittelt überfällige Rechnungen und die nächste fällige Mahnstufe.

    Gibt eine Liste von Tupeln ``(invoice, stage)`` zurück — eine Rechnung
    pro Eintrag, jeweils mit der höchsten Stage, für die sie reif ist und
    die über ihrer aktuellen Mahnstufe liegt.
    """
    if today is None:
        today = date.today()

    # Nur aktive Stages dieser Policy, aufsteigend nach Level
    stages = (
        DunningStage.query
        .filter_by(policy_id=policy.id, active=True)
        .order_by(DunningStage.level.asc())
        .all()
    )
    if not stages:
        return []

    # Alle Rechnungen mit Status "Versendet" und überschrittenem due_date
    min_days = stages[0].days_after_due
    cutoff = today - timedelta(days=min_days)
    overdue_invoices = (
        Invoice.query
        .filter(
            Invoice.status == Invoice.STATUS_SENT,
            Invoice.due_date != None,   # noqa: E711
            Invoice.due_date <= cutoff,
        )
        .all()
    )

    results = []
    for inv in overdue_invoices:
        days_overdue = (today - inv.due_date).days
        cur_level = current_dunning_level(inv)

        # Höchste Stage finden, für die days_after_due <= days_overdue
        target_stage = None
        for stage in stages:
            if stage.days_after_due <= days_overdue and stage.level > cur_level:
                target_stage = stage

        if target_stage is not None:
            results.append((inv, target_stage))

    return results


# ---------------------------------------------------------------------------
# Mahnung erzeugen
# ---------------------------------------------------------------------------

def create_dunning_notice(invoice, stage, created_by_id):
    """Legt eine DunningNotice + ggf. Fee-InvoiceItem atomar an.

    Status wird direkt auf 'Aktiv' gesetzt (kein Entwurf-Zwischenstatus).
    ``recalculate_total()`` wird NICHT aufgerufen (Fee-Items sind Phantom).
    """
    principal = Decimal(str(invoice.total_amount or 0))
    fee = compute_fee(stage, principal)
    new_due = date.today() + timedelta(days=stage.new_due_days or 14)

    notice = DunningNotice(
        invoice_id=invoice.id,
        stage_id=stage.id,
        level_snapshot=stage.level,
        name_snapshot=stage.name,
        print_title_snapshot=stage.print_title or stage.name,
        issued_date=date.today(),
        new_due_date=new_due,
        fee_amount=fee,
        status=DunningNotice.STATUS_AKTIV,
        created_by_id=created_by_id,
    )
    db.session.add(notice)
    db.session.flush()

    # Fee-Item nur anlegen, wenn Gebühr > 0
    fee_item = None
    if fee > 0:
        fee_item = InvoiceItem(
            invoice_id=invoice.id,
            description=f"Mahngebühr – {stage.name}",
            quantity=Decimal("1"),
            unit="Stk",
            unit_price=fee,
            amount=fee,
            tax_rate=None,  # Mahngebühren nicht USt-pflichtig (ADR-003 §6)
            is_dunning_fee=1,
            dunning_notice_id=notice.id,
        )
        db.session.add(fee_item)
        db.session.flush()
        notice.fee_invoice_item_id = fee_item.id

    db.session.flush()
    return notice


# ---------------------------------------------------------------------------
# Reset (Stufe zurücksetzen)
# ---------------------------------------------------------------------------

def reset_dunning_notice(notice, user, reason=None):
    """Setzt eine Mahnung auf 'Zurückgesetzt' und entfernt deren Fee-Item.

    Nur die *aktuelle* Notice — für Mehrfach-Reset mehrfach aufrufen.
    """
    notice.status = DunningNotice.STATUS_ZURUECKGESETZT
    notice.reset_at = datetime.now(UTC)
    notice.reset_by_id = user.id
    notice.reset_reason = reason

    # Fee-Item löschen (Lock-Bypass für is_dunning_fee=True)
    if notice.fee_invoice_item:
        db.session.delete(notice.fee_invoice_item)
        notice.fee_invoice_item_id = None

    db.session.flush()


# ---------------------------------------------------------------------------
# Defer (Nachfrist verlängern)
# ---------------------------------------------------------------------------

def defer_dunning_notice(notice, new_due_date, user):
    """Verlängert die Nachfrist einer aktiven Mahnung.

    Status bleibt 'Aktiv', Fee bleibt bestehen.
    Audit-Eintrag wird in notice.notes angehängt.
    """
    old_due = notice.new_due_date
    notice.new_due_date = new_due_date

    audit = (
        f"[{datetime.now(UTC):%Y-%m-%d %H:%M}] "
        f"Nachfrist verlängert von {old_due} auf {new_due_date} "
        f"durch {user.username}"
    )
    if notice.notes:
        notice.notes += "\n" + audit
    else:
        notice.notes = audit

    db.session.flush()


# ---------------------------------------------------------------------------
# Storno aller Mahnungen einer Rechnung
# ---------------------------------------------------------------------------

def cancel_dunnings_for_invoice(invoice):
    """Setzt alle aktiven Notices einer Rechnung auf 'Storniert' und löscht Fee-Items.

    Wird aufgerufen, wenn die Rechnung selbst storniert wird.
    """
    active_notices = (
        DunningNotice.query
        .filter_by(invoice_id=invoice.id, status=DunningNotice.STATUS_AKTIV)
        .all()
    )
    for notice in active_notices:
        notice.status = DunningNotice.STATUS_STORNIERT
        if notice.fee_invoice_item:
            db.session.delete(notice.fee_invoice_item)
            notice.fee_invoice_item_id = None

    db.session.flush()


# ---------------------------------------------------------------------------
# Zusammenfassung für PDF / Anzeige
# ---------------------------------------------------------------------------

def dunning_summary(invoice):
    """Liefert eine Zusammenfassung aller aktiven Mahnungen einer Rechnung.

    Returns dict mit:
        - level: höchste aktive Stufe (int)
        - notices: Liste der aktiven DunningNotice-Objekte
        - total_fees: Summe aller aktiven Mahngebühren (Decimal)
        - principal: Hauptforderung (= invoice.total_amount)
        - gross_total: Hauptforderung + Mahngebühren
    """
    notices = (
        DunningNotice.query
        .filter_by(invoice_id=invoice.id, status=DunningNotice.STATUS_AKTIV)
        .order_by(DunningNotice.level_snapshot.asc())
        .all()
    )

    principal = Decimal(str(invoice.total_amount or 0))
    total_fees = sum(
        (Decimal(str(n.fee_amount or 0)) for n in notices),
        Decimal("0"),
    )

    return {
        "level": max((n.level_snapshot for n in notices), default=0),
        "notices": notices,
        "total_fees": total_fees,
        "principal": principal,
        "gross_total": principal + total_fees,
    }
