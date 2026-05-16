"""Integration-Tests fuer den Stammdaten-Import (app/import_csv).

Schwerpunkt: das additive Verhalten von ``_run_import``.

- Ein Kunde, der mehrfach in der CSV vorkommt, sammelt mehrere Objekte/Zaehler.
- Ein zweiter Import gegen einen *bereits in der DB existierenden* Kunden
  ergaenzt neue Objekte/Zaehler, statt die Zeile zu verwerfen.
- ``duplicate_mode`` steuert nur, ob bereits vorhandene Stammdaten/Ablesungen
  aktualisiert werden – neue Datensaetze werden in beiden Modi ergaenzt.
- ``dry_run=True`` (Vorschau) schreibt nichts in die DB.
"""
from decimal import Decimal

import pandas as pd
import pytest

from app.extensions import db
from app.import_csv.routes import _run_import, _detect_stand_columns
from app.models import (
    Customer, MeterReading, Property, PropertyOwnership, WaterMeter,
)

# Spalten-Zuordnung: CSV-Spaltenname je Zielfeld. Nicht in jedem DataFrame
# vorhandene Spalten liefern in _get_cell einfach "" – das ist gewollt.
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
    stand_columns = _detect_stand_columns(df.columns.tolist())
    return _run_import(df, COLS, stand_columns, mode, dry_run=dry_run)


def _customer(cnum):
    return Customer.query.filter_by(customer_number=cnum).first()


def _object_numbers(customer):
    """Aktive Objekt-Nummern eines Kunden."""
    return sorted(o.property.object_number for o in customer.ownerships.all())


# ---------------------------------------------------------------------------
# Grund-Import
# ---------------------------------------------------------------------------

class TestBasicImport:
    def test_creates_customer_property_meter(self, app):
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        assert res["customers_created"] == 1
        assert res["properties_created"] == 1
        assert res["ownerships_created"] == 1
        assert res["meters_created"] == 1
        assert Customer.query.count() == 1
        assert Property.query.count() == 1
        assert WaterMeter.query.count() == 1

        c = _customer(100)
        assert c.name == "Huber"
        assert _object_numbers(c) == ["Haus A"]
        assert res["plan"][0]["category"] == "import"

    def test_imports_readings_with_consumption(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2023": "100", "Stand 2024": "175"},
        ])
        meter = WaterMeter.query.filter_by(meter_number="Z1").first()
        readings = {r.year: r for r in meter.readings.all()}
        assert readings[2023].value == Decimal("100")
        assert readings[2023].consumption is None
        assert readings[2024].value == Decimal("175")
        assert readings[2024].consumption == Decimal("75")


# ---------------------------------------------------------------------------
# Mehrere Objekte / Zaehler innerhalb derselben CSV
# ---------------------------------------------------------------------------

