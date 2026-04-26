from datetime import date


def next_invoice_number(year=None):
    """Nächste freie Rechnungsnummer im Format YYYY-00000 generieren.

    Verwendet den persistenten InvoiceCounter für das angegebene Jahr.
    Wenn kein Jahr angegeben, wird das aktuelle Jahr verwendet.
    """
    from app.extensions import db
    from app.models import InvoiceCounter

    if year is None:
        year = date.today().year

    counter = db.session.get(InvoiceCounter, year)
    if counter is None:
        # Ersten vorhandenen Wert aus bestehenden Rechnungen ableiten
        from app.models import Invoice
        from sqlalchemy import func
        prefix = f"{year}-"
        last = (
            Invoice.query
            .filter(Invoice.invoice_number.like(f"{prefix}%"))
            .order_by(Invoice.invoice_number.desc())
            .first()
        )
        if last:
            try:
                seq = int(last.invoice_number.split("-")[-1]) + 1
            except ValueError:
                seq = 1
        else:
            seq = 1
        counter = InvoiceCounter(year=year, next_seq=seq)
        db.session.add(counter)

    seq = counter.next_seq
    counter.next_seq = seq + 1
    return f"{year}-{seq:05d}"


def _customer_counter():
    """Singleton-Counter holen oder seedweise anlegen (aus max(customer_number)+1)."""
    from app.extensions import db
    from app.models import Customer, CustomerCounter
    from sqlalchemy import func

    counter = db.session.get(CustomerCounter, 1)
    if counter is None:
        seed = (db.session.query(func.max(Customer.customer_number)).scalar() or 0) + 1
        counter = CustomerCounter(id=1, next_seq=seed)
        db.session.add(counter)
        db.session.flush()
    return counter


def next_customer_number(peek: bool = False) -> int:
    """Nächste freie Kundennummer.

    peek=True liefert den aktuellen Vorschlag, ohne den Counter zu inkrementieren —
    dafuer ist auch ``db.session.rollback()`` direkt danach unkritisch.
    """
    counter = _customer_counter()
    nr = counter.next_seq
    if not peek:
        counter.next_seq = nr + 1
    return nr


def bump_customer_counter_to(value: int) -> None:
    """Counter auf value+1 anheben, falls value >= aktueller next_seq.

    Wird nach manueller Vergabe einer Nummer aufgerufen, damit Folge-Vorschlaege
    nicht denselben Wert nochmal liefern.
    """
    counter = _customer_counter()
    if value >= counter.next_seq:
        counter.next_seq = value + 1
