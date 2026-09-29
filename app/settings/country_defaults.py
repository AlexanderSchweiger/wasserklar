"""Länder-Defaults auf den Mandanten anwenden (Einstellungen → Land/Steuern).

Wird an zwei Stellen gebraucht:

* **Landwechsel** im Einstellungsformular (``overwrite=False``): die
  Standardsaetze des neuen Landes werden ergaenzt, Wasser-USt und Eichfrist nur
  dann umgestellt, wenn sie noch auf dem Default des bisherigen Landes stehen —
  eine bewusst gepflegte Abweichung ueberlebt den Wechsel.
* Button **„Länder-Defaults übernehmen"** (``overwrite=True``): setzt Wasser-USt
  und Eichfrist hart auf die Defaults und kann optional die Saetze anderer
  Laender deaktivieren.

Nie destruktiv: Steuersaetze werden hoechstens deaktiviert, nie geloescht
(Belege speichern den Satz als Zahl). Der Wassercent (Landesabgabe je m³) wird
in Laendern mit Abgabe (Deutschland) eingeblendet und nur mit
``deactivate_foreign`` wieder ausgeblendet — und auch dann nie, solange ein
Tarif die Position nutzt. Der Aufrufer committet.
"""
from decimal import Decimal, InvalidOperation

from app import country as country_mod
from app import tax_service
from app.extensions import db
from app.invoices.charges import LEVY_COUNTRIES
from app.models import AppSetting, ChargeType, TariffComponent, TaxRate

# Zwei Intervall-Settings mit derselben fachlichen Bedeutung (Nacheichfrist):
# Dashboard/Einstellungen und die Zaehlertausch-Touren.
INTERVAL_KEYS = ("meters.replacement_interval_years",
                 "meter_tours.calibration_interval_years")


def _as_decimal(raw):
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _as_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def apply_country_defaults(code, *, previous_code=None, overwrite=False,
                           deactivate_foreign=False):
    """Uebernimmt die Defaults des Landes ``code``.

    Rueckgabe: Dict mit ``added``/``reactivated``/``deactivated`` (Listen von
    Decimals), ``water_rate`` (neuer Wert oder ``None`` = unveraendert) und
    ``interval`` (analog) — fuer die Flash-Meldung.
    """
    target = country_mod.profile(code)
    previous = country_mod.profile(previous_code) if previous_code else None
    result = {"added": [], "reactivated": [], "deactivated": [],
              "water_rate": None, "interval": None, "levy": None}

    standard = {rate: label for rate, label in target.tax_rates}
    rows = {Decimal(str(r.rate)): r for r in TaxRate.query.all()}

    for rate, label in target.tax_rates:
        row = rows.get(rate)
        if row is None:
            db.session.add(TaxRate(rate=rate, label=label, active=True))
            result["added"].append(rate)
        elif not row.active:
            row.active = True
            result["reactivated"].append(rate)

    if deactivate_foreign:
        for rate, row in rows.items():
            if rate not in standard and row.active:
                row.active = False
                result["deactivated"].append(rate)

    # Wassercent: in Abgabe-Laendern einblenden; ausblenden nur auf ausdruecklichen
    # Wunsch und nur, wenn kein Tarif die Position enthaelt.
    levy = ChargeType.query.filter_by(key=ChargeType.KEY_WATER_LEVY).first()
    if levy is not None:
        if target.code in LEVY_COUNTRIES:
            if not levy.active:
                levy.active = True
                result["levy"] = "activated"
        elif deactivate_foreign and levy.active:
            in_use = TariffComponent.query.filter_by(
                charge_type_id=levy.id).first() is not None
            if not in_use:
                levy.active = False
                result["levy"] = "deactivated"

    # Wasser-USt: bei overwrite immer, sonst nur solange sie noch auf dem
    # Default des bisherigen Landes steht (oder gar nicht gepflegt ist).
    current_water = _as_decimal(AppSetting.get(tax_service.WATER_RATE_KEY))
    untouched_water = (current_water is None
                       or (previous is not None
                           and current_water == previous.water_tax_rate))
    if overwrite or untouched_water:
        if current_water != target.water_tax_rate:
            result["water_rate"] = target.water_tax_rate
        AppSetting.set(tax_service.WATER_RATE_KEY, str(target.water_tax_rate))

    # Eichfrist analog (beide Intervall-Settings).
    for key in INTERVAL_KEYS:
        current_interval = _as_int(AppSetting.get(key))
        untouched_interval = (current_interval is None
                              or (previous is not None
                                  and current_interval == previous.calibration_years))
        if overwrite or untouched_interval:
            if current_interval != target.calibration_years:
                result["interval"] = target.calibration_years
            AppSetting.set(key, str(target.calibration_years))

    return result


def _fmt_rates(rates):
    return ", ".join(tax_service.default_label(r) for r in sorted(rates))


def summary_message(result):
    """Deutsche Kurzbeschreibung der Aenderungen fuer die Flash-Meldung."""
    parts = []
    if result["added"]:
        parts.append(f"Steuersätze ergänzt: {_fmt_rates(result['added'])}")
    if result["reactivated"]:
        parts.append(f"wieder aktiviert: {_fmt_rates(result['reactivated'])}")
    if result["deactivated"]:
        parts.append(f"deaktiviert: {_fmt_rates(result['deactivated'])}")
    if result["water_rate"] is not None:
        parts.append(f"Wasser-USt: {tax_service.default_label(result['water_rate'])}")
    if result["interval"] is not None:
        parts.append(f"Zählertausch-Intervall: {result['interval']} Jahre")
    if result.get("levy") == "activated":
        parts.append("Wassercent (Wasserentnahmeentgelt) eingeblendet")
    elif result.get("levy") == "deactivated":
        parts.append("Wassercent ausgeblendet")
    return "; ".join(parts)
