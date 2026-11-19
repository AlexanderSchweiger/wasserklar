"""Betraege der Teilrechnungen (rein, ohne DB).

* ``round_installment`` — Jahresbetrag ÷ Anzahl, auf den Rundungsschritt.
* ``split_gross`` — einen Bruttobetrag cent-genau auf Gruppen (Gebuehrenart /
  USt-Satz) verteilen und je Gruppe den Nettobetrag finden, dessen Brutto
  laut ``app/invoices/amounts.line_gross`` exakt passt. Damit stimmt die
  Teilrechnung auf den Cent mit dem geplanten Teilbetrag ueberein, und die
  spaetere Gegenverrechnung (gespiegelte Zeilen) zieht genau diesen Betrag ab.
"""
from decimal import ROUND_HALF_UP, Decimal

from app.invoices.amounts import CENT, line_gross

ZERO = Decimal("0")


def round_installment(annual_gross, count, step):
    """Teilbetrag (brutto) aus Jahresbetrag und Anzahl.

    ``step`` = Rundungsschritt in Euro (0 = auf Cent). Kaufmaennisch gerundet;
    ergaebe der Schritt 0 € bei positivem Jahresbetrag, wird auf Cent gerundet.
    """
    annual_gross = Decimal(str(annual_gross or 0))
    if count <= 0 or annual_gross <= 0:
        return ZERO
    per = annual_gross / Decimal(count)
    step = Decimal(str(step or 0))
    if step > 0:
        rounded = (per / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
        if rounded > 0:
            return rounded.quantize(CENT)
    return per.quantize(CENT, rounding=ROUND_HALF_UP)


def _net_for_gross(gross, rate):
    """Nettobetrag, dessen ``line_gross`` dem Brutto am naechsten liegt
    (bevorzugt exakt). Gibt ``(netto, brutto_tatsaechlich)``."""
    gross = Decimal(str(gross))
    rate = Decimal(str(rate or 0))
    if rate <= 0:
        return gross, gross
    guess = (gross / (Decimal("1") + rate / Decimal("100"))).quantize(CENT)
    best = None
    for delta in range(-3, 4):
        net = guess + CENT * delta
        actual = line_gross(net, rate)
        diff = abs(actual - gross)
        if best is None or diff < best[2]:
            best = (net, actual, diff)
        if diff == 0:
            return net, actual
    return best[0], best[1]


def split_gross(gross, groups):
    """Verteilt ``gross`` (Decimal, darf negativ sein) auf ``groups``.

    ``groups``: Liste von Dicts mit mindestens ``share`` (Anteil, Summe ≈ 1)
    und ``tax_rate`` (None/0 = ohne USt). Gibt fuer jede Gruppe ein Dict
    ``{**gruppe, "net", "gross"}`` zurueck; die Summe der ``gross`` ist exakt
    ``gross``, sofern sich ein Cent-Rest nicht ueber einen USt-Satz abbilden
    laesst (dann hoechstens 1 Cent Abweichung in der letzten Gruppe).
    Gruppen mit Anteil 0 entfallen.
    """
    gross = Decimal(str(gross))
    groups = [g for g in groups if Decimal(str(g.get("share") or 0)) != 0]
    if not groups:
        return []
    total_share = sum((Decimal(str(g["share"])) for g in groups), ZERO)
    result = []
    remaining = gross
    for i, g in enumerate(groups):
        if i == len(groups) - 1:
            target = remaining
        else:
            target = (gross * Decimal(str(g["share"])) / total_share).quantize(CENT)
        net, actual = _net_for_gross(target, g.get("tax_rate"))
        remaining -= actual
        result.append({**g, "net": net, "gross": actual})
    if sum((r["gross"] for r in result), ZERO) != gross:
        _fix_cents(result, gross)
    return result


def _fix_cents(parts, gross):
    """Nicht jeder Bruttobetrag ist mit einem Satz erreichbar (10 %: netto +1 ct
    hebt brutto manchmal um 2 ct). Dann die Netto-Betraege einzelner Gruppen um
    wenige Cent verschieben, bis die Summe stimmt (zuerst eine, dann zwei)."""
    import itertools
    deltas = [CENT * d for d in (1, -1, 2, -2, 3, -3)]

    def total():
        return sum((line_gross(p["net"], p.get("tax_rate")) for p in parts), ZERO)

    for p in parts:
        base = p["net"]
        for d in deltas:
            p["net"] = base + d
            if total() == gross:
                break
        else:
            p["net"] = base
            continue
        break
    else:
        for a, b in itertools.permutations(range(len(parts)), 2):
            base_a, base_b = parts[a]["net"], parts[b]["net"]
            hit = False
            for da in deltas:
                for db_ in deltas:
                    parts[a]["net"], parts[b]["net"] = base_a + da, base_b + db_
                    if total() == gross:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
            parts[a]["net"], parts[b]["net"] = base_a, base_b
    for p in parts:
        p["gross"] = line_gross(p["net"], p.get("tax_rate"))
