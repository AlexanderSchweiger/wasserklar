"""Tarif-Engine: aus Tarif + individuellen Gebühren die Rechnungspositionen.

Single Source of Truth fuer die Frage „welche Position mit welchem Betrag?" —
**jeder** Betrag, der aus einem Tarif entsteht, kommt aus ``build_lines``:
Massen-Rechnungslauf (``invoices.generate``), manuelle Tarifzeile im
Positions-Editor, Schlussrechnung beim Eigentuemerwechsel, Tarifrechner und
Demo-Daten. Schaetzkorrektur und Plankostenrechnung rechnen ueber
``volume_amount``. Ausserhalb der Engine steht nirgends ``Menge × Preis`` aus
einem Tarif — wer einen neuen Abrechnungsweg baut, ruft ``build_lines``.

Regeln (siehe auch ``ChargeOverride`` und ``app/invoices/tariff_spec.py``):

1. **Der Tarif bestimmt, WELCHE Positionen es gibt** — Text, Berechnungsart,
   USt-Satz, Konto, Staffel und Bedingung kommen immer aus der Tarifposition.
2. **Bedingung** (nur WG-Modus): eine Position mit Kontaktstatus-Bedingung
   entsteht nur, wenn der Status des Rechnungsempfaengers passt (ohne
   WG-Profil gilt „Mitglied"). Eine individuelle Gebühr hebelt das nicht aus.
3. Eine individuelle Gebühr ersetzt nur den **Betrag**; Prioritaet je
   Gebührenart: Objekt > Kunde > Tarif. Keine Zeile = erbt, ``amount`` NULL =
   die Position entfaellt. Nur bei Gebührenarten mit ``overridable``. Bei
   einer m³-Art ist der individuelle Betrag ein fester Preis — die Staffel
   entfaellt dann.
4. Tarifposition ohne Betrag = „nur mit individuellem Betrag": ohne Override
   keine Position.
5. Der USt-Satz der Position greift nur in umsatzsteuerpflichtigen Jahren.
6. ``valid_from`` einer Position (z.B. Wassercent ab 1.7.2026) grenzt Menge
   bzw. Betrag linear nach Tagen ab.
7. **Staffel**: die Stufe ergibt sich aus der Menge der ganzen Rechnung (volle
   Grenzen je Rechnung, auch bei Teilzeitraeumen); ``graduated`` = eine Zeile
   je angebrochener Stufe, ``whole`` = eine Zeile zum Preis der erreichten
   Stufe. Bei ``valid_from`` wird danach die Menge je Stufe abgegrenzt.

Die Engine rechnet nur — sie schreibt nichts in die DB.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.invoices.amounts import line_tax
from app.invoices.price_format import (
    DEFAULT_PLACES, LEGACY_PLACES, clamp_places, format_price,
)
from app.invoices.tariff_spec import (
    TIER_GRADUATED, TIER_MODES, TIER_WHOLE, conditions_as_dict, contact_statuses,
    parse_tiers, tiers_as_list,
)
from app.models import ChargeOverride, ChargeType

CENT = Decimal("0.01")
QTY_STEP = Decimal("0.001")          # InvoiceItem.quantity: Numeric(12, 3)
ZERO = Decimal("0")

SOURCE_TARIFF = "tariff"
SOURCE_CUSTOMER = "customer"
SOURCE_PROPERTY = "property"


@dataclass(frozen=True)
class Charge:
    """Eine aufgeloeste Tarifposition (Betrag nach Überschreibung)."""
    key: str
    label: str
    calc_type: str
    unit_price: Decimal          # €/m³ (1. Stufe) bzw. € pauschal (nach Override)
    tax_rate: Decimal = None     # Satz der Tarifposition (ungefiltert)
    account_id: int = None
    is_levy: bool = False
    source: str = SOURCE_TARIFF
    valid_from: date = None
    sort_order: int = 100
    tiers: tuple = ()            # weitere Stufen (TierStep), nur m³-Positionen
    tier_mode: str = TIER_GRADUATED
    statuses: frozenset = None   # Bedingung Kontaktstatus (None = alle)
    price_decimals: int = DEFAULT_PLACES   # Anzeige des m³-Preises (Gebührenart)

    @property
    def is_per_m3(self):
        return self.calc_type == ChargeType.CALC_PER_M3

    @property
    def is_water(self):
        return self.key == ChargeType.KEY_WATER

    @property
    def is_tiered(self):
        return self.is_per_m3 and bool(self.tiers)

    @property
    def unit(self):
        return "m³" if self.is_per_m3 else "Pauschal"

    def tax(self, vat_liable):
        """USt-Satz fuer die Rechnungsposition (``None`` = keine USt)."""
        return effective_tax(self.tax_rate, vat_liable)


def effective_tax(rate, vat_liable):
    """Satz einer Tarifposition, wie er auf die Rechnung kommt: nur in
    umsatzsteuerpflichtigen Jahren und nur > 0, sonst ``None``."""
    if not vat_liable or rate is None:
        return None
    rate = Decimal(str(rate))
    return rate if rate > 0 else None


def make_charge(*, key, label, calc_type, amount, tax_rate=None, account_id=None,
                is_levy=False, valid_from=None, sort_order=100, tiers=(),
                tier_mode=TIER_GRADUATED, statuses=None, source=SOURCE_TARIFF,
                price_decimals=None):
    """Baut eine ``Charge`` — die eine Stelle, die Wasser-Sonderregeln kennt
    (kein ``valid_from``, keine Bedingung) und Staffeln auf m³-Arten begrenzt.
    ``price_decimals`` = Nachkommastellen der Gebührenart (Default 2)."""
    per_m3 = calc_type == ChargeType.CALC_PER_M3
    is_water = key == ChargeType.KEY_WATER
    return Charge(
        key=key,
        label=label,
        calc_type=calc_type,
        unit_price=Decimal(str(amount)),
        tax_rate=(Decimal(str(tax_rate)) if tax_rate is not None else None),
        account_id=account_id,
        is_levy=bool(is_levy),
        source=source,
        valid_from=None if is_water else valid_from,
        sort_order=sort_order if sort_order is not None else 100,
        tiers=tuple(tiers or ()) if per_m3 else (),
        tier_mode=tier_mode if tier_mode in TIER_MODES else TIER_GRADUATED,
        statuses=(frozenset(statuses) if statuses and not is_water else None),
        price_decimals=clamp_places(price_decimals),
    )


def _sorted(charges):
    return sorted(charges, key=lambda c: (c.sort_order, c.key))


# ---------------------------------------------------------------------------
# Bedingungen
# ---------------------------------------------------------------------------

def wg_mode_active(wg_mode=None):
    """Gelten Kontaktstatus-Bedingungen? Nur im Mandant-Typ
    Wassergenossenschaft — im Versorger-Modus gibt es keinen Status."""
    if wg_mode is not None:
        return bool(wg_mode)
    from app.settings_service import is_wassergenossenschaft
    return is_wassergenossenschaft()


def condition_met(statuses, contact_status, wg_mode):
    """Bedingung erfuellt? Ohne Bedingung, ausserhalb des WG-Modus und ohne
    bekannten Kontakt (reine Tarifansicht) immer. ``wg_mode`` darf ein
    Callable sein — dann wird der Mandant-Typ erst gelesen, wenn eine
    Position tatsaechlich eine Bedingung hat."""
    if not statuses or contact_status is None:
        return True
    if callable(wg_mode):
        wg_mode = wg_mode()
    if not wg_mode:
        return True
    return contact_status in statuses


def _lazy_wg_mode(wg_mode):
    """``wg_mode`` vorgegeben → unveraendert; sonst ein Callable, das den
    Mandant-Typ hoechstens einmal liest (Tarife ohne Bedingung fragen nie)."""
    if wg_mode is not None:
        return bool(wg_mode)
    cache = []

    def _get():
        if not cache:
            cache.append(wg_mode_active())
        return cache[0]
    return _get


def _contact_status(customer, contact_status):
    if contact_status is not None:
        return contact_status
    return customer.wg_status if customer is not None else None


def _component_statuses(comp):
    if comp.charge_type.key == ChargeType.KEY_WATER:
        return None
    return comp.contact_statuses


# ---------------------------------------------------------------------------
# Aufloesung
# ---------------------------------------------------------------------------

def _overrides(owner_id, column):
    if owner_id is None:
        return {}
    rows = ChargeOverride.query.filter(column == owner_id).all()
    return {r.charge_type_id: r for r in rows}


def _is_overridable(ct):
    """Wasser ist nie ueberschreibbar — Verbrauch, Schaetzung und
    Plankostenrechnung haengen am Tarifpreis (Schutz auch gegen ein per DB
    gesetztes Flag)."""
    return bool(ct.overridable) and ct.key != ChargeType.KEY_WATER


def resolve_charges(tariff, *, prop=None, customer=None, contact_status=None,
                    wg_mode=None):
    """Alle Positionen des Tarifs mit effektivem Betrag, sortiert.

    ``prop``/``customer`` duerfen fehlen (manuelle Tarifzeile, Vorschau) —
    dann gilt schlicht der Tarif. ``contact_status`` schlaegt den Status von
    ``customer`` (Tarifrechner, manuelle Tarifzeile); ohne beides gelten alle
    Bedingungen als erfuellt. ``wg_mode`` (Default: Mandant-Typ) laesst sich
    fuer Massenlaeufe einmal vorab bestimmen.

    Positionen ohne effektiven Betrag (Tarif ohne Betrag und kein Override,
    oder Override „entfällt") und Positionen mit nicht erfuellter Bedingung
    sind nicht dabei.
    """
    if tariff is None:
        return []
    wg_mode = _lazy_wg_mode(wg_mode)
    status = _contact_status(customer, contact_status)
    prop_ov = _overrides(getattr(prop, "id", None), ChargeOverride.property_id)
    cust_ov = _overrides(getattr(customer, "id", None), ChargeOverride.customer_id)

    charges = []
    for comp in tariff.components:
        ct = comp.charge_type
        statuses = _component_statuses(comp)
        if not condition_met(statuses, status, wg_mode):
            continue
        amount = comp.amount
        tiers = comp.tier_steps
        source = SOURCE_TARIFF
        if _is_overridable(ct):
            ov = prop_ov.get(ct.id)
            if ov is not None:
                amount, source = ov.amount, SOURCE_PROPERTY
            else:
                ov = cust_ov.get(ct.id)
                if ov is not None:
                    amount, source = ov.amount, SOURCE_CUSTOMER
            if source != SOURCE_TARIFF:
                tiers = ()       # individueller Betrag = fester Preis
        if amount is None:
            continue
        charges.append(make_charge(
            key=ct.key,
            label=comp.label or ct.label,
            calc_type=ct.calc_type,
            amount=amount,
            tax_rate=comp.tax_rate,
            account_id=comp.account_id,
            is_levy=ct.is_levy,
            source=source,
            valid_from=comp.valid_from,
            sort_order=comp.sort_order,
            tiers=tiers,
            tier_mode=comp.tier_mode,
            statuses=statuses,
            price_decimals=ct.price_decimals,
        ))
    return _sorted(charges)


def select_charges(charges, *, contact_status=None, wg_mode=None):
    """Bedingungen auf fertige Charges anwenden — fuer den Tarifrechner, der
    aus dem ungespeicherten Formular rechnet (``charges_from_specs``)."""
    wg_mode = _lazy_wg_mode(wg_mode)
    return _sorted(c for c in charges if condition_met(c.statuses, contact_status, wg_mode))


def charges_from_specs(specs):
    """Charges aus den geparsten Formular-Positionen des Tarifformulars
    (``invoices.routes._parse_tariff_form``) — ohne ORM-Objekte, damit der
    Tarifrechner nie etwas in die Session haengt. Positionen ohne Betrag
    („nur individuell") fehlen wie im Rechnungslauf."""
    out = []
    for spec in specs:
        ct = spec["ct"]
        if spec.get("amount") is None:
            continue
        out.append(make_charge(
            key=ct.key,
            label=spec.get("label") or ct.label,
            calc_type=ct.calc_type,
            amount=spec["amount"],
            tax_rate=spec.get("tax_rate"),
            account_id=spec.get("account_id"),
            is_levy=ct.is_levy,
            valid_from=spec.get("valid_from"),
            sort_order=ct.sort_order,
            tiers=spec.get("tiers") or (),
            tier_mode=spec.get("tier_mode") or TIER_GRADUATED,
            statuses=contact_statuses(spec.get("conditions")),
            price_decimals=ct.price_decimals,
        ))
    return _sorted(out)


def charge_sources(tariff, *, prop=None, customer=None, wg_mode=None):
    """``{key: (kommt_aus_dem_tarif, wirksamer_betrag)}`` je Tarifposition —
    auch fuer Positionen ohne Betrag bzw. mit „entfällt" (Betrag ``None``).
    Positionen, deren Bedingung fuer den Kunden nicht erfuellt ist, fehlen
    (sie entstehen fuer ihn gar nicht).

    Fuer die Plankostenrechnung: ein Aufschlag auf den Tarif wirkt nur dort,
    wo der Betrag tatsaechlich aus dem Tarif kommt; ``None`` heisst "keine
    Gebuehr" und ist ein gueltiger Wert (0,00 € ist eine Gebuehr von null, die
    einen Aufschlag NICHT mitnimmt).
    """
    if tariff is None:
        return {}
    wg_mode = _lazy_wg_mode(wg_mode)
    status = _contact_status(customer, None)
    prop_ov = _overrides(getattr(prop, "id", None), ChargeOverride.property_id)
    cust_ov = _overrides(getattr(customer, "id", None), ChargeOverride.customer_id)
    out = {}
    for comp in tariff.components:
        ct = comp.charge_type
        if not condition_met(_component_statuses(comp), status, wg_mode):
            continue
        from_tariff, amount = True, comp.amount
        if _is_overridable(ct):
            ov = prop_ov.get(ct.id) or cust_ov.get(ct.id)
            if ov is not None:
                from_tariff, amount = False, ov.amount
        out[ct.key] = (from_tariff, amount)
    return out


def water_charge(charges):
    return next((c for c in charges if c.is_water), None)


def ignored_override_types(tariff, *, prop=None, customer=None, wg_mode=None):
    """Individuelle Gebühren, die nicht greifen — Hinweis nach dem
    Rechnungslauf: Gebührenarten, die der Tarif gar nicht kennt, und
    Positionen, deren Bedingung fuer den Kunden nicht erfuellt ist (dort wird
    nur ein Override mit Betrag genannt)."""
    if tariff is None:
        return []
    wg_mode = _lazy_wg_mode(wg_mode)
    status = _contact_status(customer, None)
    in_tariff, blocked = set(), set()
    for comp in tariff.components:
        if condition_met(_component_statuses(comp), status, wg_mode):
            in_tariff.add(comp.charge_type_id)
        else:
            blocked.add(comp.charge_type_id)
    rows = []
    if prop is not None:
        rows += ChargeOverride.query.filter_by(property_id=prop.id).all()
    if customer is not None:
        rows += ChargeOverride.query.filter_by(customer_id=customer.id).all()
    labels = set()
    for r in rows:
        if not r.charge_type.overridable:
            continue
        if r.charge_type_id in blocked:
            if r.amount is not None:
                labels.add(f"{r.charge_type.label} (Bedingung nicht erfüllt)")
        elif r.charge_type_id not in in_tariff:
            labels.add(r.charge_type.label)
    return sorted(labels)


# ---------------------------------------------------------------------------
# Staffel
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Band:
    """Eine (angebrochene) Stufe einer m³-Position."""
    index: int                   # Stufe 1, 2, …
    lower: Decimal               # Untergrenze (exklusiv), Stufe 1: 0
    upper: Decimal               # Obergrenze (inklusiv) oder None = offen
    qty: Decimal
    price: Decimal


def price_levels(charge):
    """Stufen einer m³-Position als Liste ``(index, lower, upper, price)`` —
    ohne Staffel genau eine offene Stufe zum Positionspreis."""
    steps = charge.tiers if charge.is_tiered else ()
    bounds = [s.above for s in steps]
    prices = [charge.unit_price] + [s.price for s in steps]
    lowers = [ZERO] + bounds
    uppers = bounds + [None]
    return [(i + 1, lowers[i], uppers[i], prices[i]) for i in range(len(prices))]


def tier_bands(charge, qty):
    """Teilmengen je Stufe fuer ``qty`` m³ (rein, ohne DB).

    * ohne Staffel bzw. Menge ≤ 0: eine Stufe zum Preis der 1. Stufe
    * ``graduated``: jede angebrochene Stufe mit ihrer Teilmenge
    * ``whole``: die ganze Menge in der erreichten Stufe
    """
    qty = Decimal(str(qty or 0))
    levels = price_levels(charge)
    if len(levels) == 1 or qty <= 0:
        index, lower, upper, price = levels[0]
        return [Band(index, lower, upper, qty, price)]
    if charge.tier_mode == TIER_WHOLE:
        for index, lower, upper, price in levels:
            if upper is None or qty <= upper:
                return [Band(index, lower, upper, qty, price)]
    bands = []
    for index, lower, upper, price in levels:
        if qty <= lower:
            break
        part = (min(qty, upper) if upper is not None else qty) - lower
        bands.append(Band(index, lower, upper, part, price))
    return bands


def band_range(lower, upper):
    """„bis 200 m³" / „über 200 bis 500 m³" / „über 500 m³"."""
    if upper is None:
        return f"über {fmt_qty(lower)} m³" if lower > 0 else "jede Menge"
    if lower <= 0:
        return f"bis {fmt_qty(upper)} m³"
    return f"über {fmt_qty(lower)} bis {fmt_qty(upper)} m³"


# ---------------------------------------------------------------------------
# Zeitabgrenzung + Formatierung
# ---------------------------------------------------------------------------

def span_days(start, end):
    """Tage im geschlossenen Intervall [start, end] (0 wenn leer)."""
    if start is None or end is None or end < start:
        return 0
    return (end - start).days + 1


def active_days(charge, start, end):
    """Tage in [start, end], an denen die Position gilt (``valid_from`` inkl.)."""
    lo = start
    if charge.valid_from is not None and charge.valid_from > lo:
        lo = charge.valid_from
    return span_days(lo, end)


def fmt_price(value, decimals=DEFAULT_PLACES):
    """``Decimal('1.4')`` -> ``"1,40"`` bzw. ``"1,4000"`` bei 4 Stellen
    (Rechnungstext; nie weniger Stellen, als der Preis hat)."""
    return format_price(value, decimals)


def fmt_qty(value):
    """Menge fuer den Rechnungstext: ganze m³ ohne, sonst mit bis zu 3 Stellen."""
    q = Decimal(str(value))
    if q == q.to_integral_value():
        return f"{q.quantize(Decimal('1'))}"
    return f"{q.quantize(QTY_STEP).normalize():f}".replace(".", ",")


def money(value):
    """Auf Cent runden — bewusst mit der Default-Rundung des Decimal-Kontexts,
    exakt wie der Rechnungslauf es vor der Engine tat (Beleg-Stabilitaet)."""
    return Decimal(str(value)).quantize(CENT)


def _active_share(charge, start, end):
    """``(gueltige Tage, Tage gesamt)`` der Abgrenzung nach ``valid_from`` —
    ``None`` = ganzer Zeitraum, ``(0, n)`` = gilt im Zeitraum gar nicht."""
    if charge.valid_from is None or start is None or end is None:
        return None
    total = span_days(start, end)
    valid = active_days(charge, start, end)
    if valid <= 0:
        return 0, total
    if valid < total:
        return valid, total
    return None


def volume_lines(charge, qty, *, head, detail="", start=None, end=None):
    """Rechnungszeilen (Dicts) einer m³-Position fuer ``qty`` m³ — leer, wenn
    sie im Zeitraum [start, end] gar nicht gilt.

    Ohne Staffel genau eine Zeile ``<head> (<m³> m³ × <Preis> €/m³)``; mit
    Staffel ``graduated`` eine Zeile je angebrochener Stufe, ``whole`` eine
    Zeile zum Stufenpreis. Mit ``valid_from`` innerhalb des Zeitraums wird die
    Menge (je Stufe) linear nach Tagen abgegrenzt. ``detail`` (Kontext des
    Aufrufers, z.B. Abzug laut Schlussrechnung) haengt an der ersten Zeile.
    """
    total_qty = Decimal(str(qty or 0))
    share = _active_share(charge, start, end)
    if share is not None and share[0] <= 0:
        return []
    suffix = ""
    if share is not None:
        suffix = (f", zeitanteilig ab {charge.valid_from.strftime('%d.%m.%Y')}: "
                  f"{share[0]}/{share[1]} Tage")
    staged = charge.is_tiered and total_qty > 0
    lines = []
    for i, band in enumerate(tier_bands(charge, total_qty)):
        q = band.qty
        if share is not None:
            q = (q * Decimal(share[0]) / Decimal(share[1])).quantize(QTY_STEP)
        calc = f"{fmt_qty(q)} m³ × {fmt_price(band.price, charge.price_decimals)} €/m³"
        if not staged:
            desc = f"{head} ({calc}{suffix})"
        elif charge.tier_mode == TIER_WHOLE:
            desc = (f"{head} ({calc}, Stufenpreis "
                    f"{band_range(band.lower, band.upper)}{suffix})")
        else:
            desc = (f"{head} – Stufe {band.index} "
                    f"({band_range(band.lower, band.upper)}): {calc}{suffix}")
        if i == 0 and detail:
            desc += detail
        lines.append({
            "description": desc,
            "quantity": q,
            "unit": "m³",
            "unit_price": band.price,
            "price_decimals": charge.price_decimals,
            "amount": money(q * band.price),
            "charge_key": charge.key,
        })
    return lines


def volume_amount(charge, qty, *, start=None, end=None):
    """Nettobetrag einer m³-Position fuer ``qty`` m³ (Summe der Zeilen) —
    fuer Schaetzkorrektur und Plankostenrechnung."""
    return sum((line["amount"] for line in volume_lines(
        charge, qty, head="", start=start, end=end)), ZERO)


def flat_line(charge, *, start=None, end=None, period_days=None, label_suffix="",
              span_text=None):
    """Rechnungszeile (Dict) einer Pauschal-Position, ggf. anteilig.

    Anteilig wird, wenn der abgerechnete Zeitraum [start, end] kuerzer ist als
    ``period_days`` (Eigentuemerwechsel) oder die Position erst ab
    ``valid_from`` gilt. ``None``, wenn im Zeitraum kein einziger Tag gilt.
    ``span_text`` (Schlussrechnung) nennt den Zeitraum im Text:
    ``<Label> anteilig x/y Tage (<span_text>)``.
    """
    amount = charge.unit_price
    desc = charge.label
    if start is not None and end is not None and period_days:
        days = active_days(charge, start, end)
        if days <= 0:
            return None
        if days < period_days:
            amount = money(amount * Decimal(days) / Decimal(period_days))
            if span_text:
                desc = f"{charge.label} anteilig {days}/{period_days} Tage ({span_text})"
            else:
                desc = f"{charge.label} (anteilig {days}/{period_days} Tage)"
    desc += label_suffix
    return {
        "description": desc,
        "quantity": Decimal("1"),
        "unit": "Pauschal",
        "unit_price": amount,
        "price_decimals": 2,
        "amount": money(amount),
        "charge_key": charge.key,
    }


# ---------------------------------------------------------------------------
# Rechnung zusammensetzen
# ---------------------------------------------------------------------------

@dataclass
class MeterPart:
    """Verbrauch eines Zaehlers, wenn die Rechnung ihn einzeln ausweist
    (Zaehlertausch, Schlussrechnung)."""
    label: str                   # "Zähler 4711"
    qty: Decimal
    note: str = ""               # "ausgebaut 01.03.2026"
    suffix: str = ""             # ", abzügl. 5 m³ lt. Schlussrechnung"
    is_estimated: bool = False


@dataclass
class BillingCase:
    """Was abgerechnet wird — Menge, Zeitraum, Textkontext. Die Engine macht
    daraus die Rechnungszeilen (``build_lines``)."""
    consumption: Decimal = ZERO  # abzurechnende m³ des Objekts (netto)
    vat_liable: bool = False
    period_name: str = None      # Zusatz hinter dem Positionstext ("2025/26")
    usage_start: date = None     # Abgrenzung der m³-Positionen (valid_from)
    usage_end: date = None
    fee_start: date = None       # Abgrenzung der Pauschalen
    fee_end: date = None
    period_days: int = None
    include_flat: bool = True
    flat_span_text: str = None   # Schlussrechnung: Zeitraum im Pauschal-Text
    meter_parts: list = field(default_factory=list)
    water_detail: str = ""       # z.B. Abzug laut Schlussrechnung
    is_estimated: bool = False   # beruht der Verbrauch (teils) auf Schaetzung?

    @property
    def billed_m3(self):
        """Abgerechnete Wassermenge — Basis aller m³-Positionen."""
        if self.meter_parts:
            return sum((Decimal(str(p.qty)) for p in self.meter_parts), ZERO)
        return Decimal(str(self.consumption or 0))


def _head(charge, case):
    return f"{charge.label} {case.period_name}" if case.period_name else charge.label


def _water_lines(water, case):
    head = _head(water, case)
    parts = case.meter_parts
    if parts and not water.is_tiered:
        # Linearer Preis: eine Zeile je Zaehler (wie vor der Engine).
        lines = []
        for part in parts:
            qty = Decimal(str(part.qty))
            note = f"{part.note}, " if part.note else ""
            lines.append({
                "description": f"{head} – {part.label} ({note}{fmt_qty(qty)} m³{part.suffix})",
                "quantity": qty,
                "unit": "m³",
                "unit_price": water.unit_price,
                "price_decimals": water.price_decimals,
                "amount": money(qty * water.unit_price),
                "charge_key": water.key,
                "is_estimated": bool(part.is_estimated),
            })
        return lines
    detail = case.water_detail
    if parts:
        # Staffel gilt je Objekt: die Summe wird gestaffelt, die Aufteilung
        # auf die Zaehler steht im Text.
        split = ", ".join(f"{p.label}: {fmt_qty(p.qty)} m³{p.suffix}" for p in parts)
        detail = f" — {split}{detail}"
    lines = volume_lines(water, case.billed_m3, head=head, detail=detail)
    for line in lines:
        line["is_estimated"] = case.is_estimated
    return lines


def build_lines(charges, case):
    """Alle Rechnungszeilen aus den aufgeloesten Positionen (``resolve_charges``).

    Wasser zuerst, danach die uebrigen Positionen in Tarif-Reihenfolge: weitere
    m³-Positionen (z.B. Wassercent) auf die abgerechnete Wassermenge,
    Pauschalen (``include_flat``) ggf. anteilig. Jede Zeile ist ein Dict mit
    genau den ``InvoiceItem``-Feldern ``description, quantity, unit,
    unit_price, price_decimals, amount, charge_key, tax_rate, account_id,
    is_estimated``.
    """
    lines = []
    water = water_charge(charges)
    if water is not None:
        for line in _water_lines(water, case):
            line.update(tax_rate=water.tax(case.vat_liable), account_id=water.account_id)
            lines.append(line)
    billed = case.billed_m3
    for charge in charges:
        if charge.is_water:
            continue
        if charge.is_per_m3:
            new = volume_lines(charge, billed, head=_head(charge, case),
                               start=case.usage_start, end=case.usage_end)
            estimated = case.is_estimated
        else:
            if not case.include_flat:
                continue
            line = flat_line(charge, start=case.fee_start, end=case.fee_end,
                             period_days=case.period_days, span_text=case.flat_span_text)
            new = [line] if line is not None else []
            estimated = False
        for line in new:
            line.update(tax_rate=charge.tax(case.vat_liable), account_id=charge.account_id,
                        is_estimated=estimated)
            lines.append(line)
    return lines


def totals(lines):
    """Netto, USt je Satz und Brutto der Zeilen — gerundet exakt wie
    ``Invoice.recalculate_total`` (USt je Position, ``amounts.line_tax``)."""
    net = ZERO
    taxes = OrderedDict()
    for line in lines:
        amount = Decimal(str(line["amount"]))
        net += amount
        rate = line.get("tax_rate")
        if rate and Decimal(str(rate)) > 0:
            entry = taxes.setdefault(Decimal(str(rate)), {"net": ZERO, "tax": ZERO})
            entry["net"] += amount
            entry["tax"] += line_tax(amount, rate)
    tax = sum((e["tax"] for e in taxes.values()), ZERO)
    return {"net": net, "taxes": taxes, "tax": tax, "gross": net + tax}


def calculate(charges, *, consumption, vat_liable):
    """Tarifrechner: Zeilen + Summen fuer ``consumption`` m³ ohne
    Zeitabgrenzung (ganzer Zeitraum, Pauschalen voll)."""
    lines = build_lines(charges, BillingCase(
        consumption=Decimal(str(consumption or 0)), vat_liable=vat_liable))
    return lines, totals(lines)


# ---------------------------------------------------------------------------
# Snapshot (BillingRun)
# ---------------------------------------------------------------------------

def snapshot(tariff):
    """JSON-taugliche Kopie aller Tarifpositionen (fuer ``BillingRun``)."""
    rows = []
    for comp in tariff.components:
        ct = comp.charge_type
        rows.append({
            "key": ct.key,
            "label": comp.label or ct.label,
            "calc_type": ct.calc_type,
            "amount": (str(comp.amount) if comp.amount is not None else None),
            "tax_rate": (str(comp.tax_rate) if comp.tax_rate is not None else None),
            "account_id": comp.account_id,
            "is_levy": bool(ct.is_levy),
            "valid_from": (comp.valid_from.isoformat() if comp.valid_from else None),
            "tier_mode": comp.tier_mode or TIER_GRADUATED,
            "tiers": tiers_as_list(comp.tier_steps),
            "conditions": conditions_as_dict(comp.conditions),
            "price_decimals": ct.price_decimals,
        })
    return rows


def charges_from_snapshot(rows):
    """Charges aus ``BillingRun.tariff_components_snapshot`` (Tarifbetraege,
    ohne individuelle Gebühren und ohne Bedingungs-Filter) — die Schaetzkorrektur
    rechnet damit genau die Staffel nach, mit der die Rechnung entstand."""
    out = []
    for r in rows or ():
        if r.get("amount") is None:
            continue
        valid_from = r.get("valid_from")
        if isinstance(valid_from, str):
            valid_from = date.fromisoformat(valid_from)
        out.append(make_charge(
            key=r["key"],
            label=r.get("label") or r["key"],
            calc_type=r.get("calc_type") or ChargeType.CALC_FLAT,
            amount=r["amount"],
            tax_rate=r.get("tax_rate"),
            account_id=r.get("account_id"),
            is_levy=r.get("is_levy"),
            valid_from=valid_from,
            tiers=parse_tiers(r.get("tiers")),
            tier_mode=r.get("tier_mode") or TIER_GRADUATED,
            statuses=contact_statuses(r.get("conditions")),
            price_decimals=r.get("price_decimals") or LEGACY_PLACES,   # Altlaeufe: 4
        ))
    return out


def last_day_before(day):
    return day - timedelta(days=1)
