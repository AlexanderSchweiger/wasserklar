"""Hilfsfunktionen zur Mahn-Dokument-Generierung (ADR-003)."""
import io
from decimal import Decimal

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, Cm, RGBColor

from app.invoices.design import get_design


def _hex_to_rgb(value: str) -> RGBColor:
    v = (value or "").lstrip("#")
    if len(v) != 6:
        return RGBColor(0x33, 0x33, 0x33)
    return RGBColor(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _hex_fill(value: str) -> str:
    return (value or "").lstrip("#").upper() or "FFFFFF"


def _right_align_cell(cell, text: str, *, font_name: str | None = None,
                      font_size=None, color: RGBColor | None = None,
                      bold: bool = False) -> None:
    """Setzt Text + RIGHT-Alignment sicher (Alignment NACH Text)."""
    cell.text = text or ""
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if p.runs:
        run = p.runs[0]
        if font_name:
            run.font.name = font_name
        if font_size is not None:
            run.font.size = font_size
        if color is not None:
            run.font.color.rgb = color
        if bold:
            run.bold = True


# ---------------------------------------------------------------------------
# Helpers (identisch zu app/invoices/document_service.py)
# ---------------------------------------------------------------------------

def _de_fmt(value, decimals=2) -> str:
    try:
        value = Decimal(str(value))
    except Exception:
        return str(value)
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def _set_cell_bg(cell, hex_color: str):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _set_cell_border_bottom(cell, size_pt: int = 12, color_hex: str = "333333"):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcBorders = OxmlElement("w:tcBorders")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size_pt))
    bottom.set(qn("w:space"), "0")
    bottom.set(qn("w:color"), _hex_fill(color_hex))
    tcBorders.append(bottom)
    tcPr.append(tcBorders)


def _remove_table_borders(table):
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


# ---------------------------------------------------------------------------
# DOCX-Generierung
# ---------------------------------------------------------------------------

