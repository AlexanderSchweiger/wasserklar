"""Integrity-conflict trackers for import wizards.

These trackers accumulate planned assignments during a preview/commit run
and emit non-blocking warnings when integrity rules would be violated.

Design:
- Model-free: callers pass existing DB keys as plain values; no ORM imports.
- Non-blocking: a conflict warning is returned (str), but the assignment is
  still registered so subsequent rows can also be checked.
- Stateless across requests: one tracker instance per import run.

All user-facing warning texts are in German.
"""
from __future__ import annotations


class OwnerConflictTracker:
    """Warns when a property would gain more than one distinct active owner.

    Covers both intra-file duplicates and conflicts against existing DB data.
    The assignment is recorded regardless (multiple active owners are allowed
    for married couples / inheritance communities) — the warning is purely
    informational.
    """

    def __init__(self) -> None:
        # property_key -> set of customer_keys planned in this run
        self._planned: dict = {}

    def check_and_register(
        self,
        property_key,
        customer_key,
        existing_owner_keys=(),
    ) -> "str | None":
        """Check for an owner conflict and register the assignment.

        Args:
            property_key: Identifier for the property (e.g. object_number).
            customer_key: Identifier for the customer being assigned.
            existing_owner_keys: Iterable of customer keys that are already
                active owners in the database for this property.

        Returns:
            A German warning string if a conflict is detected, else ``None``.
        """
        # Combine existing DB owners with those planned so far in this run.
        combined: set = set(existing_owner_keys) | self._planned.get(property_key, set())

        # A conflict exists when there is already a *different* active owner.
        conflict = combined - {customer_key}

        # Register this customer for this property.
        self._planned.setdefault(property_key, set()).add(customer_key)

        if conflict:
            return (
                f"Objekt {property_key}: mehrere aktive Eigentümer "
                f"(zusätzlich Kunde {customer_key})"
            )
        return None


class MeterObjectTracker:
    """Warns when the same meter number would be assigned to different objects.

    Covers both intra-file conflicts and conflicts against existing DB data.
    The assignment is registered regardless — the caller decides whether to
    skip or overwrite in update mode.
    """

    def __init__(self) -> None:
        # meter_number -> object_key planned in this run
        self._planned: dict = {}

    def check_and_register(
        self,
        meter_number,
        object_key,
        existing_object_key=None,
    ) -> "str | None":
        """Check for a meter-object conflict and register the assignment.

        Args:
            meter_number: The meter's unique identifier.
            object_key: The object (property) being assigned in this row.
            existing_object_key: The object key currently stored in the DB
                for this meter, or ``None`` if the meter is new.

        Returns:
            A German warning string if a conflict is detected, else ``None``.
        """
        warning: str | None = None

        # Conflict against existing DB data
        if existing_object_key is not None and existing_object_key != object_key:
            warning = (
                f"Zähler {meter_number}: laut Datei Objekt {object_key}, "
                f"im Bestand Objekt {existing_object_key}"
            )

        # Conflict within the file itself (previous row planned a different object)
        planned_key = self._planned.get(meter_number)
        if planned_key is not None and planned_key != object_key and warning is None:
            warning = (
                f"Zähler {meter_number}: in dieser Datei verschiedenen Objekten "
                f"zugeordnet ({planned_key} und {object_key})"
            )

        # Register (latest row wins)
        self._planned[meter_number] = object_key

        return warning
