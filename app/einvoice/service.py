"""E-Rechnung in der App: Einstellungen, XML pro Rechnung, Einfrieren, Readiness.

Das XML einer Rechnung wird **eingefroren**, sobald sie versendet bzw.
archiviert wird (``invoices.xml_path``): beim Hybrid-PDF gehen die XML-Daten
dem Bildteil vor (BMF 15.10.2025), und § 14b UStG verlangt die unveraenderte
Aufbewahrung. Jedes spaetere PDF derselben Rechnung (Druck- und Mail-Variante,
``_V2`` …) bettet deshalb genau dieses XML ein — auch wenn sich Stammdaten des
Mandanten inzwischen geaendert haben. Entwuerfe bekommen bei jeder Vorschau
ein frisch erzeugtes XML, eingefroren wird nichts.

Drei Profile: Standard ist ZUGFeRD (XML steckt im PDF). Kunden mit
``einvoice_format='xrechnung'`` (deutsche Behoerden) bekommen die XRechnung, Kunden
mit ``'peppol_ubl'`` (Bundesdienststellen in AT, Peppol-Portale) die UBL-Rechnung —
beides reines XML, das PDF haengt als Sichtkopie (BG-24) darin. Welches Profil gilt,
bestimmt der Kunde; einmal eingefroren, bleibt das Profil der Rechnung bestehen,
auch wenn der Kunde spaeter umgestellt wird.
"""
from dataclasses import dataclass, field

from flask import current_app

from app.einvoice import cii, ubl
from app.einvoice.mapper import (
    DEFAULT_EXEMPT_REASON, EXEMPT_REASON_KEY, build_einvoice, seller_address,
)
from app.einvoice.model import (
    FORMAT_XRECHNUNG, PROFILE_EN16931, PROFILE_FOR_FORMAT, PROFILE_PEPPOL_UBL,
    PROFILE_XRECHNUNG, XML_ONLY_PROFILES, Attachment,
)
from app.einvoice.rules import check
from app.file_safety import safe_tenant_path
from app.models import AppSetting, Invoice

ENABLED_KEY = "einvoice.enabled"
XRECHNUNG_FORMAT = FORMAT_XRECHNUNG   # Wert von Customer.einvoice_format
PROFILE_LABELS = {
    PROFILE_EN16931: "ZUGFeRD · EN 16931",
    PROFILE_XRECHNUNG: "XRechnung 3.0",
    PROFILE_PEPPOL_UBL: "UBL · Peppol BIS 3.0",
}
FORMAT_NAMES = {
    PROFILE_EN16931: "ZUGFeRD im PDF",
    PROFILE_XRECHNUNG: "XRechnung",
    PROFILE_PEPPOL_UBL: "UBL-Rechnung",
}


def is_enabled():
    """Standard: an. Aus nur, wenn der Mandant es in den Einstellungen abschaltet."""
    return AppSetting.get(ENABLED_KEY) != "false"


@dataclass
class InvoiceEInvoiceStatus:
    enabled: bool
    frozen: bool = False
    issues: list = field(default_factory=list)
    profile: str = PROFILE_EN16931

    @property
    def ok(self):
        return self.enabled and not self.issues

    @property
    def label(self):
        return PROFILE_LABELS.get(self.profile, self.profile)

    @property
    def format_name(self):
        return FORMAT_NAMES.get(self.profile, self.profile)

    @property
    def is_xrechnung(self):
        return self.profile == PROFILE_XRECHNUNG

    @property
    def is_xml_only(self):
        """Das XML ist der Beleg, das PDF steckt als Anhang darin (XRechnung, UBL)."""
        return self.profile in XML_ONLY_PROFILES


class EInvoiceUnavailable(Exception):
    """Die geforderte E-Rechnung laesst sich nicht erzeugen; ``issues`` nennt die Gruende."""

    def __init__(self, issues, format_name="E-Rechnung"):
        super().__init__("; ".join(issues))
        self.issues = issues
        self.format_name = format_name      # „XRechnung“, „UBL-Rechnung“ — fuer die Meldung


def customer_profile(invoice):
    """Profil, das der Kunde der Rechnung verlangt (Standard: ZUGFeRD)."""
    return PROFILE_FOR_FORMAT.get(invoice.customer.einvoice_format, PROFILE_EN16931)


def profile_of(invoice):
    """Profil der Rechnung: das eingefrorene, solange es ein XML gibt, sonst das des Kunden."""
    if _is_frozen(invoice):
        return invoice.einvoice_profile or PROFILE_EN16931
    return customer_profile(invoice)


def invoice_status(invoice):
    """Status fuer die Rechnungsdetailseite. Schreibt nichts."""
    if not is_enabled():
        return InvoiceEInvoiceStatus(enabled=False)
    if _is_frozen(invoice):
        return InvoiceEInvoiceStatus(enabled=True, frozen=True, profile=profile_of(invoice))
    profile = customer_profile(invoice)
    return InvoiceEInvoiceStatus(enabled=True, profile=profile,
                                 issues=check(build_einvoice(invoice, profile)))


def _frozen_path(invoice):
    """Eingefrorenes XML — nur aus dem Dateibaum des Mandanten (app/file_safety.py):
    der Inhalt wird unbesehen ausgeliefert und eingebettet."""
    return safe_tenant_path(invoice.xml_path)


