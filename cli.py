"""
CLI-Befehle für die Anwendung.
Verwendung:
  flask --app run init-db
  flask --app run create-admin
"""

import re as _re

import sqlalchemy as sa


# Spalten, die per ALTER TABLE ADD COLUMN nachgezogen werden muessen.
# Reihenfolge wird respektiert (FKs koennen auf vorher ergaenzte Spalten zeigen).
# Jeder Eintrag: (table, col_def, col_name)
_SCHEMA_UPGRADE_COLUMNS = [
    ("water_meters",  "installed_from DATE",                                          "installed_from"),
    ("water_meters",  "installed_to DATE",                                            "installed_to"),
    ("water_meters",  "initial_value NUMERIC(12,3)",                                  "initial_value"),
    ("bookings",      "open_item_id INTEGER REFERENCES open_items(id)",               "open_item_id"),
    ("bookings",      "project_id INTEGER REFERENCES projects(id)",                   "project_id"),
    ("bookings",      "status VARCHAR(20) NOT NULL DEFAULT 'Offen'",                  "status"),
    ("bookings",      "storno_of_id INTEGER REFERENCES bookings(id)",                 "storno_of_id"),
    ("bookings",      "storno_reason VARCHAR(500)",                                   "storno_reason"),
    ("bookings",      "storno_date DATE",                                             "storno_date"),
    ("invoice_items", "tax_rate NUMERIC(5,2)",                                        "tax_rate"),
    ("water_tariffs", "base_fee_label VARCHAR(100) DEFAULT 'Grundgebühr'",            "base_fee_label"),
    ("water_tariffs", "additional_fee NUMERIC(10,2) DEFAULT 0",                       "additional_fee"),
    ("water_tariffs", "additional_fee_label VARCHAR(100) DEFAULT 'Zusatzgebühr'",     "additional_fee_label"),
    ("customers",     "base_fee_override NUMERIC(10,2)",                              "base_fee_override"),
    ("customers",     "additional_fee_override NUMERIC(10,2)",                        "additional_fee_override"),
    ("properties",    "base_fee_override NUMERIC(10,2)",                              "base_fee_override"),
    ("properties",    "additional_fee_override NUMERIC(10,2)",                        "additional_fee_override"),
    ("open_items",    "period_year INTEGER",                                          "period_year"),
    ("water_meters",  "eichjahr INTEGER",                                             "eichjahr"),
    ("customers",     "customer_number INTEGER",                                      "customer_number"),
    ("customers",     "externe_kennung VARCHAR(100)",                                 "externe_kennung"),
    ("bookings",      "real_account_id INTEGER REFERENCES real_accounts(id)",         "real_account_id"),
    ("bookings",      "tax_rate NUMERIC(5,2)",                                        "tax_rate"),
    ("bookings",      "customer_id INTEGER REFERENCES customers(id)",                 "customer_id"),
    ("projects",      "color VARCHAR(20) DEFAULT '#3498db'",                          "color"),
    ("real_accounts", "icon VARCHAR(50) DEFAULT 'fa-university'",                     "icon"),
    ("real_accounts", "is_default INTEGER NOT NULL DEFAULT 0",                        "is_default"),
    ("customers",     "rechnung_per_email INTEGER NOT NULL DEFAULT 0",                "rechnung_per_email"),
    ("accounts",      "code VARCHAR(3)",                                              "code"),
    ("projects",      "code VARCHAR(3)",                                              "code"),
    ("invoices",      "doc_path VARCHAR(500)",                                        "doc_path"),
    ("invoices",      "billing_run_id INTEGER REFERENCES billing_runs(id)",           "billing_run_id"),
    ("fiscal_years",  "is_vat_liable INTEGER NOT NULL DEFAULT 0",                     "is_vat_liable"),
    ("open_items",    "account_id INTEGER REFERENCES accounts(id)",                   "account_id"),
    ("billing_runs",  "account_id INTEGER REFERENCES accounts(id)",                   "account_id"),
    # ADR-002: Sammelbuchung. booking_groups kommt ueber db.create_all();
    # hier nur die ALTER-Spalten auf bestehenden Tabellen ergaenzen.
    ("bookings",      "group_id INTEGER REFERENCES booking_groups(id)",               "group_id"),
    ("invoice_items", "account_id INTEGER REFERENCES accounts(id)",                   "account_id"),
    ("invoice_items", "project_id INTEGER REFERENCES projects(id)",                   "project_id"),
    # ADR-003: Mahnwesen. dunning_* kommen ueber db.create_all();
    # hier nur die ALTER-Spalten auf bestehenden Tabellen ergaenzen.
    ("invoice_items", "is_dunning_fee INTEGER NOT NULL DEFAULT 0",                    "is_dunning_fee"),
    ("invoice_items", "dunning_notice_id INTEGER REFERENCES dunning_notices(id)",     "dunning_notice_id"),
    # Kontakttypen: ein Kontakt kann gleichzeitig Kunde und/oder Lieferant sein.
    # customer_counters kommt ueber db.create_all().
    ("customers",     "is_customer INTEGER NOT NULL DEFAULT 1",                       "is_customer"),
    ("customers",     "is_supplier INTEGER NOT NULL DEFAULT 0",                       "is_supplier"),
]


