"""Rechnung (OSS-Modell) → EN-16931-Modell.

Die einzige Stelle, die OSS-Modelle fuer die E-Rechnung liest. Alle Betraege
kommen aus den gespeicherten Positionen und werden exakt so gerechnet wie
``Invoice.recalculate_total``/``tax_breakdown`` (USt je Position gerundet, je
Satz summiert) — beim Hybrid-PDF gehen die XML-Daten dem Bildteil vor, beide
muessen also dieselben Zahlen tragen. Weicht die Summe trotzdem von
``invoice.total_amount`` ab, landet das als ``mapping_errors`` im Modell und
die Rechnung bekommt kein XML.

Normalisierungen (siehe E_RECHNUNG_PLAN.md § 4.4/4.5):

* Mahngebuehr-Positionen gehoeren zur Mahnung, nicht zur Rechnung → raus.
* Storno-Beleg (``credit_note``) → Typ 381, alle Werte positiv.
* Negativer Einzelpreis (Schaetzkorrektur, Guthaben-Uebertrag) → Menge
  negativ, Preis positiv (BR-27: der Preis ist nie negativ).
* USt-Satz > 0 → Kategorie S; ohne Satz → E mit Begruendung (Kleinunternehmer-
  Hinweis in nicht USt-pflichtigen Jahren, sonst ``einvoice.exempt_reason``).
  Nie O: „nicht steuerbar" darf laut BR-O-* nicht neben anderen Kategorien und
  nicht neben einer USt-IdNr. stehen.

Die Profile mit reinem XML (``XML_ONLY_PROFILES``: XRechnung und UBL/Peppol)
unterscheiden sich in drei Punkten: die Kaeuferreferenz (BT-10) faellt auf die
Kundennummer zurueck (BR-DE-15), die elektronische Adresse des Kaeufers (BT-49)
ist Pflicht — bei der XRechnung nimmt sie notfalls die Leitweg-ID (Schema 0204) —
und auch ein Storno-Beleg traegt eine Zahlungsanweisung (BR-DE-1). Das UBL-Profil
fuehrt zusaetzlich Auftragsreferenz (BT-13) und Lieferantennummer (BT-29).
"""
import re
from collections import OrderedDict
from decimal import Decimal

from app import country
from app.einvoice import leitweg
from app.einvoice.model import (
    ENDPOINT_EMAIL, ENDPOINT_LEITWEG, PROFILE_EN16931, PROFILE_PEPPOL_UBL,
    PROFILE_XRECHNUNG, XML_ONLY_PROFILES, Address, Delivery, EInvoice, Line, Party, Payment, VatBreakdown,
)
from app.einvoice.units import unit_code
from app.models import AppSetting

CENT = Decimal("0.01")

EXEMPT_REASON_KEY = "einvoice.exempt_reason"
DEFAULT_EXEMPT_REASON = "Nicht steuerbarer bzw. steuerfreier Umsatz"

TYPE_INVOICE = "380"
TYPE_CREDIT_NOTE = "381"
PAYMENT_MEANS_SEPA_TRANSFER = "58"

# Auslandsanschriften der Nachbarlaender (AT/DE selbst kommen aus country.PROFILES).
_FOREIGN_COUNTRIES = {
    "italien": "IT", "italy": "IT", "italia": "IT",
    "schweiz": "CH", "switzerland": "CH", "suisse": "CH",
    "liechtenstein": "LI",
    "slowenien": "SI", "slovenia": "SI",
    "ungarn": "HU", "hungary": "HU",
    "tschechien": "CZ", "tschechische republik": "CZ", "czechia": "CZ",
    "slowakei": "SK", "slovakia": "SK",
    "polen": "PL", "poland": "PL",
    "kroatien": "HR", "croatia": "HR",
    "frankreich": "FR", "france": "FR",
    "niederlande": "NL", "netherlands": "NL",
    "belgien": "BE", "belgium": "BE",
    "luxemburg": "LU", "luxembourg": "LU",
    "dänemark": "DK", "daenemark": "DK", "denmark": "DK",
    "spanien": "ES", "spain": "ES",
    "portugal": "PT",
    "schweden": "SE", "sweden": "SE",
    "vereinigtes königreich": "GB", "großbritannien": "GB", "united kingdom": "GB",
}

