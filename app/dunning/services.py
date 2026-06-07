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
# Stufentexte (pro Stufe konfigurierbar, mit Default-Fallback)
# ---------------------------------------------------------------------------
#
# Die Defaults reproduzieren das frühere hartcodierte Verhalten 1:1, damit
# Bestands-Stufen ohne hinterlegten Text unverändert mahnen. In allen Texten
# sind Jinja-Platzhalter erlaubt — siehe ``dunning_text_context`` für die Liste.

DEFAULT_LETTER_INTRO = (
    "zu unserer Rechnung {{ rechnungsnummer }} vom {{ rechnungsdatum }} "
    "mit Fälligkeit am {{ faelligkeit }} konnten wir bisher leider keinen "
    "Zahlungseingang feststellen."
)
DEFAULT_LETTER_CLOSING_SOFT = (
    "Sollte sich Ihre Zahlung mit diesem Schreiben gekreuzt haben, "
    "betrachten Sie dieses bitte als gegenstandslos."
)
DEFAULT_LETTER_CLOSING_HARD = (
    "Wir bitten Sie dringend, den ausstehenden Betrag innerhalb der "
    "genannten Frist zu begleichen, um weitere Maßnahmen zu vermeiden."
)
DEFAULT_EMAIL_SUBJECT = "{{ mahntitel }} – {{ rechnungsnummer }}"
DEFAULT_EMAIL_BODY = (
    "Sehr geehrte Damen und Herren,\n\n"
    "anbei erhalten Sie eine {{ mahntitel }} zu unserer Rechnung "
    "{{ rechnungsnummer }}.\n\n"
    "Bitte überweisen Sie den offenen Betrag von {{ betrag }} € bis zum "
    "{{ nachfrist }}.\n\n"
    "Mit freundlichen Grüßen\n{{ wg_name }}"
)

# Für die Platzhalter-Hilfe im Policy-Formular (Frontend rendert diese Liste).
TEXT_PLACEHOLDERS = [
    ("kunde", "Name des Kunden"),
    ("rechnungsnummer", "Rechnungsnummer"),
    ("rechnungsdatum", "Rechnungsdatum (TT.MM.JJJJ)"),
    ("faelligkeit", "ursprüngliche Fälligkeit der Rechnung"),
    ("nachfrist", "neue Zahlungsfrist der Mahnung"),
    ("stufe", "Mahnstufe (Zahl)"),
    ("mahntitel", "Titel der Mahnung (Drucktitel)"),
    ("hauptforderung", "offene Hauptforderung"),
    ("mahngebuehr", "Mahngebühr dieser Stufe"),
    ("summe_mahngebuehren", "Summe aller Mahngebühren"),
    ("betrag", "Gesamtbetrag offen (Hauptforderung + Gebühren)"),
    ("iban", "IBAN der Genossenschaft"),
    ("wg_name", "Name der Genossenschaft"),
]


def _money(value):
    """Deutsches Geldformat ohne Währungssymbol, z.B. ``1.234,56``."""
    try:
        v = Decimal(str(value or 0))
    except Exception:
        return str(value)
    s = f"{v:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def dunning_text_context(notice, summary=None, wg=None):
    """Platzhalter-Werte für die Stufentexte einer Mahnung."""
    if summary is None:
        summary = dunning_summary(notice.invoice)
    if wg is None:
        from app.settings_service import wg_settings
        wg = wg_settings()
    invoice = notice.invoice

    def _d(dt):
        return dt.strftime("%d.%m.%Y") if dt else "—"

    return {
        "kunde": invoice.customer.letter_name,
        "rechnungsnummer": invoice.invoice_number,
        "rechnungsdatum": _d(invoice.date),
        "faelligkeit": _d(invoice.due_date),
        "nachfrist": _d(notice.new_due_date),
        "stufe": notice.level_snapshot,
        "mahntitel": notice.print_title_snapshot or notice.name_snapshot,
        "hauptforderung": _money(summary["principal"]),
        "mahngebuehr": _money(notice.fee_amount or 0),
        "summe_mahngebuehren": _money(summary["total_fees"]),
        "betrag": _money(summary["gross_total"]),
        "iban": wg.get("iban", ""),
        "bic": wg.get("bic", ""),
        "wg_name": wg.get("name", ""),
    }