def apply_schema_upgrades(conn, dialect, *, verbose=False, schema=None):
    """Idempotent fehlende Spalten per ALTER TABLE ADD COLUMN ergaenzen.

    WICHTIG: Inspektion und DDL laufen ueber dieselbe Connection, damit
    in Postgres der gesetzte search_path (fuer Multi-Tenant-Schemas) auch
    die Spaltenabfrage steuert. Rein engine-basierte Inspektion wuerde
    einen frischen Pool-Checkout ausloesen — in der SaaS-Schicht reicht der
    'reset_on_checkout'-Listener dann den search_path auf public zurueck,
    und Tabellen des tenant-Schemas waeren unsichtbar.

    Multi-Tenant: Wer in einem Tenant-Schema arbeitet, muss ``schema`` explizit
    setzen. SQLAlchemy 2.x cached den ``default_schema_name`` beim ersten Connect
    der Engine — der ist dann 'public', auch wenn die Connection danach ihren
    search_path auf 'tenant_xyz, public' setzt. ``inspector.get_columns(...)``
    ohne expliziten ``schema`` sucht dann im falschen Schema und liefert
    NoSuchTableError. Mit ``schema=tenant_xyz`` wird das umgangen.

    NoSuchTableError wird auch sonst toleriert, damit alte _SCHEMA_UPGRADE_COLUMNS-
    Eintraege auf inzwischen entfernte Tabellen den Lauf nicht crashen.
    """
    inspector = sa.inspect(conn)

    def _add(table, col_def, col_name):
        try:
            cols = [c["name"] for c in inspector.get_columns(table, schema=schema)]
        except sa.exc.NoSuchTableError:
            if verbose:
                print(f"  ⏭ {table}: Tabelle nicht im Schema — uebersprungen.")
            return
        if col_name in cols:
            if verbose:
                print(f"  ✓ {table}.{col_name} bereits vorhanden.")
            return
        # MariaDB/MySQL unterstuetzt kein inline REFERENCES in ALTER TABLE ADD COLUMN
        effective_def = col_def
        if dialect in ("mysql", "mariadb"):
            effective_def = _re.sub(r"\s+REFERENCES\s+\S+", "", col_def, flags=_re.IGNORECASE)
        # ALTER TABLE bleibt unqualifiziert — search_path entscheidet, in welchem
        # Schema die DDL landet. Inspector ist die einzige Stelle, die ein
        # explizites schema= braucht.
        conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {effective_def}"))
        if verbose:
            print(f"  + {table}.{col_name} hinzugefuegt.")

    for table, col_def, col_name in _SCHEMA_UPGRADE_COLUMNS:
        _add(table, col_def, col_name)


