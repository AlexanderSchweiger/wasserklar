"""Integration tests for app.imports.relations trackers.

Pure logic tests — no DB required.  Both trackers are tested for:
  (a) intra-file conflict (same property/meter with different partner)
  (b) conflict against existing stock (existing_owner_keys / existing_object_key)
  (c) same owner/object → no conflict (None)
  (d) non-blocking: after a conflict the key is still registered
"""
import pytest

from app.imports.relations import MeterObjectTracker, OwnerConflictTracker


# ---------------------------------------------------------------------------
# OwnerConflictTracker
# ---------------------------------------------------------------------------

class TestOwnerConflictTracker:

    def test_first_assignment_no_warning(self):
        """Registering an owner for the first time should return None."""
        tracker = OwnerConflictTracker()
        result = tracker.check_and_register("OBJ1", "CUST1")
        assert result is None

    def test_same_owner_no_conflict(self):
        """Registering the same owner twice for the same property is fine."""
        tracker = OwnerConflictTracker()
        tracker.check_and_register("OBJ1", "CUST1")
        result = tracker.check_and_register("OBJ1", "CUST1")
        assert result is None

    def test_intrafile_conflict_second_customer(self):
        """Second row assigns a different customer to the same property → warning."""
        tracker = OwnerConflictTracker()
        tracker.check_and_register("OBJ1", "CUST1")
        result = tracker.check_and_register("OBJ1", "CUST2")
        assert result is not None
        assert "OBJ1" in result
        assert "CUST2" in result

    def test_conflict_against_existing_stock(self):
        """Property already has CUST1 in DB; new row assigns CUST2 → warning."""
        tracker = OwnerConflictTracker()
        result = tracker.check_and_register("OBJ1", "CUST2",
                                            existing_owner_keys=["CUST1"])
        assert result is not None
        assert "OBJ1" in result
        assert "CUST2" in result

    def test_same_owner_as_existing_stock_no_conflict(self):
        """Property already has CUST1 in DB; new row also assigns CUST1 → no warning."""
        tracker = OwnerConflictTracker()
        result = tracker.check_and_register("OBJ1", "CUST1",
                                            existing_owner_keys=["CUST1"])
        assert result is None

    def test_non_blocking_key_registered_after_conflict(self):
        """After a conflict the customer key must still be recorded."""
        tracker = OwnerConflictTracker()
        tracker.check_and_register("OBJ1", "CUST1")
        tracker.check_and_register("OBJ1", "CUST2")  # conflict
        # Third row with CUST3 should also warn (both CUST1 and CUST2 are planned)
        result = tracker.check_and_register("OBJ1", "CUST3")
        assert result is not None

    def test_different_properties_independent(self):
        """Conflicts on different properties are independent."""
        tracker = OwnerConflictTracker()
        tracker.check_and_register("OBJ1", "CUST1")
        # OBJ2 gets CUST1 — no conflict because OBJ2 had no owner yet
        result = tracker.check_and_register("OBJ2", "CUST1")
        assert result is None


# ---------------------------------------------------------------------------
# MeterObjectTracker
# ---------------------------------------------------------------------------

class TestMeterObjectTracker:

    def test_first_assignment_no_warning(self):
        """Registering a meter for the first time should return None."""
        tracker = MeterObjectTracker()
        result = tracker.check_and_register("M001", "OBJ1")
        assert result is None

    def test_same_object_no_conflict(self):
        """Same meter assigned to same object twice → no warning."""
        tracker = MeterObjectTracker()
        tracker.check_and_register("M001", "OBJ1")
        result = tracker.check_and_register("M001", "OBJ1")
        assert result is None

    def test_intrafile_conflict_different_object(self):
        """Two rows assign the same meter to different objects → warning."""
        tracker = MeterObjectTracker()
        tracker.check_and_register("M001", "OBJ1")
        result = tracker.check_and_register("M001", "OBJ2")
        assert result is not None
        assert "M001" in result
        assert "OBJ1" in result or "OBJ2" in result

    def test_conflict_against_existing_stock(self):
        """Meter exists in DB under OBJ1; file assigns it to OBJ2 → warning."""
        tracker = MeterObjectTracker()
        result = tracker.check_and_register("M001", "OBJ2",
                                            existing_object_key="OBJ1")
        assert result is not None
        assert "M001" in result
        assert "OBJ1" in result
        assert "OBJ2" in result

    def test_same_object_as_existing_stock_no_conflict(self):
        """Meter exists in DB under OBJ1; file also assigns it to OBJ1 → no warning."""
        tracker = MeterObjectTracker()
        result = tracker.check_and_register("M001", "OBJ1",
                                            existing_object_key="OBJ1")
        assert result is None

    def test_non_blocking_key_registered_after_conflict(self):
        """After a conflict the latest object key must still be registered.

        A subsequent row with *yet another* object key should also warn,
        proving the first conflicting assignment was indeed registered.
        """
        tracker = MeterObjectTracker()
        tracker.check_and_register("M001", "OBJ1")
        tracker.check_and_register("M001", "OBJ2")  # conflict → OBJ2 registered
        result = tracker.check_and_register("M001", "OBJ3")
        # OBJ3 ≠ OBJ2 (latest planned) → should warn
        assert result is not None

    def test_different_meters_independent(self):
        """Assignments for different meters are fully independent."""
        tracker = MeterObjectTracker()
        tracker.check_and_register("M001", "OBJ1")
        result = tracker.check_and_register("M002", "OBJ1")
        assert result is None

    def test_no_existing_key_no_db_conflict(self):
        """existing_object_key=None means meter is new — no DB conflict."""
        tracker = MeterObjectTracker()
        result = tracker.check_and_register("M001", "OBJ1",
                                            existing_object_key=None)
        assert result is None
