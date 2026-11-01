"""Vorschau ungespeicherter Einstellungen (settings_service.setting_overrides)
und der Render-Cache-Helfer mit angemeldeten Zusatz-Keys."""
from flask import g

from app.extensions import db
from app.invoices import render_cache
from app.models import AppSetting
from app.settings_service import setting_overrides, wg_settings


def _set(key, value):
    AppSetting.set(key, value)
    db.session.commit()


class TestSettingOverrides:
    def test_override_wins_over_stored_value(self, app):
        _set("wg.name", "WG Gespeichert")
        with setting_overrides({"wg.name": "WG Vorschau"}):
            assert AppSetting.get("wg.name") == "WG Vorschau"
            assert wg_settings()["name"] == "WG Vorschau"
        assert AppSetting.get("wg.name") == "WG Gespeichert"

    def test_override_of_missing_key(self, app):
        with setting_overrides({"invoice.design": "blue"}):
            assert AppSetting.get("invoice.design", "classic") == "blue"
        assert AppSetting.get("invoice.design", "classic") == "classic"

    def test_none_behaves_like_an_empty_row(self, app):
        _set("wg.logo", "data:image/png;base64,AAAA")
        with setting_overrides({"wg.logo": None}):
            assert AppSetting.get("wg.logo", "default") is None
            assert wg_settings()["logo"] == ""

    def test_other_keys_stay_untouched(self, app):
        _set("wg.iban", "AT611904300234573201")
        with setting_overrides({"wg.name": "X"}):
            assert AppSetting.get("wg.iban") == "AT611904300234573201"

    def test_nesting_restores_the_outer_state(self, app):
        with setting_overrides({"a.key": "1", "b.key": "1"}):
            with setting_overrides({"b.key": "2"}):
                assert AppSetting.get("a.key") == "1"
                assert AppSetting.get("b.key") == "2"
            assert AppSetting.get("b.key") == "1"
        assert g.get("_appsetting_overrides") is None

    def test_restored_after_an_exception(self, app):
        try:
            with setting_overrides({"wg.name": "X"}):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert g.get("_appsetting_overrides") is None

    def test_nothing_is_written(self, app):
        with setting_overrides({"wg.name": "Nur Vorschau"}):
            AppSetting.get("wg.name")
        db.session.commit()
        assert AppSetting.query.filter_by(key="wg.name").first() is None


class TestRenderCacheExtraKeys:
    def _register(self, app, *keys):
        render_cache.register_render_keys(app, *keys)

    def _unregister(self, app):
        app.extensions.pop("invoice.render_keys_extra", None)

    def test_empty_extra_key_keeps_the_signature(self, app):
        before = render_cache.signature()
        self._register(app, "test.layout_blocks")
        try:
            assert render_cache.signature() == before
        finally:
            self._unregister(app)

    def test_extra_key_with_value_changes_the_signature(self, app):
        self._register(app, "test.layout_blocks")
        try:
            before = render_cache.signature()
            _set("test.layout_blocks", '[{"title": "Info"}]')
            assert render_cache.signature() != before
        finally:
            self._unregister(app)

    def test_base_keys_are_not_registered_twice(self, app):
        self._register(app, "invoice.design", "test.x", "test.x")
        try:
            assert render_cache.extra_keys() == ("test.x",)
        finally:
            self._unregister(app)