def seed_default_tax_rates(db, *, verbose=False):
    """Die vier Standard-Steuersaetze idempotent anlegen."""
    from decimal import Decimal
    from app.models import TaxRate

    default_rates = [
        (0,  "0 % – keine MwSt"),
        (10, "10 %"),
        (13, "13 %"),
        (20, "20 %"),
    ]
    for rate_val, label in default_rates:
        if not TaxRate.query.filter_by(rate=Decimal(str(rate_val))).first():
            db.session.add(TaxRate(rate=Decimal(str(rate_val)), label=label))
            if verbose:
                print(f"  + Steuersatz {rate_val}% angelegt.")
        elif verbose:
            print(f"  ✓ Steuersatz {rate_val}% bereits vorhanden.")
    db.session.commit()


def seed_default_dunning_policy(db, *, verbose=False):
    """Die Standard-Mahnvorlage mit 4 Stufen idempotent anlegen (ADR-003)."""
    from decimal import Decimal
    from app.models import DunningPolicy, DunningStage

    if DunningPolicy.query.first():
        if verbose:
            print("  ✓ Mahnvorlage bereits vorhanden.")
        return

    policy = DunningPolicy(name="Standard", description="Standard-Mahnvorlage", is_default=True)
    db.session.add(policy)
    db.session.flush()
    stages = [
        DunningStage(policy_id=policy.id, level=1, name="Freundliche Zahlungserinnerung",
                     days_after_due=14, fee_fixed=Decimal("0.00"), new_due_days=14,
                     print_title="Zahlungserinnerung", color="blue", icon="fa-envelope"),
        DunningStage(policy_id=policy.id, level=2, name="Zahlungserinnerung",
                     days_after_due=30, fee_fixed=Decimal("0.00"), new_due_days=14,
                     print_title="Zahlungserinnerung", color="orange", icon="fa-exclamation-circle"),
        DunningStage(policy_id=policy.id, level=3, name="1. Mahnung",
                     days_after_due=45, fee_fixed=Decimal("5.00"), new_due_days=14,
                     print_title="1. Mahnung", color="red", icon="fa-exclamation-triangle"),
        DunningStage(policy_id=policy.id, level=4, name="2. Mahnung (letzte)",
                     days_after_due=60, fee_fixed=Decimal("10.00"), new_due_days=7,
                     print_title="Letzte Mahnung", color="pink", icon="fa-gavel"),
    ]
    db.session.add_all(stages)
    db.session.commit()
    if verbose:
        print("  + Standard-Mahnvorlage mit 4 Stufen angelegt.")


def run_data_migrations(db, *, verbose=False):
    """Datenmigrationen fuer bestehende DBs (Kundennummern, OpenItems aus versendeten Rechnungen).

    Alle Aenderungen laufen in EINER Transaktion (ein Commit am Ende). Das ist
    fuer die SaaS-Schicht wichtig: jeder Commit gibt die Connection in den Pool
    zurueck, und der Pool-Checkout-Listener setzt den search_path wieder auf
    public — eine Folge-Query landete sonst im falschen Schema.
    """
    from sqlalchemy import func
    from app.models import Customer, Invoice, OpenItem

    # Kundennummern fuer Altbestand vergeben
    kunden_ohne_nr = (
        Customer.query
        .filter(Customer.customer_number == None)  # noqa: E711
        .order_by(Customer.id)
        .all()
    )
    if kunden_ohne_nr:
        max_nr = db.session.query(func.max(Customer.customer_number)).scalar() or 0
        for kunde in kunden_ohne_nr:
            max_nr += 1
            kunde.customer_number = max_nr

    # Fuer versendete Rechnungen ohne OpenItem einen anlegen
    sent_no_oi = (
        Invoice.query
        .filter(Invoice.status == Invoice.STATUS_SENT)
        .filter(~Invoice.open_item.has())
        .all()
    )
    for inv in sent_no_oi:
        oi = OpenItem(
            customer_id=inv.customer_id,
            description=inv.invoice_number,
            amount=inv.total_amount,
            date=inv.date,
            due_date=inv.due_date,
            period_year=inv.period_year,
            status=OpenItem.STATUS_OPEN,
            invoice_id=inv.id,
        )
        db.session.add(oi)

    if kunden_ohne_nr or sent_no_oi:
        db.session.commit()

    if verbose:
        if kunden_ohne_nr:
            print(f"  {len(kunden_ohne_nr)} Kunden mit Kundennummern versehen.")
        if sent_no_oi:
            print(f"  {len(sent_no_oi)} Rechnungen -> OpenItem migriert.")


