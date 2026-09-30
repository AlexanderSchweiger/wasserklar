"""EN-16931-Datenmodell (Teilmenge, die eine Wasserrechnung braucht).

Reine Datenklassen ohne OSS-Bezug. Die BT-/BG-Nummern im Kommentar verweisen
auf die semantischen Elemente der EN 16931-1. Betraege sind ``Decimal`` mit
dem Vorzeichen, das im XML steht (Storno-Belege sind hier schon positiv).
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

# Profile einer E-Rechnung. Die Werte landen in ``invoices.einvoice_profile``.
PROFILE_EN16931 = "en16931"          # ZUGFeRD/Factur-X, als XML im PDF
PROFILE_XRECHNUNG = "xrechnung-3.0"  # XRechnung 3.0 (CII), reines XML mit PDF-Anhang
PROFILE_PEPPOL_UBL = "peppol-bis-3"  # UBL 2.1 / Peppol BIS Billing 3.0 (e-Rechnung.gv.at), reines XML
# Profile, in denen das XML der Beleg ist und das PDF nur als Anhang darin steckt.
XML_ONLY_PROFILES = (PROFILE_XRECHNUNG, PROFILE_PEPPOL_UBL)

# Wert von ``Customer.einvoice_format`` fuer Kunden, die eine XRechnung verlangen
# (leer = ZUGFeRD-PDF).
FORMAT_XRECHNUNG = "xrechnung"
FORMAT_PEPPOL_UBL = "peppol_ubl"
XML_ONLY_FORMATS = (FORMAT_XRECHNUNG, FORMAT_PEPPOL_UBL)
PROFILE_FOR_FORMAT = {FORMAT_XRECHNUNG: PROFILE_XRECHNUNG, FORMAT_PEPPOL_UBL: PROFILE_PEPPOL_UBL}

# Schema der elektronischen Adresse (BT-34/BT-49, EAS-Codeliste)
ENDPOINT_EMAIL = "EM"
ENDPOINT_LEITWEG = "0204"


@dataclass
class Address:
    line1: str = ""                     # BT-35 / BT-50 / BT-75
    postcode: str = ""                  # BT-38 / BT-53 / BT-78
    city: str = ""                      # BT-37 / BT-52 / BT-77
    country_code: Optional[str] = None  # BT-40 / BT-55 / BT-80 (ISO 3166-1 alpha-2)


@dataclass
class Party:
    name: str                           # BT-27 / BT-44
    address: Address
    identifier: Optional[str] = None    # BT-46 (Kundennummer)
    legal_id: Optional[str] = None      # BT-30 (Register-/Firmenbuchnummer)
    vat_id: Optional[str] = None        # BT-31 / BT-48
    tax_number: Optional[str] = None    # BT-32 (Steuernummer)
    endpoint: Optional[str] = None      # BT-34 / BT-49 (elektronische Adresse)
    endpoint_scheme: str = ENDPOINT_EMAIL  # schemeID: EM (E-Mail) oder 0204 (Leitweg-ID)
    contact_name: Optional[str] = None  # BT-41
    contact_phone: Optional[str] = None  # BT-42
    contact_email: Optional[str] = None  # BT-43


@dataclass
class Delivery:
    date: Optional[date] = None         # BT-72 (Leistungs-/Lieferdatum)
    name: Optional[str] = None          # BT-70
    location_id: Optional[str] = None   # BT-71 (Objektnummer)
    address: Optional[Address] = None   # BG-15


@dataclass
class Payment:
    means_code: str                     # BT-81 (58 = SEPA-Überweisung)
    iban: str                           # BT-84
    account_name: Optional[str] = None  # BT-85
    bic: Optional[str] = None           # BT-86
    reference: Optional[str] = None     # BT-83 (Verwendungszweck)


@dataclass
class Attachment:
    """BG-24: Anhang, bei der XRechnung die PDF-Sichtkopie."""
    filename: str                       # BT-125 (Dateiname)
    mime_code: str                      # BT-125 (z.B. application/pdf)
    data: bytes                         # BT-125 (Inhalt)
    name: str = "Rechnung"              # BT-123 (Beschreibung)


@dataclass
class Line:
    line_id: str                        # BT-126
    name: str                           # BT-153
    quantity: Decimal                   # BT-129 (darf negativ sein)
    unit_code: str                      # BT-130
    net_price: Decimal                  # BT-146 (nie negativ, BR-27)
    net_amount: Decimal                 # BT-131
    vat_category: str                   # BT-151 (S | E)
    vat_rate: Decimal                   # BT-152


@dataclass
class VatBreakdown:
    category: str                       # BT-118
    rate: Decimal                       # BT-119
    taxable_amount: Decimal             # BT-116
    tax_amount: Decimal                 # BT-117
    exemption_reason: Optional[str] = None  # BT-120


@dataclass
class EInvoice:
    number: str                         # BT-1
    issue_date: date                    # BT-2
    type_code: str                      # BT-3 (380 Rechnung, 381 Gutschrift)
    currency: str                       # BT-5
    seller: Party                       # BG-4
    buyer: Party                        # BG-7
    lines: list                         # BG-25
    vat: list                           # BG-23
    line_total: Decimal                 # BT-106
    tax_total: Decimal                  # BT-110
    grand_total: Decimal                # BT-112
    due_payable: Decimal                # BT-115
    due_date: Optional[date] = None     # BT-9
    payment_terms: Optional[str] = None  # BT-20
    notes: list = field(default_factory=list)   # BT-22
    period_start: Optional[date] = None  # BT-73
    period_end: Optional[date] = None   # BT-74
    delivery: Optional[Delivery] = None  # BG-13
    payment: Optional[Payment] = None   # BG-16/BG-17
    preceding_number: Optional[str] = None      # BT-25
    preceding_date: Optional[date] = None       # BT-26
    profile: str = PROFILE_EN16931              # steuert Regeln und Serialisierung
    buyer_reference: Optional[str] = None       # BT-10 (Leitweg-ID oder Kundenreferenz)
    order_reference: Optional[str] = None       # BT-13 (AT: Auftragsreferenz der Bundesdienststelle)
    attachment: Optional[Attachment] = None     # BG-24 (nur XRechnung)
    # Befunde des Mappers, die keine EN-Regel abbildet (z.B. Summenabweichung
    # zur gespeicherten Rechnung) — rules.check() meldet sie mit.
    mapping_errors: list = field(default_factory=list)

    @property
    def tax_basis_total(self):
        """BT-109: ohne Zu-/Abschlaege auf Belegebene = Summe der Positionen."""
        return self.line_total
