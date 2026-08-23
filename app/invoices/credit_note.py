"""Storno-Rechnung (Gutschrift) zu einer stornierten Rechnung.

Fachlicher Hintergrund
----------------------
Eine einmal ausgestellte Rechnung darf nicht geloescht oder inhaltlich
veraendert werden (UStG § 11, BAO § 132). Wird sie storniert, ist der saubere
Weg ein **zweiter Beleg**: die Storno-Rechnung / Gutschrift. Sie

* traegt eine eigene fortlaufende Rechnungsnummer aus demselben Nummernkreis,
* spiegelt die Positionen der Originalrechnung mit negativem Vorzeichen
  (inkl. USt-Aufteilung — sonst stimmt die UVA nicht),
* verweist ueber ``Invoice.cancels_invoice_id`` auf das Original (die
  Pflichtangabe „Bezug auf die urspruengliche Rechnung"),
* wird wie jede andere Rechnung gedruckt und per E-Mail versendet.

Geld-Fluss
----------
Nur wenn auf die Originalrechnung tatsaechlich schon gezahlt wurde, entsteht
eine Rueckzahlungspflicht. Dafuer legt der Service einen **negativen Offenen
Posten** an (``OpenItem.amount < 0``): die Genossenschaft schuldet dem Kunden
Geld. Ueberweist der Kassier den Betrag zurueck, taucht die Abbuchung im
naechsten Bankauszug als negative Zeile auf und wird vom Bankauszug-Import
genau diesem OP zugeordnet (siehe ``app/bank_import/matching.py``) — der OP
geht damit auf 0 und die Storno-Rechnung auf „Bezahlt".

War die Originalrechnung noch **unbezahlt**, entsteht KEIN Offener Posten:
die Forderung wurde beim Stornieren bereits ausgebucht, ein negativer OP
wuerde eine Verbindlichkeit vortaeuschen, die es nicht gibt.

Abgrenzung
----------
Die Buchungen der Originalrechnung werden NICHT hier zurueckgenommen — das
macht ``app.accounting.services.storno_invoice_bookings`` beim Statuswechsel
auf „Storniert". Dieser Service erzeugt ausschliesslich den Beleg und den
Rueckzahlungs-OP.
"""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import Invoice, InvoiceItem, OpenItem
from app.utils import next_invoice_number

from app.invoices.services import invoice_period_year


def refundable_amount(invoice):
    """Bereits auf die Rechnung geflossener Betrag — der Rueckzahlungs-Vorschlag.

    **Muss vor** ``storno_invoice_bookings`` gelesen werden: nach dem Storno
    heben sich Original- und Gegenbuchung auf und ``paid_amount`` ist 0.

    Teilzahlungen ergeben hier folgerichtig nur den tatsaechlich erhaltenen
    Teilbetrag — die Gutschrift lautet zwar ueber die volle Rechnungssumme
    (sie hebt den ganzen Beleg auf), zurueckzuzahlen ist aber nur das, was
    auch eingegangen ist.
    """
    paid = Decimal(str(invoice.paid_amount or 0))
    return paid if paid > 0 else Decimal("0")


def historic_paid_amount(invoice):
    """Betrag, der **vor** dem Storno auf die Rechnung geflossen war.

    Nach ``storno_invoice_bookings`` heben sich Original- und Gegenbuchung auf,
    ``paid_amount`` ist also 0. Die Originalbuchungen stehen aber weiterhin in
    der Tabelle (Status ``Storniert``) — ihre Summe ist der Betrag, der dem
    Kunden zurueckzuzahlen ist.

    Das ist die Quelle fuer :func:`refund_amount_for` und damit fuer den
    Rueckzahlungs-Posten: der Betrag wird jederzeit neu abgeleitet statt
    gespeichert — er kann also nicht veralten, egal wie viel Zeit zwischen
    Storno, Gutschrift-Entwurf und Versand liegt.
    """
    from sqlalchemy import func
    from app.models import Booking

    total = (
        db.session.query(func.sum(Booking.amount))
        .filter(
            Booking.invoice_id == invoice.id,
            Booking.status == Booking.STATUS_STORNIERT,
        )
        .scalar()
    )
    total = Decimal(str(total or 0))
    return total if total > 0 else Decimal("0")


def refund_suggestion(invoice):
    """Vorschlag fuer den Rueckzahlbetrag — unabhaengig davon, ob die Rechnung
    gerade storniert wird oder es schon ist."""
    if invoice.status == Invoice.STATUS_CANCELLED:
        return historic_paid_amount(invoice)
    return refundable_amount(invoice)


def credit_note_blocker(invoice):
    """Grund, warum zu ``invoice`` keine Storno-Rechnung erstellt werden kann.

    Gibt ``None`` zurueck, wenn nichts dagegen spricht.
    """
    if invoice.is_credit_note:
        return ("Zu einer Storno-Rechnung kann keine weitere Storno-Rechnung "
                "erstellt werden.")
    if invoice.status == Invoice.STATUS_DRAFT:
        return ("Ein Entwurf wurde nie ausgestellt und wird geloescht statt "
                "storniert — eine Gutschrift ist dafuer nicht vorgesehen.")
    existing = invoice.active_credit_note
    if existing is not None:
        return (f"Zu dieser Rechnung existiert bereits die Storno-Rechnung "
                f"{existing.invoice_number}.")
    return None


