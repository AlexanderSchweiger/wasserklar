"""Nummernkreise am Kontakt: Kunden-/Mitgliedsnummer und Lieferantennummer.

Ein Kontakt kann Kunde (Mitglied, Wasserbezieher) und Lieferant zugleich sein; jede
Rolle hat ihren eigenen Nummernkreis:

* ``customer_number`` — Kunden-/Mitgliedsnummer (``CustomerCounter``). Steht auf Rechnung,
  Mahnung und Ablesebrief, ist Login-Kennung der Selbstablesung und geht in die E-Rechnung
  (BT-46, XRechnung BT-10 ersatzweise).
* ``creditor_number`` — Lieferantennummer (``SupplierCounter``, Standard ab 70001).

Regeln (eine Stelle, alle Anlagewege — Formular, Schnellanlage, Import, E-Rechnung — nutzen sie):

* Vergabe automatisch, sobald die Rolle besteht und noch keine Nummer da ist; eine Hand-
  eingabe ist möglich (Altbestand), aber nie stilles Weiterzählen über einen Tippfehler.
* Eine verwendete Nummer ist **geschützt**: sie steht auf ausgestellten Belegen
  (Nachvollziehbarkeit, BAO § 131 / GoBD) bzw. ist Login-Kennung. Sie wird weder geleert
  noch geändert; eine Rolle abzuwählen lässt die Nummer stehen.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.extensions import db

# Eine Handeingabe, die mehr als so weit über dem Vorschlag liegt, muss bestätigt werden —
# sonst hebt ein Tippfehler (10000 statt 100) den Zähler dauerhaft an.
JUMP_CONFIRM_THRESHOLD = 100


def customer_number_locked(customer) -> bool:
    """True, wenn die Kunden-/Mitgliedsnummer schon verwendet wird (Rechnung oder
    Selbstablesungs-Code — Mahnungen hängen an Rechnungen)."""
    if customer is None or customer.id is None or customer.customer_number is None:
        return False
    from app.models import Invoice, MeterReadingAccessCode

    return bool(
        db.session.query(Invoice.query.filter(Invoice.customer_id == customer.id).exists()).scalar()
        or db.session.query(MeterReadingAccessCode.query.filter(
            MeterReadingAccessCode.customer_id == customer.id).exists()).scalar()
    )


def creditor_number_locked(customer) -> bool:
    """True, wenn die Lieferantennummer schon verwendet wird (Buchung, Sammelbuchung
    oder Beleg am Kontakt)."""
    if customer is None or customer.id is None or customer.creditor_number is None:
        return False
    from app.models import Booking, BookingGroup, Document

    for model, column in ((Booking, Booking.customer_id),
                          (BookingGroup, BookingGroup.customer_id),
                          (Document, Document.supplier_id)):
        if db.session.query(model.query.filter(column == customer.id).exists()).scalar():
            return True
    return False


def supplier_numbers_in_use() -> bool:
    """True, sobald irgendein Kontakt eine Lieferantennummer hat (Startwert fix)."""
    from app.models import Customer

    return bool(db.session.query(
        Customer.query.filter(Customer.creditor_number.isnot(None)).exists()).scalar())


def assign_numbers(customer) -> None:
    """Vergibt fehlende Nummern für die gesetzten Rollen (nie überschreibend)."""
    from app.utils import next_customer_number, next_supplier_number

    if customer.is_customer and customer.customer_number is None:
        customer.customer_number = next_customer_number()
    if customer.is_supplier and customer.creditor_number is None:
        customer.creditor_number = next_supplier_number()


def number_label(customer=None, *, is_wg: bool | None = None, status: str | None = None) -> str:
    """Beschriftung der Kunden-/Mitgliedsnummer.

    Im WG-Modus richtet sie sich nach dem Status: Mitglied, Interessent und Ausgeschiedener
    führen eine Mitgliedsnummer, ein Externer (Wasserbezieher ohne Mitgliedschaft) eine
    Kundennummer. Versorger: immer Kundennummer.
    """
    if is_wg is None:
        from app.settings_service import is_wassergenossenschaft
        is_wg = is_wassergenossenschaft()
    if not is_wg:
        return "Kundennummer"
    if status is None:
        status = customer.wg_status if customer is not None else "member"
    from app.wg import STATUS_EXTERNAL
    return "Kundennummer" if status == STATUS_EXTERNAL else "Mitgliedsnummer"


def form_context(customer=None) -> dict:
    """Kontext fuer die Nummernfelder des Kontaktformulars (Jinja-Global)."""
    from app.utils import next_customer_number, next_supplier_number

    return {
        "next_customer": next_customer_number(peek=True),
        "next_supplier": next_supplier_number(peek=True),
        "customer_locked": customer_number_locked(customer),
        "creditor_locked": creditor_number_locked(customer),
    }


@dataclass
class NumberField:
    """Ergebnis der Formularauswertung einer Nummer."""
    error: str | None = None
    needs_confirm: bool = False


def apply_number_field(customer, *, attr: str, raw: str, locked: bool, peek, bump,
                       label: str, confirm_jump: bool, clear: bool = False) -> NumberField:
    """Wertet ein Nummernfeld des Kontaktformulars aus und setzt ``customer.<attr>``.

    * Gesperrte Nummer: Formularwert wird ignoriert (das Feld ist schreibgeschützt).
    * Leeres Feld: eine vorhandene Nummer bleibt stehen (nie stilles Löschen); ohne
      Nummer vergibt ``assign_numbers`` danach die nächste, wenn die Rolle aktiv ist.
    * ``clear``: ausdrückliches Entfernen (Checkbox „Nummer entfernen“), nur ungesperrt.
    * Wert: Zahl > 0, eindeutig; Sprung > ``JUMP_CONFIRM_THRESHOLD`` über dem Vorschlag
      nur mit Bestätigung.
    """
    from app.models import Customer

    if locked:
        return NumberField()
    if clear:
        # Ausdrueckliches Entfernen (nur ungesperrt, z.B. faelschlich als Kunde angelegt).
        setattr(customer, attr, None)
        return NumberField()
    raw = (raw or "").strip()
    if not raw:
        return NumberField()
    try:
        requested = int(raw)
    except ValueError:
        return NumberField(error=f"{label} muss eine Zahl sein: {raw}")
    if requested < 1:
        return NumberField(error=f"{label} muss positiv sein.")
    current = getattr(customer, attr)
    if current == requested:
        return NumberField()
    column = getattr(Customer, attr)
    q = Customer.query.filter(column == requested)
    if customer.id is not None:
        q = q.filter(Customer.id != customer.id)
    if db.session.query(q.exists()).scalar():
        return NumberField(error=f"{label} {requested} ist bereits vergeben.")
    suggested = peek()
    if requested > suggested + JUMP_CONFIRM_THRESHOLD and not confirm_jump:
        # Das Formular zeigt daraufhin die Bestaetigungs-Checkbox ``confirm_number_jump``.
        from flask import g
        g.number_jump_confirm = True
        return NumberField(
            error=(f"{label} {requested} liegt weit über der nächsten freien Nummer "
                   f"({suggested}). Alle folgenden Nummern würden danach weiterzählen. "
                   "Bitte prüfen und zum Übernehmen bestätigen."),
            needs_confirm=True,
        )
    setattr(customer, attr, requested)
    bump(requested)
    return NumberField()
