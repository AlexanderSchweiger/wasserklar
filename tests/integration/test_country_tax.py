"""Länderprofil + einstellbare Steuersaetze (app/country.py, app/tax_service.py,
app/settings/country_defaults.py)."""
from datetime import date
from decimal import Decimal

import pytest

from app import country, tax_service
from app.accounting.services import default_water_tax_rate
from app.extensions import db
from app.models import AppSetting, FiscalYear, TaxRate
from app.settings.country_defaults import apply_country_defaults
from app.settings_service import meter_replacement_interval


def _set_country(code):
    AppSetting.set(country.SETTING_KEY, code)
    db.session.commit()


def _rates(**kw):
    return [o.rate for o in tax_service.tax_rates(**kw)]


class TestCountryProfile:
    def test_default_is_austria(self, app):
        assert country.current_code() == "AT"
        assert country.current_profile().water_tax_rate == Decimal("10")

    def test_setting_selects_germany(self, app):
        _set_country("DE")
        prof = country.current_profile()
        assert prof.code == "DE"
        assert prof.water_tax_rate == Decimal("7")
        assert prof.calibration_years == 6

    def test_unknown_code_falls_back(self, app):
        _set_country("XX")
        assert country.current_code() == "AT"
        assert country.normalize_code(" de ") == "DE"
        assert country.normalize_code("fr") is None


class TestTaxService:
    def test_empty_table_uses_country_defaults(self, app):
        assert _rates() == [Decimal("0"), Decimal("10"), Decimal("13"), Decimal("20")]
        _set_country("DE")
        assert _rates() == [Decimal("0"), Decimal("7"), Decimal("19")]

    def test_db_rates_only_active(self, app):
        db.session.add_all([
            TaxRate(rate=Decimal("0"), label="0 %", active=True),
            TaxRate(rate=Decimal("7"), label="7 %", active=True),
            TaxRate(rate=Decimal("10"), label="10 %", active=False),
        ])
        db.session.commit()
        assert _rates() == [Decimal("0"), Decimal("7")]
        assert tax_service.known_rate_values() == [Decimal("0"), Decimal("7"), Decimal("10")]
        # include= haengt den Satz eines Altbelegs wieder an
        assert _rates(include=Decimal("10.00")) == [Decimal("0"), Decimal("7"), Decimal("10")]
        assert _rates(include=[None, "20"]) == [Decimal("0"), Decimal("7"), Decimal("20")]

    def test_display_label(self, app):
        assert tax_service.default_label(Decimal("7.00")) == "7 %"
        assert tax_service.default_label(Decimal("5.5")) == "5,5 %"
        assert tax_service.display_label(Decimal("0"), "0 % – keine USt") == "0 % – keine USt"
        assert tax_service.display_label(Decimal("7"), "ermäßigt") == "7 % – ermäßigt"
        assert tax_service.display_label(Decimal("19"), None) == "19 %"

    def test_water_rate_setting_overrides_country(self, app):
        assert tax_service.water_tax_rate() == Decimal("10")
        _set_country("DE")
        assert tax_service.water_tax_rate() == Decimal("7")
        AppSetting.set(tax_service.WATER_RATE_KEY, "19")
        db.session.commit()
        assert tax_service.water_tax_rate() == Decimal("19")

    def test_default_water_tax_rate_respects_vat_liability(self, app):
        _set_country("DE")
        year = date.today().year
        assert default_water_tax_rate(year) is None
        db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1),
                                  end_date=date(year, 12, 31), is_vat_liable=True))
        db.session.commit()
        assert default_water_tax_rate(year) == Decimal("7")


class TestSeeding:
    def test_seed_by_country(self, app):
        from cli import seed_default_tax_rates
        seed_default_tax_rates(db, country_code="DE")
        assert [Decimal(str(r.rate)) for r in TaxRate.query.order_by(TaxRate.rate)] == [
            Decimal("0"), Decimal("7"), Decimal("19")]

    def test_seed_keeps_deactivated_rate(self, app):
        from cli import seed_default_tax_rates
        seed_default_tax_rates(db)
        row = TaxRate.query.filter_by(rate=Decimal("13")).first()
        row.active = False
        db.session.commit()
        seed_default_tax_rates(db)
        assert TaxRate.query.filter_by(rate=Decimal("13")).one().active is False

    def test_treasurer_role_is_named_by_country(self, app):
        from cli import seed_default_roles
        from app.models import Role, RolePermission
        seed_default_roles(db)
        assert {r.name for r in Role.query} == {"Admin", "Kassier", "Zählerverwalter"}
        RolePermission.query.delete()   # Bulk-Delete kaskadiert nicht (SQLite ohne FK-Pragma)
        Role.query.delete()
        db.session.commit()
        seed_default_roles(db, country_code="DE")
        assert {r.name for r in Role.query} == {"Admin", "Kassierer", "Zählerverwalter"}

    def test_treasurer_role_follows_the_tenant_country(self, app):
        from cli import seed_default_roles
        from app.models import Role
        _set_country("DE")
        seed_default_roles(db)
        assert Role.query.filter_by(name="Kassierer").count() == 1

    def test_unused_treasurer_role_is_renamed_for_the_country(self, app):
        """Die RBAC-Migration legt „Kassier" an, bevor das Land feststeht —
        solange niemand die Rolle nutzt, bekommt sie die Landes-Schreibweise."""
        from cli import seed_default_roles
        from app.models import Role
        seed_default_roles(db)                       # wie die Migration: „Kassier"
        seed_default_roles(db, country_code="DE")
        names = {r.name for r in Role.query}
        assert names == {"Admin", "Kassierer", "Zählerverwalter"}

    def test_used_treasurer_role_is_not_renamed(self, app):
        from cli import seed_default_roles
        from app.models import Role, User
        seed_default_roles(db)
        role = Role.query.filter_by(name="Kassier").one()
        user = User(username="kassier", email="k@example.test", role_id=role.id)
        user.set_password("x")
        db.session.add(user)
        db.session.commit()
        seed_default_roles(db, country_code="DE")
        assert Role.query.filter_by(name="Kassier").count() == 1
        assert Role.query.filter_by(name="Kassierer").count() == 0

    def test_treasurer_role_not_duplicated_after_country_switch(self, app):
        """AT-Mandant wechselt auf DE: ein erneuter Seed-Lauf (z. B. Reset)
        legt neben „Kassier" keine zweite Kassier-Rolle an."""
        from cli import seed_default_roles
        from app.models import Role
        seed_default_roles(db)
        _set_country("DE")
        seed_default_roles(db)
        assert Role.query.filter(Role.name.in_(("Kassier", "Kassierer"))).count() == 1
        assert Role.query.count() == 3