def _is_frozen(invoice):
    return _frozen_path(invoice) is not None


def _frozen_xml(invoice):
    path = _frozen_path(invoice)
    if path is None:
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _visual_copy(invoice, pdf):
    """BG-24: PDF-Sichtkopie der Rechnung fuer XRechnung und UBL.

    ``pdf`` ist ein schon gerendertes PDF; sonst wird hier das E-Mail-PDF ohne
    eingebettetes XML erzeugt. Ohne WeasyPrint (Windows-Dev) entfaellt der Anhang —
    er ist in der XRechnung freiwillig.
    """
    if pdf is None:
        from app.invoices.pdf_service import render_invoice_pdf
        try:
            pdf = render_invoice_pdf(invoice, for_email=True, embed=False)
        except (ImportError, OSError):
            current_app.logger.warning(
                "XRechnung %s ohne PDF-Anhang: WeasyPrint nicht verfuegbar",
                invoice.invoice_number)
            return None
    return Attachment(filename=f"{invoice.invoice_number}.pdf", mime_code="application/pdf",
                      data=pdf, name=f"Rechnung {invoice.invoice_number} (PDF-Ansicht)")


def einvoice_xml(invoice, *, freeze=False, visual_pdf=None):
    """XML der Rechnung (ZUGFeRD oder XRechnung) oder ``None`` (abgeschaltet/unvollstaendig).

    Eingefrorenes XML hat Vorrang. Sonst wird neu erzeugt und — bei gesperrten
    Rechnungen oder mit ``freeze=True`` (Versand eines Entwurfs) — im Dokumentenregister
    archiviert (``archive_invoice_file``). ``xml_path`` wird am Objekt gesetzt;
    committen muss der Aufrufer (tun alle Render-Wege ohnehin).
    ``visual_pdf``: fertig gerendertes PDF als Anhang einer XRechnung/UBL-Rechnung
    (spart den zweiten Renderlauf).
    """
    if not is_enabled():
        return None
    frozen = _frozen_xml(invoice)
    if frozen is not None:
        return frozen
    profile = customer_profile(invoice)
    e = build_einvoice(invoice, profile)
    if check(e):
        return None
    if profile in XML_ONLY_PROFILES:
        e.attachment = _visual_copy(invoice, visual_pdf)
    xml = ubl.serialize(e) if profile == PROFILE_PEPPOL_UBL else cii.serialize(e)
    if freeze or invoice.status != Invoice.STATUS_DRAFT:
        # Einfrieren = archivieren: das XML kommt ins Dokumentenregister (Pruefsumme, Frist) und
        # ``xml_path`` zeigt darauf (app/invoices/archive.py).
        from app.invoices.archive import archive_invoice_file
        archive_invoice_file(invoice, xml, "xml")
        invoice.einvoice_profile = profile
    return xml


def xml_only_attachment(invoice, *, freeze):
    """Mail-Anhang ``(Dateiname, MIME-Typ, Bytes)`` einer Rechnung mit reinem XML.

    Gilt fuer XRechnung und UBL; ``None`` fuer alle anderen Rechnungen (dort gilt der
    normale PDF-Weg). Kann die Rechnung nicht erzeugt werden, wirft die Funktion
    ``EInvoiceUnavailable`` — eine Behoerde bekommt keine Rechnung, die das Portal
    ohnehin ablehnt.
    """
    if not is_enabled() or profile_of(invoice) not in XML_ONLY_PROFILES:
        return None
    xml = einvoice_xml(invoice, freeze=freeze)
    if xml is None:
        raise EInvoiceUnavailable(
            invoice_status(invoice).issues or ["Die E-Rechnung konnte nicht erzeugt werden."],
            FORMAT_NAMES[profile_of(invoice)])
    return f"{invoice.invoice_number}.xml", "application/xml", xml


xrechnung_attachment = xml_only_attachment      # alter Name (Tests, Aufrufer)


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
        ReadinessItem(all((get_wg(key) or "").strip()
                          for key in ("contact_name", "phone", "email")),
                      "Ansprechpartner mit Telefon und E-Mail",
                      "nur für die XRechnung an Behörden nötig", required=False),
    ]


def settings_context():
    """Kontext fuer die Karte „E-Rechnung" auf der Einstellungsseite."""
    from app.einvoice import obligation
    street, postcode, city, parsed = seller_address()
    items = readiness()
    return {
        "b2b_applies": obligation.applies(),
        "mandate_from": obligation.mandate_from().isoformat(),
        "mandate_options": [
            (obligation.MANDATE_ALL.isoformat(),
             "1. Januar 2028 — Pflicht für alle"),
            (obligation.MANDATE_LARGE_TURNOVER.isoformat(),
             "1. Januar 2027 — Gesamtumsatz im Vorjahr über 800.000 €"),
        ],
        "enabled": is_enabled(),
        "exempt_reason": exempt_reason(),
        "default_exempt_reason": DEFAULT_EXEMPT_REASON,
        "address_suggestion": {"street": street, "postal_code": postcode, "city": city}
        if parsed else None,
        "readiness": items,
        "ready": all(item.ok for item in items if item.required),
    }
