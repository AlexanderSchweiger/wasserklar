"""Teilrechnungen (Akonto/Abschlag) im Demo-Datensatz.

Aufgerufen aus ``demo.seed_demo_data(installments=True)``. Die Genossenschaft
stellt im laufenden Jahr auf Teilrechnungen um — gerechnet wird ausschliesslich
ueber die echten Dienste (``app.installments``), der Datensatz zeigt also genau
das, was die Software selbst erzeugt:

1. Verfahren einschalten (Faelligkeiten, Rundung, Guthaben-Modus bleiben, wie sie
   eingestellt sind — ohne Eintrag gelten die Standards 15.02./15.05./15.08./15.11.).
2. Teilbetragsplaene des laufenden Jahres aus dem Vorjahresverbrauch × Tarif;
   zwei Kontakte zahlen nur jaehrlich (``installment_opt_out``).
3. Jede Faelligkeit, deren Teilrechnung bis zum Stichtag ausgestellt waere
   (14 Tage vorher), bekommt ihren eigenen Teilrechnungslauf am Ausstellungstag,
   wird versendet und — bis auf wenige Kontakte — am Faelligkeitstag bezahlt.
   Die juengste, noch nicht faellige Teilrechnung bleibt offen.
4. Ab Mitte September (``with_next_period``) die Folgeperiode mit Plaenen aus
   dem Verbrauch des laufenden Jahres: die Jahresabrechnung zeigt damit die neuen
   Teilbetraege (wie ein Endabrechnungsbescheid mit „Festsetzung der kuenftigen
   Akontobetraege“).

Die Jahresabrechnung selbst entsteht bewusst nicht — sie ist der naechste
Schritt, den man im Demo-Mandanten ausprobiert (Rechnungslauf der laufenden
Periode). Kein Commit, kein Zufall (Auswahl ueber die Kundennummer).
"""
from __future__ import annotations

from datetime import date, timedelta

# Kontakte, die nur mit der Jahresabrechnung zahlen (Status „ausgenommen“).
OPT_OUT_NUMBERS = (4, 58)
# Kontakte, deren zuletzt faellige Teilrechnung noch offen ist (Spalte „Zahlung“
# der Gegenverrechnung, Mahnlauf).
UNPAID_MODULO = 13
# So viele Tage vor der Faelligkeit wird die Teilrechnung ausgestellt
# (= ``invoicing.MIN_DUE_DAYS``, Vorabankuendigung bei Lastschrift).
ISSUE_DAYS_BEFORE = 14


def seed_installments(db, *, today: date, p_curr, t_curr, customers, admin, giro,
                      with_next_period: bool = True) -> dict:
    from app.installments import invoicing, plans, schedule, terms
    from app.direct_debit.services import pay_open_item
    from app.invoices.services import create_or_update_open_item
    from app.models import AppSetting, BillingPeriod, Invoice, OpenItem

    counts = {}
    AppSetting.set(terms.ENABLED_KEY, "true")
    for c in customers:
        if c.customer_number in OPT_OUT_NUMBERS:
            c.installment_opt_out = True
    db.session.flush()

    # --- 2. Plaene des laufenden Jahres ------------------------------------
    proposals = plans.compute_plans(p_curr, t_curr, today=today)
    created, _updated = plans.save_plans(p_curr, t_curr, proposals, created_by_id=admin.id)
    counts["installment_plans"] = created
    counts["installment_opt_out"] = sum(1 for p in proposals if p.status == plans.STATUS_OPT_OUT)

    # --- 3. Teilrechnungen bis zum Stichtag ----------------------------------
    spec = schedule.parse_due_dates(terms.due_dates_text())
    due_dates = schedule.dates_for_period(spec, p_curr.start_date, p_curr.end_date)
    issued = paid = 0
    last_due = max((d for d in due_dates if d <= today), default=None)
    for due in due_dates:
        issue_day = due - timedelta(days=ISSUE_DAYS_BEFORE)
        if issue_day > today:
            break
        entries = [e for e in invoicing.open_entries(p_curr, due_until=due) if e.due_date == due]
        result = invoicing.create_installment_invoices(
            p_curr, entries, created_by_id=admin.id, today=issue_day)
        for inv in result.created:
            inv.status = Invoice.STATUS_SENT
            create_or_update_open_item(inv)
        db.session.flush()
        issued += len(result.created)
        if due > today:
            continue                                   # noch nicht faellig: bleibt offen
        for inv in result.created:
            number = inv.customer.customer_number or 0
            if due == last_due and number % UNPAID_MODULO == 0:
                continue
            op = OpenItem.query.filter_by(invoice_id=inv.id).first()
            if op is None or op.open_balance <= 0:
                continue
            pay_open_item(op, op.open_balance, min(due, today), giro.id, admin.id)
            paid += 1
    counts["installment_invoices"] = issued
    counts["installment_invoices_paid"] = paid

    # --- 4. Folgeperiode mit Plaenen fuer die Vorschau ------------------------
    # Erst ab Mitte September: frueher im Jahr stuenden in der laufenden Periode
    # nur ein paar Monate Verbrauch, die Plaene des Folgejahres waeren viel zu klein.
    if not with_next_period:
        return counts
    year = p_curr.end_date.year + 1
    p_next = BillingPeriod(
        name=str(year), start_date=date(year, 1, 1), end_date=date(year, 12, 31),
        active=False,
        notes=f"Folgeperiode — {terms.installment_words()} aus dem Verbrauch {p_curr.name}.",
    )
    db.session.add(p_next)
    db.session.flush()
    proposals = plans.compute_plans(p_next, t_curr, today=today)
    created, _updated = plans.save_plans(p_next, t_curr, proposals, created_by_id=admin.id)
    counts["installment_plans_next"] = created
    counts["billing_periods_next"] = 1
    return counts
