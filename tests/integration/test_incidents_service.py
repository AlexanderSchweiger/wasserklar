"""Integration-Tests fuer die Service-Logik des Stoerungsjournals.

GeoJSON-Parse, Attribut-Mapping inkl. status<->resolved_at-Kopplung,
Decimal-genaue Kennzahlen-Aggregation und ``duration_days``-Edge-Cases.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Incident
from app.incidents import services as svc
from app.incidents import vocab


class TestApplyLocation:
    def test_point_parsed(self):
        inc = Incident()
        svc.apply_location(inc, {"type": "Point", "coordinates": [14.1, 47.5]})
        assert inc.lat == 47.5 and inc.lng == 14.1
        assert '"Point"' in inc.location_geojson

    def test_none_clears(self):
        inc = Incident(lat=1.0, lng=2.0, location_geojson="{}")
        svc.apply_location(inc, None)
        assert inc.lat is None and inc.lng is None and inc.location_geojson is None

    def test_invalid_geometry_raises(self):
        inc = Incident()
        with pytest.raises(ValueError):
            svc.apply_location(inc, {"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
        with pytest.raises(ValueError):
            svc.apply_location(inc, {"type": "Point", "coordinates": [1]})


class TestApplyAttributes:
    def test_invalid_values_fall_back(self):
        inc = Incident()
        svc.apply_attributes(inc, {"title": "T", "incident_type": "bogus",
                                   "severity": "bogus", "status": "bogus", "cause": "bogus"})
        assert inc.incident_type == Incident.TYPE_ROHRBRUCH
        assert inc.severity == Incident.SEVERITY_MEDIUM
        assert inc.status == Incident.STATUS_OPEN
        assert inc.cause is None

    def test_decimal_parsing_german_comma(self):
        inc = Incident()
        svc.apply_attributes(inc, {"title": "T", "cost": "850,50", "water_loss_m3": "12,5"})
        assert inc.cost == Decimal("850.50")
        assert inc.water_loss_m3 == Decimal("12.5")

    def test_status_resolved_coupling(self):
        inc = Incident()
        svc.apply_attributes(inc, {"title": "T", "detected_at": "2026-03-01", "status": "behoben"})
        assert inc.resolved_at == date.today()
        svc.apply_attributes(inc, {"title": "T", "detected_at": "2026-03-01", "status": "offen"})
        assert inc.resolved_at is None

    def test_manual_resolved_at_respected(self):
        inc = Incident()
        svc.apply_attributes(inc, {"title": "T", "detected_at": "2026-03-01",
                                   "status": "behoben", "resolved_at": "2026-03-05"})
        assert inc.resolved_at == date(2026, 3, 5)


class TestDuration:
    def test_open_is_none(self):
        inc = Incident(detected_at=date(2026, 3, 1))
        assert inc.duration_days() is None

    def test_resolved(self):
        inc = Incident(detected_at=date(2026, 3, 1), resolved_at=date(2026, 3, 4))
        assert inc.duration_days() == 3


class TestReportAggregates:
    def test_decimal_sums_and_avg(self, app):
        db.session.add_all([
            Incident(title="A", incident_type="rohrbruch", severity="hoch", status="behoben",
                     detected_at=date(2026, 1, 1), resolved_at=date(2026, 1, 3),
                     cost=Decimal("100.00"), water_loss_m3=Decimal("10.5"), affected_count=4),
            Incident(title="B", incident_type="undichtheit", severity="mittel", status="offen",
                     detected_at=date(2026, 2, 1),
                     cost=Decimal("50.50"), water_loss_m3=Decimal("2.5"), affected_count=2),
        ])
        db.session.commit()
        agg = svc.report_aggregates(year=2026)
        assert agg["total"] == 2
        assert agg["cost_sum"] == Decimal("150.50")
        assert isinstance(agg["cost_sum"], Decimal)
        assert agg["loss_sum"] == Decimal("13.0")
        assert agg["affected_sum"] == 6
        assert agg["affected_max"] == 4
        # nur die behobene Stoerung zaehlt fuer die Durchschnittsdauer
        assert agg["avg_duration_days"] == 2.0
        assert agg["resolved_count"] == 1
        assert agg["by_type"]["rohrbruch"] == 1
        assert agg["by_status"]["offen"] == 1
