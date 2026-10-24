"""Einzugslaeufe (SEPA-Lastschrift): Auswahl, Anlage, Verbuchen, Ruecklastschrift.

Ablauf:

1. ``candidates()`` waehlt die offenen Posten der Kontakte mit Bankeinzug aus —
   nur Rechnungs-Posten (keine Gutschriften), mit offenem Saldo, die in keinem
   aktiven Lauf stehen. Fehlende Stammdaten schliessen einen Posten aus (mit
   Grund), Auffaelliges (knappe Vorabankuendigung, lange ungenutztes Mandat) ist
   nur eine Warnung.
2. ``create_run()`` legt den Lauf mit Snapshots an; die pain.008-Datei und die
   CSV entstehen daraus bei jedem Download neu (``pain008.py``, ``export_csv.py``).
3. Bezahlt wird ein Posten ueber den Bankimport (Sammelgutschrift wird auf die
   Posten des Laufs aufgeteilt, ``match_bank_line``) oder ueber
   ``book_run_paid``. Eine Ruecklastschrift (``record_return``) bucht die
   Zahlung zurueck; der Posten ist dann wieder offen, mahnbar und einziehbar.

Ein Posten „im Einzug“ (aktiver Lauf, nicht zurueckgegeben) wird weder ein
zweites Mal eingezogen noch gemahnt.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func

from app.banking import iban_valid, normalize_bic, normalize_iban, sepa_text
from app.direct_debit.notice import creditor_id, customer_debit_issues
from app.extensions import db
from app.models import (
    Booking, Customer, DirectDebitItem, DirectDebitRun, Invoice, OpenItem, RealAccount,
)

# Die Rechnung ist die Vorabankuendigung: sie muss dem Kunden 14 Kalendertage vor
# dem Einzug vorliegen, ausser im Mandat ist eine kuerzere Frist vereinbart.
PRENOTIFICATION_DAYS = 14
# Ein Mandat verfaellt, wenn 36 Monate lang nicht eingezogen wurde.
MANDATE_LAPSE_DAYS = 36 * 30 + 15
# Die Datei muss spaetestens einen TARGET-Bankarbeitstag vor dem Einzug bei der Bank
# sein; mit einem Tag Puffer fuer Annahmeschluss und Upload.
LEAD_TARGET_DAYS = 2


# ---------------------------------------------------------------------------
# TARGET-Kalender
# ---------------------------------------------------------------------------

def _easter(year):
    """Ostersonntag (gregorianisch, Anonymer Algorithmus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def target_holidays(year):
    easter = _easter(year)
    return {
        date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1),
        date(year, 5, 1), date(year, 12, 25), date(year, 12, 26),
    }


def is_target_day(day):
    return day.weekday() < 5 and day not in target_holidays(day.year)


def add_target_days(day, n):
    while n > 0:
        day += timedelta(days=1)
        if is_target_day(day):
            n -= 1
    return day


def earliest_collection_date(today=None):
    return add_target_days(today or date.today(), LEAD_TARGET_DAYS)


def next_collection_day(day):
    """``day`` selbst, wenn es ein TARGET-Tag ist, sonst der naechste."""
    while not is_target_day(day):
        day += timedelta(days=1)
    return day


# ---------------------------------------------------------------------------
# Glaeubiger
# ---------------------------------------------------------------------------

def readiness():
    """Was fuer einen Einzug in den Einstellungen fehlt: ``[(Text, Ziel-Tab)]``."""
    from app.direct_debit.notice import creditor_id_issue
    from app.settings_service import get_wg
    issues = []
    cid = creditor_id()
    if not cid:
        issues.append("Gläubiger-ID fehlt")
    elif creditor_id_issue(cid):
        issues.append("Gläubiger-ID ist ungültig")
    iban = get_wg("iban")
    if not iban:
        issues.append("IBAN der Genossenschaft fehlt")
    elif not iban_valid(iban):
        issues.append("IBAN der Genossenschaft ist ungültig")
    if not (get_wg("account_holder") or get_wg("name")):
        issues.append("Kontoinhaber bzw. Name der Genossenschaft fehlt")
    return issues


