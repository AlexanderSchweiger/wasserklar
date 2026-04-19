"""Design-Registry für Rechnungsdokumente (PDF + DOCX).

Jedes Design ist ein Dict mit den visuellen Parametern, die sowohl vom
HTML-Template (`templates/invoices/pdf_template.html`) als auch vom
DOCX-Generator (`document_service.generate_docx`) gelesen werden.

Die SaaS-Variante kann zusätzliche Designs registrieren, indem sie
`INVOICE_DESIGNS` beim App-Start erweitert (siehe `saas/__init__.py`).

Farben als Hex mit führendem '#'; die DOCX-Schicht schneidet das '#'
bei Bedarf selbst ab.
"""

INVOICE_DESIGNS: dict[str, dict] = {
    "classic": {
        "label": "Klassisch (schlicht, grau)",
        "font_family": "Arial, sans-serif",
        "docx_font": "Arial",
        "text_color": "#333333",
        "muted_color": "#666666",
        "heading_color": "#333333",
        "accent_color": "#333333",
        "border_color": "#dddddd",
        "rule_color": "#333333",
        "header_bg": "#f0f0f0",
        "header_text": "#333333",
        "payment_bg": "#f9f9f9",
        "payment_border": "#dddddd",
        "payment_text": "#333333",
        "meta_label_color": "#333333",
    },
    "blue": {
        "label": "Farbig (Blau)",
        "font_family": "Arial, sans-serif",
        "docx_font": "Arial",
        "text_color": "#1a2b42",
        "muted_color": "#5a6b82",
        "heading_color": "#0b3c5d",
        "accent_color": "#1f7fb5",
        "border_color": "#c7dcec",
        "rule_color": "#1f7fb5",
        "header_bg": "#d9ebf7",
        "header_text": "#0b3c5d",
        "payment_bg": "#eaf3fa",
        "payment_border": "#1f7fb5",
        "payment_text": "#0b3c5d",
        "meta_label_color": "#0b3c5d",
    },
}


def get_design(key: str | None) -> dict:
    """Liefert das Design-Dict zum Schlüssel; fällt auf 'classic' zurück."""
    if key and key in INVOICE_DESIGNS:
        return INVOICE_DESIGNS[key]
    return INVOICE_DESIGNS["classic"]


def available_designs() -> list[tuple[str, str]]:
    """Liste (key, label) für Dropdown-Darstellung — stabile Reihenfolge."""
    return [(k, d["label"]) for k, d in INVOICE_DESIGNS.items()]
