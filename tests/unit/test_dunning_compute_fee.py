"""Unit-Tests für compute_fee() — reine Funktion ohne DB-Abfragen."""
import types
from decimal import Decimal

from app.dunning.services import compute_fee


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _stage(**kwargs):
    """Mock-DunningStage ohne DB-Zugriff."""
    defaults = dict(
        fee_fixed=0,
        fee_percent=0,
        fee_min=None,
        fee_max=None,
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Nur fixe Gebühr
# ---------------------------------------------------------------------------

class TestComputeFeeFixed:
    def test_zero_fixed(self):
        assert compute_fee(_stage(fee_fixed=0), Decimal("1000")) == Decimal("0.00")

    def test_simple_fixed(self):
        assert compute_fee(_stage(fee_fixed=5), Decimal("1000")) == Decimal("5.00")

    def test_fixed_decimal_precision(self):
        assert compute_fee(_stage(fee_fixed="7.50"), Decimal("100")) == Decimal("7.50")

    def test_fixed_ignores_principal(self):
        """Fixe Gebühr hängt nicht von der Hauptforderung ab."""
        s = _stage(fee_fixed=10)
        assert compute_fee(s, Decimal("1")) == compute_fee(s, Decimal("999999"))


# ---------------------------------------------------------------------------
# Nur prozentuale Gebühr
# ---------------------------------------------------------------------------

class TestComputeFeePercent:
    def test_zero_percent(self):
        assert compute_fee(_stage(fee_percent=0), Decimal("1000")) == Decimal("0.00")

    def test_two_percent(self):
        # 2 % von 1000 = 20
        assert compute_fee(_stage(fee_percent=2), Decimal("1000")) == Decimal("20.00")

    def test_percent_rounding(self):
        # 3 % von 33.33 = 0.9999 → gerundet auf 1.00
        assert compute_fee(_stage(fee_percent=3), Decimal("33.33")) == Decimal("1.00")

    def test_percent_small_amount(self):
        # 1 % von 1.00 = 0.01
        assert compute_fee(_stage(fee_percent=1), Decimal("1.00")) == Decimal("0.01")


# ---------------------------------------------------------------------------
# Fix + Prozent kombiniert
# ---------------------------------------------------------------------------

class TestComputeFeeFixedPlusPercent:
    def test_fixed_plus_percent(self):
        # 5 fix + 2 % von 1000 = 5 + 20 = 25
        s = _stage(fee_fixed=5, fee_percent=2)
        assert compute_fee(s, Decimal("1000")) == Decimal("25.00")

    def test_fixed_plus_percent_small(self):
        # 2.50 fix + 1 % von 50 = 2.50 + 0.50 = 3.00
        s = _stage(fee_fixed="2.50", fee_percent=1)
        assert compute_fee(s, Decimal("50")) == Decimal("3.00")


# ---------------------------------------------------------------------------
# Min/Max-Caps auf den prozentualen Anteil
# ---------------------------------------------------------------------------

class TestComputeFeeMinMax:
    def test_fee_min_raises_low_percent(self):
        # 1 % von 100 = 1.00, aber min=5 → Prozent-Anteil = 5, total = 5
        s = _stage(fee_percent=1, fee_min=5)
        assert compute_fee(s, Decimal("100")) == Decimal("5.00")

    def test_fee_min_no_effect_when_above(self):
        # 10 % von 100 = 10, min=5 → 10 >= 5, kein Effekt
        s = _stage(fee_percent=10, fee_min=5)
        assert compute_fee(s, Decimal("100")) == Decimal("10.00")

    def test_fee_max_caps_high_percent(self):
        # 10 % von 1000 = 100, aber max=50 → Prozent-Anteil = 50, total = 50
        s = _stage(fee_percent=10, fee_max=50)
        assert compute_fee(s, Decimal("1000")) == Decimal("50.00")

    def test_fee_max_no_effect_when_below(self):
        # 1 % von 100 = 1, max=50 → 1 < 50, kein Effekt
        s = _stage(fee_percent=1, fee_max=50)
        assert compute_fee(s, Decimal("100")) == Decimal("1.00")

    def test_min_and_max_together_min_wins(self):
        # 1 % von 100 = 1.00, min=3, max=50 → 3
        s = _stage(fee_percent=1, fee_min=3, fee_max=50)
        assert compute_fee(s, Decimal("100")) == Decimal("3.00")

    def test_min_and_max_together_max_wins(self):
        # 20 % von 1000 = 200, min=3, max=50 → 50
        s = _stage(fee_percent=20, fee_min=3, fee_max=50)
        assert compute_fee(s, Decimal("1000")) == Decimal("50.00")

    def test_fixed_plus_percent_with_max(self):
        # 5 fix + (10 % von 1000 = 100, max=20 → 20) = 25
        s = _stage(fee_fixed=5, fee_percent=10, fee_max=20)
        assert compute_fee(s, Decimal("1000")) == Decimal("25.00")

    def test_fixed_plus_percent_with_min(self):
        # 3 fix + (1 % von 50 = 0.50, min=2 → 2) = 5
        s = _stage(fee_fixed=3, fee_percent=1, fee_min=2)
        assert compute_fee(s, Decimal("50")) == Decimal("5.00")


# ---------------------------------------------------------------------------
# Grenzfälle
# ---------------------------------------------------------------------------

class TestComputeFeeEdgeCases:
    def test_zero_principal(self):
        s = _stage(fee_fixed=5, fee_percent=10)
        assert compute_fee(s, Decimal("0")) == Decimal("5.00")

    def test_none_principal(self):
        s = _stage(fee_fixed=5, fee_percent=2)
        assert compute_fee(s, None) == Decimal("5.00")

    def test_none_fee_fields(self):
        """Alle Stage-Felder None → 0.00."""
        s = _stage(fee_fixed=None, fee_percent=None)
        assert compute_fee(s, Decimal("1000")) == Decimal("0.00")

    def test_result_always_quantized_two_decimals(self):
        s = _stage(fee_fixed="1.111")
        result = compute_fee(s, Decimal("100"))
        assert result == result.quantize(Decimal("0.01"))
