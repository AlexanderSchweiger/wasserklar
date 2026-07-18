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
    # ADR-002: Sammelbuchung. booking_groups kommt ueber db.create_all();
    # hier nur die ALTER-Spalten auf bestehenden Tabellen ergaenzen.
    ("bookings",      "group_id INTEGER REFERENCES booking_groups(id)",               "group_id"),
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
                print(f"  ok {table}.{col_name} bereits vorhanden.")
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
    """Die Standard-Steuersaetze idempotent anlegen (Quelle: tax_service)."""
    from app.models import TaxRate
    from app import tax_service

    for tr in tax_service.tax_rates():
        if not TaxRate.query.filter_by(rate=tr.rate).first():
            db.session.add(TaxRate(rate=tr.rate, label=tr.label))
            if verbose:
                print(f"  + Steuersatz {tr.rate}% angelegt.")
        elif verbose:
            print(f"  ok Steuersatz {tr.rate}% bereits vorhanden.")
    db.session.commit()


def seed_default_dunning_policy(db, *, verbose=False):
    """Die Standard-Mahnvorlage mit 4 Stufen idempotent anlegen (ADR-003)."""
    from decimal import Decimal
    from app.models import DunningPolicy, DunningStage

    if DunningPolicy.query.first():
        if verbose:
            print("  ok Mahnvorlage bereits vorhanden.")
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
    # Default-Texte je Stufe vorbefüllen (im Policy-Formular editierbar).
    from app.dunning.services import (
        DEFAULT_LETTER_INTRO, DEFAULT_LETTER_CLOSING_SOFT,
        DEFAULT_LETTER_CLOSING_HARD, DEFAULT_EMAIL_SUBJECT, DEFAULT_EMAIL_BODY,
    )
    for st in stages:
        st.letter_intro = DEFAULT_LETTER_INTRO
        st.letter_closing = (
            DEFAULT_LETTER_CLOSING_SOFT if st.level <= 2
            else DEFAULT_LETTER_CLOSING_HARD
        )
        st.email_subject = DEFAULT_EMAIL_SUBJECT
        st.email_body = DEFAULT_EMAIL_BODY
    db.session.add_all(stages)
    db.session.commit()
    if verbose:
        print("  + Standard-Mahnvorlage mit 4 Stufen angelegt.")


def seed_default_roles(db, *, verbose=False):
    """Die drei Standard-Rollen idempotent anlegen.

    Quelle der Wahrheit: app.auth.permissions. Admin (is_system=True) hat
    implizit alle Rechte und kann nicht editiert/geloescht werden — daher
    keine RolePermission-Eintraege noetig (Logik im Model). Kassier und
    Zaehlerverwalter bekommen ihre Default-Berechtigungen.
    """
    from app.models import Role, RolePermission
    from app.auth.permissions import (
        PERM_AUSWERTUNGEN,
        PERM_BUCHHALTUNG,
        PERM_MAHNWESEN,
        PERM_RECHNUNGEN,
        PERM_ZAEHLER,
    )

    defaults = [
        ("Admin", "Vollzugriff auf alle Bereiche", True, []),
        ("Kassier", "Buchhaltung, Rechnungen/OP, Mahnwesen und Auswertungen", False,
         [PERM_BUCHHALTUNG, PERM_RECHNUNGEN, PERM_MAHNWESEN, PERM_AUSWERTUNGEN]),
        ("Zählerverwalter", "Verwaltung von Zählern und Ablesungen", False,
         [PERM_ZAEHLER]),
    ]
    for name, desc, is_system, perms in defaults:
        role = Role.query.filter_by(name=name).first()
        if role is None:
            role = Role(name=name, description=desc, is_system=is_system)
            db.session.add(role)
            db.session.flush()
            for key in perms:
                db.session.add(RolePermission(role_id=role.id, permission_key=key))
            if verbose:
                print(f"  + Rolle '{name}' angelegt.")
        elif verbose:
            print(f"  ok Rolle '{name}' bereits vorhanden.")
    db.session.commit()