def nav_visible():
    """Menuepunkt „Lastschrift-Einzug“ nur, wenn Lastschrift genutzt wird
    (Glaeubiger-ID hinterlegt oder mindestens ein Kontakt mit Bankeinzug)."""
    if creditor_id():
        return True
    # Savepoint: laeuft beim Rendern jeder Seite — scheitert die Abfrage (Schema noch
    # ohne Migration), darf sie die Transaktion der Seite nicht abgebrochen zuruecklassen
    # (Postgres: „current transaction is aborted“ bei jeder weiteren Abfrage).
    try:
        with db.session.begin_nested():
            return db.session.query(Customer.id).filter(
                Customer.payment_method == Customer.PAYMENT_DIRECT_DEBIT).first() is not None
    except Exception:  # noqa: BLE001
        return False


def default_real_account():
    """Geldkonto fuer Einzuege: das Bankkonto mit der IBAN der Genossenschaft,
    sonst das Standardkonto, sonst das erste aktive Bankkonto."""
    from app.settings_service import get_wg
    own = normalize_iban(get_wg("iban"))
    accounts = RealAccount.query.filter_by(active=True).all()
    banks = [ra for ra in accounts if not ra.is_cash]
    for ra in banks:
        if own and normalize_iban(ra.iban) == own:
            return ra
    for ra in banks:
        if ra.is_default:
            return ra
    return banks[0] if banks else None


# ---------------------------------------------------------------------------
# Was steht im Einzug?
# ---------------------------------------------------------------------------

def _pending_items_query():
    """Positionen aktiver Laeufe, die nicht zurueckgegeben wurden."""
    return (DirectDebitItem.query
            .join(DirectDebitRun, DirectDebitItem.run_id == DirectDebitRun.id)
            .filter(DirectDebitRun.status == DirectDebitRun.STATUS_ACTIVE,
                    DirectDebitItem.returned_on.is_(None)))


def open_item_ids_in_collection():
    """IDs der offenen Posten, die gerade in einem Einzug stehen."""
    rows = (db.session.query(DirectDebitItem.open_item_id)
            .join(DirectDebitRun, DirectDebitItem.run_id == DirectDebitRun.id)
            .filter(DirectDebitRun.status == DirectDebitRun.STATUS_ACTIVE,
                    DirectDebitItem.returned_on.is_(None),
                    DirectDebitItem.open_item_id.isnot(None))
            .all())
    return {r[0] for r in rows}


def current_item_for_open_item(open_item_id):
    """Die laufende (nicht zurueckgegebene) Lastschrift eines Postens, sonst ``None``."""
    return (_pending_items_query()
            .filter(DirectDebitItem.open_item_id == open_item_id)
            .order_by(DirectDebitItem.id.desc())
            .first())


def is_in_collection(open_item):
    """True, solange der Posten in einem Einzug steht und noch offen ist."""
    if open_item is None:
        return False
    item = current_item_for_open_item(open_item.id)
    return item is not None and item.state == DirectDebitItem.STATE_PENDING


def _last_item_for_customer(customer_id):
    return (DirectDebitItem.query
            .join(DirectDebitRun, DirectDebitItem.run_id == DirectDebitRun.id)
            .filter(DirectDebitRun.status == DirectDebitRun.STATUS_ACTIVE,
                    DirectDebitItem.customer_id == customer_id)
            .order_by(DirectDebitRun.created_at.desc(), DirectDebitItem.id.desc())
            .first())


# ---------------------------------------------------------------------------
# Auswahl
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    open_item: OpenItem
    invoice: Invoice
    customer: Customer
    amount: Decimal
    collection_date: date
    issues: list = field(default_factory=list)      # sperren den Einzug
    warnings: list = field(default_factory=list)    # nur Hinweis
    # Rechnung wurde mit Lastschrift-Hinweis versendet (= Vorabankuendigung).
    announced: bool = True

    @property
    def selectable(self):
        return not self.issues

    @property
    def preselected(self):
        return self.selectable and self.announced