def register_commands(app):
    from app.extensions import db
    from app.models import User, Account, TaxRate, DunningPolicy, DunningStage

    @app.cli.command("init-db")
    def init_db():
        """Datenbanktabellen erstellen und Standard-Konten anlegen."""
        db.create_all()
        print("Datenbanktabellen erstellt.")

        # Fehlende Spalten ergaenzen (SQLite unterstuetzt kein ALTER COLUMN,
        # aber ADD COLUMN ist moeglich – sicher fuer Re-Runs).
        with db.engine.begin() as conn:
            apply_schema_upgrades(conn, db.engine.dialect.name)

        seed_default_tax_rates(db)
        seed_default_dunning_policy(db)

    @app.cli.command("upgrade-db")
    def upgrade_db():
        """Fehlende Spalten in bestehender Datenbank ergaenzen (fuer Updates)."""
        db.create_all()

        with db.engine.begin() as conn:
            apply_schema_upgrades(conn, db.engine.dialect.name, verbose=True)

        seed_default_tax_rates(db, verbose=True)
        seed_default_dunning_policy(db, verbose=True)
        run_data_migrations(db, verbose=True)

        print("Datenbank aktualisiert.")

    @app.cli.command("seed-testdata")
    def seed_testdata():
        """Testdaten für alle Tabellen einfügen (nur wenn DB leer ist)."""
        from datetime import date
        from decimal import Decimal
        from app.models import (
            User, Customer, Property, PropertyOwnership,
            WaterMeter, MeterReading, WaterTariff,
            Invoice, InvoiceItem, Account, Booking, OpenItem,
        )

        if Customer.query.count() > 0:
            print("Testdaten bereits vorhanden – abgebrochen.")
            return

        db.create_all()

        # ------------------------------------------------------------------
        # Benutzer
        # ------------------------------------------------------------------
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin", email="admin@wassergenossenschaft.at", role="admin")
            admin.set_password("admin123")
            db.session.add(admin)
        if not User.query.filter_by(username="sachbearbeiter").first():
            user1 = User(username="sachbearbeiter", email="sachbearbeiter@wassergenossenschaft.at", role="user")
            user1.set_password("user123")
            db.session.add(user1)
        db.session.flush()
        # admin neu laden falls bereits vorhanden
        admin = User.query.filter_by(username="admin").first()

        # ------------------------------------------------------------------
        # Konten sicherstellen
        # ------------------------------------------------------------------
        if Account.query.count() == 0:
            default_accounts = [
                Account(name="Wassergebühren", description="Jährliche Wasserabrechnung"),
                Account(name="Sonstige Einnahmen", description=""),
                Account(name="Wartung und Reparatur", description=""),
                Account(name="Strom (Pumpen)", description=""),
                Account(name="Versicherung", description=""),
                Account(name="Verwaltung", description=""),
                Account(name="Sonstige Ausgaben", description=""),
            ]
            db.session.add_all(default_accounts)
        db.session.flush()
        income_account = Account.query.filter_by(name="Wassergebühren").first()
        expense_account_wartung = Account.query.filter_by(name="Wartung und Reparatur").first()
        expense_account_strom = Account.query.filter_by(name="Strom (Pumpen)").first()

        # ------------------------------------------------------------------
        # Tarife
        # ------------------------------------------------------------------
        tarif2021 = WaterTariff(
            name="Tarif 2021",
            valid_from=2021, valid_to=2021,
            base_fee=Decimal("28.00"), price_per_m3=Decimal("1.10"),
        )
        tarif2022 = WaterTariff(
            name="Tarif 2022",
            valid_from=2022, valid_to=2023,
            base_fee=Decimal("30.00"), price_per_m3=Decimal("1.20"),
        )
        tarif2024 = WaterTariff(
            name="Tarif 2024",
            valid_from=2024, valid_to=None,
            base_fee=Decimal("35.00"), price_per_m3=Decimal("1.45"),
            notes="Preisanpassung wegen gestiegener Betriebskosten",
        )
        db.session.add_all([tarif2021, tarif2022, tarif2024])
        db.session.flush()

        # ------------------------------------------------------------------
        # Kunden
        # ------------------------------------------------------------------
        kunden_daten = [
            dict(name="Franz Huber", strasse="Dorfstraße", hausnummer="4",
                 plz="4232", ort="Hagenberg", email="f.huber@example.at",
                 phone="07236 12345", member_since=date(2010, 3, 15)),
            dict(name="Maria Gruber", strasse="Hauptstraße", hausnummer="12",
                 plz="4232", ort="Hagenberg", email="m.gruber@example.at",
                 phone="07236 23456", member_since=date(2012, 6, 1)),
            dict(name="Johann Mayr", strasse="Birkenweg", hausnummer="3a",
                 plz="4233", ort="Katsdorf", email="j.mayr@example.at",
                 member_since=date(2015, 1, 20)),
            dict(name="Anna Leitner", strasse="Gartenstraße", hausnummer="8",
                 plz="4232", ort="Hagenberg", email="a.leitner@example.at",
                 phone="0664 9876543", member_since=date(2018, 9, 10)),
            dict(name="Klaus Steinbauer", strasse="Wiesenweg", hausnummer="2",
                 plz="4233", ort="Katsdorf", member_since=date(2020, 4, 5)),
            dict(name="Elisabeth Weidinger", strasse="Am Bach", hausnummer="1",
                 plz="4232", ort="Hagenberg", email="e.weidinger@example.at",
                 phone="0699 11223344", member_since=date(2008, 11, 30),
                 notes="Langjähriges Mitglied, Zahlungseingang immer pünktlich"),
        ]
        kunden = []
        for i, d in enumerate(kunden_daten, start=1):
            k = Customer(customer_number=i, **d)
            db.session.add(k)
            kunden.append(k)
        db.session.flush()

        # ------------------------------------------------------------------
        # Objekte (Liegenschaften)
        # ------------------------------------------------------------------
        objekte_daten = [
            dict(object_number="OBJ-001", object_type="Haus",
                 strasse="Dorfstraße", hausnummer="4", plz="4232", ort="Hagenberg"),
            dict(object_number="OBJ-002", object_type="Haus",
                 strasse="Hauptstraße", hausnummer="12", plz="4232", ort="Hagenberg"),
            dict(object_number="OBJ-003", object_type="Haus",
                 strasse="Birkenweg", hausnummer="3a", plz="4233", ort="Katsdorf"),
            dict(object_number="OBJ-004", object_type="Garten",
                 strasse="Gartenstraße", hausnummer="8", plz="4232", ort="Hagenberg",
                 notes="Kleingarten, Saisonbetrieb"),
            dict(object_number="OBJ-005", object_type="Haus",
                 strasse="Wiesenweg", hausnummer="2", plz="4233", ort="Katsdorf"),
            dict(object_number="OBJ-006", object_type="Haus",
                 strasse="Am Bach", hausnummer="1", plz="4232", ort="Hagenberg"),
        ]
        objekte = []
        for d in objekte_daten:
            p = Property(**d)
            db.session.add(p)
            objekte.append(p)
        db.session.flush()

        # ------------------------------------------------------------------
        # Eigentümerverhältnisse
        # ------------------------------------------------------------------
        for kunde, objekt in zip(kunden, objekte):
            po = PropertyOwnership(
                property_id=objekt.id,
                customer_id=kunde.id,
                valid_from=kunde.member_since,
            )
            db.session.add(po)
        db.session.flush()

        # ------------------------------------------------------------------
        # Wasserzähler + Ablesungen
        # ------------------------------------------------------------------
        zähler_daten = [
            dict(meter_number="Dorfstraße 4",    location="Keller", installed_from=date(2010, 3, 15), initial_value=Decimal("0.000")),
            dict(meter_number="Hauptstraße 12",  location="Keller", installed_from=date(2012, 6, 1),  initial_value=Decimal("0.000")),
            dict(meter_number="Birkenweg 3a",    location="Außen",  installed_from=date(2015, 1, 20), initial_value=Decimal("0.000")),
            dict(meter_number="Gartenstraße 8",  location="Außen",  installed_from=date(2018, 9, 10), initial_value=Decimal("0.000")),
            dict(meter_number="Wiesenweg 2",     location="Keller", installed_from=date(2020, 4, 5),  initial_value=Decimal("0.000")),
            dict(meter_number="Am Bach 1",       location="Keller", installed_from=date(2008, 11, 30),initial_value=Decimal("0.000")),
        ]
        # Jahresstände pro Zähler (Anfangsstand + jährlicher Verbrauch), letzte 5 Jahre
        jahres_ablesungen = [
            [(2021, Decimal("97.500")),  (2022, Decimal("125.000")), (2023, Decimal("152.500")), (2024, Decimal("181.000")), (2025, Decimal("208.000"))],
            [(2021, Decimal("278.000")), (2022, Decimal("310.000")), (2023, Decimal("342.000")), (2024, Decimal("375.000")), (2025, Decimal("411.500"))],
            [(2021, Decimal("70.000")),  (2022, Decimal("89.000")),  (2023, Decimal("108.000")), (2024, Decimal("130.500")), (2025, Decimal("155.000"))],
            [(2021, Decimal("14.000")),  (2022, Decimal("22.000")),  (2023, Decimal("30.000")),  (2024, Decimal("38.500")),  (2025, Decimal("45.000"))],
            [(2021, Decimal("22.000")),  (2022, Decimal("45.000")),  (2023, Decimal("68.000")),  (2024, Decimal("92.000")),  (2025, Decimal("118.500"))],
            [(2021, Decimal("482.000")), (2022, Decimal("520.000")), (2023, Decimal("558.000")), (2024, Decimal("598.000")), (2025, Decimal("640.000"))],
        ]
        zähler_liste = []
        for objekt, zd, ablesungen in zip(objekte, zähler_daten, jahres_ablesungen):
            meter = WaterMeter(property_id=objekt.id, **zd)
            db.session.add(meter)
            db.session.flush()
            prev_val = zd["initial_value"]
            for jahr, wert in ablesungen:
                verbrauch = wert - prev_val
                reading = MeterReading(
                    meter_id=meter.id,
                    year=jahr,
                    reading_date=date(jahr, 12, 31),
                    value=wert,
                    consumption=verbrauch,
                    created_by_id=admin.id,
                )
                db.session.add(reading)
                prev_val = wert
            zähler_liste.append(meter)
        db.session.flush()

        # ------------------------------------------------------------------
        # Rechnungen + Positionen + Buchungen
        # ------------------------------------------------------------------
        def make_invoice(nr, kunde, objekt, jahr, status, tarif, verbrauch_m3, created_by):
            base = tarif.base_fee
            preis = tarif.price_per_m3
            wasserkosten = (preis * verbrauch_m3).quantize(Decimal("0.01"))
            gesamt = (base + wasserkosten).quantize(Decimal("0.01"))
            inv = Invoice(
                invoice_number=f"RE-{jahr}-{nr:04d}",
                customer_id=kunde.id,
                property_id=objekt.id,
                period_year=jahr,
                date=date(jahr + 1, 1, 15),
                due_date=date(jahr + 1, 2, 28),
                status=status,
                total_amount=gesamt,
                created_by_id=created_by.id,
            )
            db.session.add(inv)
            db.session.flush()
            db.session.add(InvoiceItem(
                invoice_id=inv.id,
                description="Grundgebühr Wasserversorgung",
                quantity=Decimal("1"), unit="Stk",
                unit_price=base, amount=base,
            ))
            db.session.add(InvoiceItem(
                invoice_id=inv.id,
                description=f"Wasserverbrauch {jahr} ({verbrauch_m3} m³)",
                quantity=verbrauch_m3, unit="m³",
                unit_price=preis, amount=wasserkosten,
            ))
            db.session.flush()
            if status == Invoice.STATUS_PAID:
                db.session.add(Booking(
                    date=date(jahr + 1, 3, 5),
                    account_id=income_account.id,
                    amount=gesamt,
                    description=f"Zahlung {inv.invoice_number}",
                    reference=inv.invoice_number,
                    invoice_id=inv.id,
                    created_by_id=created_by.id,
                ))
            return inv

        # 2021-Rechnungen (alle bezahlt)
        verbrauch_2021 = [Decimal("97.5"), Decimal("278.0"), Decimal("70.0"),
                          Decimal("14.0"), Decimal("22.0"),  Decimal("482.0")]
        for i, (kunde, objekt, verbr) in enumerate(zip(kunden, objekte, verbrauch_2021), start=1):
            make_invoice(i, kunde, objekt, 2021, Invoice.STATUS_PAID, tarif2021, verbr, admin)

        # 2022-Rechnungen (alle bezahlt)
        verbrauch_2022 = [Decimal("27.5"), Decimal("32.0"), Decimal("19.0"),
                          Decimal("8.0"),  Decimal("23.0"), Decimal("38.0")]
        for i, (kunde, objekt, verbr) in enumerate(zip(kunden, objekte, verbrauch_2022), start=7):
            make_invoice(i, kunde, objekt, 2022, Invoice.STATUS_PAID, tarif2022, verbr, admin)

        # 2023-Rechnungen (alle bezahlt)
        verbrauch_2023 = [Decimal("28.5"), Decimal("33.0"), Decimal("22.5"),
                          Decimal("8.5"),  Decimal("24.0"), Decimal("40.0")]
        for i, (kunde, objekt, verbr) in enumerate(zip(kunden, objekte, verbrauch_2023), start=13):
            make_invoice(i, kunde, objekt, 2023, Invoice.STATUS_PAID, tarif2022, verbr, admin)

        # 2024-Rechnungen (gemischte Status)
        verbrauch_2024 = [Decimal("28.5"), Decimal("33.0"), Decimal("22.5"),
                          Decimal("8.5"),  Decimal("26.0"), Decimal("40.0")]
        status_2024 = [Invoice.STATUS_PAID, Invoice.STATUS_PAID, Invoice.STATUS_SENT,
                       Invoice.STATUS_SENT, Invoice.STATUS_DRAFT, Invoice.STATUS_PAID]
        for i, (kunde, objekt, verbr, st) in enumerate(
                zip(kunden, objekte, verbrauch_2024, status_2024), start=19):
            make_invoice(i, kunde, objekt, 2024, st, tarif2024, verbr, admin)

        db.session.flush()

        # ------------------------------------------------------------------
        # Zusätzliche Ausgaben-Buchungen
        # ------------------------------------------------------------------
        ausgaben = [
            Booking(date=date(2024, 3, 10), account_id=expense_account_wartung.id,
                    amount=Decimal("-450.00"), description="Reparatur Hydrant Dorfstraße",
                    reference="RG-2024-0034", created_by_id=admin.id),
            Booking(date=date(2024, 6, 22), account_id=expense_account_strom.id,
                    amount=Decimal("-280.50"), description="Strom Pumpwerk Q2 2024",
                    reference="E-2024-Q2", created_by_id=admin.id),
            Booking(date=date(2024, 9, 5), account_id=expense_account_wartung.id,
                    amount=Decimal("-125.00"), description="Wartung Druckbehälter",
                    reference="RG-2024-0078", created_by_id=admin.id),
            Booking(date=date(2025, 1, 8), account_id=expense_account_strom.id,
                    amount=Decimal("-310.00"), description="Strom Pumpwerk Q4 2024",
                    reference="E-2024-Q4", created_by_id=admin.id),
        ]
        db.session.add_all(ausgaben)

        # ------------------------------------------------------------------
        # Offene Posten
        # ------------------------------------------------------------------
        op1 = OpenItem(
            customer_id=kunden[2].id,
            description="Mahngebühr Rechnung RE-2024-0009",
            amount=Decimal("15.00"),
            date=date(2025, 3, 1),
            due_date=date(2025, 3, 31),
            status=OpenItem.STATUS_OPEN,
            created_by_id=admin.id,
        )
        op2 = OpenItem(
            customer_id=kunden[3].id,
            description="Anschlussgebühr Gartenzähler Nachrüstung",
            amount=Decimal("120.00"),
            date=date(2025, 2, 15),
            due_date=date(2025, 4, 30),
            status=OpenItem.STATUS_OPEN,
            notes="Nachrüstung auf Fernablesung vereinbart",
            created_by_id=admin.id,
        )
        db.session.add_all([op1, op2])

        db.session.commit()
        print("Testdaten erfolgreich eingefügt:")
        print(f"  2 Benutzer, {len(kunden)} Kunden, {len(objekte)} Objekte")
        print(f"  {len(zähler_liste)} Wasserzähler mit je 5 Ablesungen (2021–2025)")
        print(f"  3 Tarife, 24 Rechnungen (2021–2024), 4 Ausgabenbuchungen, 2 Offene Posten")

    @app.cli.command("mark-posted")
    def mark_posted():
        """Alle 'Offen'-Buchungen von Vortagen als 'Verbucht' markieren."""
        from datetime import date as date_cls
        from app.models import Booking
        today = date_cls.today()
        updated = (
            db.session.query(Booking)
            .filter(
                Booking.status == Booking.STATUS_OFFEN,
                Booking.date < today,
            )
            .update({"status": Booking.STATUS_VERBUCHT}, synchronize_session=False)
        )
        db.session.commit()
        print(f"{updated} Buchung(en) als 'Verbucht' markiert.")

    @app.cli.command("reset-db")
    def reset_db():
        """ALLE Daten löschen und Datenbank neu initialisieren (mit Bestätigung)."""
        print("WARNUNG: Alle Daten werden unwiderruflich gelöscht!")
        antwort = input("Zur Bestätigung bitte 'RESET' eingeben: ").strip()
        if antwort != "RESET":
            print("Abgebrochen.")
            return
        db.drop_all()
        print("Alle Tabellen gelöscht.")
        from click import get_current_context
        ctx = get_current_context()
        ctx.invoke(init_db)

    @app.cli.command("create-admin")
    def create_admin():
        """Admin-Benutzer interaktiv anlegen."""
        import getpass
        db.create_all()
        username = input("Benutzername: ").strip()
        email = input("E-Mail: ").strip()
        password = getpass.getpass("Passwort: ")

        existing = User.query.filter_by(username=username).first()
        if existing:
            print(f"Benutzer '{username}' existiert bereits.")
            return

        user = User(username=username, email=email, role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"Admin '{username}' angelegt.")
