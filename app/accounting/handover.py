"""Übergabe an die Steuerberatung — neutrales Gerüst im OSS.

Das OSS erzeugt selbst keine Exportdatei. Es liefert nur, was eine Erweiterung (die SaaS mit
ihrem DATEV-Export, später z. B. BMD) nicht selbst mitbringen kann:

* **Aktivierung je Mandant.** Eine Erweiterung meldet ihr Format per :func:`register_format` an.
  Der Mandant wählt es unter *Einstellungen → Steuern* (``AppSetting`` ``accounting.handover_format``).
  Ohne angemeldetes Format gibt es weder Schalter noch Felder; ein nicht (mehr) angemeldetes
  Format gilt als aus.
* **Zuordnungsfelder** (Sachkonto an Konto und Geldkonto, Steuerschlüssel am Steuersatz) — nur
  sichtbar und nur gespeichert, solange ein Format aktiv ist. Ausschalten löscht nichts.
* **Übergabeprotokoll + Sperre.** Eine aktive :class:`~app.models.AccountingHandover` sperrt
  Konto, Geldkonto und Projekt ihrer Buchungen und das Löschen ihrer Umbuchungen — unabhängig
  vom Schalter, denn übergeben ist übergeben. Eine zurückgezogene Übergabe sperrt nichts mehr.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from flask import current_app

from app.extensions import db
from app.models import (AccountingHandover, AccountingHandoverItem, AppSetting, Booking,
                        TaxRate, Transfer)

SETTING_KEY = "accounting.handover_format"
FORMATS_EXTENSION = "accounting.handover_formats"

# Felder einer Buchung, die nach der Übergabe nicht mehr geändert werden dürfen.
LOCKED_FIELDS = ("account_id", "real_account_id", "project_id")

LEDGER_ACCOUNT_MAX_LEN = 9
TAX_KEY_MAX_LEN = 4

_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class HandoverFormat:
    """Ein angemeldetes Übergabeformat.

    ``tax_key_defaults(rate)`` liefert ``(Umsatzsteuer-Schlüssel, Vorsteuer-Schlüssel)`` für einen
    Satz (z. B. aus dem Land des Mandanten) oder ``(None, None)``. ``account_hint`` ist ein
    Beispiel für das Sachkonto-Feld.
    """
    key: str
    label: str
    tax_key_defaults: Optional[Callable] = None
    account_hint: str = ""
    setup_endpoint: str = ""    # Endpoint der Einrichtungsseite der Erweiterung (Link in den Einstellungen)


# ---------------------------------------------------------------------------
# Anmeldung + Aktivierung
# ---------------------------------------------------------------------------

def register_format(app, key, label, *, tax_key_defaults=None, account_hint="", setup_endpoint=""):
    """Meldet ein Übergabeformat an (einmal beim App-Start, z. B. in der SaaS)."""
    formats = app.extensions.setdefault(FORMATS_EXTENSION, {})
    formats[key] = HandoverFormat(key=key, label=label, tax_key_defaults=tax_key_defaults,
                                  account_hint=account_hint, setup_endpoint=setup_endpoint)


def available_formats():
    """``{key: HandoverFormat}`` aller angemeldeten Formate (leer im reinen OSS)."""
    try:
        return dict(current_app.extensions.get(FORMATS_EXTENSION) or {})
    except RuntimeError:            # ausserhalb eines App-Kontexts
        return {}


def active_format():
    """Schlüssel des aktiven Formats oder ``None`` (aus bzw. nicht mehr angemeldet)."""
    key = (AppSetting.get(SETTING_KEY) or "").strip()
    return key if key and key in available_formats() else None


def active_format_info():
    """Das aktive :class:`HandoverFormat` oder ``None``."""
    key = active_format()
    return available_formats().get(key) if key else None


def is_active():
    return active_format() is not None


def set_format(key):
    """Schaltet das Format um (``""``/``None`` = aus). ``False`` bei unbekanntem Format.

    Committet nicht; Zuordnungen und Protokoll bleiben beim Ausschalten erhalten.
    """
    key = (key or "").strip()
    if key and key not in available_formats():
        return False
    AppSetting.set(SETTING_KEY, key or None)
    return True


def save_setting_from_form(form):
    """Speichert die Auswahl der Einstellungsseite — nur wenn das Feld im Formular steht.

    Ohne angemeldetes Format rendert die Seite das Feld nicht; dann bleibt der Wert, wie er ist.
    Ein manipulierter Wert für ein unbekanntes Format wird verworfen.
    """
    if "accounting_handover_format" not in form:
        return
    set_format(form.get("accounting_handover_format"))


# ---------------------------------------------------------------------------
# Zuordnungsfelder
# ---------------------------------------------------------------------------

def normalize_ledger_account(raw):
    """``(wert, fehler)`` für eine Sachkontonummer: nur Ziffern, höchstens 9 Stellen, leer = keine."""
    value = re.sub(r"\s+", "", raw or "")
    if not value:
        return None, None
    if not _DIGITS.fullmatch(value) or len(value) > LEDGER_ACCOUNT_MAX_LEN:
        return None, (f"Das Sachkonto darf nur aus Ziffern bestehen "
                      f"(höchstens {LEDGER_ACCOUNT_MAX_LEN} Stellen).")
    return value, None


def normalize_tax_key(raw):
    """``(wert, fehler)`` für einen Steuerschlüssel: nur Ziffern, höchstens 4 Stellen, leer = Standard."""
    value = re.sub(r"\s+", "", raw or "")
    if not value:
        return None, None
    if not _DIGITS.fullmatch(value) or len(value) > TAX_KEY_MAX_LEN:
        return None, (f"Ein Steuerschlüssel besteht nur aus Ziffern "
                      f"(höchstens {TAX_KEY_MAX_LEN} Stellen).")
    return value, None


def _form_has_fields(form):
    # Das Formular trägt die Felder nur, wenn ein Format aktiv ist (Marker-Feld). Ein Formular
    # ohne die Felder (Format aus, älterer Tab) darf gespeicherte Werte nie löschen.
    return is_active() and form.get("ledger_fields") == "1"


def apply_account_fields(account, form):
    """Sachkonto + Automatikkonto aus dem Konto-Formular übernehmen. Liefert eine Fehlermeldung oder None."""
    if not _form_has_fields(form):
        return None
    value, error = normalize_ledger_account(form.get("ledger_account"))
    if error:
        return error
    account.ledger_account = value
    account.ledger_auto_tax = bool(form.get("ledger_auto_tax"))
    return None


def apply_real_account_fields(real_account, form):
    """Sachkonto aus dem Geldkonto-Formular übernehmen. Liefert eine Fehlermeldung oder None."""
    if not _form_has_fields(form):
        return None
    value, error = normalize_ledger_account(form.get("ledger_account"))
    if error:
        return error
    real_account.ledger_account = value
    return None


def apply_tax_rate_fields(row, form):
    """Steuerschlüssel aus dem Steuersatz-Formular übernehmen. Liefert eine Fehlermeldung oder None."""
    if not _form_has_fields(form):
        return None
    output, error = normalize_tax_key(form.get("ledger_tax_key_output"))
    if error:
        return error
    input_, error = normalize_tax_key(form.get("ledger_tax_key_input"))
    if error:
        return error
    row.ledger_tax_key_output = output
    row.ledger_tax_key_input = input_
    return None


def default_tax_keys(rate):
    """Länder-Default ``(Umsatzsteuer, Vorsteuer)`` des aktiven Formats für einen Satz."""
    fmt = active_format_info()
    if fmt is None or fmt.tax_key_defaults is None or rate is None:
        return None, None
    try:
        keys = fmt.tax_key_defaults(Decimal(str(rate)))
    except (InvalidOperation, ValueError, TypeError):
        return None, None
    return tuple(keys) if keys else (None, None)


def effective_tax_keys(rate):
    """``(Umsatzsteuer, Vorsteuer)`` für einen Satz: gepflegter Wert vor Länder-Default."""
    if rate is None:
        return None, None
    try:
        value = Decimal(str(rate))
    except (InvalidOperation, ValueError):
        return None, None
    row = TaxRate.query.filter(TaxRate.rate == value).first()
    default_out, default_in = default_tax_keys(value)
    if row is None:
        return default_out, default_in
    return (row.ledger_tax_key_output or default_out, row.ledger_tax_key_input or default_in)


# ---------------------------------------------------------------------------
# Übergabeprotokoll + Sperre
# ---------------------------------------------------------------------------

def _active_items():
    return (db.session.query(AccountingHandoverItem)
            .join(AccountingHandover, AccountingHandoverItem.handover_id == AccountingHandover.id)
            .filter(AccountingHandover.status == AccountingHandover.STATUS_ACTIVE))


def handed_over_booking_ids(ids=None):
    """Menge der Buchungs-IDs in einer aktiven Übergabe (optional auf ``ids`` begrenzt)."""
    q = _active_items().with_entities(AccountingHandoverItem.booking_id).filter(
        AccountingHandoverItem.booking_id.isnot(None))
    if ids is not None:
        ids = [i for i in ids if i is not None]
        if not ids:
            return set()
        q = q.filter(AccountingHandoverItem.booking_id.in_(ids))
    return {row[0] for row in q.distinct()}


def handed_over_transfer_ids(ids=None):
    """Menge der Umbuchungs-IDs in einer aktiven Übergabe (optional auf ``ids`` begrenzt)."""
    q = _active_items().with_entities(AccountingHandoverItem.transfer_id).filter(
        AccountingHandoverItem.transfer_id.isnot(None))
    if ids is not None:
        ids = [i for i in ids if i is not None]
        if not ids:
            return set()
        q = q.filter(AccountingHandoverItem.transfer_id.in_(ids))
    return {row[0] for row in q.distinct()}


def active_handover_for(obj):
    """Die jüngste aktive Übergabe, die eine Buchung bzw. Umbuchung enthält, oder ``None``."""
    if obj is None or getattr(obj, "id", None) is None:
        return None
    q = (db.session.query(AccountingHandover)
         .join(AccountingHandoverItem, AccountingHandoverItem.handover_id == AccountingHandover.id)
         .filter(AccountingHandover.status == AccountingHandover.STATUS_ACTIVE))
    if isinstance(obj, Booking):
        q = q.filter(AccountingHandoverItem.booking_id == obj.id)
    elif isinstance(obj, Transfer):
        q = q.filter(AccountingHandoverItem.transfer_id == obj.id)
    else:
        return None
    return q.order_by(AccountingHandover.created_at.desc(), AccountingHandover.id.desc()).first()


def format_label(key):
    """Anzeigename eines Formats (auch eines nicht mehr angemeldeten)."""
    fmt = available_formats().get(key)
    return fmt.label if fmt else (key or "").upper()


def lock_message(handover):
    """Deutsche Meldung, warum eine übergebene Buchung nicht geändert werden darf."""
    when = handover.created_at.strftime("%d.%m.%Y") if handover.created_at else ""
    return (f"Diese Buchung wurde am {when} an die Steuerberatung übergeben "
            f"({format_label(handover.format)}). Konto, Bank/Kasse und Projekt lassen sich "
            "nicht mehr ändern — zum Ändern stornieren und neu buchen oder die Übergabe "
            "zurückziehen.")


def transfer_lock_message(handover):
    when = handover.created_at.strftime("%d.%m.%Y") if handover.created_at else ""
    return (f"Diese Umbuchung wurde am {when} an die Steuerberatung übergeben "
            f"({format_label(handover.format)}) und kann nicht gelöscht werden. "
            "Zuerst die Übergabe zurückziehen.")


def locked_changes(booking, data):
    """Liste der gesperrten Felder, die ``data`` gegenüber ``booking`` ändern würde."""
    changed = []
    for field in LOCKED_FIELDS:
        if field in data and data[field] != getattr(booking, field):
            changed.append(field)
    return changed
