"""Integration-Tests fuer die Technik-Service-Funktionen
(GeoJSON-(De)Serialisierung, Haversine-Laenge, Import-Aufbau, inspections_due,
Plan-Kopie/Merge)."""
import json
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import NetworkPlan, NetworkFeature, MaintenanceLog
from app.technik import services as svc


def _make_plan(name="Plan", status=NetworkPlan.STATUS_ACTIVE, maintenance=True, source=None):
    p = NetworkPlan(
        name=name, status=status, maintenance_enabled=maintenance,
        source_plan_id=(source.id if source else None),
    )
    db.session.add(p)
    db.session.commit()
    return p


def _point_feature(plan, name=None, ftype="hydrant", coords=(16, 48), source_feature_id=None):
    nf = NetworkFeature(
        plan_id=plan.id, source_feature_id=source_feature_id,
        geometry_kind="point", feature_type=ftype, name=name,
        geometry=json.dumps({"type": "Point", "coordinates": list(coords)}),
        lat=coords[1], lng=coords[0],
    )
    db.session.add(nf)
    db.session.commit()
    return nf


class TestGeometry:
    def test_apply_point(self):
        nf = NetworkFeature()
        svc.apply_geometry(nf, {"type": "Point", "coordinates": [16.37, 48.21]})
        assert nf.geometry_kind == "point"
        assert nf.lat == pytest.approx(48.21)
        assert nf.lng == pytest.approx(16.37)
        assert nf.length_m is None
        assert json.loads(nf.geometry)["type"] == "Point"

    def test_apply_line_computes_length(self):
        nf = NetworkFeature()
        svc.apply_geometry(nf, {"type": "LineString", "coordinates": [[16.0, 48.0], [16.0, 48.001]]})
        assert nf.geometry_kind == "line"
        assert nf.lat is None and nf.lng is None
        # 0.001 Grad Breite ~ 111 m
        assert nf.length_m == pytest.approx(111.3, abs=2.0)

    def test_invalid_geometry_raises(self):
        nf = NetworkFeature()
        with pytest.raises(ValueError):
            svc.apply_geometry(nf, {"type": "Polygon", "coordinates": []})
        with pytest.raises(ValueError):
            svc.apply_geometry(nf, {"type": "LineString", "coordinates": [[16.0, 48.0]]})


class TestBuildAndSerialize:
    def test_build_unknown_type_defaults(self):
        nf_pt = svc.build_feature_from_geojson({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [16, 48]},
            "properties": {"feature_type": "bogus"},
        })
        assert nf_pt.feature_type == "sonstiges"

        nf_ln = svc.build_feature_from_geojson({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[16, 48], [16.001, 48]]},
            "properties": {},
        })
        assert nf_ln.feature_type == "sonstige_leitung"

    def test_build_assigns_plan(self):
        plan = _make_plan()
        nf = svc.build_feature_from_geojson(
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [16, 48]},
             "properties": {"feature_type": "hydrant"}},
            plan_id=plan.id,
        )
        assert nf.plan_id == plan.id

    def test_roundtrip(self):
        plan = _make_plan()
        nf = svc.build_feature_from_geojson({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [16.37, 48.21]},
            "properties": {"feature_type": "hydrant", "name": "H1", "accuracy": "exakt", "material": "PE"},
        }, plan_id=plan.id)
        db.session.add(nf)
        db.session.commit()

        gj = svc.feature_to_geojson(nf)
        assert gj["type"] == "Feature"
        assert gj["geometry"]["coordinates"] == [16.37, 48.21]
        props = gj["properties"]
        assert props["feature_type"] == "hydrant"
        assert props["name"] == "H1"
        assert props["accuracy"] == "exakt"
        assert props["type_label"] == "Hydrant"
        assert props["photo_count"] == 0
        assert props["maintenance_count"] == 0

    def test_summarize_counts_and_skips(self):
        raw = json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [16, 48]}, "properties": {"feature_type": "hydrant"}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [16, 48]}, "properties": {"feature_type": "schieber"}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}, "properties": {}},
        ]})
        counts, total, skipped = svc.summarize_geojson(raw)
        assert total == 2
        assert skipped == 1
        assert counts.get("Hydrant") == 1
        assert counts.get("Schieber") == 1

    def test_iter_invalid_json_raises(self):
        with pytest.raises(ValueError):
            svc.iter_geojson_features('{"type": "NotACollection"}')