class TestMultipleWithinOneFile:
    def test_customer_twice_gets_two_objects(self, app):
        """Derselbe Kunde in zwei Zeilen → ein Kunde, zwei Objekte."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus B",
             "Zählernummer": "Z2"},
        ])
        assert Customer.query.count() == 1
        assert res["customers_created"] == 1
        assert res["properties_created"] == 2
        assert res["ownerships_created"] == 2
        assert _object_numbers(_customer(100)) == ["Haus A", "Haus B"]
        assert [p["category"] for p in res["plan"]] == ["import", "import"]

    def test_same_object_two_meters(self, app):
        """Selber Kunde, selbes Objekt, zwei Zeilen mit verschiedenen Zaehlern."""
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        assert Customer.query.count() == 1
        assert Property.query.count() == 1
        assert res["properties_created"] == 1
        assert res["properties_reused"] == 1
        # Zweite Zeile legt keine zweite Zuordnung an.
        assert res["ownerships_created"] == 1
        prop = Property.query.filter_by(object_number="Haus A").first()
        assert sorted(m.meter_number for m in prop.meters.all()) == ["Z1", "Z2"]


# ---------------------------------------------------------------------------
# Nachtrag gegen bereits in der DB existierende Daten (zweiter Import)
# ---------------------------------------------------------------------------

class TestAddObjectToExistingCustomer:
    """Kunde existiert bereits in der DB – ein zweiter Import ergaenzt ein Objekt."""

    def test_new_object_added_to_existing_customer(self, app):
        # Erstimport
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        assert Customer.query.count() == 1

        # Zweitimport: SELBER Kunde, NEUES Objekt
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus B",
             "Zählernummer": "Z2"},
        ])
        # Kein neuer Kunde, aber ein neues Objekt + Zuordnung + Zaehler.
        assert Customer.query.count() == 1
        assert res["customers_created"] == 0
        assert res["properties_created"] == 1
        assert res["ownerships_created"] == 1
        assert res["meters_created"] == 1
        assert _object_numbers(_customer(100)) == ["Haus A", "Haus B"]
        assert res["plan"][0]["category"] == "import"

    def test_works_in_overwrite_mode_too(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus B",
             "Zählernummer": "Z2"},
        ], mode="overwrite")
        assert Customer.query.count() == 1
        assert _object_numbers(_customer(100)) == ["Haus A", "Haus B"]


class TestAddMeterToExistingObject:
    """Kunde UND Objekt existieren bereits – ein zweiter Import ergaenzt einen Zaehler."""

    def test_new_meter_added_to_existing_object(self, app):
        # Erstimport: Kunde 100, Objekt Haus A, Zaehler Z1
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])

        # Zweitimport: SELBER Kunde, SELBES Objekt, NEUER Zaehler
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z2"},
        ])
        assert Customer.query.count() == 1
        assert Property.query.count() == 1
        assert res["customers_created"] == 0
        assert res["properties_created"] == 0
        assert res["properties_reused"] == 1
        # Objekt schon dem Kunden zugeordnet → keine neue Zuordnung.
        assert res["ownerships_created"] == 0
        assert res["meters_created"] == 1
        assert res["plan"][0]["category"] == "import"

        prop = Property.query.filter_by(object_number="Haus A").first()
        assert sorted(m.meter_number for m in prop.meters.all()) == ["Z1", "Z2"]
        # Der neue Zaehler haengt am bestehenden Objekt.
        z2 = WaterMeter.query.filter_by(meter_number="Z2").first()
        assert z2.property_id == prop.id

    def test_new_meter_with_readings(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "50"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z2", "Stand 2024": "30"},
        ])
        assert res["meters_created"] == 1
        assert res["readings_created"] == 1
        z2 = WaterMeter.query.filter_by(meter_number="Z2").first()
        assert z2.readings.first().value == Decimal("30")


# ---------------------------------------------------------------------------
# Re-Import ohne Aenderung
# ---------------------------------------------------------------------------

class TestReimportUnchanged:
    def test_identical_reimport_changes_nothing(self, app):
        rows = [
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "50"},
        ]
        _run(rows)
        res = _run(rows)  # exakt dieselbe Datei nochmal

        assert res["customers_created"] == 0
        assert res["properties_created"] == 0
        assert res["ownerships_created"] == 0
        assert res["meters_created"] == 0
        assert res["readings_created"] == 0
        assert res["rows_unchanged"] == 1
        assert res["plan"][0]["category"] == "exists"
        # Keine Duplikate entstanden.
        assert Customer.query.count() == 1
        assert Property.query.count() == 1
        assert WaterMeter.query.count() == 1
        assert MeterReading.query.count() == 1


# ---------------------------------------------------------------------------
# duplicate_mode: skip vs. overwrite
# ---------------------------------------------------------------------------

class TestDuplicateMode:
    def test_skip_keeps_existing_customer_data(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "E-Mail": "alt@example.at"},
        ])
        # Zweitimport mit geaenderten Stammdaten, Modus skip
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber NEU", "Objekt": "Haus A",
             "Zählernummer": "Z1", "E-Mail": "neu@example.at"},
        ], mode="skip")
        assert res["customers_updated"] == 0
        c = _customer(100)
        assert c.name == "Huber"
        assert c.email == "alt@example.at"

    def test_overwrite_updates_existing_customer_data(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "E-Mail": "alt@example.at"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber NEU", "Objekt": "Haus A",
             "Zählernummer": "Z1", "E-Mail": "neu@example.at"},
        ], mode="overwrite")
        assert res["customers_updated"] == 1
        c = _customer(100)
        assert c.name == "Huber NEU"
        assert c.email == "neu@example.at"

    def test_skip_keeps_existing_reading(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "100"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "999"},
        ], mode="skip")
        assert res["readings_updated"] == 0
        reading = MeterReading.query.filter_by(year=2024).first()
        assert reading.value == Decimal("100")

    def test_overwrite_updates_existing_reading(self, app):
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "100"},
        ])
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "999"},
        ], mode="overwrite")
        assert res["readings_updated"] == 1
        reading = MeterReading.query.filter_by(year=2024).first()
        assert reading.value == Decimal("999")


# ---------------------------------------------------------------------------
# Vorschau (dry_run)
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_writes_nothing(self, app):
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2024": "50"},
        ], dry_run=True)
        # Statistik wird berechnet …
        assert res["customers_created"] == 1
        assert res["meters_created"] == 1
        # … aber nichts landet in der DB.
        assert Customer.query.count() == 0
        assert Property.query.count() == 0
        assert WaterMeter.query.count() == 0
        assert MeterReading.query.count() == 0

    def test_dry_run_matches_real_import(self, app):
        rows = [
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus B",
             "Zählernummer": "Z2"},
            {"Kunden-Nr.": "", "Name": "leer"},
        ]
        preview = _run(rows, dry_run=True)
        real = _run(rows, dry_run=False)
        for key in ("customers_created", "properties_created",
                    "ownerships_created", "meters_created", "rows_skipped"):
            assert preview[key] == real[key]
        assert [p["category"] for p in preview["plan"]] == \
               [p["category"] for p in real["plan"]]

    def test_preview_classifies_existing_customer_object_add(self, app):
        # Kunde existiert bereits in der DB.
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        # Vorschau eines Imports, der ein Objekt ergaenzt → Kategorie "import".
        preview = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus B",
             "Zählernummer": "Z2"},
        ], dry_run=True)
        assert preview["plan"][0]["category"] == "import"
        assert preview["properties_created"] == 1
        # Nichts geschrieben.
        assert Property.query.count() == 1


# ---------------------------------------------------------------------------
# Uebersprungene Zeilen
# ---------------------------------------------------------------------------

class TestSkippedRows:
    def test_empty_customer_number_skipped(self, app):
        res = _run([{"Kunden-Nr.": "", "Name": "leer"}])
        assert res["rows_skipped"] == 1
        assert res["plan"][0]["category"] == "skip"
        assert Customer.query.count() == 0

    def test_ergebnis_row_skipped(self, app):
        res = _run([
            {"Kunden-Nr.": "Ergebnis", "Name": "", "Objekt": "", "Zählernummer": ""},
        ])
        assert res["rows_skipped"] == 1
        assert res["plan"][0]["category"] == "skip"

    def test_storno_row_skipped(self, app):
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Kommentar": "Storno wegen Umzug"},
        ])
        assert res["rows_skipped"] == 1
        assert res["plan"][0]["category"] == "skip"
        assert Customer.query.count() == 0

    def test_invalid_customer_number_reported_as_error(self, app):
        res = _run([
            {"Kunden-Nr.": "abc", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1"},
        ])
        assert res["rows_skipped"] == 1
        assert len(res["errors"]) == 1
        assert res["plan"][0]["category"] == "skip"
        assert res["plan"][0]["label"] == "Fehler"

    def test_missing_meter_number_imports_customer_without_meter(self, app):
        res = _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": ""},
        ])
        # Kunde + Objekt entstehen, nur der Zaehler fehlt.
        assert res["customers_created"] == 1
        assert res["properties_created"] == 1
        assert res["meters_created"] == 0
        assert len(res["warnings"]) == 1
        assert res["plan"][0]["category"] == "import"
        assert WaterMeter.query.count() == 0
