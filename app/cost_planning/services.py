"""Rechenkern der Plankostenrechnung — reine Funktionen auf ``Decimal``.

Aufgabe: aus einem Finanzierungsziel (Ruecklage ansparen oder Kredit tilgen)
den jaehrlichen Mehrbedarf ableiten und daraus drei Tarifpakete bauen, die
sich im **Verteilungsschluessel** unterscheiden (Grundgebuehr vs. m³-Preis).

Bewusst nur der **Zusatzbedarf**: der aktuelle Tarif bleibt die Basis, gerechnet
wird ausschliesslich der Aufschlag. Eine Vollkostenrechnung aus der Buchhaltung
waere maechtiger, haengt aber an sauber gepflegten Ist-Buchungen — hier nicht
gewollt.

Alles rechnet mit ``Decimal``; ``float`` taucht nur an der Template-Grenze auf
(Chart-Daten). Keine DB-Schreibzugriffe.
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from app import consumption
from app.extensions import db
from app.models import (
    Customer,
    FundingGoal,
    Property,
    PropertyOwnership,
    WaterMeter,
)

CENT = Decimal("0.01")
M3_PRICE_STEP = Decimal("0.0001")   # WaterTariff.price_per_m3 hat 4 Nachkommastellen
ZERO = Decimal("0")

# Verteilungsschluessel: Anteil des Mehrbedarfs, der ueber die Grundgebuehr
# hereinkommt. Der Rest laeuft ueber den m³-Preis.
SCENARIOS = [
    ("base", "Grundgebühr-lastig", Decimal("1.0"),
     "Planbarer Ertrag — unabhängig davon, wie viel Wasser verbraucht wird. "
     "Belastet Haushalte mit geringem Verbrauch relativ stärker."),
    ("balanced", "Ausgewogen", Decimal("0.5"),
     "Hälfte fix, Hälfte über den Verbrauch. Kompromiss aus Planbarkeit und "
     "Verursachergerechtigkeit."),
    ("volume", "Verbrauchslastig", Decimal("0.0"),
     "Setzt einen Sparanreiz und belastet Großverbraucher stärker. Der Ertrag "
     "schwankt aber mit dem Verbrauch — in einem nassen Jahr kommt weniger "
     "herein als geplant."),
]

SCENARIO_LABELS = {key: label for key, label, _, _ in SCENARIOS}

# Rundungsstufen fuer die vorgeschlagenen Betraege.
ROUNDING_CHOICES = [
    ("exact", "Exakt (auf den Cent)"),
    ("ten_cent", "Auf 10 Cent"),
    ("euro", "Auf volle Euro"),
]

DEFAULT_SAMPLE_HOUSEHOLD_M3 = Decimal("120")


# ---------------------------------------------------------------------------
# 1. Jahresbedarf
# ---------------------------------------------------------------------------

def annual_requirement(goal):
    """Jaehrlich zusaetzlich aufzubringender Betrag fuer ``goal``.

    ``investment`` — nachschuessige Sparrate. Eine bereits vorhandene Ruecklage
    wird bis zum Zieljahr mitverzinst und vom Ziel abgezogen::

        FV  = target_amount - existing_reserve * (1+i)^n
        PMT = FV / n                     (i = 0)
        PMT = FV * i / ((1+i)^n - 1)     (i > 0)

    ``loan`` — nachschuessige Annuitaet (Zins + Tilgung), also der volle
    jaehrliche Kapitaldienst::

        A = P / n                        (i = 0)
        A = P * i / (1 - (1+i)^-n)       (i > 0)

    Beide Formeln gehen bei ``i = 0`` in die lineare Variante ueber; der
    Nullzins-Fall wird trotzdem explizit behandelt, weil die geschlossene Form
    dort durch 0 teilen wuerde.

    Rueckgabe: Dict mit ``amount`` (Decimal, auf Cent gerundet), ``years``,
    ``rate``, ``total_payments``, ``interest_total`` und ``already_covered``.
    """
    years = goal.target_year - goal.start_year + 1
    if years <= 0:
        raise ValueError("Das Zieljahr muss im Startjahr oder danach liegen.")

    rate = _rate(goal.interest_rate)
    principal = Decimal(goal.target_amount or 0)

    if goal.is_loan:
        if principal <= 0:
            return _requirement_result(ZERO, years, rate, ZERO, True)
        if rate == 0:
            payment = principal / Decimal(years)
        else:
            payment = principal * rate / (1 - (1 + rate) ** -years)
        return _requirement_result(payment, years, rate, principal, False)

    # investment
    reserve = Decimal(goal.existing_reserve or 0)
    needed = principal - reserve * (1 + rate) ** years
    if needed <= 0:
        # Die vorhandene Ruecklage (samt Verzinsung) deckt das Ziel bereits.
        return _requirement_result(ZERO, years, rate, ZERO, True,
                                   is_loan=False)
    if rate == 0:
        payment = needed / Decimal(years)
    else:
        payment = needed * rate / ((1 + rate) ** years - 1)
    return _requirement_result(payment, years, rate, needed, False,
                               is_loan=False)


def _requirement_result(payment, years, rate, base, already_covered,
                        is_loan=True):
    """Ergebnis-Dict — Summen werden aus der **gerundeten** Jahresrate
    abgeleitet.

    Bewusst nicht aus dem exakten Zwischenwert: auf der Beschlussvorlage steht
    die gerundete Jahresrate, und ein Kassier rechnet sie mit dem Taschenrechner
    mal der Laufzeit nach. Wenn dabei nicht die ausgewiesene Summe herauskommt,
    ist die Vorlage unbrauchbar — Konsistenz schlaegt hier die letzte
    Nachkommastelle.
    """
    amount = _q(payment)
    total = amount * years
    return {
        "amount": amount,
        "years": years,
        "rate": rate,
        "total_payments": _q(total),
        # Kredit: gezahlte Summe minus Schuld = Zinsaufwand.
        # Ruecklage: was die Verzinsung an Sparleistung erspart.
        "interest_total": _q(total - base if is_loan else base - total),
        "already_covered": already_covered,
    }


def _rate(interest_rate):
    """Prozentsatz p.a. -> Dezimalzins. ``None``/leer -> 0."""
    if interest_rate is None:
        return ZERO
    return Decimal(interest_rate) / Decimal("100")


def _q(value, exp=CENT):
    return Decimal(value).quantize(exp, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 2. Basis (aktueller Verbrauch + gebuehrenpflichtige Einheiten)
# ---------------------------------------------------------------------------

def baseline(tariff, avg_years=3):
    """Rechenbasis fuer die Tarifpakete.

    Der entscheidende Punkt sind die **Einheiten-Zahlen**: ein Aufschlag auf die
    Tarif-Grundgebuehr wirkt nur bei Objekten, deren Gebuehr tatsaechlich aus
    dem Tarif kommt. Objekte oder Kunden mit einem ``*_fee_override`` sind vom
    Aufschlag nicht betroffen (Prioritaet Objekt > Kunde > Tarif, identisch zu
    ``invoices.generate``). Wer das ignoriert, ueberschaetzt den Mehrertrag.

    ``abrechenbar`` = aktives Objekt mit mindestens einem aktiven Zaehler und
    einem aktuellen Eigentuemer. Mehrere parallele aktive ``PropertyOwnership``
    (Ehepaare, Erbengemeinschaften) zaehlen als EIN Objekt — abgerechnet wird
    pro Objekt.
    """
    avg_m3, basis_years = consumption.average_basis(avg_years)

    props = (
        Property.query
        .filter(Property.active.is_(True))
        .all()
    )
    meter_prop_ids = {
        pid for (pid,) in db.session.query(WaterMeter.property_id)
        .filter(WaterMeter.active.is_(True))
        .distinct()
        .all()
    }
    owned_prop_ids = {
        pid for (pid,) in db.session.query(PropertyOwnership.property_id)
        .filter(PropertyOwnership.valid_to.is_(None))
        .distinct()
        .all()
    }

    billable = [
        p for p in props
        if p.id in meter_prop_ids and p.id in owned_prop_ids
    ]

    base_units = 0
    additional_units = 0
    override_units = 0
    current_base_revenue = ZERO
    current_additional_revenue = ZERO

    for prop in billable:
        ownership = prop.current_owner()
        customer = (
            db.session.get(Customer, ownership.customer_id)
            if ownership is not None else None
        )
        base_from_tariff, base_value = _effective_fee(
            prop.base_fee_override,
            customer.base_fee_override if customer is not None else None,
            tariff.base_fee if tariff is not None else None,
        )
        add_from_tariff, add_value = _effective_fee(
            prop.additional_fee_override,
            customer.additional_fee_override if customer is not None else None,
            tariff.additional_fee if tariff is not None else None,
        )
        if base_from_tariff and base_value is not None:
            base_units += 1
        if add_from_tariff and add_value is not None:
            additional_units += 1
        if not base_from_tariff or not add_from_tariff:
            override_units += 1
        if base_value is not None:
            current_base_revenue += Decimal(base_value)
        if add_value is not None:
            current_additional_revenue += Decimal(add_value)

    price = Decimal(tariff.price_per_m3) if tariff is not None else ZERO
    volume_revenue = avg_m3 * price

    return {
        "tariff": tariff,
        "avg_m3": avg_m3,
        "avg_years": avg_years,
        "basis_years": basis_years,
        "billable_units": len(billable),
        "base_fee_units": base_units,
        "additional_fee_units": additional_units,
        "override_units": override_units,
        "current_base_revenue": _q(current_base_revenue),
        "current_additional_revenue": _q(current_additional_revenue),
        "current_volume_revenue": _q(volume_revenue),
        "current_revenue": _q(
            current_base_revenue + current_additional_revenue + volume_revenue
        ),
    }


def _effective_fee(prop_override, customer_override, tariff_value):
    """``(kommt_aus_dem_tarif, wirksamer_wert)``.

    Prioritaet Objekt > Kunde > Tarif; ``None`` heisst "keine Gebuehr" und ist
    ein gueltiger Wert, kein "nicht gesetzt" — deshalb wird auf ``is not None``
    geprueft und nicht auf Truthiness (0,00 € ist eine Gebuehr von null, die
    einen Aufschlag NICHT mitnimmt).
    """
    if prop_override is not None:
        return False, prop_override
    if customer_override is not None:
        return False, customer_override
    return True, tariff_value


# ---------------------------------------------------------------------------
# 3. Tarifpakete
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    key: str
    label: str
    share_base: Decimal
    description: str
    base_fee: Decimal = None           # neue Grundgebuehr (None = keine Position)
    additional_fee: Decimal = None     # unveraendert uebernommen
    price_per_m3: Decimal = ZERO
    delta_base_fee: Decimal = ZERO
    delta_price_per_m3: Decimal = ZERO
    realized: Decimal = ZERO           # tatsaechlicher Mehrertrag nach Rundung
    required: Decimal = ZERO
    coverage: Decimal = ZERO           # % des Bedarfs
    sample_delta_year: Decimal = ZERO
    sample_delta_month: Decimal = ZERO
    feasible: bool = True
    warnings: list = field(default_factory=list)

    @property
    def gap(self):
        """Ueber-/Unterdeckung in € pro Jahr (positiv = Ueberdeckung)."""
        return self.realized - self.required

    @property
    def coverage_badge_class(self):
        if not self.feasible:
            return "bg-secondary-lt"
        if self.coverage >= Decimal("99.5"):
            return "bg-green text-white"
        if self.coverage >= Decimal("95"):
            return "bg-yellow text-dark"
        return "bg-orange text-white"


def build_scenarios(required, base_info, rounding="exact",
                    sample_household_m3=None):
    """Drei Tarifpakete fuer den Jahresbedarf ``required``.

    Je Paket wird der Aufschlag nach dem Verteilungsschluessel gesplittet,
    **gerundet** und der dadurch tatsaechlich erzielbare Mehrertrag
    zurueckgerechnet. Genau diese Rueckrechnung macht den Vorschlag belastbar:
    "38,00 € Grundgebuehr" deckt eben nicht exakt 100 % des Bedarfs, und der
    Deckungsgrad zeigt das offen an, statt Scheingenauigkeit zu behaupten.
    """
    required = Decimal(required or 0)
    if sample_household_m3 is None:
        sample_household_m3 = DEFAULT_SAMPLE_HOUSEHOLD_M3

    tariff = base_info["tariff"]
    avg_m3 = Decimal(base_info["avg_m3"] or 0)
    base_units = base_info["base_fee_units"]
    current_base = (
        Decimal(tariff.base_fee) if tariff is not None and tariff.base_fee is not None
        else None
    )
    current_price = Decimal(tariff.price_per_m3) if tariff is not None else ZERO
    current_additional = (
        Decimal(tariff.additional_fee)
        if tariff is not None and tariff.additional_fee is not None else None
    )

    out = []
    for key, label, share, description in SCENARIOS:
        sc = Scenario(key=key, label=label, share_base=share,
                      description=description)
        sc.required = _q(required)
        sc.additional_fee = current_additional

        need_base = required * share
        need_volume = required - need_base

        # --- Grundgebuehr-Anteil ---
        if need_base > 0 and base_units == 0:
            sc.feasible = False
            sc.warnings.append(
                "Kein Objekt bezieht seine Grundgebühr aus dem Tarif "
                "(keine Grundgebühr hinterlegt oder überall individuelle "
                "Beträge) — über die Grundgebühr ist kein Aufschlag möglich."
            )
            delta_base = ZERO
        elif base_units == 0:
            delta_base = ZERO
        else:
            delta_base = _round_money(
                need_base / Decimal(base_units), rounding)

        # --- Verbrauchs-Anteil ---
        if need_volume > 0 and avg_m3 <= 0:
            sc.feasible = False
            sc.warnings.append(
                "Für den Durchschnittsverbrauch liegen keine Daten vor — "
                "über den m³-Preis lässt sich nichts berechnen. Bitte "
                "Verbrauchsjahre erfassen."
            )
            delta_price = ZERO
        elif avg_m3 <= 0:
            delta_price = ZERO
        else:
            delta_price = _round_price(need_volume / avg_m3, rounding)

        sc.delta_base_fee = delta_base
        sc.delta_price_per_m3 = delta_price
        sc.base_fee = (
            _q((current_base or ZERO) + delta_base)
            if (current_base is not None or delta_base > 0) else None
        )
        sc.price_per_m3 = (current_price + delta_price).quantize(
            M3_PRICE_STEP, rounding=ROUND_HALF_UP)

        sc.realized = _q(delta_base * Decimal(base_units) + delta_price * avg_m3)
        sc.coverage = (
            (sc.realized / required * Decimal("100")).quantize(CENT,
                                                               rounding=ROUND_HALF_UP)
            if required > 0 else Decimal("100.00")
        )

        sample = Decimal(sample_household_m3)
        sc.sample_delta_year = _q(delta_base + delta_price * sample)
        sc.sample_delta_month = _q(sc.sample_delta_year / Decimal("12"))

        out.append(sc)
    return out


def _round_money(value, mode):
    if mode == "euro":
        return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if mode == "ten_cent":
        return Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return _q(value)


def _round_price(value, mode):
    """m³-Preise werden feiner gerundet als Gebuehren — 1 Cent Aufschlag pro m³
    macht bei 40.000 m³ schon 400 € aus, "auf volle Euro" waere hier unbrauchbar.
    """
    if mode == "euro":
        return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
    if mode == "ten_cent":
        return Decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return Decimal(value).quantize(M3_PRICE_STEP, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 4. Projektion ueber die Laufzeit
# ---------------------------------------------------------------------------

def projection(goal, scenario):
    """Jahresweiser Verlauf fuer ``scenario``.

    ``investment`` — Ruecklagenstand am Jahresende (Vorjahresstand verzinst
    plus Mehrertrag), gegen den Zielbetrag.
    ``loan`` — Zins- und Tilgungsanteil des Kapitaldienstes, Restschuld.
    """
    rate = _rate(goal.interest_rate)
    contribution = Decimal(scenario.realized or 0)
    rows = []

    if goal.is_loan:
        balance = Decimal(goal.target_amount or 0)
        for year in range(goal.start_year, goal.target_year + 1):
            interest = _q(balance * rate)
            principal = _q(contribution - interest)
            # Letzte Rate kappen, damit die Restschuld nicht negativ wird.
            if principal > balance:
                principal = balance
            balance = _q(balance - principal)
            rows.append({
                "year": year,
                "contribution": _q(contribution),
                "interest": interest,
                "principal": principal,
                "balance": balance,
                "target": Decimal(goal.target_amount or 0),
            })
        return rows

    balance = Decimal(goal.existing_reserve or 0)
    target = Decimal(goal.target_amount or 0)
    for year in range(goal.start_year, goal.target_year + 1):
        interest = _q(balance * rate)
        balance = _q(balance + interest + contribution)
        rows.append({
            "year": year,
            "contribution": _q(contribution),
            "interest": interest,
            "principal": _q(contribution),
            "balance": balance,
            "target": target,
        })
    return rows


# ---------------------------------------------------------------------------
# 5. Mehrere Ziele gleichzeitig
# ---------------------------------------------------------------------------

def active_goals():
    """Ziele, die in die Gesamtbetrachtung eingehen (Entwurf + beschlossen)."""
    return (
        FundingGoal.query
        .filter(FundingGoal.status.in_(
            [FundingGoal.STATUS_DRAFT, FundingGoal.STATUS_ACTIVE]))
        .order_by(FundingGoal.target_year.asc(), FundingGoal.name.asc())
        .all()
    )


def combined_requirement(goals=None):
    """Gesamtbedarf je Jahr ueber alle Ziele.

    Laufen ein Kredit und ein Sparziel parallel, addieren sich die
    Jahresbedarfe — ein Tarifvorschlag, der nur ein Ziel kennt, waere zu
    niedrig. Liefert ``(zeilen_je_jahr, spitzenbedarf)``; der Spitzenbedarf ist
    die Groesse, auf die der Tarif ausgelegt werden muss.
    """
    if goals is None:
        goals = active_goals()

    per_goal = []
    for goal in goals:
        try:
            req = annual_requirement(goal)
        except ValueError:
            continue
        per_goal.append((goal, req["amount"]))

    if not per_goal:
        return [], ZERO

    first = min(g.start_year for g, _ in per_goal)
    last = max(g.target_year for g, _ in per_goal)
    rows = []
    peak = ZERO
    for year in range(first, last + 1):
        parts = [
            {"goal": g, "amount": amount}
            for g, amount in per_goal
            if g.start_year <= year <= g.target_year
        ]
        total = sum((p["amount"] for p in parts), ZERO)
        peak = max(peak, total)
        rows.append({"year": year, "total": _q(total), "parts": parts})
    return rows, _q(peak)