class TestInspectionsDue:
    def test_due_when_next_due_in_past(self):
        plan = _make_plan()
        nf = _point_feature(plan)
        db.session.add(MaintenanceLog(
            feature_id=nf.id, date=date.today() - timedelta(days=400),
            kind="spuelung", next_due=date.today() - timedelta(days=30),
        ))
        db.session.commit()
        due = svc.inspections_due(date.today())
        assert len(due) == 1
        assert due[0]["feature"].id == nf.id
        assert due[0]["overdue_days"] == 30

    def test_newest_log_without_next_due_resets(self):
        plan = _make_plan()
        nf = _point_feature(plan)
        db.session.add_all([
            MaintenanceLog(feature_id=nf.id, date=date.today() - timedelta(days=400),
                           kind="spuelung", next_due=date.today() - timedelta(days=30)),
            MaintenanceLog(feature_id=nf.id, date=date.today() - timedelta(days=1),
                           kind="spuelung", next_due=None),
        ])
        db.session.commit()
        assert svc.inspections_due(date.today()) == []

    def test_future_next_due_not_due(self):
        plan = _make_plan()
        nf = _point_feature(plan)
        db.session.add(MaintenanceLog(
            feature_id=nf.id, date=date.today(), kind="inspektion",
            next_due=date.today() + timedelta(days=30),
        ))
        db.session.commit()
        assert svc.inspections_due(date.today()) == []

    def test_draft_plan_not_due(self):
        """Nur aktive Plaene treiben die Erinnerung — Entwuerfe nicht."""
        plan = _make_plan(status=NetworkPlan.STATUS_DRAFT)
        nf = _point_feature(plan)
        db.session.add(MaintenanceLog(
            feature_id=nf.id, date=date.today() - timedelta(days=400),
            kind="spuelung", next_due=date.today() - timedelta(days=30),
        ))
        db.session.commit()
        assert svc.inspections_due(date.today()) == []

    def test_maintenance_disabled_plan_not_due(self):
        """Aktiver Plan, aber Wartung deaktiviert -> keine Erinnerung."""
        plan = _make_plan(maintenance=False)
        nf = _point_feature(plan)
        db.session.add(MaintenanceLog(
            feature_id=nf.id, date=date.today() - timedelta(days=400),
            kind="spuelung", next_due=date.today() - timedelta(days=30),
        ))
        db.session.commit()
        assert svc.inspections_due(date.today()) == []


class TestPlanCopyMerge:
    def test_copy_features_only(self):
        src = _make_plan(name="Haupt")
        f1 = _point_feature(src, name="H1")
        db.session.add(MaintenanceLog(feature_id=f1.id, date=date.today(), kind="spuelung"))
        db.session.commit()

        dup, count = svc.copy_plan(src, uid=None)
        assert count == 1
        assert dup.status == NetworkPlan.STATUS_DRAFT
        assert dup.maintenance_enabled is False
        assert dup.source_plan_id == src.id
        cf = dup.features[0]
        assert cf.source_feature_id == f1.id
        assert cf.name == "H1"
        assert cf.maintenance_logs == []          # Logs werden NICHT mitkopiert
        # Quell-Feature behaelt seinen Log
        assert len(f1.maintenance_logs) == 1

    def test_merge_add_update_delete_and_relink(self):
        src = _make_plan(name="Haupt")
        a = _point_feature(src, name="A")
        _point_feature(src, name="B")
        dup, _ = svc.copy_plan(src, uid=None)

        ca = next(f for f in dup.features if f.source_feature_id == a.id)
        cb = next(f for f in dup.features if f.name == "B")
        ca.name = "A-neu"                 # Aenderung
        db.session.delete(cb)             # Loeschung in der Kopie
        _point_feature(dup, name="C")     # neu gezeichnet (source_feature_id None)
        db.session.commit()

        res = svc.merge_plan_into_source(dup, uid=None)
        assert (res["added"], res["updated"], res["deleted"]) == (1, 1, 1)
        assert sorted(f.name for f in src.features) == ["A-neu", "C"]

        # Re-Link: das neu uebertragene Feature der Kopie zeigt nun auf den Quell-Datensatz
        new_in_copy = next(f for f in dup.features if f.name == "C")
        assert new_in_copy.source_feature_id is not None

        # Zweiter Merge ohne weitere Aenderung dupliziert nichts mehr.
        res2 = svc.merge_plan_into_source(dup, uid=None)
        assert (res2["added"], res2["deleted"]) == (0, 0)
        assert len(src.features) == 2

    def test_merge_without_source_returns_none(self):
        plan = _make_plan(name="Solo")
        assert svc.merge_plan_into_source(plan, uid=None) is None
