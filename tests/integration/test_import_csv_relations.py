"""Integration tests for integrity warnings in the combined CSV import.

Phase 2d — verifies that _run_import emits non-blocking warnings for:
  (a) Same meter number assigned to different objects within one file.
  (b) A second distinct active owner assigned to the same object.
  (c) Same owner / same object → no warning.
"""
from datetime import date

import pandas as pd
import pytest

from app.extensions import db
from app.import_csv.routes import _run_import
from app.models import (
    BillingPeriod, Customer, Property, PropertyOwnership, WaterMeter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _periods(app):
    """Abrechnungsperiode 2024 — wird von Tests benoetigt, die Stand-Spalten nutzen."""
    db.session.add(BillingPeriod(
        name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        active=True,
    ))
    db.session.commit()


# Standard-Spalten-Zuordnung (spiegelt test_import_csv.py)
COLS = {
    "customer_number": "Kunden-Nr.",
    "customer_name": "Name",
    "property_name": "Objekt",
    "property_type": "Typ",
    "meter_number": "Zählernummer",
    "meter_eichjahr": "Eichjahr",
    "strasse": "Straße",
    "plz": "PLZ",
    "ort": "Ort",
    "land": "Land",
    "phone": "Telefon",
    "email": "E-Mail",
    "notes": "Kommentar",
}


def _run(rows, mode="skip", dry_run=False):
    """Baut ein DataFrame aus Zeilen-Dicts und ruft _run_import."""
    df = pd.DataFrame(rows).fillna("")
    return _run_import(df, COLS, mode, dry_run=dry_run)


def _warn_actions(plan_entry):
    """Gibt alle warn-Aktionen aus einem Plan-Eintrag zurueck."""
    return [a for a in plan_entry["actions"] if a["kind"] == "warn"]


# ---------------------------------------------------------------------------
# (a) Zähler↔Objekt — gleiche Zählernummer, verschiedene Objekte
# ---------------------------------------------------------------------------

class TestMeterObjectConflict:

    def test_same_meter_different_objects_warns(self, app):
        """Zwei Zeilen mit identischer Zählernummer unter verschiedenen Objekten
        → mindestens eine Warnung in results['warnings'] (nicht-blockierend)."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus B",
             "Zählernummer": "Z1"},
        ])
        # Mindestens eine Warnung
        assert len(res["warnings"]) >= 1
        assert any("Z1" in w for w in res["warnings"])

    def test_same_meter_different_objects_both_rows_processed(self, app):
        """Beide Zeilen werden trotz Warnung verarbeitet (nicht-blockierend)."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus B",
             "Zählernummer": "Z1"},
        ])
        # Beide Zeilen landen in der Plan-Liste mit Kategorie 'import'
        categories = [e["category"] for e in res["plan"]]
        assert "skip" not in categories
        # Beide Kunden wurden angelegt
        assert res["customers_created"] == 2

    def test_same_meter_different_objects_warn_action_in_plan(self, app):
        """Die Warnung erscheint auch als Pro-Zeile-Aktion (kind='warn')."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus B",
             "Zählernummer": "Z1"},
        ])
        # Mindestens eine Zeile soll eine warn-Aktion haben
        warn_count = sum(len(_warn_actions(e)) for e in res["plan"])
        assert warn_count >= 1

    def test_existing_meter_other_object_warns(self, app):
        """Zähler Z1 ist im Bestand bei Objekt Haus A; CSV ordnet ihn Haus B zu
        → Warnung (Bestand-vs-Datei-Konflikt)."""
        # Erstimport: Zähler Z1 an Haus A anlegen
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        # Zweitimport: Zähler Z1 soll Haus B zugeordnet werden
        res = _run([
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus B",
             "Zählernummer": "Z1"},
        ])
        assert len(res["warnings"]) >= 1
        assert any("Z1" in w for w in res["warnings"])
        # Zeile wurde trotzdem verarbeitet
        assert res["plan"][0]["category"] != "skip"

    def test_same_meter_same_object_no_warning(self, app):
        """Gleiche Zählernummer, gleiches Objekt (erneuter Import) → keine Warnung."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        # Keine Zähler-Objekt-Konflikt-Warnung (gleicher Zähler, gleiches Objekt).
        meter_warnings = [w for w in res["warnings"] if "Z1" in w]
        assert meter_warnings == []


