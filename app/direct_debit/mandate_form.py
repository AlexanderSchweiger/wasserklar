"""SEPA-Lastschrift-Mandat: Formular zum Beilegen und Nachweis online erteilter Mandate.

Das Formular ist ein eigener Brief (nicht Teil der Rechnung): vorausgefuellt mit
Name, Anschrift, Glaeubiger und Mandatsreferenz, leer fuer Konto und Unterschrift.
Es entsteht einzeln am Kontakt, als Sammeldruck aus der Kontaktliste oder als
Beilage hinter der Rechnung im Sammeldruck (Einstellung ``sepa.mandate_form_enclosure``).

* **Beim Druck wird nichts gespeichert.** Die gedruckte Referenz ist die vorhandene
  oder der Vorschlag Praefix + Kundennummer; vergeben wird sie erst, wenn das
  Mandat am Kontakt eingetragen wird (``notice.ensure_mandate_reference``).
* **Erweiterungspunkt** wie ``app/invoices/render_hooks.py``: Die SaaS steuert ueber
  ``register_mandate_form_context_provider`` den Online-Block (Link + QR-Code) bei.
* **Nachweis:** ``record_online_grant`` schreibt ein ``DirectDebitMandateEvent`` mit
  dem Wortlaut, dem der Zahler zugestimmt hat — beim online erteilten Mandat tritt
  es an die Stelle der Unterschrift.
"""
from __future__ import annotations

import io
from datetime import date
from typing import Callable, Optional

from flask import current_app, render_template

from app.banking import (
    bic_valid, format_iban, iban_is_sepa, iban_valid, normalize_bic, normalize_iban,
)
from app.direct_debit import notice
from app.models import AppSetting, Customer, DirectDebitMandateEvent

SCOPE_KEY = "sepa.mandate_scope"
INTRO_KEY = "sepa.mandate_form_intro"
ENCLOSURE_KEY = "sepa.mandate_form_enclosure"
SETTING_KEYS = (SCOPE_KEY, INTRO_KEY, ENCLOSURE_KEY)

# Wortlaut-Version fuer den Nachweis. Bei jeder inhaltlichen Aenderung von
# ``mandate_paragraphs`` hochzaehlen.
MANDATE_TEXT_VERSION = "v1"

# Platzhalter fuer den Einleitungstext (Einstellungen, Jinja-Sandbox).
INTRO_PLACEHOLDERS = [
    ("anrede", "Anredezeile, z. B. „Sehr geehrte Frau Huber“"),
    ("name", "Name des Kontakts"),
    ("mandatsreferenz", "Mandatsreferenz"),
    ("wg_name", "Name der Genossenschaft"),
]


# ---------------------------------------------------------------------------
# Wortlaut
# ---------------------------------------------------------------------------

def default_scope():
    from app.settings_service import is_wassergenossenschaft
    basis = "Wasserbezug und Mitgliedschaft" if is_wassergenossenschaft() else "Wasserbezug"
    return (f"für fällige Forderungen aus {basis}, einschließlich sonstiger Leistungen "
            "(z. B. Poolbefüllung, Arbeiten am Hausanschluss)")


def mandate_scope():
    """Umfang des Mandats (wofuer abgebucht werden darf); leer = Standard."""
    return (AppSetting.get(SCOPE_KEY) or "").strip() or default_scope()


def default_intro():
    return ("{{ anrede }},\n\n"
            "bezahlen Sie Ihre Rechnungen bequem per Lastschrift: Füllen Sie dieses Formular "
            "aus und schicken Sie es unterschrieben an uns zurück. Wir buchen den Betrag dann "
            "jeweils am Fälligkeitstag ab — vorher erhalten Sie wie gewohnt Ihre Rechnung mit "
            "Betrag und Abbuchungsdatum.")