def _base_query():
    return (OpenItem.query
            .join(Invoice, OpenItem.invoice_id == Invoice.id)
            .join(Customer, OpenItem.customer_id == Customer.id)
            .filter(Customer.payment_method == Customer.PAYMENT_DIRECT_DEBIT,
                    OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
                    OpenItem.amount > 0,
                    Invoice.invoice_kind != Invoice.KIND_CREDIT_NOTE,
                    Invoice.status == Invoice.STATUS_SENT))


def _candidate(op, today, earliest):
    invoice, customer = op.invoice, op.customer
    balance = op.open_balance
    due = op.due_date or invoice.due_date or today
    collect = max(next_collection_day(due), earliest)
    cand = Candidate(open_item=op, invoice=invoice, customer=customer, amount=balance,
                     collection_date=collect)
    cand.issues = list(customer_debit_issues(customer))
    if invoice.payment_method != Customer.PAYMENT_DIRECT_DEBIT:
        cand.announced = False
        cand.warnings.append(
            "Rechnung wurde mit Zahlungsaufforderung (Überweisung) versendet — der Kunde "
            "kennt den Einzug noch nicht")
    elif invoice.date and (collect - invoice.date).days < PRENOTIFICATION_DAYS:
        cand.warnings.append(
            f"Vorabankündigung knapp: Rechnung vom {invoice.date:%d.%m.%Y}, Einzug am "
            f"{collect:%d.%m.%Y} (weniger als {PRENOTIFICATION_DAYS} Tage)")
    if not cand.issues:
        last = _last_item_for_customer(customer.id)
        if last is not None and last.mandate_reference == customer.mandate_reference:
            last_use = last.collection_date
        else:
            last_use = customer.mandate_signed_on
        if last_use and (collect - last_use).days > MANDATE_LAPSE_DAYS:
            cand.warnings.append(
                f"Mandat seit {last_use:%d.%m.%Y} nicht genutzt — nach 36 Monaten ohne "
                "Einzug verfällt es; bitte prüfen")
    return cand


def candidates(due_until=None, billing_run_id=None, today=None):
    """``(einziehbar, ausgeschlossen)`` — je eine Liste von ``Candidate``.

    ``due_until``: nur Posten, die bis zu diesem Tag faellig sind (ohne
    Faelligkeit: immer). ``billing_run_id``: nur Rechnungen dieses Rechnungslaufs.
    """
    today = today or date.today()
    earliest = earliest_collection_date(today)
    query = _base_query()
    if billing_run_id:
        query = query.filter(Invoice.billing_run_id == billing_run_id)
    if due_until:
        query = query.filter(db.or_(OpenItem.due_date.is_(None), OpenItem.due_date <= due_until))
    taken = open_item_ids_in_collection()
    ready, excluded = [], []
    for op in query.order_by(Customer.name, Invoice.invoice_number).all():
        if op.id in taken:
            continue
        if op.open_balance <= 0:
            continue
        cand = _candidate(op, today, earliest)
        (ready if cand.selectable else excluded).append(cand)
    return ready, excluded


@dataclass
class NotListed:
    """Was die Auswahl bewusst nicht zeigt — fuer den Hinweis unter dem Filter."""
    drafts: int = 0                 # Rechnungsentwuerfe (noch keine Vorabankuendigung)
    due_later: int = 0              # versendet, aber erst nach ``due_until`` faellig
    last_due: date | None = None    # spaeteste dieser Faelligkeiten

    def __bool__(self):
        return bool(self.drafts or self.due_later)