class TestCountryDefaults:
    def _seed_at(self):
        from cli import seed_default_tax_rates
        seed_default_tax_rates(db, country_code="AT")

    def test_switch_at_to_de_adds_rates_and_defaults(self, app):
        self._seed_at()
        _set_country("DE")
        result = apply_country_defaults("DE", previous_code="AT")
        db.session.commit()
        assert result["added"] == [Decimal("7"), Decimal("19")]
        assert tax_service.water_tax_rate() == Decimal("7")
        assert meter_replacement_interval() == 6
        # AT-Saetze bleiben (nur Ergaenzung, keine Deaktivierung beim Wechsel)
        assert Decimal("20") in _rates()

    def test_switch_keeps_custom_values(self, app):
        self._seed_at()
        AppSetting.set(tax_service.WATER_RATE_KEY, "13")
        AppSetting.set("meters.replacement_interval_years", "8")
        db.session.commit()
        _set_country("DE")
        apply_country_defaults("DE", previous_code="AT")
        db.session.commit()
        assert tax_service.water_tax_rate() == Decimal("13")
        assert meter_replacement_interval() == 8

    def test_overwrite_and_deactivate_foreign(self, app):
        self._seed_at()
        _set_country("DE")
        result = apply_country_defaults("DE", overwrite=True, deactivate_foreign=True)
        db.session.commit()
        assert sorted(result["deactivated"]) == [Decimal("10"), Decimal("13"), Decimal("20")]
        assert _rates() == [Decimal("0"), Decimal("7"), Decimal("19")]
        # nie loeschen
        assert TaxRate.query.count() == 6


class TestWaterLevyVisibility:
    """Der Wassercent wird beim Wechsel nach Deutschland eingeblendet."""

    def _levy(self):
        from app.models import ChargeType
        return ChargeType.query.filter_by(key="water_levy").one()

    @pytest.fixture(autouse=True)
    def _catalog(self, app):
        from app.invoices.charges import ensure_system_charge_types
        ensure_system_charge_types("AT")   # Wassercent zunaechst ausgeblendet
        db.session.commit()

    def test_switch_to_germany_activates_it(self, app):
        assert self._levy().active is False
        _set_country("DE")
        result = apply_country_defaults("DE", previous_code="AT")
        db.session.commit()
        assert result["levy"] == "activated"
        assert self._levy().active is True

    def test_already_active_is_not_reported(self, app):
        _set_country("DE")
        apply_country_defaults("DE", previous_code="AT")
        db.session.commit()
        result = apply_country_defaults("DE", overwrite=True)
        assert result["levy"] is None

    def test_switch_to_austria_keeps_it_without_flag(self, app):
        _set_country("DE")
        apply_country_defaults("DE", previous_code="AT")
        db.session.commit()
        _set_country("AT")
        result = apply_country_defaults("AT", previous_code="DE")
        db.session.commit()
        assert result["levy"] is None and self._levy().active is True

    def test_deactivated_with_flag_when_unused(self, app):
        _set_country("DE")
        apply_country_defaults("DE", previous_code="AT")
        db.session.commit()
        _set_country("AT")
        result = apply_country_defaults("AT", overwrite=True, deactivate_foreign=True)
        db.session.commit()
        assert result["levy"] == "deactivated" and self._levy().active is False

    def test_kept_when_a_tariff_uses_it(self, app):
        from app.invoices.charges import build_tariff
        _set_country("DE")
        apply_country_defaults("DE", previous_code="AT")
        build_tariff(name="T", valid_from=date(2026, 1, 1), water_price="1.85",
                     water_levy="0.10")
        db.session.commit()
        _set_country("AT")
        result = apply_country_defaults("AT", overwrite=True, deactivate_foreign=True)
        db.session.commit()
        assert result["levy"] is None and self._levy().active is True

    def test_summary_mentions_the_levy(self, app):
        from app.settings.country_defaults import summary_message
        _set_country("DE")
        result = apply_country_defaults("DE", previous_code="AT")
        assert "Wassercent" in summary_message(result)