def _render_text(template_str, context):
    """Rendert einen Stufentext mit Jinja (Platzhalter), Fehler → Rohtext."""
    from jinja2 import Environment
    try:
        return Environment(autoescape=False).from_string(
            template_str or ""
        ).render(**context)
    except Exception:
        return template_str or ""


def rendered_letter_texts(notice, summary=None, wg=None):
    """``(intro, closing)`` für den Mahnbrief, mit Platzhaltern gerendert.

    Pro-Stufe-Text wenn hinterlegt, sonst der Default (Schlusstext-Default
    richtet sich nach der Stufe: ≤2 sanft, sonst dringlich).
    """
    ctx = dunning_text_context(notice, summary, wg)
    stage = notice.stage
    intro_tpl = (getattr(stage, "letter_intro", None) or "").strip() or DEFAULT_LETTER_INTRO
    closing_tpl = (getattr(stage, "letter_closing", None) or "").strip()
    if not closing_tpl:
        closing_tpl = (
            DEFAULT_LETTER_CLOSING_SOFT if notice.level_snapshot <= 2
            else DEFAULT_LETTER_CLOSING_HARD
        )
    return _render_text(intro_tpl, ctx), _render_text(closing_tpl, ctx)


def rendered_email(notice, summary=None, wg=None):
    """``(subject, body)`` für die Mahn-Mail, mit Platzhaltern gerendert."""
    ctx = dunning_text_context(notice, summary, wg)
    stage = notice.stage
    subj_tpl = (getattr(stage, "email_subject", None) or "").strip() or DEFAULT_EMAIL_SUBJECT
    body_tpl = (getattr(stage, "email_body", None) or "").strip() or DEFAULT_EMAIL_BODY
    return _render_text(subj_tpl, ctx), _render_text(body_tpl, ctx)


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
        # Teilzahlungen berücksichtigen: bereits (teil-)beglichene Rechnungen
        # nicht über den vollen Betrag weitermahnen. Voll bezahlte Rechnungen
        # haben Status 'Bezahlt' und fielen schon oben raus; das fängt den Fall
        # ab, dass per Buchung beglichen wurde, ohne den Status zu wechseln.
        if inv.open_balance <= 0:
            continue
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

    Die Gebühr wird auf den **offenen** Betrag berechnet (``open_balance``),
    nicht auf den ursprünglichen Rechnungsbetrag — Teilzahlungen mindern die
    prozentuale Mahngebühr.
    """
    principal = Decimal(str(invoice.open_balance or 0))
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
        - principal: **offene** Hauptforderung (= invoice.open_balance)
        - original_total: ursprünglicher Rechnungsbetrag (invoice.total_amount)
        - paid: bereits geleistete Zahlung (original_total - principal)
        - gross_total: offene Hauptforderung + Mahngebühren
    """
    notices = (
        DunningNotice.query
        .filter_by(invoice_id=invoice.id, status=DunningNotice.STATUS_AKTIV)
        .order_by(DunningNotice.level_snapshot.asc())
        .all()
    )

    principal = Decimal(str(invoice.open_balance or 0))
    original_total = Decimal(str(invoice.total_amount or 0))
    paid = original_total - principal
    total_fees = sum(
        (Decimal(str(n.fee_amount or 0)) for n in notices),
        Decimal("0"),
    )

    return {
        "level": max((n.level_snapshot for n in notices), default=0),
        "notices": notices,
        "total_fees": total_fees,
        "principal": principal,
        "original_total": original_total,
        "paid": paid,
        "gross_total": principal + total_fees,
    }
