"""Tarif-Engine: aus Tarif + individuellen Gebühren die Rechnungspositionen.

Single Source of Truth fuer die Frage „welche Position mit welchem Betrag?" —
genutzt vom Massen-Rechnungslauf (``invoices.generate``), der manuellen
Tarifzeile im Positions-Editor, der Schlussrechnung beim Eigentuemerwechsel
und der Plankostenrechnung. Vorher war diese Kaskade an vier Stellen
dupliziert.

Regeln (siehe auch ``ChargeOverride``):

1. **Der Tarif bestimmt, WELCHE Positionen es gibt** — Text, Berechnungsart,
   USt-Satz und Konto kommen immer aus der Tarifposition.
2. Eine individuelle Gebühr ersetzt nur den **Betrag**; Prioritaet je
   Gebührenart: Objekt > Kunde > Tarif. Keine Zeile = erbt, ``amount`` NULL =
   die Position entfaellt. Nur bei Gebührenarten mit ``overridable``.
3. Tarifposition ohne Betrag = „nur mit individuellem Betrag": ohne Override
   keine Position.
4. Der USt-Satz der Position greift nur in umsatzsteuerpflichtigen Jahren.
5. ``valid_from`` einer Position (z.B. Wassercent ab 1.7.2026) grenzt Menge
   bzw. Betrag linear nach Tagen ab.

Die Engine rechnet nur — sie schreibt nichts in die DB.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.models import ChargeOverride, ChargeType

CENT = Decimal("0.01")
QTY_STEP = Decimal("0.001")          # InvoiceItem.quantity: Numeric(12, 3)

SOURCE_TARIFF = "tariff"
SOURCE_CUSTOMER = "customer"
SOURCE_PROPERTY = "property"


@dataclass(frozen=True)
class Charge:
    """Eine aufgeloeste Tarifposition (Betrag nach Überschreibung)."""
    key: str
    label: str
    calc_type: str
    unit_price: Decimal          # €/m³ bzw. € pauschal (nach Override)
    tax_rate: Decimal = None     # Satz der Tarifposition (ungefiltert)
    account_id: int = None
    is_levy: bool = False
    source: str = SOURCE_TARIFF
    valid_from: date = None
    sort_order: int = 100

    @property
    def is_per_m3(self):
        return self.calc_type == ChargeType.CALC_PER_M3

    @property
    def is_water(self):
        return self.key == ChargeType.KEY_WATER

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


def resolve_charges(tariff, *, prop=None, customer=None):
    """Alle Positionen des Tarifs mit effektivem Betrag, sortiert.

    ``prop``/``customer`` duerfen fehlen (manuelle Tarifzeile, Vorschau) —
    dann gilt schlicht der Tarif. Positionen ohne effektiven Betrag (Tarif
    ohne Betrag und kein Override, oder Override „entfällt") sind nicht dabei.
    """
    if tariff is None:
        return []
    prop_ov = _overrides(getattr(prop, "id", None), ChargeOverride.property_id)
    cust_ov = _overrides(getattr(customer, "id", None), ChargeOverride.customer_id)

    charges = []
    for comp in tariff.components:
        ct = comp.charge_type
        amount = comp.amount
        source = SOURCE_TARIFF
        if _is_overridable(ct):
            ov = prop_ov.get(ct.id)
            if ov is not None:
                amount, source = ov.amount, SOURCE_PROPERTY
            else:
                ov = cust_ov.get(ct.id)
                if ov is not None:
                    amount, source = ov.amount, SOURCE_CUSTOMER
        if amount is None:
            continue
        charges.append(Charge(
            key=ct.key,
            label=comp.label or ct.label,
            calc_type=ct.calc_type,
            unit_price=Decimal(str(amount)),
            tax_rate=(Decimal(str(comp.tax_rate)) if comp.tax_rate is not None else None),
            account_id=comp.account_id,
            is_levy=bool(ct.is_levy),
            source=source,
            valid_from=None if ct.key == ChargeType.KEY_WATER else comp.valid_from,
            sort_order=comp.sort_order,
        ))
    charges.sort(key=lambda c: (c.sort_order, c.key))
    return charges


def charge_sources(tariff, *, prop=None, customer=None):
    """``{key: (kommt_aus_dem_tarif, wirksamer_betrag)}`` je Tarifposition —
    auch fuer Positionen ohne Betrag bzw. mit „entfällt" (Betrag ``None``).

    Fuer die Plankostenrechnung: ein Aufschlag auf den Tarif wirkt nur dort,
    wo der Betrag tatsaechlich aus dem Tarif kommt; ``None`` heisst "keine
    Gebuehr" und ist ein gueltiger Wert (0,00 € ist eine Gebuehr von null, die
    einen Aufschlag NICHT mitnimmt).
    """
    if tariff is None:
        return {}
    prop_ov = _overrides(getattr(prop, "id", None), ChargeOverride.property_id)
    cust_ov = _overrides(getattr(customer, "id", None), ChargeOverride.customer_id)
    out = {}
    for comp in tariff.components:
        ct = comp.charge_type
        from_tariff, amount = True, comp.amount
        if _is_overridable(ct):
            ov = prop_ov.get(ct.id) or cust_ov.get(ct.id)
            if ov is not None:
                from_tariff, amount = False, ov.amount
        out[ct.key] = (from_tariff, amount)
    return out


def water_charge(charges):
    return next((c for c in charges if c.is_water), None)


def ignored_override_types(tariff, *, prop=None, customer=None):
    """Gebührenarten mit individueller Gebühr, die der Tarif gar nicht kennt —
    diese Overrides greifen nicht (Hinweis nach dem Rechnungslauf)."""
    if tariff is None:
        return []
    in_tariff = {c.charge_type_id for c in tariff.components}
    rows = []
    if prop is not None:
        rows += ChargeOverride.query.filter_by(property_id=prop.id).all()
    if customer is not None:
        rows += ChargeOverride.query.filter_by(customer_id=customer.id).all()
    return sorted({r.charge_type.label for r in rows
                   if r.charge_type_id not in in_tariff and r.charge_type.overridable})


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


def fmt_price(value, decimals=4):
    """``Decimal('1.4')`` -> ``"1,4000"`` (Rechnungstext)."""
    return f"{Decimal(str(value)):.{decimals}f}".replace(".", ",")


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


def per_m3_line(charge, consumption, *, start=None, end=None, period_name=None):
    """Rechnungszeile (Dict) einer m³-Position — ``None`` wenn sie im
    Zeitraum [start, end] gar nicht gilt.

    Mit ``valid_from`` innerhalb des Zeitraums wird die Menge linear nach
    Tagen abgegrenzt (zeitanteilige Verbrauchsabgrenzung). Beschreibung:
    ``<Label> [<Periode>] (<m³> m³ × <Preis> €/m³)``.
    """
    qty = Decimal(str(consumption or 0))
    suffix = ""
    if charge.valid_from is not None and start is not None and end is not None:
        total = span_days(start, end)
        valid = active_days(charge, start, end)
        if valid <= 0:
            return None
        if valid < total:
            qty = (qty * Decimal(valid) / Decimal(total)).quantize(QTY_STEP)
            suffix = (f", zeitanteilig ab {charge.valid_from.strftime('%d.%m.%Y')}: "
                      f"{valid}/{total} Tage")
    head = f"{charge.label} {period_name}" if period_name else charge.label
    desc = f"{head} ({fmt_qty(qty)} m³ × {fmt_price(charge.unit_price)} €/m³{suffix})"
    return {
        "description": desc,
        "quantity": qty,
        "unit": "m³",
        "unit_price": charge.unit_price,
        "amount": money(qty * charge.unit_price),
        "charge_key": charge.key,
    }


def flat_line(charge, *, start=None, end=None, period_days=None, label_suffix=""):
    """Rechnungszeile (Dict) einer Pauschal-Position, ggf. anteilig.

    Anteilig wird, wenn der abgerechnete Zeitraum [start, end] kuerzer ist als
    ``period_days`` (Eigentuemerwechsel) oder die Position erst ab
    ``valid_from`` gilt. ``None``, wenn im Zeitraum kein einziger Tag gilt.
    """
    amount = charge.unit_price
    desc = charge.label
    if start is not None and end is not None and period_days:
        days = active_days(charge, start, end)
        if days <= 0:
            return None
        if days < period_days:
            amount = money(amount * Decimal(days) / Decimal(period_days))
            desc = f"{charge.label} (anteilig {days}/{period_days} Tage)"
    desc += label_suffix
    return {
        "description": desc,
        "quantity": Decimal("1"),
        "unit": "Pauschal",
        "unit_price": amount,
        "amount": money(amount),
        "charge_key": charge.key,
    }


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
        })
    return rows


def last_day_before(day):
    return day - timedelta(days=1)
