from datetime import date


def next_invoice_number():
    """Nächste freie Rechnungsnummer im Format RE-YYYY-NNNN generieren."""
    from app.extensions import db
    from app.models import Invoice

    year = date.today().year
    prefix = f"RE-{year}-"
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
    return f"{prefix}{seq:04d}"
