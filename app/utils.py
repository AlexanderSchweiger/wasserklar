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
