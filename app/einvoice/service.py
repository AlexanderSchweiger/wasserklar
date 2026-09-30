"""E-Rechnung in der App: Einstellungen, XML pro Rechnung, Einfrieren, Readiness.

Das XML einer Rechnung wird **eingefroren**, sobald sie versendet bzw.
archiviert wird (``invoices.xml_path``): beim Hybrid-PDF gehen die XML-Daten
dem Bildteil vor (BMF 15.10.2025), und § 14b UStG verlangt die unveraenderte
Aufbewahrung. Jedes spaetere PDF derselben Rechnung (Druck- und Mail-Variante,
``_V2`` …) bettet deshalb genau dieses XML ein — auch wenn sich Stammdaten des
Mandanten inzwischen geaendert haben. Entwuerfe bekommen bei jeder Vorschau
ein frisch erzeugtes XML, eingefroren wird nichts.
"""
import os
from dataclasses import dataclass, field

from app.einvoice import cii
from app.einvoice.mapper import (
    DEFAULT_EXEMPT_REASON, EXEMPT_REASON_KEY, build_einvoice, seller_address,
)
from app.einvoice.rules import check
from app.models import AppSetting, Invoice

ENABLED_KEY = "einvoice.enabled"
PROFILE_EN16931 = "en16931"


def is_enabled():
    """Standard: an. Aus nur, wenn der Mandant es in den Einstellungen abschaltet."""
    return AppSetting.get(ENABLED_KEY) != "false"


@dataclass
class InvoiceEInvoiceStatus:
    enabled: bool
    frozen: bool = False
    issues: list = field(default_factory=list)

    @property
    def ok(self):
        return self.enabled and not self.issues


def invoice_status(invoice):
    """Status fuer die Rechnungsdetailseite. Schreibt nichts."""
    if not is_enabled():
        return InvoiceEInvoiceStatus(enabled=False)
    if _frozen_xml(invoice) is not None:
        return InvoiceEInvoiceStatus(enabled=True, frozen=True)
    return InvoiceEInvoiceStatus(enabled=True, issues=check(build_einvoice(invoice)))


def _frozen_xml(invoice):
    if invoice.xml_path and os.path.exists(invoice.xml_path):
        with open(invoice.xml_path, "rb") as fh:
            return fh.read()
    return None


def einvoice_xml(invoice, *, freeze=False):
    """EN-16931-XML der Rechnung oder ``None`` (abgeschaltet oder Daten unvollstaendig).

    Eingefrorenes XML hat Vorrang. Sonst wird neu erzeugt und — bei gesperrten
    Rechnungen oder mit ``freeze=True`` (Versand eines Entwurfs) — neben dem PDF
    abgelegt (``<PDF_DIR>/<Jahr>/<Nr>.xml``). ``xml_path`` wird am Objekt
    gesetzt; committen muss der Aufrufer (tun alle Render-Wege ohnehin).
    """
    if not is_enabled():
        return None
    frozen = _frozen_xml(invoice)
    if frozen is not None:
        return frozen
    e = build_einvoice(invoice)
    if check(e):
        return None
    xml = cii.serialize(e)
    if freeze or invoice.status != Invoice.STATUS_DRAFT:
        from app.invoices.pdf_service import invoice_doc_dir, versioned_path
        path = versioned_path(invoice_doc_dir(invoice), invoice.invoice_number, "xml")
        with open(path, "wb") as fh:
            fh.write(xml)
        invoice.xml_path = path
        invoice.einvoice_profile = PROFILE_EN16931
    return xml


def exempt_reason():
    return AppSetting.get(EXEMPT_REASON_KEY) or ""


@dataclass
class ReadinessItem:
    ok: bool
    label: str
    hint: str = ""
    required: bool = True


def readiness():
    """Checkliste der Mandanten-Stammdaten fuer die Einstellungsseite.

    Prueft nur die Verkaeuferseite — Kundendaten meldet die Rechnungsdetailseite.
    """
    from app.settings_service import get_wg

    street, postcode, city, parsed = seller_address()
    vat_id = (get_wg("vat_id") or "").strip()
    tax_number = (get_wg("tax_number") or "").strip()
    register = (get_wg("register_number") or "").strip()
    address_hint = ("aus der Kontaktadresse übernommen — bitte prüfen" if parsed and postcode
                    else "")
    return [
        ReadinessItem(bool((get_wg("name") or "").strip()), "Name der Genossenschaft"),
        ReadinessItem(bool(postcode and city), "Anschrift mit PLZ und Ort", address_hint),
        ReadinessItem(bool(vat_id or register),
                      "UID/USt-IdNr. oder Register-/Firmenbuchnummer",
                      "Kennung des Rechnungsstellers (EN 16931, BR-CO-26)"),
        ReadinessItem(bool(vat_id or tax_number), "UID/USt-IdNr. oder Steuernummer",
                      "Steuerliche Registrierung (EN 16931, BR-S-02)"),
        ReadinessItem(bool((get_wg("iban") or "").strip()), "IBAN für die Zahlungsanweisung",
                      "ohne IBAN enthält die E-Rechnung keine Zahlungsdaten", required=False),
        ReadinessItem(bool((get_wg("email") or "").strip()), "E-Mail-Adresse der Genossenschaft",
                      "elektronische Adresse des Rechnungsstellers", required=False),
    ]


def settings_context():
    """Kontext fuer die Karte „E-Rechnung" auf der Einstellungsseite."""
    street, postcode, city, parsed = seller_address()
    items = readiness()
    return {
        "enabled": is_enabled(),
        "exempt_reason": exempt_reason(),
        "default_exempt_reason": DEFAULT_EXEMPT_REASON,
        "address_suggestion": {"street": street, "postal_code": postcode, "city": city}
        if parsed else None,
        "readiness": items,
        "ready": all(item.ok for item in items if item.required),
    }