def creditor():
    """Glaeubiger fuer Formular und Nachweis: Name, Anschriftzeilen, Glaeubiger-ID."""
    from app.einvoice.mapper import seller_address
    from app.settings_service import get_wg
    street, postcode, city, _parsed = seller_address()
    city_line = " ".join(p for p in (postcode, city) if p)
    return {
        "name": (get_wg("name") or "").strip(),
        "street": street or "",
        "city": city_line,
        "creditor_id": notice.creditor_id(),
    }


def mandate_paragraphs(creditor_name=None, scope=None):
    """Ermaechtigung + Hinweis (EPC-Standardwortlaut, Laender-unabhaengig)."""
    name = creditor_name if creditor_name is not None else creditor()["name"]
    name = name or "den Zahlungsempfänger"
    scope = scope if scope is not None else mandate_scope()
    scope = f" {scope}" if scope else ""
    return [
        f"Ich ermächtige / Wir ermächtigen {name}, Zahlungen{scope} von meinem / unserem "
        "Konto mittels SEPA-Lastschrift einzuziehen. Zugleich weise ich mein / weisen wir "
        f"unser Kreditinstitut an, die von {name} auf mein / unser Konto gezogenen "
        "SEPA-Lastschriften einzulösen.",
        "Hinweis: Ich kann / Wir können innerhalb von acht Wochen, beginnend mit dem "
        "Belastungsdatum, die Erstattung des belasteten Betrages verlangen. Es gelten dabei "
        "die mit meinem / unserem Kreditinstitut vereinbarten Bedingungen.",
    ]


def mandate_text():
    """Wortlaut als ein Text (Snapshot im Nachweis, Bestaetigungsmail)."""
    return "\n\n".join(mandate_paragraphs())


def render_intro(template, context):
    """Einleitungstext in der Jinja-Sandbox (wie der Lastschrift-Hinweis)."""
    return notice.render_notice_text(template, context)


def intro_text(customer, reference):
    from app.settings_service import get_wg
    ctx = {
        "anrede": customer.salutation_line if customer is not None else "Guten Tag",
        "name": customer.letter_name if customer is not None else "",
        "mandatsreferenz": reference or "",
        "wg_name": get_wg("name") or "",
    }
    custom = (AppSetting.get(INTRO_KEY) or "").strip()
    try:
        return render_intro(custom or default_intro(), ctx).strip()
    except Exception:
        return render_intro(default_intro(), ctx).strip()


# ---------------------------------------------------------------------------
# Wer braucht ein Formular?
# ---------------------------------------------------------------------------

def printed_reference(customer):
    """Referenz fuers Formular: vorhanden oder Vorschlag. Schreibt nichts."""
    return customer.mandate_reference or notice.suggested_mandate_reference(customer)


def has_complete_mandate(customer):
    return customer.is_direct_debit and not notice.customer_debit_issues(customer)


def needs_mandate_form(customer):
    """True, wenn der Kontakt ein Mandatsformular bekommen sollte."""
    return bool(
        customer is not None and customer.active and customer.is_customer
        and notice.creditor_id() and not has_complete_mandate(customer)
    )


def enclosure_enabled():
    return (AppSetting.get(ENCLOSURE_KEY) or "") == "true" and bool(notice.creditor_id())


def no_mandate_clause():
    """SQL-Bedingung „kein vollstaendiges Mandat“ (Ueberweiser oder Angaben fehlen)."""
    from sqlalchemy import or_
    return or_(Customer.payment_method != Customer.PAYMENT_DIRECT_DEBIT,
               notice.incomplete_clause())


# ---------------------------------------------------------------------------
# Erweiterungspunkt (SaaS: Online-Block)
# ---------------------------------------------------------------------------

# Provider-Signatur: fn(customer) -> dict | None
_PROVIDERS: list[Callable[..., Optional[dict]]] = []


def register_mandate_form_context_provider(fn) -> None:
    if fn not in _PROVIDERS:
        _PROVIDERS.append(fn)