def create_credit_note(original, *, reason=None, created_by_id=None,
                       credit_date=None):
    """Erzeugt die Storno-Rechnung (Gutschrift) zu ``original`` — als Entwurf.

    Parameter
    ---------
    reason : str | None
        Storno-Grund; landet als Notiz auf dem Beleg (und damit im PDF).

    Die Gutschrift entsteht im Status **Entwurf** und durchlaeuft danach
    denselben Weg wie jede andere Rechnung: pruefen, ggf. anpassen, dann per
    Mail oder Ausdruck versenden und auf „Versendet" setzen. Erst dieser
    Schritt legt den Rueckzahlungs-Posten an (siehe
    :func:`create_refund_open_item`) — genau wie bei einer normalen Rechnung
    der Offene Posten erst beim Versenden entsteht.

    Flusht, committet NICHT — der Aufrufer bestimmt die Transaktionsgrenze
    (der Statuswechsel auf „Storniert" und die Gutschrift muessen zusammen
    gelten oder gar nicht).
    """
    blocker = credit_note_blocker(original)
    if blocker:
        raise ValueError(blocker)

    credit_date = credit_date or date.today()

    credit = Invoice(
        invoice_number=next_invoice_number(credit_date.year),
        customer_id=original.customer_id,
        property_id=original.property_id,
        # Bewusst KEIN billing_run_id: der Rechnungslauf ist das
        # unveraenderliche Protokoll eines Erzeugungsvorgangs, die Gutschrift
        # gehoert nicht dazu (sie wuerde dessen Kennzahlen verfaelschen).
        billing_run_id=None,
        billing_period_id=original.billing_period_id,
        date=credit_date,
        # Kein Zahlungsziel — das Geld fliesst in die andere Richtung; die
        # Rueckzahlung ist sofort faellig.
        due_date=credit_date,
        status=Invoice.STATUS_DRAFT,
        invoice_kind=Invoice.KIND_CREDIT_NOTE,
        cancels_invoice_id=original.id,
        notes=reason or None,
        created_by_id=created_by_id,
    )
    db.session.add(credit)
    db.session.flush()

    # Positionen spiegeln: Menge und Betrag negiert, Einzelpreis unveraendert
    # (so liest sich die Zeile als Rueckabwicklung "-120 m³ x 2,50 €").
    # Mahngebuehr-Items (ADR-003) werden NICHT gespiegelt — sie zaehlen nicht
    # zur Hauptforderung und werden ueber das Mahnungs-Storno erledigt.
    for item in original.items:
        if getattr(item, "is_dunning_fee", 0):
            continue
        db.session.add(InvoiceItem(
            invoice_id=credit.id,
            description=item.description,
            quantity=-Decimal(str(item.quantity or 0)),
            unit=item.unit,
            unit_price=item.unit_price,
            amount=-Decimal(str(item.amount or 0)),
            tax_rate=item.tax_rate,
            # Kontierung spiegeln: die Rueckzahlung muss dasselbe Erloeskonto
            # entlasten, das die Originalrechnung bebucht hat.
            account_id=item.account_id,
            project_id=item.project_id,
            # Schaetz-Marker und Korrektur-Verweis bewusst nicht uebernehmen:
            # ``reverse_corrections_for_invoice`` wuerde die Korrektur sonst
            # ein zweites Mal zurueckgeben.
            is_estimated=False,
            reading_correction_id=None,
        ))
    db.session.flush()
    credit.recalculate_total()

    return credit


def refund_amount_for(credit):
    """Rueckzahlbetrag der Gutschrift ``credit`` — abgeleitet, nicht gespeichert.

    Zurueckzuzahlen ist genau das, was auf die stornierte Originalrechnung
    tatsaechlich geflossen ist. Nach dem Storno steht dieser Betrag in den auf
    ``Storniert`` gesetzten Originalbuchungen (:func:`historic_paid_amount`) —
    er ist also jederzeit rekonstruierbar und muss nirgends dupliziert werden.

    Gedeckelt auf die Gutschriftssumme: mehr als die Gutschrift ausweist kann
    nie zurueckgezahlt werden, auch wenn der Kunde ueberwiesen haette.

    Rueckgabe ist positiv (0, wenn nichts zurueckzuzahlen ist).
    """
    original = credit.cancels_invoice
    if original is None:
        return Decimal("0")
    paid = historic_paid_amount(original)
    cap = abs(Decimal(str(credit.total_amount or 0)))
    return min(paid, cap)


def create_refund_open_item(credit, *, created_by_id=None):
    """Legt den negativen Offenen Posten zur Gutschrift ``credit`` an.

    Wird beim Statuswechsel der Gutschrift auf „Versendet" aufgerufen —
    analog dazu, dass eine normale Rechnung ihren Offenen Posten erst beim
    Versenden bekommt. Vorher ist die Gutschrift ein Entwurf und begruendet
    noch keine Verbindlichkeit.

    Kein Posten entsteht, wenn nichts zurueckzuzahlen ist (die stornierte
    Rechnung war unbezahlt) — ein negativer OP wuerde sonst eine
    Verbindlichkeit vortaeuschen, die es nicht gibt.

    Idempotent: existiert schon ein Posten, wird nur sein Betrag nachgezogen
    (der Nutzer koennte den Entwurf zwischendurch bearbeitet haben).
    """
    refund = refund_amount_for(credit)
    existing = credit.open_item

    if refund <= 0:
        return existing

    original = credit.cancels_invoice

    if existing is not None:
        existing.amount = -refund
        existing.due_date = credit.due_date
        return existing

    oi = OpenItem(
        customer_id=credit.customer_id,
        description=credit.invoice_number,
        notes=(f"Rueckzahlung aus Storno-Rechnung {credit.invoice_number}"
               + (f" zu Rechnung {original.invoice_number}" if original else "")),
        amount=-refund,
        date=credit.date,
        due_date=credit.due_date,
        period_year=invoice_period_year(credit),
        status=OpenItem.STATUS_OPEN,
        invoice_id=credit.id,
        created_by_id=created_by_id,
    )
    db.session.add(oi)
    db.session.flush()
    return oi
