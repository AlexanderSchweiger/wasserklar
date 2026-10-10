"""Nachkommastellen von Preisen (app/invoices/price_format.py)."""
from decimal import Decimal as D

from app.invoices import price_format as pf


class TestPlaces:
    def test_needed_places(self):
        assert pf.needed_places(D("0.1000")) == 1
        assert pf.needed_places(D("0.0815")) == 4
        assert pf.needed_places(D("1.85")) == 2
        assert pf.needed_places(D("100")) == 0
        assert pf.needed_places(D("0.12345")) == 4      # mehr kann die DB nicht
        assert pf.needed_places(None) == 0
        assert pf.needed_places("kaputt") == 0

    def test_never_fewer_places_than_the_price_has(self):
        assert pf.price_places(D("0.1000"), 2) == 2
        assert pf.price_places(D("0.081"), 2) == 3
        assert pf.price_places(D("0.0815"), 2) == 4
        assert pf.price_places(D("1.4"), 4) == 4

    def test_clamp(self):
        assert pf.clamp_places(1) == 2
        assert pf.clamp_places(9) == 4
        assert pf.clamp_places("3") == 3
        assert pf.clamp_places(None) == pf.DEFAULT_PLACES

    def test_typed_places(self):
        """Wie eingetippt — Decimal behaelt die Nullen am Ende."""
        assert pf.typed_places(D("1.40")) == 2
        assert pf.typed_places(D("0.1000")) == 4
        assert pf.typed_places(D("0.125")) == 3
        assert pf.typed_places(D("12")) == 2
        assert pf.typed_places(D("0.12345")) == 4
        assert pf.typed_places(None) == 2


class TestFormat:
    def test_format_price(self):
        assert pf.format_price(D("0.1"), 2) == "0,10"
        assert pf.format_price(D("0.1"), 3) == "0,100"
        assert pf.format_price(D("0.1"), 4) == "0,1000"
        assert pf.format_price(D("0.081"), 2) == "0,081"
        assert pf.format_price(D("1.85"), 2, sep=".") == "1.85"
        assert pf.format_price(None, 2) == ""