def reset_mandate_form_context_providers() -> None:
    """Nur fuer Tests / wiederholte App-Erzeugung."""
    _PROVIDERS.clear()


def build_mandate_form_context(customer) -> dict:
    ctx: dict = {}
    for fn in _PROVIDERS:
        try:
            extra = fn(customer)
        except Exception:  # noqa: BLE001 - ein Provider darf das Formular nicht killen
            current_app.logger.exception("Mandatsformular-Context-Provider fehlgeschlagen")
            extra = None
        if extra:
            ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------

def _weasyprint():
    try:
        from app.pdf_html import HTML
        return HTML
    except (ImportError, OSError):
        return None


def _letter_context():
    from app.circulars.documents import current_design
    from app.settings_service import (
        get_contact_info, get_contact_info_font_size, get_invoice_sender_address, wg_settings,
    )
    return {
        "design": current_design(),
        "contact_info": get_contact_info(),
        "contact_info_font_size": get_contact_info_font_size(),
        "invoice_sender_address": get_invoice_sender_address(),
        "wg": wg_settings(),
    }


def _iban_length():
    """Kaestchen fuer die IBAN: Laenge im Land des Mandanten (AT 20, DE 22)."""
    from app.country import current_code
    return 20 if current_code() == "AT" else 22


# Dichtestufen des Formulars: 0 normal, 1 kleinere Schrift/Abstaende, 2 zusaetzlich
# ohne Einleitungstext. Ein Mandat zum Unterschreiben muss auf EINE Seite passen.
COMPACT_LEVELS = (0, 1, 2)


def render_mandate_html(customer, compact=0, _context=None):
    """HTML des Mandatsformulars eines Kontakts (fuer WeasyPrint)."""
    reference = printed_reference(customer)
    extra = _context if _context is not None else build_mandate_form_context(customer)
    return render_template(
        "direct_debit/pdf/mandate_form.html",
        customer=customer,
        reference=reference,
        intro=intro_text(customer, reference),
        creditor=creditor(),
        paragraphs=mandate_paragraphs(),
        iban_boxes=_iban_length(),
        compact=compact,
        **_letter_context(),
        **extra,
    )


def _page_count(document):
    pages = getattr(document, "pages", None)
    return len(pages) if pages is not None else 1


def render_mandate_pdf(customer):
    """PDF-bytes des Formulars (immer eine Seite), oder None ohne WeasyPrint.

    Laeuft das Formular ueber (Logo, langer Name, langer Umfang), wird es dichter
    gerendert; die letzte Stufe gilt in jedem Fall.
    """
    HTML = _weasyprint()
    if HTML is None:
        return None
    extra = build_mandate_form_context(customer)  # Provider nur einmal (legt ggf. einen Code an)
    document = None
    for level in COMPACT_LEVELS:
        document = HTML(string=render_mandate_html(customer, level, extra)).render()
        if _page_count(document) <= 1:
            break
    if _page_count(document) > 1:
        current_app.logger.warning("Mandatsformular fuer Kontakt %s passt nicht auf eine Seite",
                                   customer.id)
    return document.write_pdf()


def render_merged_mandates(customers):
    """Ein PDF mit einem Formular je Kontakt (``PdfWriter``), oder None ohne WeasyPrint.

    Request-unabhaengig; Einzel-Render + pypdf-Concat wie die Rundschreiben (kein
    WeasyPrint-``copy()`` ueber Dokumente).
    """
    if _weasyprint() is None:
        return None
    from pypdf import PdfWriter
    writer = PdfWriter()
    for customer in customers:
        writer.append(io.BytesIO(render_mandate_pdf(customer)))
    return writer


