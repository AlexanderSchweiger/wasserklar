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
    """Singleton-Counter holen oder seedweise anlegen.

    next_seq wird immer auf mindestens ``max(customer_number)+1`` angehoben, damit
    der Vorschlag auch nach manueller Vergabe, Loeschungen oder einem zurueck-
    gesetzten Zaehler nie eine bereits vergebene Nummer liefert.
    """
    from app.extensions import db
    from app.models import Customer, CustomerCounter
    from sqlalchemy import func

    max_nr = (db.session.query(func.max(Customer.customer_number)).scalar() or 0)
    counter = db.session.get(CustomerCounter, 1)
    if counter is None:
        counter = CustomerCounter(id=1, next_seq=max_nr + 1)
        db.session.add(counter)
        db.session.flush()
    elif counter.next_seq <= max_nr:
        counter.next_seq = max_nr + 1
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


# ---------------------------------------------------------------------------
# Lieferantennummern — eigener Nummernkreis, getrennt von den Kunden-/Mitglieds-
# nummern. Start nach DATEV-Konvention im Kreditorenbereich (70001), damit eine
# Lieferantennummer nie mit einer Mitgliedsnummer verwechselt wird.
# ---------------------------------------------------------------------------

SUPPLIER_START_SETTING = "numbers.supplier_start"
SUPPLIER_START_DEFAULT = 70001


def supplier_number_start() -> int:
    """Startwert des Lieferanten-Nummernkreises (AppSetting, sonst 70001)."""
    from app.models import AppSetting

    raw = AppSetting.get(SUPPLIER_START_SETTING)
    try:
        value = int(str(raw).strip()) if raw not in (None, "") else SUPPLIER_START_DEFAULT
    except ValueError:
        return SUPPLIER_START_DEFAULT
    return value if value >= 1 else SUPPLIER_START_DEFAULT


def _supplier_counter():
    """Singleton-Counter der Lieferantennummern; nie unter Startwert bzw. max+1."""
    from app.extensions import db
    from app.models import Customer, SupplierCounter
    from sqlalchemy import func

    floor = max(supplier_number_start(),
                (db.session.query(func.max(Customer.creditor_number)).scalar() or 0) + 1)
    counter = db.session.get(SupplierCounter, 1)
    if counter is None:
        counter = SupplierCounter(id=1, next_seq=floor)
        db.session.add(counter)
        db.session.flush()
    elif counter.next_seq < floor:
        counter.next_seq = floor
    return counter


def next_supplier_number(peek: bool = False) -> int:
    """Nächste freie Lieferantennummer (peek=True: nur ansehen, nicht weiterzählen)."""
    counter = _supplier_counter()
    nr = counter.next_seq
    if not peek:
        counter.next_seq = nr + 1
    return nr


def bump_supplier_counter_to(value: int) -> None:
    """Counter auf value+1 anheben, falls value >= aktueller next_seq."""
    counter = _supplier_counter()
    if value >= counter.next_seq:
        counter.next_seq = value + 1
