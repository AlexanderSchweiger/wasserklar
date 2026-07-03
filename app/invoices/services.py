"""Wiederverwendbare Rechnungs-Services ausserhalb des Blueprint-Routencodes.

Aus ``app/invoices/routes.py`` extrahiert, damit andere Module (z.B. die
Zaehlertausch-Touren in ``app/meter_tours``) Rechnungen erzeugen und Offene
Posten anlegen koennen, ohne Routen-Logik zu duplizieren. Die Routen binden
die Funktionen per Import-Alias unter ihren alten Underscore-Namen zurueck —
bestehende Aufrufer und Tests bleiben unberuehrt.
"""
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import Invoice, InvoiceItem, OpenItem
from app.utils import next_invoice_number

# Zahlungsziel fuer Einzel-/Pauschalen-Rechnungen ausserhalb des
# Massen-Rechnungslaufs (dort kommt der Wert aus dem Formular, Default 30).
DEFAULT_DUE_DAYS = 30


def invoice_period_year(invoice):
    """Leitet das Integer-Jahr fuer einen OpenItem aus der Rechnung ab —
    aus der Abrechnungsperiode (Enddatum), sonst aus dem Rechnungsdatum.

    ``OpenItem.period_year`` bleibt bewusst eine Jahreszahl (Buchhaltungs-
    Tag); die Abrechnungsperiode kann ein abweichendes Geschaeftsjahr haben.
    """
    if invoice.billing_period is not None:
        return invoice.billing_period.end_date.year
    if invoice.date is not None:
        return invoice.date.year
    return None


def create_or_update_open_item(invoice, account_id=None):
    """Erzeugt oder aktualisiert den verknüpften OpenItem wenn eine Rechnung versendet wird."""
    oi = invoice.open_item
    if oi is None:
        oi = OpenItem(
            customer_id=invoice.customer_id,
            description=invoice.invoice_number,
            amount=invoice.total_amount,
            date=invoice.date,
            due_date=invoice.due_date,
            period_year=invoice_period_year(invoice),
            status=OpenItem.STATUS_OPEN,
            invoice_id=invoice.id,
            account_id=account_id,
        )
        db.session.add(oi)
    else:
        oi.amount = invoice.total_amount
        oi.due_date = invoice.due_date
        oi.period_year = invoice_period_year(invoice)
        if account_id is not None:
            oi.account_id = account_id


def create_fee_invoice(*, customer, property, description, amount,
                       tax_rate=None, created_by_id=None, notes=None):
    """Entwurfs-Rechnung mit genau einer Pauschal-Position (z.B.
    Zaehlertausch-Pauschale).

    ``amount`` ist der NETTO-Positionsbetrag; ``recalculate_total()`` rechnet
    die USt gemaess ``tax_rate`` dazu (None = nicht umsatzsteuerpflichtig).
    Flusht (damit ``invoice.id`` verfuegbar ist), committet NICHT — der
    Aufrufer entscheidet ueber die Transaktionsgrenze.
    """
    amount = Decimal(str(amount))
    inv = Invoice(
        invoice_number=next_invoice_number(date.today().year),
        customer_id=customer.id,
        property_id=property.id if property is not None else None,
        date=date.today(),
        due_date=date.today() + timedelta(days=DEFAULT_DUE_DAYS),
        status=Invoice.STATUS_DRAFT,
        notes=notes,
        created_by_id=created_by_id,
    )
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id,
        description=description,
        quantity=Decimal("1"),
        unit="Pauschal",
        unit_price=amount,
        amount=amount,
        tax_rate=tax_rate,
    ))
    inv.recalculate_total()
    return inv
