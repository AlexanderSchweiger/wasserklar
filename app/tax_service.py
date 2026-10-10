"""Zentrale Steuersatz-Definition.

Single Source of Truth fuer die im System verfuegbaren USt-Saetze. Die Saetze
pflegt der Mandant in den Einstellungen (Tabelle ``tax_rates``, Flag
``active``); die Standardsaetze seines Landes (``app.country``) werden beim
Anlegen geseedet. Solange die Tabelle noch leer ist (frische Installation,
Tests), gelten direkt die Länder-Defaults.

Deaktivieren statt Loeschen: Buchungen und Rechnungspositionen speichern den
Satz als **Zahl**, nicht als Fremdschluessel — ein deaktivierter Satz
verschwindet nur aus den Auswahllisten, die Historie bleibt unberuehrt. Beim
Bearbeiten eines Altbelegs haengen die Aufrufer den bereits gespeicherten Satz
ueber ``include=`` wieder an, damit er beim Speichern nicht verloren geht.
"""
from decimal import Decimal, InvalidOperation

# AppSetting-Key des Standard-USt-Satzes fuer Wasserpositionen.
WATER_RATE_KEY = "tax.water_rate"

# AppSetting-Key des Voranmeldungszeitraums der Umsatzsteuer (vierteljaehrlich
# ist der Normalfall in AT und DE; monatlich ab den Schwellen des Landes).
VAT_RETURN_PERIOD_KEY = "tax.vat_return_period"
VAT_RETURN_QUARTER = "quarter"
VAT_RETURN_MONTH = "month"
VAT_RETURN_PERIODS = (VAT_RETURN_QUARTER, VAT_RETURN_MONTH)


class TaxRateOption:
    """Schlanker Steuersatz-Datensatz mit ``.rate`` (Decimal) und ``.label``
    (str) — feldkompatibel zum ``TaxRate``-Model, damit Templates unveraendert
    darueber iterieren koennen."""

    __slots__ = ("rate", "label", "active")

    def __init__(self, rate, label, active=True):
        self.rate = rate
        self.label = label
        self.active = active

    @property
    def display(self):
        return display_label(self.rate, self.label)


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def default_label(rate):
    """Anzeige-Label fuer einen Satz ohne gepflegte Bezeichnung (``20 %``)."""
    rate = _to_decimal(rate)
    if rate is None:
        return ""
    text = f"{rate.normalize():f}".replace(".", ",")
    return f"{text} %"


def display_label(rate, label=None):
    """Auswahl-Text eines Satzes: die Bezeichnung, wenn sie den Satz schon
    nennt ("10 %", "0 % – keine MwSt"), sonst "7 % – ermäßigt" bzw. "7 %"."""
    base = default_label(rate)
    label = (label or "").strip()
    if not label:
        return base
    if label.startswith(base):
        return label
    return f"{base} – {label}"


def _db_rows():
    """Alle gepflegten Saetze (auch inaktive) oder ``None`` ohne DB-Zugriff."""
    try:
        from app.models import TaxRate
        return TaxRate.query.order_by(TaxRate.rate).all()
    except Exception:
        return None


def _defaults():
    from app import country
    return [TaxRateOption(rate, label) for rate, label in country.current_profile().tax_rates]


def tax_rates(include=None, include_inactive=False):
    """Auswaehlbare Steuersaetze, aufsteigend, als :class:`TaxRateOption`.

    ``include`` — ein Satz oder eine Liste von Saetzen, die zusaetzlich
    enthalten sein muessen (der gespeicherte Satz eines Altbelegs, dessen Satz
    inzwischen deaktiviert ist). ``include_inactive`` liefert alle gepflegten
    Saetze (z.B. fuer Filter ueber Altbuchungen).
    """
    rows = _db_rows()
    if rows:
        options = [
            TaxRateOption(Decimal(str(r.rate)), r.label or default_label(r.rate),
                          active=bool(r.active))
            for r in rows
            if include_inactive or r.active
        ]
    else:
        options = _defaults()

    extra = include if isinstance(include, (list, tuple, set)) else [include]
    known = {o.rate for o in options}
    for value in extra:
        rate = _to_decimal(value)
        if rate is None or rate in known:
            continue
        options.append(TaxRateOption(rate, default_label(rate), active=False))
        known.add(rate)
    options.sort(key=lambda o: o.rate)
    return options


def tax_rate_values():
    """Nur die Werte der aktiven Saetze als Liste von Decimals, aufsteigend."""
    return [o.rate for o in tax_rates()]


def known_rate_values():
    """Alle bekannten Satz-Werte inkl. deaktivierter — fuer die Validierung und
    Filter, die auch Altbelege mit einem inzwischen deaktivierten Satz
    akzeptieren bzw. finden muessen."""
    return [o.rate for o in tax_rates(include_inactive=True)]


def water_tax_rate():
    """Standard-USt fuer Wasserpositionen (Decimal).

    AppSetting ``tax.water_rate`` (vom Mandanten gepflegt) > Länder-Default.
    Ob der Satz auf einer Rechnung tatsaechlich greift, entscheidet der
    Aufrufer ueber die USt-Pflicht des Buchungsjahres.
    """
    try:
        from app.models import AppSetting
        raw = AppSetting.get(WATER_RATE_KEY)
    except Exception:
        raw = None
    rate = _to_decimal(raw)
    if rate is not None and rate >= 0:
        return rate
    from app import country
    return country.current_profile().water_tax_rate


def vat_return_period():
    """Voranmeldungszeitraum des Mandanten: ``"quarter"`` (Standard) oder ``"month"``.

    Steuert nur die Darstellung — Reihenfolge der Zeitraum-Auswahl der
    USt-Voranmeldung und die USt-Blaetter im Jahresbericht. Waehlbar ist auf der
    Seite immer jeder Monat und jedes Quartal.
    """
    try:
        from app.models import AppSetting
        raw = AppSetting.get(VAT_RETURN_PERIOD_KEY)
    except Exception:
        raw = None
    return raw if raw in VAT_RETURN_PERIODS else VAT_RETURN_QUARTER