def seed_default_billing_period(db, *, verbose=False):
    """Eine Default-Abrechnungsperiode fuers laufende Kalenderjahr anlegen,
    falls noch keine existiert.

    Es muss immer genau eine aktive Abrechnungsperiode geben, damit
    Zaehlerablesungen erfasst werden koennen.
    """
    from datetime import date
    from app.models import BillingPeriod

    if BillingPeriod.query.first():
        if verbose:
            print("  ok Abrechnungsperiode bereits vorhanden.")
        return

    year = date.today().year
    period = BillingPeriod(
        name=str(year),
        start_date=date(year, 1, 1),
        end_date=date(year, 12, 31),
        active=True,
    )
    db.session.add(period)
    db.session.commit()
    if verbose:
        print(f"  + Abrechnungsperiode '{year}' angelegt (aktiv).")


def run_data_migrations(db, *, verbose=False):
    """Datenmigrationen fuer bestehende DBs (Kundennummern, OpenItems aus versendeten Rechnungen).

    Alle Aenderungen laufen in EINER Transaktion (ein Commit am Ende). Das ist
    fuer die SaaS-Schicht wichtig: jeder Commit gibt die Connection in den Pool
    zurueck, und der Pool-Checkout-Listener setzt den search_path wieder auf
    public — eine Folge-Query landete sonst im falschen Schema.
    """
    from sqlalchemy import func
    from app.models import Customer, Invoice, OpenItem

    # Kundennummern fuer Altbestand vergeben — nur fuer echte Kunden,
    # reine Lieferanten (is_customer=False) bleiben ohne Nummer.
    kunden_ohne_nr = (
        Customer.query
        .filter(Customer.customer_number == None, Customer.is_customer == True)  # noqa: E711, E712
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
        if inv.billing_period is not None:
            oi_period_year = inv.billing_period.end_date.year
        elif inv.date is not None:
            oi_period_year = inv.date.year
        else:
            oi_period_year = None
        oi = OpenItem(
            customer_id=inv.customer_id,
            description=inv.invoice_number,
            amount=inv.total_amount,
            date=inv.date,
            due_date=inv.due_date,
            period_year=oi_period_year,
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


def _assert_demo_seed_allowed(app, *, yes: bool) -> None:
    """Mehrstufiges Sicherheitsnetz fuer demo-seed: niemals in Prod.

    Drei unabhaengige Gates:
      1. ``app.debug`` muss True sein (Prod-Configs setzen DEBUG=False).
      2. Env-Var ``WASSERKLAR_ALLOW_DEMO_SEED=1`` muss gesetzt sein.
      3. Interaktive Bestaetigung 'SEED' (mit ``--yes`` ueberspringbar).

    Bei Fehlschlag: ``click.ClickException``. Caller bricht den Command ab.
    """
    import os
    import click

    if not app.debug:
        raise click.ClickException(
            "demo-seed nur im Development-Modus erlaubt (app.debug muss True sein).\n"
            "FLASK_ENV=development setzen und den Command erneut ausfuehren."
        )

    if os.environ.get("WASSERKLAR_ALLOW_DEMO_SEED") != "1":
        raise click.ClickException(
            "Sicherheits-Gate: Env-Var WASSERKLAR_ALLOW_DEMO_SEED=1 muss gesetzt sein, "
            "bevor der Demo-Seed laeuft. Schuetzt vor versehentlichem Wipe einer Prod-DB."
        )

    if not yes:
        antwort = input(
            "WARNUNG: Alle Geschaefts-Daten werden geloescht und durch Demo-Daten ersetzt.\n"
            "Zur Bestaetigung bitte 'SEED' eingeben: "
        ).strip()
        if antwort != "SEED":
            raise click.ClickException("Abgebrochen.")


def _wipe_business_data(db, *, verbose: bool = False) -> None:
    """Loescht alle Geschaefts-Daten (Tabellen behalten), re-seeded Defaults.

    Identisches Verhalten wie ``clear-db --full``, aber ohne interaktiven Prompt.
    Schutzliste: tax_rates, dunning_policies, dunning_stages. Defaults
    (Steuersaetze, Mahnvorlage, Abrechnungsperiode, Rollen) werden danach
    idempotent neu eingespielt.
    """
    schutz = {"tax_rates", "dunning_policies", "dunning_stages"}
    dialect = db.engine.dialect.name
    existing = set(sa.inspect(db.engine).get_table_names())
    tables = [t for t in db.metadata.tables.values()
              if t.name not in schutz and t.name in existing]

    with db.engine.begin() as conn:
        if dialect == "mysql":
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=0"))
        elif dialect == "sqlite":
            conn.execute(sa.text("PRAGMA foreign_keys=OFF"))

        for table in tables:
            if dialect == "postgresql":
                conn.execute(sa.text(f"TRUNCATE TABLE {table.name} RESTART IDENTITY CASCADE"))
            else:
                conn.execute(table.delete())

        if dialect == "mysql":
            conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=1"))
        elif dialect == "sqlite":
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))

    if verbose:
        print(f"  {len(tables)} Tabelle(n) geleert.")

    seed_default_tax_rates(db, verbose=verbose)
    seed_default_dunning_policy(db, verbose=verbose)
    seed_default_billing_period(db, verbose=verbose)
    seed_default_roles(db, verbose=verbose)