def generate_dunning_docx(notice, wg: dict, design: dict | None = None) -> bytes:
    """Erstellt ein Word-Dokument (.docx) für eine Mahnung.

    Parameters
    ----------
    notice : DunningNotice
        Die Mahnung mit verknüpfter Invoice.
    wg : dict
        WG-Kontaktdaten (name, address, email, phone, iban, bic).
    design : dict | None
        Design-Parameter (Schriftart, Farben). Wenn ``None``, wird das
        Standard-Design ``classic`` verwendet.

    Returns
    -------
    bytes
        Rohe .docx-Datei als Bytes.
    """
    from app.dunning.services import dunning_summary

    if design is None:
        design = get_design("classic")

    font_name = design.get("docx_font", "Arial")
    text_rgb = _hex_to_rgb(design.get("text_color", "#333333"))
    muted_rgb = _hex_to_rgb(design.get("muted_color", "#666666"))
    heading_rgb = _hex_to_rgb(design.get("heading_color", "#333333"))
    accent_rgb = _hex_to_rgb(design.get("accent_color", "#333333"))
    rule_hex = _hex_fill(design.get("rule_color", "#333333"))
    header_bg_hex = _hex_fill(design.get("header_bg", "#F0F0F0"))
    header_text_rgb = _hex_to_rgb(design.get("header_text", "#333333"))
    payment_bg_hex = _hex_fill(design.get("payment_bg", "#F9F9F9"))

    invoice = notice.invoice
    customer = invoice.customer
    summary = dunning_summary(invoice)

    doc = Document()

    # ── Seitenränder ─────────────────────────────────────────────────────
    section = doc.sections[0]
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

    # ── Standardschrift ───────────────────────────────────────────────────
    style = doc.styles["Normal"]
    style.font.name = font_name
    style.font.size = Pt(11)
    style.font.color.rgb = text_rgb

    # ── Briefkopf (2-Spalten-Tabelle ohne Rahmen) ────────────────────────
    header_tbl = doc.add_table(rows=1, cols=2)
    _remove_table_borders(header_tbl)
    header_tbl.columns[0].width = Cm(10)
    header_tbl.columns[1].width = Cm(6)

    left_cell = header_tbl.cell(0, 0)
    p_name = left_cell.paragraphs[0]
    run_name = p_name.add_run(wg.get("name", ""))
    run_name.bold = True
    run_name.font.size = Pt(12)
    run_name.font.color.rgb = heading_rgb

    address = wg.get("address", "")
    if address:
        for line in address.replace("\\n", "\n").split("\n"):
            p_addr = left_cell.add_paragraph(line.strip())
            p_addr.runs[0].font.size = Pt(9)
            p_addr.runs[0].font.color.rgb = muted_rgb

    right_cell = header_tbl.cell(0, 1)
    right_cell.paragraphs[0].clear()
    for key in ("email", "phone"):
        val = wg.get(key, "")
        if val:
            p = right_cell.add_paragraph(val)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.runs[0].font.size = Pt(9)
            p.runs[0].font.color.rgb = accent_rgb
    if not right_cell.paragraphs[0].text:
        right_cell.paragraphs[0]._element.getparent().remove(
            right_cell.paragraphs[0]._element
        )

    doc.add_paragraph()  # Abstand

    # ── Meta (rechtsbündig) ──────────────────────────────────────────────
    meta_lines = [
        ("Rechnungsnummer", invoice.invoice_number),
    ]
    if customer.customer_number:
        meta_lines.append(("Kundennummer", str(customer.customer_number)))
    meta_lines.append(("Rechnungsdatum", invoice.date.strftime("%d.%m.%Y")))
    meta_lines.append(("Mahndatum", notice.issued_date.strftime("%d.%m.%Y")))
    if notice.new_due_date:
        meta_lines.append(("Zahlbar bis", notice.new_due_date.strftime("%d.%m.%Y")))

    for label, value in meta_lines:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run_lbl = p.add_run(f"{label}: ")
        run_lbl.bold = True
        run_lbl.font.color.rgb = heading_rgb
        p.add_run(value)

    doc.add_paragraph()  # Abstand

    # ── Empfänger ────────────────────────────────────────────────────────
    p_cust = doc.add_paragraph(customer.name)
    p_cust.runs[0].bold = True

    street_parts = [customer.strasse, customer.hausnummer]
    street = " ".join(p for p in street_parts if p)
    city_parts = [customer.plz, customer.ort]
    city = " ".join(p for p in city_parts if p)
    if street:
        doc.add_paragraph(street)
    if city:
        doc.add_paragraph(city)
    land = customer.land
    if land and land != "Österreich":
        doc.add_paragraph(land)

    # ── Überschrift ──────────────────────────────────────────────────────
    title = notice.print_title_snapshot or notice.name_snapshot
    heading = doc.add_heading(title, level=1)
    heading.runs[0].font.color.rgb = heading_rgb
    heading.runs[0].font.name = font_name

    # ── Einleitungstext ──────────────────────────────────────────────────
    due_str = invoice.due_date.strftime("%d.%m.%Y") if invoice.due_date else "—"
    intro = (
        f"zu unserer Rechnung {invoice.invoice_number} vom "
        f"{invoice.date.strftime('%d.%m.%Y')} mit Fälligkeit am {due_str} "
        f"konnten wir bisher leider keinen Zahlungseingang feststellen."
    )
    p_intro = doc.add_paragraph()
    p_intro.add_run("Sehr geehrte Damen und Herren,").bold = False
    doc.add_paragraph()
    doc.add_paragraph(intro)
    doc.add_paragraph()

    # ── Forderungsübersicht ──────────────────────────────────────────────
    tbl = doc.add_table(rows=1, cols=2)
    tbl.style = "Table Grid"
    tbl.columns[0].width = Cm(11)
    tbl.columns[1].width = Cm(5)

    hdr = tbl.rows[0].cells
    hdr[0].text = "Position"
    _right_align_cell(hdr[1], "Betrag", font_name=font_name,
                      font_size=Pt(10), color=header_text_rgb, bold=True)
    # Position-Kopf ohne Ausrichtung links — nur Formatierung
    run_pos = hdr[0].paragraphs[0].runs[0]
    run_pos.bold = True
    run_pos.font.size = Pt(10)
    run_pos.font.name = font_name
    run_pos.font.color.rgb = header_text_rgb
    for i in range(2):
        _set_cell_bg(hdr[i], header_bg_hex)
        _set_cell_border_bottom(hdr[i], 16, rule_hex)

    # Hauptforderung
    row = tbl.add_row().cells
    row[0].text = f"Rechnungsbetrag ({invoice.invoice_number})"
    _right_align_cell(row[1], f"{_de_fmt(summary['principal'], 2)} €", font_size=Pt(10))
    for r in row[0].paragraphs[0].runs:
        r.font.size = Pt(10)

    # Kumulative Mahngebühren
    for n in summary["notices"]:
        if n.fee_amount and n.fee_amount > 0:
            row = tbl.add_row().cells
            row[0].text = f"Mahngebühr – {n.name_snapshot}"
            _right_align_cell(row[1], f"{_de_fmt(n.fee_amount, 2)} €", font_size=Pt(10))
            for r in row[0].paragraphs[0].runs:
                r.font.size = Pt(10)

    # Gesamtbetrag
    row_total = tbl.add_row().cells
    row_total[0].text = "Gesamtbetrag"
    run_total_lbl = row_total[0].paragraphs[0].runs[0]
    run_total_lbl.bold = True
    run_total_lbl.font.color.rgb = heading_rgb
    _right_align_cell(row_total[1], f"{_de_fmt(summary['gross_total'], 2)} €",
                      color=heading_rgb, bold=True)

    doc.add_paragraph()

    # ── Zahlungsaufforderung ─────────────────────────────────────────────
    new_due_str = notice.new_due_date.strftime("%d.%m.%Y") if notice.new_due_date else "—"

    payment_tbl = doc.add_table(rows=1, cols=1)
    payment_tbl.style = "Table Grid"
    payment_cell = payment_tbl.cell(0, 0)
    _set_cell_bg(payment_cell, payment_bg_hex)

    p_pay = payment_cell.paragraphs[0]
    run_pay_lbl = p_pay.add_run("Zahlungsinformationen")
    run_pay_lbl.bold = True
    run_pay_lbl.font.color.rgb = heading_rgb

    p_pay2 = payment_cell.add_paragraph("Bitte überweisen Sie den Betrag von ")
    p_pay2.add_run(f"{_de_fmt(summary['gross_total'], 2)} €").bold = True
    p_pay2.add_run(f" bis zum {new_due_str}")

    if wg.get("iban"):
        p_iban = payment_cell.add_paragraph("IBAN: ")
        p_iban.add_run(wg["iban"]).bold = True
    if wg.get("bic"):
        payment_cell.add_paragraph(f"BIC: {wg['bic']}")
    payment_cell.add_paragraph(f"Empfänger: {wg.get('name', '')}")
    p_ref = payment_cell.add_paragraph("Verwendungszweck: ")
    p_ref.add_run(f"{invoice.invoice_number} / Mahnung").bold = True

    doc.add_paragraph()

    # ── Schlusstext (stufenspezifisch) ───────────────────────────────────
    level = notice.level_snapshot
    if level <= 2:
        closing = (
            "Sollte sich Ihre Zahlung mit diesem Schreiben gekreuzt haben, "
            "betrachten Sie dieses bitte als gegenstandslos."
        )
    else:
        closing = (
            "Wir bitten Sie dringend, den ausstehenden Betrag innerhalb der "
            "genannten Frist zu begleichen, um weitere Maßnahmen zu vermeiden."
        )
    doc.add_paragraph(closing)

    doc.add_paragraph()  # Abstand

    # ── Fußzeile ─────────────────────────────────────────────────────────
    footer_parts = [wg.get("name", "")]
    addr = wg.get("address", "")
    if addr:
        footer_parts.append(addr.replace("\\n", " | ").replace("\n", " | "))
    if wg.get("email"):
        footer_parts.append(wg["email"])
    p_footer = doc.add_paragraph(" \u2014 ".join(footer_parts))
    p_footer.runs[0].font.size = Pt(9)
    p_footer.runs[0].font.color.rgb = muted_rgb

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