def not_listed(due_until=None, billing_run_id=None):
    """Zaehlt Rechnungen von Bankeinzugs-Kontakten, die ``candidates`` ausblendet."""
    drafts = (db.session.query(func.count(Invoice.id))
              .join(Customer, Invoice.customer_id == Customer.id)
              .filter(Customer.payment_method == Customer.PAYMENT_DIRECT_DEBIT,
                      Invoice.status == Invoice.STATUS_DRAFT,
                      Invoice.invoice_kind != Invoice.KIND_CREDIT_NOTE,
                      Invoice.total_amount > 0))
    if billing_run_id:
        drafts = drafts.filter(Invoice.billing_run_id == billing_run_id)
    result = NotListed(drafts=drafts.scalar() or 0)
    if due_until:
        query = _base_query().filter(OpenItem.due_date > due_until)
        if billing_run_id:
            query = query.filter(Invoice.billing_run_id == billing_run_id)
        taken = open_item_ids_in_collection()
        later = [op.due_date for op in query.all()
                 if op.id not in taken and op.open_balance > 0]
        result.due_later = len(later)
        result.last_due = max(later) if later else None
    return result


# ---------------------------------------------------------------------------
# Lauf anlegen / verwerfen
# ---------------------------------------------------------------------------

class DirectDebitError(ValueError):
    """Fachlicher Fehler mit deutscher Meldung."""


def _message_id(now):
    return f"DD-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3).upper()}"


def _end_to_end_id(invoice, open_item_id):
    previous = (db.session.query(func.count(DirectDebitItem.id))
                .filter(DirectDebitItem.open_item_id == open_item_id).scalar() or 0)
    base = sepa_text(invoice.invoice_number, 35).replace(" ", "")
    return base if previous == 0 else f"{base}-{previous + 1}"[:35]


def create_run(open_item_ids, user_id=None, today=None, notes=None):
    """Legt einen Einzugslauf fuer die gewaehlten Posten an. Kein commit.

    Prueft jeden Posten erneut (Auswahl kann veraltet sein): gesperrte oder
    inzwischen anderweitig eingezogene Posten werden uebergangen. Wirft
    ``DirectDebitError``, wenn die Einstellungen unvollstaendig sind oder kein
    Posten uebrig bleibt.
    """
    from app.settings_service import get_wg
    problems = readiness()
    if problems:
        raise DirectDebitError("Einzug nicht möglich: " + ", ".join(problems) + ".")
    today = today or date.today()
    earliest = earliest_collection_date(today)
    wanted = {int(i) for i in open_item_ids}
    taken = open_item_ids_in_collection()
    ops = _base_query().filter(OpenItem.id.in_(wanted - taken)).all() if wanted else []
    cands = [c for c in (_candidate(op, today, earliest) for op in ops)
             if c.selectable and c.amount > 0]
    if not cands:
        raise DirectDebitError("Keiner der gewählten Posten kann eingezogen werden.")

    now = datetime.now().replace(microsecond=0)
    run = DirectDebitRun(
        message_id=_message_id(now),
        status=DirectDebitRun.STATUS_ACTIVE,
        creditor_name=sepa_text(get_wg("account_holder") or get_wg("name"), 70),
        creditor_id=creditor_id(),
        creditor_iban=normalize_iban(get_wg("iban")),
        creditor_bic=normalize_bic(get_wg("bic")) or None,
        notes=notes,
        created_by_id=user_id,
        created_at=now,
    )
    db.session.add(run)
    total = Decimal("0")
    for c in sorted(cands, key=lambda c: (c.collection_date, c.customer.name or "", c.invoice.invoice_number)):
        customer = c.customer
        iban = normalize_iban(customer.iban)
        last = _last_item_for_customer(customer.id)
        amended_iban = amended_mandate = None
        if last is not None:
            if last.mandate_reference and last.mandate_reference != customer.mandate_reference:
                amended_mandate = last.mandate_reference
            if last.iban and last.iban != iban:
                amended_iban = last.iban
        number = customer.customer_number
        remittance = f"Rechnung {c.invoice.invoice_number}" + (f" Kd {number}" if number else "")
        run.items.append(DirectDebitItem(
            open_item_id=c.open_item.id,
            invoice_id=c.invoice.id,
            customer_id=customer.id,
            amount=c.amount,
            collection_date=c.collection_date,
            end_to_end_id=_end_to_end_id(c.invoice, c.open_item.id),
            remittance=sepa_text(remittance, 140),
            debtor_name=sepa_text(customer.debit_holder, 70),
            iban=iban,
            bic=normalize_bic(customer.bic) or None,
            mandate_reference=customer.mandate_reference,
            mandate_signed_on=customer.mandate_signed_on,
            amended_from_iban=amended_iban,
            amended_from_mandate=amended_mandate,
        ))
        total += c.amount
    run.item_count = len(run.items)
    run.total_amount = total
    db.session.flush()
    return run


