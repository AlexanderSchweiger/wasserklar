"""Die eine Stelle, an der aus einer Rechnung ein PDF wird.

Alle Wege, auf denen ein Rechnungs-PDF entsteht — Einzel-PDF, Sammel-PDF,
ZIP, Mail-Anhang, Post-Versand eines Rechnungslaufs und die gleichnamigen
Hintergrund-Jobs der SaaS — rendern ueber ``render_invoice_pdf`` und legen
Dateien ueber ``write_invoice_pdf`` ab. Wer den Inhalt eines Rechnungs-PDFs
erweitert (z.B. die eingebettete E-Rechnung), tut das hier und nicht an den
Aufrufstellen.

WeasyPrint wird erst beim Rendern importiert: ohne GTK (Windows-Dev) wirft
``render_invoice_pdf`` ``ImportError``/``OSError`` — die Aufrufer fangen das
wie bisher selbst ab.

Aus ``app/invoices/routes.py`` extrahiert; die Routen binden die Funktionen
unter ihren alten Underscore-Namen zurueck (``_render_pdf_html``,
``_get_doc_dir``, ``_versioned_path``, ``_current_design``), bestehende
Aufrufer und Tests bleiben unberuehrt.
"""
import os

from flask import current_app, render_template

from app.invoices.design import get_design
from app.invoices.render_hooks import build_pdf_context
from app.models import AppSetting
from app.settings_service import (
    get_contact_info, get_contact_info_font_size, get_invoice_sender_address,
)


def current_design():
    """Liest das aktuell konfigurierte Rechnungsdesign aus den AppSettings."""
    return get_design(AppSetting.get("invoice.design", "classic"))


def render_invoice_html(invoice, *, for_email=False):
    """Rendert die HTML-Vorlage für WeasyPrint mit aktuellem Design.

    ``for_email``: True, wenn das PDF als E-Mail-Anhang erzeugt wird. Damit
    koennen Provider Inhalte unterdruecken, die nur auf der gedruckten Rechnung
    Sinn ergeben (z.B. der „Rechnung per E-Mail?"-Block).

    Das Design kann ein eigenes Template ueber den Schluessel ``template``
    vorgeben (z.B. das SaaS-„wasserklar"-Design); sonst die OSS-Standardvorlage.
    """
    design = current_design()
    template_name = design.get("template", "invoices/pdf_template.html")
    extra = build_pdf_context(invoice, for_email=for_email)
    from app.invoices.services import invoice_legal_context
    return render_template(
        template_name,
        invoice=invoice,
        design=design,
        legal=invoice_legal_context(invoice),
        contact_info=get_contact_info(),
        contact_info_font_size=get_contact_info_font_size(),
        invoice_sender_address=get_invoice_sender_address(),
        for_email=for_email,
        **extra,
    )


def render_invoice_pdf(invoice, *, for_email=False):
    """PDF-Bytes der Rechnung. Wirft ``ImportError``/``OSError`` ohne WeasyPrint."""
    from weasyprint import HTML
    return HTML(string=render_invoice_html(invoice, for_email=for_email)).write_pdf()


def invoice_doc_dir(invoice):
    """Gibt den jahresspezifischen Unterordner für Rechnungsdokumente zurück und legt ihn an.

    Struktur: <PDF_DIR>/<Jahr>/ z.B. instance/pdfs/2024/
    """
    year = invoice.date.year if invoice.date else "misc"
    doc_dir = os.path.join(current_app.config["PDF_DIR"], str(year))
    os.makedirs(doc_dir, exist_ok=True)
    return doc_dir


def versioned_path(doc_dir: str, invoice_number: str, ext: str) -> str:
    """Gibt einen eindeutigen Dateipfad zurück.

    Existiert bereits eine Datei mit dem Basisnamen, wird _V2, _V3, … angehängt,
    damit ältere Versionen erhalten bleiben.

    Beispiel: 2025-00042.pdf → 2025-00042_V2.pdf → 2025-00042_V3.pdf
    """
    base = os.path.join(doc_dir, f"{invoice_number}.{ext}")
    if not os.path.exists(base):
        return base
    v = 2
    while True:
        candidate = os.path.join(doc_dir, f"{invoice_number}_V{v}.{ext}")
        if not os.path.exists(candidate):
            return candidate
        v += 1


def write_invoice_pdf(invoice, pdf_bytes):
    """Legt das PDF unter einem neuen versionierten Pfad ab und gibt ihn zurück.

    Setzt ``invoice.pdf_path`` bewusst NICHT: ob die Datei zum Archiv-PDF der
    Rechnung wird, entscheidet der Aufrufer (ein Entwurfs-Ausdruck wird z.B.
    nur abgelegt, nicht archiviert).
    """
    path = versioned_path(invoice_doc_dir(invoice), invoice.invoice_number, "pdf")
    with open(path, "wb") as fh:
        fh.write(pdf_bytes)
    return path
