"""Integration-Tests fuer den Stammdaten-Import (app/import_csv).

Schwerpunkt: das additive Verhalten von ``_run_import``.

- Ein Kunde, der mehrfach in der CSV vorkommt, sammelt mehrere Objekte/Zaehler.
- Ein zweiter Import gegen einen *bereits in der DB existierenden* Kunden
  ergaenzt neue Objekte/Zaehler, statt die Zeile zu verwerfen.
- ``duplicate_mode`` steuert nur, ob bereits vorhandene Stammdaten/Ablesungen
  aktualisiert werden – neue Datensaetze werden in beiden Modi ergaenzt.
- ``dry_run=True`` (Vorschau) schreibt nichts in die DB.
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.extensions import db
from app.import_csv.routes import _run_import
from app.models import (
    BillingPeriod, Customer, MeterReading, Property, PropertyOwnership,
    WaterMeter,
)


@pytest.fixture(autouse=True)
def _periods(app):
    """Abrechnungsperioden 2022–2025 — der Stammdaten-Import ordnet
    'Stand YYYY'-Spalten ueber den Datumsbereich einer Periode zu."""
    for y in (2022, 2023, 2024, 2025):
        db.session.add(BillingPeriod(
            name=str(y), start_date=date(y, 1, 1), end_date=date(y, 12, 31),
            active=(y == 2025),
        ))
    db.session.commit()


def _period(year):
    return BillingPeriod.query.filter_by(name=str(year)).first()

# Spalten-Zuordnung: CSV-Spaltenname je Zielfeld. Nicht in jedem DataFrame
# vorhandene Spalten liefern in _get_cell einfach "" – das ist gewollt.
COLS = {
    "customer_number": "Kunden-Nr.",
    "customer_name": "Name",
    "property_name": "Objekt",
    "property_type": "Typ",
    "meter_number": "Zählernummer",
    "meter_eichjahr": "Eichjahr",
    # Genau EIN Stand je Zähler wird importiert; in den Tests die '2024'-Spalte.
    "meter_reading": "Stand 2024",
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

    def test_imports_only_selected_reading_as_baseline(self, app):
        """Nur der gewählte (jüngste) Stand wird importiert – Vorjahre nicht.
        Der Stand ist ein reiner Anfangsstand ohne fiktiven Verbrauch."""
        _run([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Stand 2023": "100", "Stand 2024": "175"},
        ])
        meter = WaterMeter.query.filter_by(meter_number="Z1").first()
        readings = {r.billing_period.name: r for r in meter.readings.all()}
        # Die 'Stand 2023'-Spalte (Vorjahr) wird ignoriert.
        assert set(readings.keys()) == {"2024"}
        assert readings["2024"].value == Decimal("175")
        # Anfangsstand → kein über einen evtl. Tausch hinweg berechneter Verbrauch.
        assert readings["2024"].consumption is None

    def test_custom_reading_column_lands_in_active_period(self, app):
        """Eine Nicht-'Stand YYYY'-Spalte landet in der aktiven Periode (2025)."""
        cols = dict(COLS, meter_reading="Letzter Stand")
        df = pd.DataFrame([
            {"Kunden-Nr.": "100", "Name": "Huber", "Objekt": "Haus A",
             "Zählernummer": "Z1", "Letzter Stand": "250"},
        ]).fillna("")
        _run_import(df, cols, "skip", dry_run=False)
        meter = WaterMeter.query.filter_by(meter_number="Z1").first()
        reading = meter.readings.first()
        assert reading.value == Decimal("250")
        assert reading.billing_period.name == "2025"   # aktive Periode (Fixture)
        assert reading.consumption is None


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
        reading = MeterReading.query.filter_by(
            billing_period_id=_period(2024).id).first()
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
        reading = MeterReading.query.filter_by(
            billing_period_id=_period(2024).id).first()
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


# ---------------------------------------------------------------------------
# Name-Aufspaltung + Firma/Person (oss-v1.23.0)
# ---------------------------------------------------------------------------

# Erweiterte Spalten-Zuordnung inkl. Anrede / Nachname / Vorname / Firma.
COLS_NAME = dict(
    COLS,
    customer_name="Name",
    name_last="Nachname",
    name_first="Vorname",
    salutation="Anrede",
    is_company="Firma",
)


def _run_named(rows, mode="skip"):
    df = pd.DataFrame(rows).fillna("")
    return _run_import(df, COLS_NAME, mode, dry_run=False)


class TestNameSplitAndCompany:
    def test_split_columns_are_stored(self, app):
        _run_named([
            {"Kunden-Nr.": "100", "Anrede": "Herr", "Nachname": "Mustermann",
             "Vorname": "Max", "Objekt": "Haus A", "Zählernummer": "Z1"},
        ])
        c = _customer(100)
        assert c.last_name == "Mustermann"
        assert c.first_name == "Max"
        assert c.salutation == "Herr"
        assert c.name == "Mustermann Max"          # kombiniert "Nachname Vorname"
        assert c.is_company is False
        assert c.letter_name == "Max Mustermann"
        assert c.salutation_line == "Sehr geehrter Herr Mustermann"

    def test_combined_only_goes_to_last_name(self, app):
        # Nur die "Name"-Spalte gemappt (kein Nachname/Vorname in der Datei).
        cols = dict(COLS, customer_name="Name")
        df = pd.DataFrame([
            {"Kunden-Nr.": "101", "Name": "Nur Kombiniert", "Objekt": "Haus B",
             "Zählernummer": "Z2"},
        ]).fillna("")
        _run_import(df, cols, "skip", dry_run=False)
        c = _customer(101)
        assert c.name == "Nur Kombiniert"
        assert c.last_name == "Nur Kombiniert"
        assert c.first_name is None
        assert c.is_company is False

    def test_company_via_firma_column(self, app):
        _run_named([
            {"Kunden-Nr.": "102", "Name": "Wasser GmbH", "Firma": "Firma",
             "Objekt": "Haus C", "Zählernummer": "Z3"},
        ])
        c = _customer(102)
        assert c.is_company is True
        assert c.name == "Wasser GmbH"
        assert c.first_name is None and c.last_name is None and c.salutation is None
        assert c.letter_name == "Wasser GmbH"

    def test_overwrite_updates_split_and_company(self, app):
        _run_named([
            {"Kunden-Nr.": "103", "Nachname": "Alt", "Vorname": "Anna",
             "Objekt": "Haus D", "Zählernummer": "Z4"},
        ])
        c = _customer(103)
        assert c.last_name == "Alt"

        _run_named([
            {"Kunden-Nr.": "103", "Name": "Neu GmbH", "Firma": "Firma",
             "Objekt": "Haus D", "Zählernummer": "Z4"},
        ], mode="overwrite")
        db.session.refresh(c)
        assert c.is_company is True
        assert c.name == "Neu GmbH"
        assert c.last_name is None and c.first_name is None


# ---------------------------------------------------------------------------
# Straße / Hausnummer (oss-v1.23.0)
# ---------------------------------------------------------------------------

class TestAddressSplit:
    def test_combined_street_number_split(self, app):
        cols = dict(COLS, strasse="Straße")
        df = pd.DataFrame([
            {"Kunden-Nr.": "200", "Name": "Huber", "Straße": "Hauptstraße 7b",
             "Objekt": "Haus A", "Zählernummer": "Z1"},
        ]).fillna("")
        _run_import(df, cols, "skip", dry_run=False)
        c = _customer(200)
        assert c.strasse == "Hauptstraße"
        assert c.hausnummer == "7b"

    def test_separate_house_number_column(self, app):
        cols = dict(COLS, strasse="Straße", hausnummer="Hausnummer")
        df = pd.DataFrame([
            {"Kunden-Nr.": "201", "Name": "Maier", "Straße": "Hauptstraße",
             "Hausnummer": "5", "Objekt": "Haus B", "Zählernummer": "Z2"},
        ]).fillna("")
        _run_import(df, cols, "skip", dry_run=False)
        c = _customer(201)
        assert c.strasse == "Hauptstraße"
        assert c.hausnummer == "5"
