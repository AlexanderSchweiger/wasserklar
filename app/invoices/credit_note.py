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

Zahlung bleibt stehen
---------------------
Das Stornieren einer Rechnung nimmt die **Zahlungsbuchungen nicht zurueck**:
das Geld ist auf dem Konto eingegangen, die Einnahme ist echt. Erst die
Rueckueberweisung (Buchung auf den Rueckzahlungs-Posten) gleicht sie aus —
Zahlung am Zahltag, Erloesminderung am Rueckzahlungstag, in Summe 0.
Frueher stornierte der Statuswechsel die Zahlung mit; zusammen mit der
Rueckueberweisung stand der Betrag dann **zweimal** als Ausgabe in der
Buchhaltung und das Bankkonto in der App lag um ihn zu niedrig.

Nur wenn das Geld **nie eingegangen** ist (Rechnung irrtuemlich als bezahlt
erfasst), storniert der Statuswechsel auf Wunsch die Zahlungsbuchungen mit
(``storno_invoice_bookings``, Schalter im Storno-Dialog). Dann ist wirksam
nichts bezahlt und es entsteht folgerichtig kein Rueckzahlungs-Posten.

Der Rueckzahlbetrag ist deshalb immer der **wirksam** auf die Originalrechnung
gebuchte Betrag (Storno-Paare heben sich auf), abzueglich dessen, was ueber
fruehere, inzwischen stornierte Gutschriften schon zurueckgeflossen ist
(:func:`refund_due`).
"""

from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import Invoice, InvoiceItem, OpenItem
from app.utils import next_invoice_number

from app.invoices.services import invoice_period_year


def refund_due(original, *, exclude=None):
    """Was dem Kunden aus ``original`` (noch) zurueckzuzahlen ist.

    = wirksam auf die Rechnung gebuchte Zahlungen (``paid_amount`` summiert
    alle Buchungen, ein Storno-Paar hebt sich also auf) plus die Buchungen auf
    den anderen Gutschriften zu ``original`` — eine dort schon verbuchte
    Rueckueberweisung ist negativ und mindert den Betrag. ``exclude`` ist die
    Gutschrift, fuer die gerade gerechnet wird (ihre eigene Rueckzahlung
    mindert ihren Betrag nicht).

    Teilzahlungen ergeben folgerichtig nur den tatsaechlich erhaltenen
    Teilbetrag — die Gutschrift lautet zwar ueber die volle Rechnungssumme
    (sie hebt den ganzen Beleg auf), zurueckzuzahlen ist aber nur das, was
    auch eingegangen ist. Der Betrag wird jederzeit neu abgeleitet statt
    gespeichert und kann deshalb nicht veralten.
    """
    total = Decimal(str(original.paid_amount or 0))
    for other in original.credit_notes:
        if exclude is not None and other.id == exclude.id:
            continue
        total += Decimal(str(other.paid_amount or 0))
    return total if total > 0 else Decimal("0")


def reversed_payment_amount(invoice):
    """Summe der auf ``Storniert`` gesetzten Zahlungsbuchungen von ``invoice``.

    Nur fuer den Hinweis im Gutschrift-Dialog: bei Rechnungen, die vor der
    Umstellung storniert wurden, hat der Statuswechsel die Zahlung immer
    mitstorniert — auch wenn das Geld tatsaechlich eingegangen war. Dann ist
    wirksam nichts bezahlt (:func:`refund_due` = 0), die Rueckueberweisung darf
    also nicht noch einmal gebucht werden.
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
    """Vorschlag fuer den Rueckzahlbetrag — vor und nach dem Storno gleich,
    weil der Storno die Zahlung stehen laesst."""
    return refund_due(invoice)


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
        # Die Gutschrift nimmt dieselbe Leistung zurueck.
        service_date_from=original.service_date_from,
        service_date_to=original.service_date_to,
        service_type=original.service_type,
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
            price_decimals=item.price_decimals,
            amount=-Decimal(str(item.amount or 0)),
            # Auffuellzeile der Mindestabnahme bleibt eine (zaehlt nicht als Verbrauch).
            is_minimum_fill=bool(item.is_minimum_fill),
            min_quantity=item.min_quantity,
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


def _derived_refund(credit):
    """Rueckzahlbetrag aus den Buchungen (:func:`refund_due`), gedeckelt auf
    die Gutschriftssumme — mehr als die Gutschrift ausweist kann nie
    zurueckgezahlt werden, auch wenn der Kunde mehr ueberwiesen haette."""
    original = credit.cancels_invoice
    if original is None:
        return Decimal("0")
    cap = abs(Decimal(str(credit.total_amount or 0)))
    return min(refund_due(original, exclude=credit), cap)


def refund_amount_for(credit):
    """Rueckzahlbetrag der Gutschrift ``credit``.

    Vor dem Versand abgeleitet (:func:`_derived_refund`). Ab dem Versand gilt
    der Rueckzahlungs-Posten: er haelt fest, was der Beleg dem Kunden
    zugesagt hat — ein spaeter neu gerenderter Beleg muss dasselbe sagen wie
    der verschickte (auch bei Gutschriften aus der Zeit, als der Storno die
    Zahlung noch mitstornierte und der Betrag anders abgeleitet wurde).

    Rueckgabe ist positiv (0, wenn nichts zurueckzuzahlen ist).
    """
    oi = credit.open_item
    if oi is not None and Decimal(str(oi.amount or 0)) < 0:
        cap = abs(Decimal(str(credit.total_amount or 0)))
        return min(-Decimal(str(oi.amount)), cap)
    return _derived_refund(credit)


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
    refund = _derived_refund(credit)
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