def cancel_blocker(run):
    """Grund, warum der Lauf nicht verworfen werden kann, sonst ``None``."""
    if run.is_cancelled:
        return "Der Einzugslauf ist bereits verworfen."
    for item in run.items:
        if item.returned_on:
            return "Zu diesem Lauf ist schon eine Rücklastschrift erfasst."
        op = item.open_item
        if op is not None and op.open_balance < Decimal(str(item.amount)):
            return ("Zu diesem Lauf sind schon Zahlungen gebucht — einzelne Posten "
                    "bitte als Rücklastschrift erfassen.")
    return None


def cancel_run(run):
    """Verwirft den Lauf (Bank hat die Datei nicht ausgefuehrt). Kein commit."""
    blocker = cancel_blocker(run)
    if blocker:
        raise DirectDebitError(blocker)
    run.status = DirectDebitRun.STATUS_CANCELLED
    run.cancelled_at = datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------------------
# Verbuchen
# ---------------------------------------------------------------------------

def _sync_status(op, invoice):
    """Status von Posten und Rechnung nach einer Buchung (auch nach der
    Ruecknahme einer Zahlung — ``settlement_status`` kennt „gar nichts bezahlt“
    nicht)."""
    from app.accounting.services import settlement_status
    balance = op.open_balance
    if balance >= Decimal(str(op.amount or 0)) and balance > 0:
        op.status = OpenItem.STATUS_OPEN
        if invoice is not None:
            invoice.status = Invoice.STATUS_SENT
        return
    op.status, new_invoice_status = settlement_status(op.amount, balance)
    if invoice is not None:
        invoice.status = new_invoice_status or Invoice.STATUS_SENT


def pay_open_item(op, amount, payment_date, real_account_id, user_id,
                  fallback_account_id=None, reference=None):
    """Bucht eine Zahlung (oder mit negativem Betrag deren Ruecknahme) auf einen
    Rechnungs-Posten und gleicht Posten- und Rechnungsstatus ab. Kein commit.

    Wirft ``DirectDebitError`` bei geschlossenem Buchungsjahr oder fehlender
    Kontierung — der Aufrufer isoliert das je Posten. Rueckgabe
    ``[(booking_id, group_id)]`` (fuer die Belegablage des Bankimports).
    """
    from app.accounting.services import (
        booking_group_from_invoice_payment, invoice_missing_account, open_fiscal_year_error,
    )
    invoice = op.invoice
    if invoice is None:
        raise DirectDebitError("Posten ohne Rechnung kann nicht per Lastschrift gebucht werden.")
    err = open_fiscal_year_error(payment_date)
    if err:
        raise DirectDebitError(err)
    if invoice_missing_account(invoice, fallback_account_id=fallback_account_id):
        raise DirectDebitError(
            f"Rechnung {invoice.invoice_number}: kein Buchungskonto auf den Positionen — "
            "bitte ein Ersatzkonto wählen.")
    group, children = booking_group_from_invoice_payment(
        invoice=invoice, amount=Decimal(str(amount)), payment_date=payment_date,
        real_account_id=real_account_id, created_by_id=user_id, open_item=op,
        reference=reference or invoice.invoice_number, fallback_account_id=fallback_account_id,
    )
    db.session.flush()
    _sync_status(op, invoice)
    if group is not None:
        return [(None, group.id)]
    return [(b.id, None) for b in children]


