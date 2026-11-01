"""Cache-Zeiger gesperrter Rechnungen nach geaenderten Darstellungs-Einstellungen.

Gesperrte (versendete) Rechnungen merken sich ihr gerendertes PDF/DOCX
(``pdf_path``/``doc_path``). Aendert sich eine Einstellung, die das Dokument
optisch beeinflusst (z. B. neu aktivierter GiroCode, Designwechsel, geaenderte
IBAN/Kontaktdaten), ist dieser Stand veraltet und muss beim naechsten Abruf neu
gerendert werden. Jede Seite, die solche Einstellungen speichert, ruft nach dem
Commit ``refresh()`` auf.

Erweiterungen (die SaaS) melden eigene darstellungsrelevante Keys ueber
``register_render_keys`` an.
"""
import hashlib

from flask import current_app

from app.extensions import db
from app.models import AppSetting, Invoice

# AppSetting-Keys, die das gerenderte Rechnungs-Dokument optisch beeinflussen.
RENDER_KEYS = (
    'invoice.design', 'invoice.show_payment_qr', 'invoice.show_email_signup',
    'invoice.print_meter_swap', 'invoice.contact_info',
    'invoice.contact_info_font_size', 'invoice.sender_address',
    'wg.name', 'wg.address', 'wg.email', 'wg.phone',
    'wg.iban', 'wg.bic', 'wg.account_holder', 'wg.vat_id', 'wg.tax_number',
    'invoice.small_business_note',
    'wg.logo', 'wg.logo_text', 'wg.logo_subtitle',
    # SEPA-Lastschrift: Hinweis + Glaeubiger-ID stehen auf Rechnungen an Bankeinzugs-Kunden
    'sepa.creditor_id', 'sepa.notice_title', 'sepa.notice_text',
    # Verbrauchsnachweis (Zaehlerstaende) auf der Rechnung
    'invoice.show_meter_readings',
)

# Spaeter dazugekommene Keys mit Standardwert: steht der Key auf dem Standard,
# zaehlt er nicht zur Signatur — sonst verwuerfe das erste Speichern nach dem
# Update ohne jede Aenderung alle gecachten Rechnungen.
RENDER_DEFAULTS = {
    'invoice.show_meter_readings': 'true',
}

SIGNATURE_KEY = 'invoice.render_cache_signature'
_EXTRA_KEYS = 'invoice.render_keys_extra'


def register_render_keys(app, *keys):
    """Weitere darstellungsrelevante Keys anmelden (z. B. SaaS-Infobloecke)."""
    extra = app.extensions.setdefault(_EXTRA_KEYS, [])
    for key in keys:
        if key not in RENDER_KEYS and key not in extra:
            extra.append(key)


def extra_keys():
    return tuple(current_app.extensions.get(_EXTRA_KEYS) or ())


def signature():
    """Stabiler Hash aller render-relevanten Einstellungen als Vergleichsschluessel.

    Die Grund-Keys bleiben im bisherigen Format (die gespeicherte Signatur
    bestehender Mandanten passt weiter); angemeldete Zusatz-Keys zaehlen nur mit
    Wert und mit ihrem Namen — leer heisst „nicht genutzt“ und aendert nichts.
    """
    parts = []
    for key in RENDER_KEYS:
        value = AppSetting.get(key) or ""
        if key in RENDER_DEFAULTS and value in ("", RENDER_DEFAULTS[key]):
            continue
        parts.append(value)
    for key in extra_keys():
        value = AppSetting.get(key) or ""
        if value:
            parts.append(f"{key}={value}")
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invalidate_cached_documents():
    """Loescht den PDF-/DOCX-Cache-Zeiger aller gesperrten Rechnungen.

    Nur der DB-Zeiger (``pdf_path``/``doc_path``) wird geleert — die Dateien
    bleiben liegen. Beim naechsten Abruf rendert die Rechnung mit dem aktuellen
    Design/den aktuellen Einstellungen neu. Entwuerfe cachen ohnehin nie.
    """
    Invoice.query.filter(Invoice.status != Invoice.STATUS_DRAFT).update(
        {Invoice.pdf_path: None, Invoice.doc_path: None},
        synchronize_session=False,
    )


def refresh():
    """Nach dem Commit geaenderter Einstellungen aufrufen.

    Weicht die aktuelle Signatur von der gespeicherten ab, werden die Caches
    gesperrter Rechnungen verworfen und die neue Signatur gespeichert (eigener
    Commit). Die Signatur wird mitgespeichert, sodass auch vor diesem Mechanismus
    veraltete Caches beim naechsten Speichern einmalig aufgefrischt werden.
    Gibt True zurueck, wenn verworfen wurde.
    """
    current = signature()
    if AppSetting.get(SIGNATURE_KEY) == current:
        return False
    invalidate_cached_documents()
    AppSetting.set(SIGNATURE_KEY, current)
    db.session.commit()
    return True
