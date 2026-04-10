"""Hilfsfunktionen zur Rechnungsdokument-Generierung."""
import io
from decimal import Decimal

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, Cm, RGBColor


def _de_fmt(value, decimals=2) -> str:
    """Formatiert eine Zahl im deutschen Format (1.250,90)."""
    try:
        value = Decimal(str(value))
    except Exception:
        return str(value)
    formatted = f"{value:,.{decimals}f}"
    # Python nutzt englisches Format: tausend=Komma, dezimal=Punkt → tauschen
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def _set_cell_bg(cell, hex_color: str):
    """Setzt die Hintergrundfarbe einer Tabellenzelle."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _set_cell_border_bottom(cell, size_pt: int = 12):
    """Setzt einen unteren Rahmen an einer Tabellenzelle."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcBorders = OxmlElement("w:tcBorders")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size_pt))
    bottom.set(qn("w:space"), "0")
    bottom.set(qn("w:color"), "333333")
    tcBorders.append(bottom)
    tcPr.append(tcBorders)


def _remove_table_borders(table):
    """Entfernt alle Rahmenlinien einer Tabelle (für Briefkopf-Layout)."""
    tbl = table._tbl
    tblPr = tbl.find(qn("w:tblPr"))
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    tblBorders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "none")
        tblBorders.append(el)
    tblPr.append(tblBorders)