def book_run_paid(run, real_account_id, user_id, fallback_account_id=None):
    """Verbucht alle noch offenen Lastschriften des Laufs als bezahlt — je
    Posten am Einzugsdatum, ueber das gewaehlte Geldkonto. Kein commit.

    Fehler (geschlossenes Jahr, fehlendes Konto) betreffen nur den einen Posten
    (Savepoint). Rueckgabe ``{"booked": n, "amount": Decimal, "errors": [...]}``.
    """
    stats = {"booked": 0, "amount": Decimal("0"), "errors": []}
    if run.is_cancelled:
        raise DirectDebitError("Der Einzugslauf ist verworfen.")
    for item in run.items:
        if item.state != DirectDebitItem.STATE_PENDING or item.open_item is None:
            continue
        op = item.open_item
        amount = min(Decimal(str(item.amount)), op.open_balance)
        if amount <= 0:
            continue
        sp = db.session.begin_nested()
        try:
            pay_open_item(op, amount, item.collection_date, real_account_id, user_id,
                          fallback_account_id=fallback_account_id,
                          reference=item.invoice.invoice_number if item.invoice else None)
            sp.commit()
        except DirectDebitError as exc:
            sp.rollback()
            stats["errors"].append(str(exc))
            continue
        stats["booked"] += 1
        stats["amount"] += amount
    return stats


def _payment_account_of(op):
    """Konto der bisherigen Zahlung auf den Posten — Ersatzkonto fuer die
    Rueckbuchung, falls die Positionen kein Konto tragen."""
    booking = (Booking.query.filter(Booking.open_item_id == op.id, Booking.amount > 0)
               .order_by(Booking.id.desc()).first())
    return booking.account_id if booking else None


def record_return(item, return_date, real_account_id, user_id, reason=None):
    """Ruecklastschrift: nimmt die gebuchte Zahlung zurueck (Gegenbuchung am
    Rueckgabetag, negativ ueber dieselbe Kontierung) und markiert die Position als
    zurueckgegeben. War noch nichts gebucht, wird nur markiert. Kein commit.

    Danach ist der Posten wieder offen — mahnbar und in einem neuen Lauf
    einziehbar. Rueckgabe ``(zurueckgebuchter Betrag, [(booking_id, group_id)])``.
    """
    if item.returned_on:
        raise DirectDebitError("Für diese Lastschrift ist schon eine Rückgabe erfasst.")
    if item.run.is_cancelled:
        raise DirectDebitError("Der Einzugslauf ist verworfen.")
    op = item.open_item
    reversed_amount = Decimal("0")
    created = []
    if op is not None:
        paid = Decimal(str(op.paid_amount or 0))
        reversed_amount = min(Decimal(str(item.amount)), paid) if paid > 0 else Decimal("0")
        if reversed_amount > 0:
            number = item.invoice.invoice_number if item.invoice else op.description
            created = pay_open_item(op, -reversed_amount, return_date, real_account_id, user_id,
                                    fallback_account_id=_payment_account_of(op),
                                    reference=f"{number} Rücklastschrift"[:100])
    item.returned_on = return_date
    item.return_reason = (reason or "").strip()[:200] or None
    return reversed_amount, created


# ---------------------------------------------------------------------------
# Bankimport
# ---------------------------------------------------------------------------

def _groups_of_active_runs():
    """``[(run, Einzugsdatum | None, [items], offene Summe)]`` aller aktiven Laeufe
    mit noch offenen Lastschriften — je Lauf gesamt und je Einzugsdatum."""
    runs = (DirectDebitRun.query.filter_by(status=DirectDebitRun.STATUS_ACTIVE)
            .order_by(DirectDebitRun.created_at.desc()).limit(50).all())
    groups = []
    for run in runs:
        pending = [i for i in run.items if i.state == DirectDebitItem.STATE_PENDING
                   and i.open_item is not None]
        if not pending:
            continue
        by_date = {}
        for i in pending:
            by_date.setdefault(i.collection_date, []).append(i)
        groups.append((run, None, pending, sum((Decimal(str(i.amount)) for i in pending), Decimal("0"))))
        if len(by_date) > 1:
            for day, items in sorted(by_date.items()):
                groups.append((run, day, items, sum((Decimal(str(i.amount)) for i in items), Decimal("0"))))
    return groups