# ---------------------------------------------------------------------------
# (b) Objekt↔Besitzer — zweiter aktiver Eigentümer
# ---------------------------------------------------------------------------

class TestOwnerConflict:

    def test_second_owner_warns(self, app):
        """Im Bestand hat Haus A bereits Kunden 100 als Eigentümer.
        CSV ordnet Kunde 101 demselben Objekt zu → Owner-Warnung."""
        # Erstimport: Kunde 100 bekommt Haus A
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        # Zweitimport: Kunde 101 soll auch Haus A bekommen
        res = _run([
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        assert len(res["warnings"]) >= 1
        # Die Warnung nennt das Objekt oder den Kunden
        assert any("101" in w or "Haus A" in w or "mehrere" in w.lower()
                   for w in res["warnings"])

    def test_second_owner_both_ownerships_created(self, app):
        """Nach echtem Import (dry_run=False) hat das Objekt zwei aktive Eigentümer."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ], dry_run=False)
        # Import liefert eine Warnung, aber die Ownership wird trotzdem angelegt
        assert res["ownerships_created"] == 1
        prop = Property.query.filter_by(object_number="Haus A").first()
        active_ownerships = PropertyOwnership.query.filter_by(
            property_id=prop.id, valid_to=None
        ).all()
        assert len(active_ownerships) == 2

    def test_second_owner_warn_action_in_plan(self, app):
        """Die Owner-Warnung erscheint als Pro-Zeile-Aktion (kind='warn')."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        warn_count = sum(len(_warn_actions(e)) for e in res["plan"])
        assert warn_count >= 1

    def test_intrafile_two_owners_same_object_warns(self, app):
        """Datei-intern: Zeile 1 ordnet Objekt Haus A Kunde 100 zu,
        Zeile 2 ordnet dasselbe Objekt Kunde 101 zu → Warnung."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        owner_warnings = [
            w for w in res["warnings"]
            if "mehrere" in w.lower() or "eigentümer" in w.lower()
        ]
        assert len(owner_warnings) >= 1

    def test_second_owner_row_not_skipped(self, app):
        """Warnungen blockieren nicht — die Zeile wird trotzdem verarbeitet."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        assert res["plan"][0]["category"] != "skip"
        assert res["rows_skipped"] == 0

    # -----------------------------------------------------------------------
    # (c) Gleicher Eigentümer / gleiches Objekt → keine Warnung
    # -----------------------------------------------------------------------

    def test_same_owner_same_object_no_warning(self, app):
        """Gleicher Kunde, gleiches Objekt: bestehende Zuordnung → keine Owner-Warnung."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        owner_warnings = [
            w for w in res["warnings"]
            if "mehrere" in w.lower() or "eigentümer" in w.lower()
        ]
        assert owner_warnings == []

    def test_intrafile_same_customer_same_object_no_owner_warning(self, app):
        """Derselbe Kunde zweimal mit demselben Objekt in einer Datei → keine Warnung."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        owner_warnings = [
            w for w in res["warnings"]
            if "mehrere" in w.lower() or "eigentümer" in w.lower()
        ]
        assert owner_warnings == []

    # -----------------------------------------------------------------------
    # Dry-run: Warnungen auch im Vorschau-Modus
    # -----------------------------------------------------------------------

    def test_warnings_visible_in_dry_run(self, app):
        """Warnungen erscheinen auch im dry_run=True (Vorschau-Lauf)."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "101", "Name": "Maier", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ], dry_run=True)
        assert len(res["warnings"]) >= 1
        # Dry-run schreibt nichts
        assert Customer.query.count() == 1  # nur der aus dem Erstimport