def register_commands(app):
    import click
    from app.extensions import db
    from app.models import User, Account, TaxRate, DunningPolicy, DunningStage

    @app.cli.command("init-db")
    def init_db():
        """Datenbanktabellen via Alembic anlegen und Defaults seeden."""
        from flask_migrate import upgrade as alembic_upgrade
        alembic_upgrade()
        print("Datenbankschema auf head migriert.")

        seed_default_tax_rates(db)
        seed_default_dunning_policy(db)
        seed_default_billing_period(db)
        seed_default_roles(db)

    @app.cli.command("upgrade-db")
    def upgrade_db():
        """Datenbankschema auf head bringen und Defaults/Datenmigrationen nachziehen.

        Erkennt automatisch DBs, die Tabellen aber keine eingetragene Alembic-
        Revision haben (entweder pre-Alembic oder ein abgebrochener init-Lauf
        hat eine leere ``alembic_version`` hinterlassen): zieht fehlende v1.0.0-
        Spalten via altem ``apply_schema_upgrades`` nach, stempelt auf die
        Initial-Revision (NICHT auf head -- sonst werden alle nachfolgenden
        Migrations als "schon gelaufen" verbucht und nie tatsaechlich ausgefuehrt)
        und laesst danach ``alembic_upgrade`` regulaer alle weiteren Migrations
        anwenden.
        """
        from flask_migrate import upgrade as alembic_upgrade, stamp as alembic_stamp

        # Initial-Revision der Alembic-History. ``apply_schema_upgrades`` deckt
        # genau die Spalten dieser Initial-Migration ab -- nach dem Nachzug ist
        # die DB also aequivalent zu "frisch via 7c7f225282c9 angelegt".
        # Spaetere Migrations laufen dann via alembic_upgrade() ganz normal.
        _INITIAL_REVISION = "7c7f225282c9"

        inspector = sa.inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        non_alembic_tables = existing_tables - {"alembic_version"}

        # Hat die DB schon Inhalts-Tabellen UND keine eingetragene Revision?
        # Dann ist sie pre-Alembic (oder halb-initialisiert) und Alembic
        # darf NICHT von Anfang upgraden — das wuerde versuchen, alle
        # Tabellen erneut anzulegen.
        current_revision = None
        if "alembic_version" in existing_tables:
            try:
                with db.engine.connect() as conn:
                    row = conn.execute(
                        sa.text("SELECT version_num FROM alembic_version LIMIT 1")
                    ).first()
                    current_revision = row[0] if row else None
            except Exception:
                current_revision = None

        if non_alembic_tables and current_revision is None:
            print("Pre-Alembic / un-stamped DB erkannt — zieh fehlende v1.0.0-Spalten nach...")
            with db.engine.begin() as conn:
                apply_schema_upgrades(conn, db.engine.dialect.name, verbose=True)
            alembic_stamp(revision=_INITIAL_REVISION)
            print(f"Alembic-Marker auf {_INITIAL_REVISION} (initial v1.0.0) gesetzt.")

        alembic_upgrade()
        print("Datenbankschema auf head migriert.")

        seed_default_tax_rates(db, verbose=True)
        seed_default_dunning_policy(db, verbose=True)
        seed_default_billing_period(db, verbose=True)
        seed_default_roles(db, verbose=True)
        run_data_migrations(db, verbose=True)

        print("Datenbank aktualisiert.")

    @app.cli.command("seed-testdata")
    def seed_testdata():
        """Testdaten für alle Tabellen einfügen (nur wenn DB leer ist)."""
        from datetime import date
        from decimal import Decimal
        from app.models import (
            User, Customer, Property, PropertyOwnership,
            WaterMeter, MeterReading, WaterTariff, BillingPeriod,
            Invoice, InvoiceItem, Account, Booking, OpenItem,
        )

        if Customer.query.count() > 0:
            print("Testdaten bereits vorhanden – abgebrochen.")
            return

        db.create_all()

        # ------------------------------------------------------------------
        # Abrechnungsperioden (Kalenderjahre 2021–2025, 2025 aktiv)
        # ------------------------------------------------------------------
        perioden = {}
        for _jahr in range(2021, 2026):
            p = BillingPeriod(
                name=str(_jahr),
                start_date=date(_jahr, 1, 1),
                end_date=date(_jahr, 12, 31),
                active=(_jahr == 2025),
            )
            db.session.add(p)
            perioden[_jahr] = p
        db.session.flush()

        # ------------------------------------------------------------------
        # Benutzer
        # ------------------------------------------------------------------
        from app.models import Role
        seed_default_roles(db)
        admin_role = Role.query.filter_by(name="Admin").first()
        kassier_role = Role.query.filter_by(name="Kassier").first()
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin", email="admin@wassergenossenschaft.at",
                         role_id=admin_role.id)
            admin.set_password("admin123")
            db.session.add(admin)
        if not User.query.filter_by(username="sachbearbeiter").first():
            user1 = User(username="sachbearbeiter",
                         email="sachbearbeiter@wassergenossenschaft.at",
                         role_id=kassier_role.id)
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
                    billing_period_id=perioden[jahr].id,
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
                billing_period_id=perioden[jahr].id,
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

    @app.cli.command("split-customer-names")
    @click.option("--dry-run", is_flag=True, default=False,
                  help="Nur anzeigen, was passieren würde — nichts speichern.")
    def split_customer_names(dry_run):
        """Heuristik-Starthilfe fuer die Namens-Aufspaltung (oss-v1.21.0).

        Fuellt fuer Kontakte, deren Vor-/Nachname noch leer sind, eine erste
        Schaetzung: Firmen-Namen (GmbH/AG/Gemeinde/Verein ...) werden als Firma
        markiert; sonst wird der kombinierte ``name`` an der ersten Leerstelle in
        Nachname + Vorname zerlegt (Konvention "Nachname Vorname"). Das
        kombinierte ``name`` bleibt unveraendert (Sortier-/Listen-Schluessel).
        Bereits gepflegte oder als Firma markierte Kontakte werden
        uebersprungen. Anrede/Geschlecht laesst sich nicht zuverlaessig raten und
        bleibt leer — bitte in der Kontaktmaske nachpflegen.
        """
        import re
        from app.models import Customer

        company_re = re.compile(
            r"\b(gmbh|ges\.?\s?m\.?\s?b\.?\s?h|ag|og|kg|keg|ohg|gbr|ug|se|kft|"
            r"srl|e\.?\s?u\.?|gen\.?|egen|genossenschaft|verein|verband|"
            r"gemeinde|marktgemeinde|stadtgemeinde|stadtwerke|pfarre|stiftung|"
            r"co\.?\s?kg)\b",
            re.IGNORECASE,
        )

        candidates = Customer.query.filter(
            Customer.is_company.is_(False),
            (Customer.last_name.is_(None)) | (Customer.last_name == ""),
            (Customer.first_name.is_(None)) | (Customer.first_name == ""),
        ).all()

        companies = 0
        persons = 0
        for c in candidates:
            raw = (c.name or "").strip()
            if not raw:
                continue
            if company_re.search(raw):
                c.is_company = True
                companies += 1
                continue
            parts = raw.split()
            if len(parts) >= 2:
                c.last_name = parts[0]
                c.first_name = " ".join(parts[1:])
            else:
                c.last_name = raw
            persons += 1

        if dry_run:
            db.session.rollback()
            print(f"[Probelauf] {persons} Person(en) würden aufgeteilt, "
                  f"{companies} als Firma markiert. Nichts gespeichert.")
            return
        db.session.commit()
        print(f"{persons} Person(en) aufgeteilt (Nachname/Vorname aus 'name'), "
              f"{companies} als Firma markiert. "
              f"Anrede bitte in der Kontaktmaske nachpflegen.")

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

    @app.cli.command("clear-db")
    @click.option("--full", is_flag=True, default=False,
                  help="Auch Benutzer und Einstellungen löschen.")
    def clear_db(full):
        """Bewegungsdaten löschen (Tabellen bleiben erhalten).

        Ohne --full: Kunden, Zähler, Ablesungen, Rechnungen, Buchungen usw.
        werden geleert; Benutzer, User-Preferences und App-Einstellungen bleiben.

        Mit --full: alle Tabellen bis auf die Seed-Defaults (Steuersätze,
        Mahnrichtlinie, Abrechnungsperiode) werden geleert, dann werden die
        Defaults neu eingespielt.
        """
        schutz = {"tax_rates", "dunning_policies", "dunning_stages"}
        if not full:
            schutz |= {"users", "user_preferences", "app_settings"}

        scope = "ALLE Daten (inkl. Benutzer und Einstellungen)" if full else \
                "alle Bewegungsdaten (Benutzer und Einstellungen bleiben erhalten)"
        print(f"WARNUNG: {scope} werden unwiderruflich gelöscht!")
        antwort = input("Zur Bestätigung bitte 'CLEAR' eingeben: ").strip()
        if antwort != "CLEAR":
            print("Abgebrochen.")
            return

        dialect = db.engine.dialect.name
        existing = set(sa.inspect(db.engine).get_table_names())
        # FK-Checks werden deaktiviert → Reihenfolge egal, nur existierende Tabellen
        tables = [t for t in db.metadata.tables.values()
                  if t.name not in schutz and t.name in existing]

        with db.engine.begin() as conn:
            if dialect == "mysql":
                conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=0"))
            elif dialect == "sqlite":
                conn.execute(sa.text("PRAGMA foreign_keys=OFF"))

            for table in tables:
                if dialect == "postgresql":
                    conn.execute(sa.text(f"TRUNCATE TABLE {table.name} RESTART IDENTITY CASCADE"))
                else:
                    conn.execute(table.delete())

            if dialect == "mysql":
                conn.execute(sa.text("SET FOREIGN_KEY_CHECKS=1"))
            elif dialect == "sqlite":
                conn.execute(sa.text("PRAGMA foreign_keys=ON"))

        geloescht = sorted(t.name for t in tables)
        print(f"{len(geloescht)} Tabelle(n) geleert: {', '.join(geloescht)}")

        if full:
            seed_default_tax_rates(db)
            seed_default_dunning_policy(db)
            seed_default_billing_period(db)
            seed_default_roles(db)
            print("Standard-Defaults neu eingespielt.")

    @app.cli.command("seed-demo")
    @click.option("--yes", is_flag=True, default=False,
                  help="Bestaetigungs-Prompt 'SEED' ueberspringen (fuer CI / Test-Setup).")
    def seed_demo(yes):
        """Reproduzierbaren Demo-Datensatz erzeugen (100 Kunden, 150 Zaehler, ...).

        Wirft alle Geschaefts-Daten weg und erzeugt einen deterministischen
        Datensatz (gleicher Befehl, gleiche Daten) fuer manuelle Tests,
        Screenshots oder Bug-Reproduktion.

        SICHERHEIT: Drei Gates schuetzen vor versehentlichem Lauf gegen Prod:
          1. ``app.debug`` muss True sein
          2. Env-Var ``WASSERKLAR_ALLOW_DEMO_SEED=1`` muss gesetzt sein
          3. Interaktives 'SEED'-Bestaetigung (mit ``--yes`` ueberspringbar)

        Erzeugt: 100 Kunden, 120 Objekte, 150 Zaehler, Vorjahres-Periode
        (abgeschlossen) mit Ablesungen, aktuelle Periode mit ~20 Zaehlertauschen,
        20 Rechnungen (gemischte Status), 4 Mahnungen (Stufen 1-4),
        2 Bankkonten + 3 Umbuchungen, 12 Sammelbuchungen mit Projekten und
        verschiedenen Steuersaetzen, passende Tarife. Plus kompletter
        Leitungsnetz-Datensatz: aktiver Plan als zusammenhaengendes Netz mit
        allen Elementtypen — 3 Quellen (historische Schuettungs-Messreihen mit
        Trockenperioden), Sammelschacht, Hochbehaelter, Pumpe, Entlueftung,
        Materialwechsel, Druckminderschacht, Leitungen inkl. Notverbund, ~30
        Hausanschluessen mit Anbohrschellen (grossteils zugeordnet + geocodet),
        Strangenden (Endhydrant/Endkappe/Entleerung), Hydranten (Ueber-/
        Unterflur)/Schiebern mit teils faelligen Pruef-Logs und 9
        Stoerungsjournal-Eintraegen.
        Dazu Schriftfuehrung (nur WG-Modus): Vorstandssitzungen + Hauptversammlungen
        mit Tagesordnung, Anwesenheit, Protokollen und einem Beschluss-Register.

        Die Buchhaltung wird zudem bis ins **aktuelle Jahr** fortgeschrieben
        (``now=date.today()``): Vorjahre abgeschlossen, ein offenes Buchungsjahr
        im laufenden Jahr und laufende Buchungen/Posten/Umbuchung bis heute.

        Login: admin / demo1234.
        """
        from datetime import date
        from app.seed.demo import seed_demo_data

        _assert_demo_seed_allowed(app, yes=yes)

        print("Wipe und Demo-Seed laeuft...")
        _wipe_business_data(db, verbose=True)
        # ``now`` = echtes heute -> Buchhaltung bis ins aktuelle Jahr fortschreiben.
        seed_demo_data(db, verbose=True, now=date.today())
        # OpenItems fuer SENT-Rechnungen + ggf. Kundennummern nachziehen
        run_data_migrations(db, verbose=True)
        db.session.commit()
        print("Demo-Daten erfolgreich erzeugt. Login: admin / demo1234")

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

        from app.models import Role
        seed_default_roles(db)
        admin_role = Role.query.filter_by(name="Admin").first()
        user = User(username=username, email=email, role_id=admin_role.id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"Admin '{username}' angelegt.")

    @app.cli.command("reset-2fa")
    @click.option("--username", required=True,
                  help="Benutzername, dessen 2FA zurueckgesetzt wird.")
    @click.option("--schema", default=None,
                  help="Optionales Tenant-Schema (SaaS), z.B. tenant_alm. "
                       "Setzt den search_path vor dem Update.")
    def reset_2fa(username, schema):
        """2FA eines Benutzers zuruecksetzen (Break-Glass: Geraet UND Recovery-Codes verloren)."""
        if schema:
            db.session.execute(sa.text(f'SET search_path TO "{schema}", public'))
        user = User.query.filter_by(username=username).first()
        if user is None:
            print(f"Benutzer '{username}' nicht gefunden.")
            return
        user.reset_totp()
        db.session.commit()
        print(f"2FA für '{username}' zurückgesetzt.")

    # -----------------------------------------------------------------------
    # Daten-Export/Import (kompatibel zum UI-Feature unter /data-transfer)
    # -----------------------------------------------------------------------

    @app.cli.command("export-data")
    @click.option("--out", "out_path", required=True,
                  help="Pfad fuer die ZIP-Ausgabedatei.")
    @click.option("--include", "include_str", default="stammdaten,buchungen,mahnwesen,einstellungen",
                  help="Komma-separierte Kategorien (stammdaten,buchungen,mahnwesen,einstellungen).")
    @click.option("--years", "years_str", default="",
                  help="Komma-separierte Jahre (nur fuer Buchungen). Leer = alle Jahre.")
    @click.option("--no-pdfs", "no_pdfs", is_flag=True, default=False,
                  help="PDF-Anhaenge NICHT mit-bundlen.")
    def export_data(out_path, include_str, years_str, no_pdfs):
        """Exportiert alle Tabellen in eine ZIP-Datei (gleiches Format wie UI)."""
        from app.data_transfer.services import export_to_zip
        cats = {c.strip() for c in include_str.split(",") if c.strip()}
        years = [int(y) for y in years_str.split(",") if y.strip().isdigit()]
        selection = {
            "stammdaten": "stammdaten" in cats,
            "buchungen": "buchungen" in cats,
            "mahnwesen": "mahnwesen" in cats,
            "einstellungen": "einstellungen" in cats,
            "include_pdfs": not no_pdfs,
            "years": years,
        }
        with open(out_path, "wb") as fh:
            manifest = export_to_zip(selection, fh, exported_by="cli")
        rows = sum(t["rows"] for t in manifest["tables"])
        print(f"Export geschrieben: {out_path}")
        print(f"  {len(manifest['tables'])} Tabellen, {rows} Records.")

    @app.cli.command("import-data")
    @click.option("--in", "in_path", required=True,
                  help="Pfad zur ZIP-Importdatei.")
    @click.option("--mode", "mode", default="replace",
                  type=click.Choice(["replace", "merge"]),
                  help="Vollersatz oder Merge.")
    @click.option("--update-existing", is_flag=True, default=False,
                  help="Im Merge-Modus: bestehende Records aktualisieren.")
    @click.option("--yes", "skip_confirm", is_flag=True, default=False,
                  help="Bestaetigungs-Prompt ueberspringen.")
    def import_data(in_path, mode, update_existing, skip_confirm):
        """Importiert eine zuvor exportierte ZIP-Datei."""
        from pathlib import Path
        from app.data_transfer.services import (
            extract_to_temp, import_from_zip, validate_manifest, ImportError_,
        )
        with open(in_path, "rb") as fh:
            extract_dir, manifest = extract_to_temp(fh, app.instance_path)
        validation = validate_manifest(manifest, extract_dir)
        if validation["errors"]:
            print("Import blockiert:")
            for err in validation["errors"]:
                print(f"  - {err}")
            return
        for warn in validation["warnings"]:
            print(f"WARNUNG: {warn}")
        print(f"Modus: {mode}{', update_existing' if update_existing else ''}")
        for row in validation["tables_overview"]:
            print(f"  {row['name']}: import={row['rows']}, current={row['current_count']}")
        if not skip_confirm:
            answer = input("Anwenden? [yes/NO]: ").strip().lower()
            if answer != "yes":
                print("Abgebrochen.")
                import shutil
                shutil.rmtree(extract_dir, ignore_errors=True)
                return
        try:
            stats = import_from_zip(
                extract_dir, manifest,
                mode=mode, update_existing=update_existing,
                instance_path=app.instance_path,
            )
        except ImportError_ as exc:
            print(f"FEHLER: {exc}")
            return
        finally:
            import shutil
            shutil.rmtree(extract_dir, ignore_errors=True)
        total_i = sum(s["inserted"] for s in stats.values())
        total_u = sum(s["updated"] for s in stats.values())
        total_s = sum(s["skipped"] for s in stats.values())
        print(f"Import fertig: {total_i} neu, {total_u} aktualisiert, {total_s} uebersprungen.")

    @app.cli.command("rotate-mail-key")
    def rotate_mail_key():
        """Gespeichertes mail.password mit aktuellem primary WASSERKLAR_MAIL_KEY re-encrypten.

        Voraussetzung: WASSERKLAR_MAIL_KEY enthaelt den neuen Key als ersten
        Eintrag, gefolgt vom alten (comma-separated). MultiFernet
        entschluesselt mit allen Keys, encrypt-ed aber nur mit dem ersten.

        Idempotent — zweite Ausfuehrung ist no-op.
        """
        from app.extensions import db
        from app.models import AppSetting
        from app.settings_service import decrypt_password, encrypt_password

        rec = AppSetting.query.filter_by(key="mail.password").first()
        if rec is None or not rec.value:
            print("Kein mail.password gespeichert — nichts zu rotieren.")
            return
        try:
            plaintext = decrypt_password(rec.value)
        except Exception as e:
            print(f"FEHLER: Decrypt fehlgeschlagen: {e}")
            return
        if not plaintext:
            print("WARNUNG: Decrypt liefert leeren String — Eintrag bleibt unveraendert.")
            return
        rec.value = encrypt_password(plaintext)
        db.session.commit()
        print("mail.password re-encrypted.")

    @app.cli.command("reset-mail-passwords")
    def reset_mail_passwords():
        """Gespeichertes mail.password loeschen (Cleanup nach Crypto-Cutover).

        Tenant traegt das Passwort danach in den Settings neu ein.
        """
        from app.extensions import db
        from app.models import AppSetting

        rec = AppSetting.query.filter_by(key="mail.password").first()
        if rec is None or not rec.value:
            print("Kein mail.password gespeichert — nichts zu loeschen.")
            return
        rec.value = None
        db.session.commit()
        print("mail.password geloescht. Bitte in /settings neu eintragen.")

    @app.cli.command("bev-refresh")
    @click.option("--file", "local_file", default=None,
                  help="Lokales BEV-ZIP (Adressregister-Stichtagsdaten) statt Download.")
    @click.option("--url", "url", default=None,
                  help="Download-URL (override fuer BEV_DOWNLOAD_URL).")
    @click.option("--geocode", is_flag=True, default=False,
                  help="Nach dem Index-Bau direkt alle Liegenschaften neu abgleichen.")
    def bev_refresh(local_file, url, geocode):
        """BEV-Adressregister laden/lesen und den Geocoding-Index neu bauen.

        Der Index ist eine eigenstaendige SQLite-Datei (BEV_INDEX_PATH), die der
        "BEV-Adressen abgleichen"-Button bei den Liegenschaften nutzt. Schwerer
        Schritt (100-MB-ZIP, CRS-Reprojektion) -> bewusst hier im CLI, nicht im
        Request. OSS: gelegentlich/Cron; SaaS: platform-scheduler 2x/Jahr.

        Quelle: --file <zip> (manuell geladen), --url <url> oder die Env-Var
        BEV_DOWNLOAD_URL.
        """
        from app.properties import bev_geocode

        index_path = app.config["BEV_INDEX_PATH"]
        src = local_file or url or app.config.get("BEV_DOWNLOAD_URL")
        if not src:
            raise click.ClickException(
                "Keine BEV-Quelle: --file <zip>, --url <url> oder BEV_DOWNLOAD_URL setzen."
            )
        is_url = not local_file
        try:
            stats = bev_geocode.build_index(
                src, index_path, is_url=is_url, progress=click.echo,
            )
        except bev_geocode.BevImportError as exc:
            raise click.ClickException(str(exc))
        click.echo(
            f"BEV-Index gebaut: {stats['addresses']} Adressen "
            f"({stats['skipped']} übersprungen) -> {index_path}"
        )

        if geocode:
            result = bev_geocode.geocode_properties(only_missing=False, index_path=index_path)
            click.echo(
                f"Liegenschaften abgeglichen: {result['geocoded']}/{result['total']} "
                f"geocodet, {len(result['not_found'])} ohne Treffer."
            )
