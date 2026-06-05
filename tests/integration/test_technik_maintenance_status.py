"""Integration-Tests fuer ``technik.services.feature_maintenance_status``.

Anders als ``inspections_due`` ist der Helper plan-status-agnostisch und liefert
den Status JEDES uebergebenen Features (auch „ok"/„kein Termin"); Features ohne
Log fehlen bewusst im Ergebnis.
"""
import json
from datetime import date

from app.extensions import db
from app.models import NetworkPlan, NetworkFeature, MaintenanceLog
from app.network import services as svc


def _plan(status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True, name="P"):
    p = NetworkPlan(name=name, status=status, maintenance_enabled=maintenance_enabled)
    db.session.add(p)
    db.session.commit()
    return p


def _point(plan, ftype="hydrant", name=None):
    f = NetworkFeature(
        plan_id=plan.id, geometry_kind="point", feature_type=ftype, name=name,
        geometry=json.dumps({"type": "Point", "coordinates": [16.0, 48.0]}),
        lat=48.0, lng=16.0,
    )
    db.session.add(f)
    db.session.commit()
    return f


def _log(feature, when, next_due=None, kind="inspektion"):
    log = MaintenanceLog(feature_id=feature.id, date=when, kind=kind, next_due=next_due)
    db.session.add(log)
    db.session.commit()
    return log


class TestFeatureMaintenanceStatus:
    def test_empty_input(self, app):
        assert svc.feature_maintenance_status([]) == {}

    def test_feature_without_logs_absent(self, app):
        f = _point(_plan())
        assert svc.feature_maintenance_status([f]) == {}

    def test_overdue(self, app):
        today = date(2026, 6, 1)
        f = _point(_plan())
        _log(f, date(2025, 6, 1), next_due=date(2026, 1, 1))
        st = svc.feature_maintenance_status([f], today=today)[f.id]
        assert st["due"] is True
        assert st["next_due"] == date(2026, 1, 1)
        assert st["overdue_days"] == (today - date(2026, 1, 1)).days

    def test_due_today(self, app):
        today = date(2026, 6, 1)
        f = _point(_plan())
        _log(f, date(2025, 6, 1), next_due=today)
        st = svc.feature_maintenance_status([f], today=today)[f.id]
        assert st["due"] is True
        assert st["overdue_days"] == 0

    def test_not_yet_due(self, app):
        today = date(2026, 6, 1)
        f = _point(_plan())
        _log(f, date(2026, 1, 1), next_due=date(2026, 12, 1))
        st = svc.feature_maintenance_status([f], today=today)[f.id]
        assert st["due"] is False
        assert st["next_due"] == date(2026, 12, 1)
        assert st["overdue_days"] < 0  # next_due in der Zukunft

    def test_newest_log_wins(self, app):
        today = date(2026, 6, 1)
        f = _point(_plan())
        _log(f, date(2024, 1, 1), next_due=date(2025, 1, 1))   # alt, ueberfaellig
        _log(f, date(2026, 5, 1), next_due=date(2027, 5, 1))   # neu, nicht faellig
        st = svc.feature_maintenance_status([f], today=today)[f.id]
        assert st["next_due"] == date(2027, 5, 1)
        assert st["due"] is False

    def test_newer_log_without_next_due_resets(self, app):
        today = date(2026, 6, 1)
        f = _point(_plan())
        _log(f, date(2024, 1, 1), next_due=date(2025, 1, 1))
        _log(f, date(2026, 5, 1), next_due=None)               # setzt den Zeitplan zurueck
        st = svc.feature_maintenance_status([f], today=today)[f.id]
        assert st["next_due"] is None
        assert st["due"] is False
        assert st["overdue_days"] is None

    def test_plan_status_agnostic(self, app):
        """Gegentest zu ``inspections_due``: Feature in Entwurf/Wartung-aus erscheint."""
        today = date(2026, 6, 1)
        p = _plan(status=NetworkPlan.STATUS_DRAFT, maintenance_enabled=False)
        f = _point(p)
        _log(f, date(2025, 1, 1), next_due=date(2026, 1, 1))
        st = svc.feature_maintenance_status([f], today=today)
        assert f.id in st
        assert st[f.id]["due"] is True