_POSTCODE_LINE = re.compile(r"^(?:[A-Z]{1,2}\s*-\s*)?(\d{4,5})\s+(.+)$")


def _dec(value):
    return Decimal(str(value)) if value is not None else Decimal("0")


def _clean(value):
    return (value or "").strip() or None


def _compact(value):
    """USt-IdNr./IBAN/BIC ohne Leerzeichen, in Grossbuchstaben."""
    return re.sub(r"\s+", "", value or "").upper() or None


def parse_address(text):
    """Freitext-Anschrift → ``(strasse, plz, ort)``.

    Versteht die Form des Einstellungsfelds („Musterstraße 1\\n1234 Musterdorf",
    auch mit Landeszeile oder einzeilig mit Komma) und das alte Literal ``\\n``.
    """
    text = (text or "").replace("\\n", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = [part.strip() for part in lines[0].split(",") if part.strip()]
    for idx in range(len(lines) - 1, -1, -1):
        match = _POSTCODE_LINE.match(lines[idx])
        if match:
            return ", ".join(lines[:idx]), match.group(1), match.group(2).strip()
    return ", ".join(lines), "", ""


def seller_address():
    """Anschrift des Rechnungsstellers als ``(strasse, plz, ort, aus_freitext)``.

    Strukturierte Einstellungen (``wg.street``/``wg.postal_code``/``wg.city``)
    haben Vorrang; solange sie leer sind, wird die Kontaktadresse (``wg.address``)
    zerlegt — so funktioniert die E-Rechnung ohne Nacharbeit fuer die meisten
    Mandanten.
    """
    from app.settings_service import get_wg
    street, postcode, city = (_clean(get_wg(k)) or "" for k in ("street", "postal_code", "city"))
    if street or postcode or city:
        return street, postcode, city, False
    street, postcode, city = parse_address(get_wg("address"))
    return street, postcode, city, True


def country_code_for(land):
    """ISO-Code fuer das Freitext-Land einer Anschrift; leer = Mandantenland.

    ``None``, wenn das Land nicht erkannt wird (rules meldet das).
    """
    text = (land or "").strip()
    if not text:
        return country.current_code()
    key = text.casefold()
    for code, prof in country.PROFILES.items():
        if key == prof.name.casefold() or key in prof.aliases:
            return code
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return _FOREIGN_COUNTRIES.get(key)


# Peppol-Schema der USt-IdNr. je Land (EAS-Codeliste): 9914 = AT-UID, 9930 = DE-USt-IdNr.
_PEPPOL_VAT_SCHEMES = {"AT": "9914", "DE": "9930"}


def _vat_endpoint(vat_id):
    """``(Adresse, Schema)`` aus einer USt-IdNr. fuer Peppol; ohne passendes Land ``(None, …)``."""
    scheme = _PEPPOL_VAT_SCHEMES.get((vat_id or "")[:2])
    return (vat_id, scheme) if vat_id and scheme else (None, ENDPOINT_EMAIL)


def _seller(supplier_number=None, peppol=False):
    from app.settings_service import get_wg
    street, postcode, city, _ = seller_address()
    email = _clean(get_wg("email"))
    phone = _clean(get_wg("phone"))
    contact_name = _clean(get_wg("contact_name"))
    vat_id = _compact(get_wg("vat_id"))
    # UBL/Peppol: „EM" ist kein Peppol-Schema — der Rechnungssteller wird ueber seine UID adressiert.
    endpoint, scheme = _vat_endpoint(vat_id) if peppol else (email, ENDPOINT_EMAIL)
    return Party(
        name=_clean(get_wg("name")) or "",
        address=Address(line1=street, postcode=postcode, city=city,
                        country_code=country.current_code()),
        identifier=_clean(supplier_number),       # BT-29 (nur UBL/Bund)
        legal_id=_clean(get_wg("register_number")),
        vat_id=vat_id,
        tax_number=_clean(get_wg("tax_number")),
        endpoint=endpoint,
        endpoint_scheme=scheme,
        contact_name=contact_name,
        contact_phone=phone if (contact_name or phone) else None,
        contact_email=email if contact_name else None,
    )


def _buyer_endpoint(customer, profile):
    """Elektronische Adresse des Kaeufers (BT-49) als ``(adresse, schema)``.

    ZUGFeRD: nur, wenn der Kunde Post per E-Mail bekommt — sonst stuende im XML
    eine Adresse, die der Beleg selbst nicht nennt. XRechnung verlangt die
    Adresse immer; Behoerden haben oft keine E-Mail hinterlegt, dort gilt die
    Leitweg-ID (Schema 0204) als Adresse.
    """
    if profile == PROFILE_PEPPOL_UBL:
        # Peppol-ID „Schema:Kennung" (z.B. 9915:b), sonst aus der USt-IdNr. des Kunden.
        scheme, _, value = (_clean(customer.peppol_id) or "").partition(":")
        if value and scheme.isdigit():
            return value, scheme
        return _vat_endpoint(_compact(customer.vat_id))
    email = _clean(customer.email)
    if profile not in XML_ONLY_PROFILES:
        return (email, ENDPOINT_EMAIL) if customer.wants_email else (None, ENDPOINT_EMAIL)
    if email:
        return email, ENDPOINT_EMAIL
    reference = _clean(customer.buyer_reference)
    if profile == PROFILE_XRECHNUNG and leitweg.looks_like(reference):
        return reference, ENDPOINT_LEITWEG
    return None, ENDPOINT_EMAIL


def _buyer(customer, profile):
    street = " ".join(p for p in (customer.strasse, customer.hausnummer) if p)
    endpoint, scheme = _buyer_endpoint(customer, profile)
    return Party(
        name=customer.letter_name or customer.name or "",
        address=Address(line1=street.strip(), postcode=(customer.plz or "").strip(),
                        city=(customer.ort or "").strip(),
                        country_code=country_code_for(customer.land)),
        identifier=str(customer.customer_number) if customer.customer_number else None,
        vat_id=_compact(customer.vat_id),
        endpoint=endpoint,
        endpoint_scheme=scheme,
    )


def _buyer_reference(customer, profile):
    """BT-10: Leitweg-ID bzw. Kundenreferenz; XRechnung faellt auf die Kundennummer zurueck."""
    reference = _clean(customer.buyer_reference)
    if reference or profile not in XML_ONLY_PROFILES:
        return reference
    return str(customer.customer_number) if customer.customer_number else None


def _delivery(invoice, period, profile):
    prop = invoice.property
    when = period[1] if period else invoice.date
    if prop is None:
        return Delivery(date=when)
    street = " ".join(p for p in (prop.strasse, prop.hausnummer) if p)
    address = Address(line1=street.strip(), postcode=(prop.plz or "").strip(),
                      city=(prop.ort or "").strip(),
                      country_code=country_code_for(prop.land))
    # BR-DE-10/11: die XRechnung verlangt an einer Lieferanschrift PLZ und Ort.
    # Fehlen sie an der Liegenschaft, entfaellt die (freiwillige) Anschrift.
    if profile in XML_ONLY_PROFILES and not (address.postcode and address.city):
        address = None
    return Delivery(date=when, name=prop.label(), location_id=prop.object_number or None,
                    address=address)


def _payment(invoice, profile):
    from app.settings_service import get_wg
    iban = _compact(get_wg("iban"))
    if not iban:
        return None
    if invoice.is_credit_note and profile not in XML_ONLY_PROFILES:
        # Beim Storno-Beleg zahlt nicht der Kunde — keine Zahlungsanweisung.
        # (Die XRechnung verlangt sie trotzdem, BR-DE-1.)
        return None
    return Payment(
        means_code=PAYMENT_MEANS_SEPA_TRANSFER,
        iban=iban,
        account_name=_clean(get_wg("account_holder")) or _clean(get_wg("name")),
        bic=_compact(get_wg("bic")),
        reference=invoice.invoice_number,
    )


def _exemption_reason(vat_liable, legal):
    """Begruendung (BT-120) fuer Positionen ohne USt."""
    if not vat_liable:
        return legal.get("small_business_note") or country.current_profile().small_business_note
    return _clean(AppSetting.get(EXEMPT_REASON_KEY)) or DEFAULT_EXEMPT_REASON


def _payment_terms(invoice):
    if invoice.is_credit_note:
        original = invoice.cancels_invoice
        ref = f" zur Rechnung {original.invoice_number}" if original else ""
        return f"Gutschrift{ref}. Der Betrag wird erstattet bzw. verrechnet."
    if invoice.due_date:
        return f"Zahlbar bis {invoice.due_date.strftime('%d.%m.%Y')} ohne Abzug."
    return "Zahlbar sofort ohne Abzug."


def build_einvoice(invoice, profile=PROFILE_EN16931):
    """Baut das EN-16931-Modell einer Rechnung. Schreibt nichts in die DB."""
    from app.accounting.services import is_year_vat_liable
    from app.invoices.services import invoice_legal_context, service_period

    credit = invoice.is_credit_note
    sign = Decimal("-1") if credit else Decimal("1")
    vat_liable = is_year_vat_liable(invoice.date.year) if invoice.date else False
    legal = invoice_legal_context(invoice)
    exemption = _exemption_reason(vat_liable, legal)

    lines, groups = [], OrderedDict()
    items = sorted((i for i in invoice.items if not i.is_dunning_fee),
                   key=lambda i: i.id or 0)
    for number, item in enumerate(items, start=1):
        quantity = _dec(item.quantity) * sign
        price = _dec(item.unit_price)
        amount = _dec(item.amount) * sign
        if price < 0:
            price, quantity = -price, -quantity
        rate = _dec(item.tax_rate) if item.tax_rate and item.tax_rate > 0 else Decimal("0")
        category = "S" if rate > 0 else "E"
        lines.append(Line(
            line_id=str(number), name=(item.description or "").strip(),
            quantity=quantity, unit_code=unit_code(item.unit), net_price=price,
            net_amount=amount, vat_category=category, vat_rate=rate,
        ))
        group = groups.setdefault((category, rate), [Decimal("0"), Decimal("0")])
        group[0] += amount
        if category == "S":
            group[1] += (amount * rate / Decimal("100")).quantize(CENT)

    vat = [VatBreakdown(category=cat, rate=rate, taxable_amount=taxable, tax_amount=tax,
                        exemption_reason=exemption if cat == "E" else None)
           for (cat, rate), (taxable, tax) in groups.items()]
    line_total = sum((line.net_amount for line in lines), Decimal("0"))
    tax_total = sum((v.tax_amount for v in vat), Decimal("0"))
    grand_total = line_total + tax_total

    errors = []
    expected = _dec(invoice.total_amount) * sign
    if grand_total != expected:
        errors.append(
            f"Summenabweichung: Die Positionen ergeben {grand_total} €, die Rechnung "
            f"weist {expected} € aus.")

    period = service_period(invoice)
    original = invoice.cancels_invoice if credit else None
    return EInvoice(
        number=invoice.invoice_number,
        issue_date=invoice.date,
        type_code=TYPE_CREDIT_NOTE if credit else TYPE_INVOICE,
        currency="EUR",
        seller=(_seller(invoice.customer.supplier_number, peppol=True)
                if profile == PROFILE_PEPPOL_UBL else _seller()),
        buyer=_buyer(invoice.customer, profile),
        lines=lines,
        vat=vat,
        line_total=line_total,
        tax_total=tax_total,
        grand_total=grand_total,
        due_payable=grand_total,
        due_date=invoice.due_date,
        payment_terms=_payment_terms(invoice),
        notes=[invoice.notes.strip()] if invoice.notes and invoice.notes.strip() else [],
        period_start=period[0] if period else None,
        period_end=period[1] if period else None,
        delivery=_delivery(invoice, period, profile),
        payment=_payment(invoice, profile),
        preceding_number=original.invoice_number if original else None,
        preceding_date=original.date if original else None,
        profile=profile,
        buyer_reference=_buyer_reference(invoice.customer, profile),
        order_reference=_clean(invoice.customer.order_reference),
        mapping_errors=errors,
    )