class EnclosureAppender:
    """Haengt im Rechnungs-Sammeldruck hinter die Rechnung das Mandatsformular.

    Nur mit eingeschalteter Einstellung, nie hinter Gutschriften, je Kunde nur
    einmal pro Druck (hinter seiner ersten Rechnung). Ein Fehler beim Formular
    bricht den Rechnungsdruck nie ab.
    """

    def __init__(self):
        self.enabled = enclosure_enabled()
        self.count = 0
        self._done = set()

    def after(self, writer, invoice):
        if not self.enabled or invoice.is_credit_note:
            return
        customer = invoice.customer
        if customer is None or customer.id in self._done or not needs_mandate_form(customer):
            return
        self._done.add(customer.id)
        try:
            pdf = render_mandate_pdf(customer)
        except Exception:  # noqa: BLE001
            current_app.logger.exception("Mandatsformular fuer Kontakt %s fehlgeschlagen",
                                         customer.id)
            return
        if pdf:
            writer.append(io.BytesIO(pdf))
            self.count += 1


# ---------------------------------------------------------------------------
# Online erteiltes Mandat (SaaS-Seite) + Nachweis
# ---------------------------------------------------------------------------

def validate_account(iban, bic, holder):
    """Prueft die Kontoangaben eines Mandats; ``(werte, fehler)``."""
    errors = []
    iban = normalize_iban(iban)
    bic = normalize_bic(bic)
    holder = (holder or "").strip()
    if not holder:
        errors.append("Bitte den Kontoinhaber angeben.")
    elif len(holder) > 70:
        errors.append("Der Kontoinhaber darf höchstens 70 Zeichen lang sein.")
    if not iban:
        errors.append("Bitte die IBAN angeben.")
    elif not iban_valid(iban):
        errors.append("Die IBAN ist ungültig — bitte noch einmal prüfen.")
    elif not iban_is_sepa(iban):
        errors.append("Die IBAN liegt außerhalb des SEPA-Raums.")
    if bic and not bic_valid(bic):
        errors.append("Der BIC ist ungültig (8 oder 11 Zeichen).")
    return {"iban": iban, "bic": bic or None, "account_holder": holder}, errors


def record_online_grant(customer, *, iban, bic, account_holder, email=None, ip=None,
                        user_agent=None, today=None):
    """Stellt den Kontakt auf Bankeinzug und schreibt den Nachweis. Kein commit.

    Eine vorhandene Mandatsreferenz bleibt (ein Kontowechsel unter demselben Mandat
    erkennt der naechste Einzugslauf selbst, ``amended_from_iban``); sonst vergibt
    ``ensure_mandate_reference`` Praefix + Kundennummer.
    """
    customer.payment_method = Customer.PAYMENT_DIRECT_DEBIT
    customer.iban = iban
    customer.bic = bic
    customer.account_holder = account_holder
    customer.mandate_signed_on = today or date.today()
    notice.ensure_mandate_reference(customer)
    cred = creditor()
    event = DirectDebitMandateEvent(
        customer_id=customer.id,
        action=DirectDebitMandateEvent.ONLINE_GRANTED,
        mandate_reference=customer.mandate_reference,
        iban=iban, bic=bic, account_holder=account_holder,
        creditor_name=cred["name"][:200] or None,
        creditor_id=cred["creditor_id"] or None,
        mandate_text=mandate_text(),
        mandate_text_version=MANDATE_TEXT_VERSION,
        email=(email or None) and email[:120],
        ip=(ip or None) and ip[:64],
        user_agent=(user_agent or None) and user_agent[:255],
    )
    from app.extensions import db
    db.session.add(event)
    return event


def render_proof_html(event):
    """Nachweis eines online erteilten Mandats (Druckansicht fuer die Akte)."""
    return render_template(
        "direct_debit/pdf/mandate_proof.html",
        event=event, customer=event.customer,
        iban_display=format_iban(event.iban) if event.iban else "",
        generated_on=date.today(),
        **_letter_context(),
    )


def render_proof_pdf(event):
    HTML = _weasyprint()
    if HTML is None:
        return None
    return HTML(string=render_proof_html(event)).write_pdf()
