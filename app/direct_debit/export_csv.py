"""Einzugslauf → CSV-Kontrollliste (Excel-tauglich: ``;``, BOM, Dezimalkomma).

Dieselben Zeilen wie in der pain.008-Datei, zum Gegenlesen, Ausdrucken und
Ablegen. Die Bank liest diese Datei nicht — dafuer ist die XML da.
"""
import csv
import io
from decimal import Decimal

from app.banking import format_iban

HEADER = [
    "Einzugsdatum", "Kundennr.", "Name", "Kontoinhaber", "IBAN", "BIC",
    "Mandatsreferenz", "Mandatsdatum", "Rechnungsnr.", "Betrag", "Verwendungszweck",
    "End-to-End-ID", "Status",
]
_STATE_LABELS = {"pending": "im Einzug", "paid": "bezahlt", "returned": "zurückgegeben"}


def _money(value):
    return format(Decimal(str(value or 0)).quantize(Decimal("0.01")), "f").replace(".", ",")


def build(run):
    """CSV-Text des Laufs (mit BOM)."""
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=";")
    writer.writerow(HEADER)
    for item in run.items:
        customer = item.customer
        writer.writerow([
            item.collection_date.strftime("%d.%m.%Y"),
            (customer.customer_number or "") if customer else "",
            customer.name if customer else "",
            item.debtor_name or "",
            format_iban(item.iban),
            item.bic or "",
            item.mandate_reference or "",
            item.mandate_signed_on.strftime("%d.%m.%Y") if item.mandate_signed_on else "",
            item.invoice.invoice_number if item.invoice else "",
            _money(item.amount),
            item.remittance or "",
            item.end_to_end_id,
            _STATE_LABELS.get(item.state, item.state),
        ])
    writer.writerow([])
    writer.writerow(["Summe", "", "", "", "", "", "", "", "", _money(run.total_amount), "",
                     run.message_id, f"{run.item_count} Lastschriften"])
    return out.getvalue()


def filename(run):
    return f"Lastschrift_{run.message_id}.csv"