def match_bank_line(line):
    """Ordnet eine Bankzeile einem Einzug zu. True, wenn zugeordnet.

    * **Sammelgutschrift** (positiv): Betrag = offene Summe eines aktiven Laufs
      (oder eines Einzugsdatums darin), und entweder steht die Nachrichten-/
      Sammler-Kennung im Verwendungszweck oder der Betrag ist eindeutig und das
      Datum liegt hoechstens 7 Tage neben dem Einzugsdatum. Dann wird die Zeile
      auf die Posten aufgeteilt (Allocations, wie eine manuelle Aufteilung).
    * **Ruecklastschrift** (negativ): Rechnungsnummer einer bezahlten,
      eingezogenen Rechnung im Verwendungszweck/End-to-End-ID und gleicher
      Betrag. Weicht der Betrag ab (Bankspesen), wird nicht zugeordnet.
    """
    from app.models import BankStatementLine, BankStatementLineAllocation
    amount = Decimal(str(line.amount or 0))
    if amount > 0:
        text = " ".join(filter(None, [line.purpose, line.end_to_end_id])).upper().replace(" ", "")
        hits = [g for g in _groups_of_active_runs() if g[3] == amount]
        if not hits:
            return False
        chosen = None
        for g in hits:
            run, day = g[0], g[1]
            ids = {run.message_id.upper().replace(" ", "")}
            if day is not None:
                ids.add(_pmt_inf_id(run, day).upper())
            if any(i in text for i in ids):
                chosen = g
                break
        if chosen is None:
            near = [g for g in hits
                    if abs((line.booking_date - (g[1] or min(i.collection_date for i in g[2]))).days) <= 7]
            if len({(g[0].id, g[1]) for g in near}) == 1:
                chosen = near[0]
        if chosen is None:
            return False
        line.allocations.clear()
        for item in chosen[2]:
            line.allocations.append(BankStatementLineAllocation(
                open_item_id=item.open_item_id, amount=Decimal(str(item.amount))))
        line.matched_open_item_id = None
        line.matched_invoice_id = None
        line.matched_customer_id = None
        line.match_type = BankStatementLine.MATCH_DIRECT_DEBIT
        line.selected = True
        return True

    if amount < 0:
        from app.bank_import.matching import _find_invoice_number
        text = " ".join(filter(None, [line.purpose, line.end_to_end_id]))
        num = _find_invoice_number(text) if text else None
        if not num:
            return False
        invoice = Invoice.query.filter_by(invoice_number=num).first()
        if invoice is None or invoice.open_item is None:
            return False
        item = current_item_for_open_item(invoice.open_item.id)
        if item is None or item.state != DirectDebitItem.STATE_PAID:
            return False
        if Decimal(str(item.amount)) != -amount:
            return False
        line.matched_invoice_id = invoice.id
        line.matched_open_item_id = invoice.open_item.id
        line.matched_customer_id = invoice.customer_id
        line.match_type = BankStatementLine.MATCH_DIRECT_DEBIT_RETURN
        line.selected = True
        return True
    return False


def commit_return_line(line, stmt, user_id):
    """Verbucht eine als Ruecklastschrift erkannte Bankzeile (Bankimport)."""
    item = current_item_for_open_item(line.matched_open_item_id)
    if item is None or item.state != DirectDebitItem.STATE_PAID:
        raise DirectDebitError("Keine eingezogene, bezahlte Lastschrift zu diesem Posten gefunden.")
    _, created = record_return(item, line.booking_date, stmt.real_account_id, user_id,
                               reason=(line.purpose or "Rücklastschrift (Bankimport)")[:200])
    return created


def _pmt_inf_id(run, day):
    """Sammler-Kennung (PmtInfId) eines Einzugsdatums — auch in ``pain008``."""
    return f"{run.message_id}-{day:%Y%m%d}"[:35]