def generate_docx(invoice, wg: dict) -> bytes:
    """Erstellt ein Word-Dokument (.docx) für die übergebene Rechnung.

    Parameters
    ----------
    invoice : Invoice
        Das Invoice-Objekt mit allen verknüpften Daten.
    wg : dict
        WG-Kontaktdaten (name, address, email, phone, iban, bic).

    Returns
    -------
    bytes
        Rohe .docx-Datei als Bytes.
    """
    doc = Document()

    # ── Seitenränder ─────────────────────────────────────────────────────
    section = doc.sections[0]
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

    # ── Standardschrift ───────────────────────────────────────────────────
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)
    style.font.color.rgb = RGBColor(0x33, 0x33, 0x33)

    # ── Briefkopf (2-Spalten-Tabelle ohne Rahmen) ─────────────────────────
    header_tbl = doc.add_table(rows=1, cols=2)
    _remove_table_borders(header_tbl)
    header_tbl.columns[0].width = Cm(10)
    header_tbl.columns[1].width = Cm(6)

    left_cell = header_tbl.cell(0, 0)
    p_name = left_cell.paragraphs[0]
    run_name = p_name.add_run(wg.get("name", ""))
    run_name.bold = True
    run_name.font.size = Pt(12)

    address = wg.get("address", "")
    if address:
        for line in address.replace("\\n", "\n").split("\n"):
            p_addr = left_cell.add_paragraph(line.strip())
            p_addr.runs[0].font.size = Pt(9)
            p_addr.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    right_cell = header_tbl.cell(0, 1)
    right_cell.paragraphs[0].clear()
    for key, label in [("email", None), ("phone", None)]:
        val = wg.get(key, "")
        if val:
            p = right_cell.add_paragraph(val)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.runs[0].font.size = Pt(9)
            p.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    # Leere erste Zeile in right_cell entfernen
    if not right_cell.paragraphs[0].text:
        right_cell.paragraphs[0]._element.getparent().remove(right_cell.paragraphs[0]._element)

    doc.add_paragraph()  # Abstand

    # ── Rechnungs-Meta (rechtsbündig) ─────────────────────────────────────
    meta_lines = [
        ("Rechnungsnummer", invoice.invoice_number),
    ]
    if invoice.customer.customer_number:
        meta_lines.append(("Kundennummer", str(invoice.customer.customer_number)))
    meta_lines.append(("Datum", invoice.date.strftime("%d.%m.%Y")))
    meta_lines.append((
        "Fällig bis",
        invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "—"
    ))
    if invoice.period_year:
        meta_lines.append(("Abrechnungsjahr", str(invoice.period_year)))

    for label, value in meta_lines:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run_lbl = p.add_run(f"{label}: ")
        run_lbl.bold = True
        p.add_run(value)

    doc.add_paragraph()  # Abstand

    # ── Empfänger ─────────────────────────────────────────────────────────
    p_cust = doc.add_paragraph(invoice.customer.name)
    p_cust.runs[0].bold = True

    street_parts = [invoice.customer.strasse, invoice.customer.hausnummer]
    street = " ".join(p for p in street_parts if p)
    city_parts = [invoice.customer.plz, invoice.customer.ort]
    city = " ".join(p for p in city_parts if p)
    if street:
        doc.add_paragraph(street)
    if city:
        doc.add_paragraph(city)
    land = invoice.customer.land
    if land and land != "Österreich":
        doc.add_paragraph(land)
    if invoice.property:
        p_prop = doc.add_paragraph()
        run_prop = p_prop.add_run(f"Betr.: {invoice.property.label()}")
        run_prop.font.size = Pt(9)

    # ── Überschrift ───────────────────────────────────────────────────────
    heading = doc.add_heading("Rechnung", level=1)
    heading.runs[0].font.color.rgb = RGBColor(0x33, 0x33, 0x33)

    # ── Positionen-Tabelle ────────────────────────────────────────────────
    items = invoice.items
    has_tax = any(i.tax_rate for i in items)
    col_count = 6 if has_tax else 5

    tbl = doc.add_table(rows=1, cols=col_count)
    tbl.style = "Table Grid"

    # Kopfzeile
    hdr_cells = tbl.rows[0].cells
    headers = ["Beschreibung", "Menge", "Einheit", "Einzelpreis"]
    if has_tax:
        headers.append("MwSt")
    headers.append("Betrag")

    for i, hdr in enumerate(headers):
        hdr_cells[i].text = hdr
        run = hdr_cells[i].paragraphs[0].runs[0]
        run.bold = True
        run.font.size = Pt(10)
        _set_cell_bg(hdr_cells[i], "F0F0F0")
        _set_cell_border_bottom(hdr_cells[i], 16)
        if hdr in ("Menge", "Einzelpreis", "MwSt", "Betrag"):
            hdr_cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    # Positionen
    for item in items:
        row_cells = tbl.add_row().cells
        row_cells[0].text = item.description or ""
        row_cells[1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        row_cells[1].text = _de_fmt(item.quantity, 2)
        row_cells[2].text = item.unit or ""
        row_cells[3].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        row_cells[3].text = f"{_de_fmt(item.unit_price, 2)} €"
        col_offset = 0
        if has_tax:
            tax_cell = row_cells[4]
            tax_cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            tax_cell.text = (
                f"{int(item.tax_rate)} %" if item.tax_rate else "—"
            )
            col_offset = 1
        amount_cell = row_cells[4 + col_offset]
        amount_cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        amount_cell.text = f"{_de_fmt(item.amount, 2)} €"

        for cell in row_cells:
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(10)

    # Summenzeilen
    if has_tax:
        tax_items = [i for i in items if i.tax_rate and i.tax_rate > 0]
        tax_total = sum(i.amount * i.tax_rate / 100 for i in tax_items)
        if tax_total:
            # Netto
            row_net = tbl.add_row().cells
            row_net[0].merge(row_net[col_count - 2])
            row_net[0].text = "Nettobetrag"
            row_net[col_count - 1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            row_net[col_count - 1].text = f"{_de_fmt(invoice.total_amount, 2)} €"
            # MwSt
            row_tax = tbl.add_row().cells
            row_tax[0].merge(row_tax[col_count - 2])
            row_tax[0].text = "MwSt"
            row_tax[col_count - 1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            row_tax[col_count - 1].text = f"{_de_fmt(tax_total, 2)} €"
            # Gesamt
            row_total = tbl.add_row().cells
            row_total[0].merge(row_total[col_count - 2])
            p_total_label = row_total[0].paragraphs[0]
            p_total_label.add_run("Gesamtbetrag inkl. MwSt").bold = True
            row_total[col_count - 1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            total_run = row_total[col_count - 1].paragraphs[0].add_run(
                f"{_de_fmt(invoice.total_amount + tax_total, 2)} €"
            )
            total_run.bold = True
        else:
            _add_total_row(tbl, col_count, invoice.total_amount, "Gesamtbetrag")
    else:
        _add_total_row(tbl, col_count, invoice.total_amount, "Gesamtbetrag")

    # ── Hinweistext ───────────────────────────────────────────────────────
    if invoice.notes:
        p_notes = doc.add_paragraph()
        run_notes = p_notes.add_run(invoice.notes)
        run_notes.italic = True

    doc.add_paragraph()  # Abstand

    # ── Zahlungsinformationen ─────────────────────────────────────────────
    payment_tbl = doc.add_table(rows=1, cols=1)
    payment_tbl.style = "Table Grid"
    payment_cell = payment_tbl.cell(0, 0)
    _set_cell_bg(payment_cell, "F9F9F9")

    p_pay = payment_cell.paragraphs[0]
    p_pay.add_run("Zahlungsinformationen").bold = True

    due_str = invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "—"
    p_pay2 = payment_cell.add_paragraph(
        f"Bitte überweisen Sie den Betrag von "
    )
    p_pay2.add_run(f"{_de_fmt(invoice.total_amount, 2)} €").bold = True
    p_pay2.add_run(f" bis zum {due_str}")

    if wg.get("iban"):
        p_iban = payment_cell.add_paragraph("IBAN: ")
        p_iban.add_run(wg["iban"]).bold = True
    if wg.get("bic"):
        payment_cell.add_paragraph(f"BIC: {wg['bic']}")
    payment_cell.add_paragraph(f"Empfänger: {wg.get('name', '')}")
    p_ref = payment_cell.add_paragraph("Verwendungszweck: ")
    p_ref.add_run(invoice.invoice_number).bold = True

    doc.add_paragraph()  # Abstand

    # ── Fußzeile ──────────────────────────────────────────────────────────
    footer_parts = [wg.get("name", "")]
    addr = wg.get("address", "")
    if addr:
        footer_parts.append(addr.replace("\\n", " | ").replace("\n", " | "))
    if wg.get("email"):
        footer_parts.append(wg["email"])
    p_footer = doc.add_paragraph(" \u2014 ".join(footer_parts))
    p_footer.runs[0].font.size = Pt(9)
    p_footer.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _add_total_row(tbl, col_count: int, amount, label: str):
    row = tbl.add_row().cells
    row[0].merge(row[col_count - 2])
    row[0].paragraphs[0].add_run(label).bold = True
    row[col_count - 1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    row[col_count - 1].paragraphs[0].add_run(f"{_de_fmt(amount, 2)} €").bold = True


def merge_docx_files(sources: list) -> bytes:
    """Fügt mehrere .docx-Dateien zu einem Dokument zusammen.

    Parameters
    ----------
    sources : list
        Liste von Dateipfaden (str) oder Byte-Inhalten (bytes) der Quelldokumente.

    Returns
    -------
    bytes
        Das zusammengeführte .docx als Bytes.
    """
    from copy import deepcopy

    merged = Document()
    # Leeres Standard-Dokument bereinigen
    for el in list(merged.element.body):
        merged.element.body.remove(el)

    for idx, source in enumerate(sources):
        if idx > 0:
            # Seitenumbruch zwischen Rechnungen
            p = OxmlElement("w:p")
            r = OxmlElement("w:r")
            br = OxmlElement("w:br")
            br.set(qn("w:type"), "page")
            r.append(br)
            p.append(r)
            merged.element.body.append(p)

        if isinstance(source, (str, bytes)):
            src_doc = Document(io.BytesIO(source) if isinstance(source, bytes) else source)
        else:
            src_doc = Document(source)

        for element in src_doc.element.body:
            merged.element.body.append(deepcopy(element))

    buf = io.BytesIO()
    merged.save(buf)
    return buf.getvalue()
