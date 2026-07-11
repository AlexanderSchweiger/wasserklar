"""Unit-Tests fuer die Rundschreiben-Vorlagen + Platzhalter-Ersetzung (ohne DB)."""
from app.models import Circular, Customer
from app.circulars import constants
from app.circulars.services import render_circular_text


class TestRenderText:
    def test_replaces_anrede_and_name(self):
        c = Customer(name="Muster Maria", first_name="Maria", last_name="Muster",
                     salutation="Frau")
        out = render_circular_text("{anrede},\nBetrifft: {name}", c)
        assert out == "Sehr geehrte Frau Muster,\nBetrifft: Maria Muster"

    def test_no_customer_uses_generic(self):
        out = render_circular_text("{anrede}!", None)
        assert out == "Sehr geehrte Damen und Herren!"

    def test_empty_text(self):
        assert render_circular_text("", Customer(name="X")) == ""

    def test_square_brackets_untouched(self):
        c = Customer(name="X")
        out = render_circular_text("[Datum] bleibt", c)
        assert "[Datum]" in out


class TestBuiltinTemplates:
    def test_five_templates(self):
        assert len(constants.BUILTIN_TEMPLATES) == 5

    def test_all_kinds_valid(self):
        for t in constants.BUILTIN_TEMPLATES:
            assert t["kind"] in Circular.KINDS
            assert "subject" in t and "body" in t and "label" in t

    def test_all_kinds_covered(self):
        kinds = {t["kind"] for t in constants.BUILTIN_TEMPLATES}
        assert kinds == set(Circular.KINDS)

    def test_boil_water_content(self):
        t = constants.TEMPLATES_BY_KEY["boil_water"]
        assert t["kind"] == Circular.KIND_BOIL_WATER
        body = t["body"]
        assert "abkoch" in body.lower()
        assert "3 Minuten" in body
        assert "{anrede}" in body
        assert "[Anlass der Abkochempfehlung]" in body
        assert "Art. 6 Abs. 1 lit. d" in body

    def test_emergency_flag(self):
        assert Circular.KIND_BOIL_WATER in Circular.EMERGENCY_KINDS
        assert Circular.KIND_OUTAGE in Circular.EMERGENCY_KINDS
        assert Circular.KIND_PLANNED_OUTAGE not in Circular.EMERGENCY_KINDS
        assert Circular.KIND_GENERAL not in Circular.EMERGENCY_KINDS

    def test_template_for_kind(self):
        assert constants.template_for_kind(Circular.KIND_OUTAGE)["kind"] == Circular.KIND_OUTAGE
        # Unbekannte Art -> general Fallback.
        assert constants.template_for_kind("nope")["key"] == "general"
