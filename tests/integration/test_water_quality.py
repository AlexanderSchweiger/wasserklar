"""Integration-Tests fuer den TWV-Parameter-Katalog (Bewertung, Grenzwert-
Override via AppSetting, Anzeige) und das WaterSample/LabResult-Modell.

Die Bewertungsfunktionen lesen ``AppSetting`` (Override) — daher DB-beruehrend.
"""
import json
from datetime import date

from app.extensions import db
from app.models import (
    AppSetting, NetworkPlan, NetworkFeature, WaterSample, LabResult,
)
from app.network import water_quality as wq


def _probenahme(plan, name="P1"):
    nf = NetworkFeature(
        plan_id=plan.id, geometry_kind="point", feature_type="probenahme", name=name,
        geometry=json.dumps({"type": "Point", "coordinates": [16, 48]}), lat=48, lng=16,
    )
    db.session.add(nf)
    db.session.commit()
    return nf


class TestAssessDefaults:
    def test_max_ok_warning_alarm(self, app):
        assert wq.assess("nitrat", 30) == wq.STATUS_OK
        assert wq.assess("nitrat", 48) == wq.STATUS_WARNING   # >= 90 % von 50
        assert wq.assess("nitrat", 60) == wq.STATUS_ALARM

    def test_zero_limit_has_no_warning_band(self, app):
        assert wq.assess("e_coli", 0) == wq.STATUS_OK
        assert wq.assess("e_coli", 1) == wq.STATUS_ALARM

    def test_range_ph(self, app):
        assert wq.assess("ph", 7.2) == wq.STATUS_OK
        assert wq.assess("ph", 5.0) == wq.STATUS_ALARM
        assert wq.assess("ph", 10.0) == wq.STATUS_ALARM

    def test_info_parameter_is_ok(self, app):
        assert wq.assess("gesamthaerte", 25) == wq.STATUS_OK

    def test_none_is_unknown(self, app):
        assert wq.assess("nitrat", None) == wq.STATUS_UNKNOWN


class TestEffectiveLimitOverride:
    def test_max_override_changes_assessment(self, app):
        AppSetting.set("water_quality.nitrat.limit", "40")
        db.session.commit()
        assert wq.effective_limit("nitrat") == ("max", 40.0)
        assert wq.assess("nitrat", 45) == wq.STATUS_ALARM     # 45 > 40

    def test_range_override_german_comma(self, app):
        AppSetting.set("water_quality.ph.limit", "6,0-8,0")
        db.session.commit()
        assert wq.effective_limit("ph") == ("range", 6.0, 8.0)
        assert wq.assess("ph", 8.5) == wq.STATUS_ALARM

    def test_invalid_override_falls_back_to_default(self, app):
        AppSetting.set("water_quality.nitrat.limit", "abc")
        db.session.commit()
        assert wq.effective_limit("nitrat") == ("max", 50.0)


class TestLimitDisplay:
    def test_max_with_unit(self, app):
        assert wq.limit_display("nitrat") == "50 mg/l"

    def test_range(self, app):
        assert wq.limit_display("ph") == "6,5–9,5"

    def test_info_is_empty(self, app):
        assert wq.limit_display("gesamthaerte") == ""

    def test_limit_value_only_for_max(self, app):
        assert wq.limit_value("nitrat") == 50.0
        assert wq.limit_value("ph") is None
        assert wq.limit_value("gesamthaerte") is None


class TestSampleModel:
    def test_overall_status_is_worst(self, app):
        plan = NetworkPlan(name="P", status=NetworkPlan.STATUS_ACTIVE)
        db.session.add(plan)
        db.session.commit()
        f = _probenahme(plan)
        s = WaterSample(feature_id=f.id, sample_date=date(2026, 6, 1))
        s.results = [
            LabResult(parameter_key="nitrat", value_num=10, status="ok"),
            LabResult(parameter_key="e_coli", value_num=1, status="alarm"),
            LabResult(parameter_key="truebung", value_num=0.95, status="warning"),
        ]
        db.session.add(s)
        db.session.commit()
        assert s.overall_status() == "alarm"
        assert s.alarm_count() == 1

    def test_overall_status_none_without_results(self, app):
        s = WaterSample(feature_id=1, sample_date=date(2026, 6, 1))
        assert s.overall_status() is None

    def test_cascade_delete_removes_results(self, app):
        plan = NetworkPlan(name="P2", status=NetworkPlan.STATUS_ACTIVE)
        db.session.add(plan)
        db.session.commit()
        f = _probenahme(plan, name="P2a")
        s = WaterSample(feature_id=f.id, sample_date=date(2026, 6, 1))
        s.results = [LabResult(parameter_key="nitrat", value_num=10, status="ok")]
        db.session.add(s)
        db.session.commit()
        db.session.delete(s)
        db.session.commit()
        assert LabResult.query.count() == 0
