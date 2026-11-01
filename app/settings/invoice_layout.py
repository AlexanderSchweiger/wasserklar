"""Darstellung der Rechnung: Logo/Wortmarke, Kontaktinfo im Kopf, Absenderzeile,
Dokumentformat, Design und die Schalter fuer Rechnungsinhalte.

Gespeichert von der Einstellungsseite (OSS) und — in der SaaS — von der Seite
„Rechnungsdesign“. Beide Formulare senden den Marker ``invoice_layout_fields=1``;
ohne Marker bleibt alles stehen. So kann eine Erweiterung die Karten von der
Einstellungsseite nehmen, ohne dass ein Speichern dort die Werte leert (viele
Felder sind Checkboxen: fehlt das Feld, hiesse das sonst „aus“).

``layout_values`` rechnet das Formular in ``{AppSetting-Key: Wert}`` um, ohne
zu schreiben — dieselbe Abbildung speist das Speichern und Vorschauen
ungespeicherter Werte (``settings_service.setting_overrides``).
"""
from app.models import AppSetting
from app.settings_service import (
    CONTACT_INFO_FONT_DEFAULT, CONTACT_INFO_FONT_MAX, CONTACT_INFO_FONT_MIN,
    get_contact_info_font_size, get_invoice_sender_address, sanitize_rich_text,
    validate_logo_data_uri,
)

MARKER = 'invoice_layout_fields'
DOCUMENT_FORMATS = ('pdf', 'docx', 'both')


def form_has_fields(form):
    """True, wenn das Formular die Darstellungsfelder enthaelt (Marker)."""
    return form.get(MARKER) == '1'


def _checkbox(form, name):
    return 'true' if form.get(name) else 'false'


def layout_values(form):
    """``({key: wert}, [fehlermeldungen])`` aus einem Formular mit Marker.

    Ungueltiges wird nicht uebernommen (Meldung), der Rest schon. Das Logo
    steht nur im Ergebnis, wenn es entfernt oder neu gesetzt wurde.
    """
    from app.invoices.design import INVOICE_DESIGNS

    values, errors = {}, []

    # Logo-Text/-Untertitel (Wortmarke als Alternative zum Logo-Bild).
    for attr in ('logo_text', 'logo_subtitle'):
        val = (form.get(f'wg_{attr}') or '').strip()
        values[f'wg.{attr}'] = val or None

    # Logo (Data-URI aus dem Cropper). „Entfernen“ hat Vorrang vor einem neu
    # hochgeladenen Bild.
    if form.get('wg_logo_remove'):
        values['wg.logo'] = None
    else:
        data_uri, logo_err = validate_logo_data_uri(form.get('wg_logo_data', ''))
        if logo_err:
            errors.append(logo_err)
        elif data_uri:
            values['wg.logo'] = data_uri

    fmt = form.get('invoice_document_format', 'pdf')
    values['invoice.document_format'] = fmt if fmt in DOCUMENT_FORMATS else 'pdf'

    design_key = form.get('invoice_design', 'classic')
    values['invoice.design'] = design_key if design_key in INVOICE_DESIGNS else 'classic'

    # Zaehlerwechsel-Detail und Verbrauchsnachweis (Checkboxen).
    values['invoice.print_meter_swap'] = _checkbox(form, 'invoice_print_meter_swap')
    values['invoice.show_meter_readings'] = _checkbox(form, 'invoice_show_meter_readings')

    # „Schriftverkehr per E-Mail?“-Leiste und GiroCode — nur von Designs genutzt,
    # die sie kennen (SaaS). Fehlt das Feld (z. B. ohne Pro-Plan), heisst das „aus“.
    values['invoice.show_email_signup'] = _checkbox(form, 'invoice_show_email_signup')
    values['invoice.show_payment_qr'] = _checkbox(form, 'invoice_show_payment_qr')

    # Rechnungs-Kontakttext (Rich-Text, auf <b>/<i>/<u>/<br> normalisiert).
    contact_info = sanitize_rich_text(form.get('invoice_contact_info', ''))
    values['invoice.contact_info'] = contact_info or None

    # Schriftgroesse des Kontakttexts (Pt) — nur wenn das Feld gesendet wird;
    # ausserhalb der Grenzen = Default.
    if 'invoice_contact_info_font_size' in form:
        try:
            font_size = int(form.get('invoice_contact_info_font_size'))
        except (TypeError, ValueError):
            font_size = CONTACT_INFO_FONT_DEFAULT
        if font_size < CONTACT_INFO_FONT_MIN or font_size > CONTACT_INFO_FONT_MAX:
            font_size = CONTACT_INFO_FONT_DEFAULT
        values['invoice.contact_info_font_size'] = str(font_size)

    # Kleinunternehmer-Hinweis (gedruckt nur in nicht USt-pflichtigen Jahren).
    sb_note = (form.get('invoice_small_business_note') or '').strip()
    values['invoice.small_business_note'] = sb_note[:300] or None

    # Absenderzeile (einzeilig, Klartext).
    sender_address = (form.get('invoice_sender_address') or '').strip()
    values['invoice.sender_address'] = sender_address or None

    return values, errors


def save_invoice_layout(form):
    """Schreibt die Darstellungsfelder (ohne Commit). Gibt Fehlermeldungen zurueck."""
    values, errors = layout_values(form)
    for key, value in values.items():
        AppSetting.set(key, value)
    return errors


def sepa_notice_values(form):
    """Titel und Text des Lastschrift-Hinweises auf der Rechnung.

    ``({key: wert}, [meldungen])``; ein Text mit Platzhalter-Fehler wird nicht
    uebernommen (er liefe sonst erst beim Rendern still auf den Standard).
    """
    from app.direct_debit import notice

    title = (form.get('sepa_notice_title') or '').strip()[:100]
    values, errors = {notice.NOTICE_TITLE_KEY: title or None}, []
    text = (form.get('sepa_notice_text') or '').strip()[:1000]
    if text:
        sample = {key: f'[{key}]' for key, _label in notice.NOTICE_PLACEHOLDERS}
        try:
            notice.render_notice_text(text, sample)
        except Exception:
            errors.append('Der Lastschrift-Hinweistext enthält einen Platzhalter-Fehler '
                          '(z. B. eine nicht geschlossene {{ … }}) — nicht übernommen.')
            return values, errors
    values[notice.NOTICE_TEXT_KEY] = text or None
    return values, errors


def form_context():
    """Aktuelle Werte fuer die Formularfelder (Einstellungen und SaaS-Seite)."""
    from app.invoices import meter_statement
    from app.invoices.design import INVOICE_DESIGNS, available_designs, contact_boxes

    invoice_design = AppSetting.get('invoice.design', 'classic')
    if invoice_design not in INVOICE_DESIGNS:
        invoice_design = 'classic'
    return {
        'doc_format': AppSetting.get('invoice.document_format', 'pdf'),
        'invoice_design': invoice_design,
        'invoice_designs': available_designs(),
        'invoice_contact_info': AppSetting.get('invoice.contact_info') or '',
        'invoice_contact_info_font_size': get_contact_info_font_size(),
        'invoice_contact_boxes': contact_boxes(),
        'invoice_sender_address': get_invoice_sender_address(),
        'invoice_small_business_note': AppSetting.get('invoice.small_business_note') or '',
        'invoice_print_meter_swap': AppSetting.get('invoice.print_meter_swap') == 'true',
        'invoice_show_meter_readings': meter_statement.enabled(),
        'invoice_show_email_signup': AppSetting.get('invoice.show_email_signup') == 'true',
        'invoice_show_payment_qr': AppSetting.get('invoice.show_payment_qr') == 'true',
    }
