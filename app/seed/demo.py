"""Deterministischer Demo-Datensatz fuer Test- und Screenshot-Zwecke.

Erzeugt 100 Kunden, 120 Objekte, 150 Zaehler, vollstaendige Vorjahres-Abrechnung
mit Zaehlerstaenden, laufende Periode mit ~20 Zaehlertauschen, gemischte
Rechnungen / offene Posten / Mahnungen, 2 Bankkonten + Umbuchungen,
Sammelbuchungen mit Projekten und verschiedenen Steuersaetzen, passende Tarife.

Wird von zwei CLI-Wrappern aufgerufen:
- OSS: ``flask --app run seed-demo`` (cli.py)
- SaaS: ``flask --app run seed-demo --slug <tenant>`` (saas/cli.py)

Beide Wrapper haben mehrstufige Production-Gates und wipen die DB davor. Diese
Funktion selbst macht KEIN wipe und KEIN initial commit — sie erwartet eine
DB mit Defaults (TaxRates, DunningPolicy "Standard", Rollen), aber leeren
Geschaeftsdaten.

Determinismus: ``random.Random(42)`` als einziger RNG, ``today`` als Parameter
(kein ``date.today()``-Aufruf im Modul).
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal


_VORNAMEN = [
    "Franz", "Maria", "Johann", "Anna", "Klaus", "Elisabeth", "Stefan", "Brigitte",
    "Wolfgang", "Renate", "Andreas", "Sabine", "Thomas", "Martina", "Christian",
    "Petra", "Manfred", "Gabriele", "Reinhard", "Ulrike", "Erich", "Helga",
    "Walter", "Christa", "Karl", "Monika", "Herbert", "Edith", "Josef", "Hildegard",
    "Werner", "Inge", "Rudolf", "Erika", "Friedrich", "Hannelore", "Peter", "Eva",
    "Gerhard", "Hannelore", "Otto", "Irmgard", "Helmut", "Irene", "Alfred",
    "Gertrude", "Heinrich", "Waltraud", "Ernst", "Gerda",
]
_NACHNAMEN = [
    "Huber", "Gruber", "Mayr", "Leitner", "Steinbauer", "Weidinger", "Berger",
    "Aigner", "Brunner", "Eder", "Fischer", "Hofer", "Lechner", "Moser",
    "Pichler", "Reiter", "Schwarz", "Wagner", "Wimmer", "Winkler", "Bauer",
    "Holzer", "Auer", "Mair", "Maier", "Bauernfeind", "Schmid", "Lang",
    "Wolf", "Schober", "Egger", "Stocker", "Köck", "Riedl", "Aichinger",
    "Schober", "Kogler", "Steiner", "Hauer", "Pirker", "Frühwirth", "Loidl",
    "Stadler", "Hochreiter", "Ennser", "Klammer", "Schiestl", "Voglhuber",
    "Plank", "Salzmann",
]
_ORTE = [
    ("4232", "Hagenberg"), ("4233", "Katsdorf"), ("4221", "Steyregg"),
    ("4222", "Langenstein"), ("4232", "Pregarten"), ("4240", "Freistadt"),
]
_STRASSEN = [
    "Dorfstraße", "Hauptstraße", "Birkenweg", "Gartenstraße", "Wiesenweg",
    "Am Bach", "Lindenweg", "Bergweg", "Mühlweg", "Schulgasse", "Kirchplatz",
    "Sonnenweg", "Quellweg", "Forstweg", "Talweg",
]


def seed_demo_data(db, *, today: date = date(2025, 9, 15), verbose: bool = True) -> dict:
    """Seedet den vollstaendigen Demo-Datensatz.

    Voraussetzung: DB hat die Defaults (TaxRates, DunningPolicy "Standard",
    Rollen) aber keine Geschaeftsdaten — d.h. Customer/Property/Invoice/...
    sind leer. Caller ruft ``clear-db --full`` o. Aequivalent davor auf.

    Parameter:
        db: SQLAlchemy-Instanz (`app.extensions.db`).
        today: Stichtag fuer die aktuelle Periode. Default 2025-09-15.
        verbose: Print-Output.

    Rueckgabe: Dict mit Counts pro Entitaet (fuer Tests / Smoke-Asserts).
    """
    from datetime import datetime
    from app.models import (
        Role, User, Customer, Property, PropertyOwnership, WaterMeter,
        MeterReading, BillingPeriod, WaterTariff, Account, Project, RealAccount,
        Transfer, Invoice, InvoiceItem, BookingGroup, Booking, OpenItem,
        DunningPolicy, DunningStage, DunningNotice, FiscalYear,
    )

    rng = random.Random(42)
    current_year = today.year
    prev_year = current_year - 1
    counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Admin-User (fuer created_by_id auf allen Folge-Eintraegen)
    # ------------------------------------------------------------------
    admin_role = Role.query.filter_by(name="Admin").first()
    kassier_role = Role.query.filter_by(name="Kassier").first()
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        admin = User(
            username="admin", email="admin@demo.local",
            role_id=admin_role.id, active=True,
        )
        admin.set_password("demo1234")
        db.session.add(admin)
    if not User.query.filter_by(username="kassier").first():
        kassier = User(
            username="kassier", email="kassier@demo.local",
            role_id=kassier_role.id, active=True,
        )
        kassier.set_password("demo1234")
        db.session.add(kassier)
    db.session.flush()
    admin = User.query.filter_by(username="admin").first()

    # ------------------------------------------------------------------
    # Abrechnungsperioden
    # ------------------------------------------------------------------
    # clear-db --full hat billing_periods geleert, aber seed_default_billing_period
    # legt im Anschluss eine Default-Periode fuers laufende Kalenderjahr an. Die
    # haette unique=True auf ``name`` -> wir entfernen sie hier, bauen unsere
    # zwei Perioden neu auf.
    BillingPeriod.query.delete()
    db.session.flush()
    p_prev = BillingPeriod(
        name=str(prev_year),
        start_date=date(prev_year, 1, 1),
        end_date=date(prev_year, 12, 31),
        active=False,
        notes="Vorjahr — abgeschlossen.",
    )
    p_curr = BillingPeriod(
        name=str(current_year),
        start_date=date(current_year, 1, 1),
        end_date=date(current_year, 12, 31),
        active=True,
        notes="Laufende Periode.",
    )
    db.session.add_all([p_prev, p_curr])
    db.session.flush()
    counts["billing_periods"] = 2

    # ------------------------------------------------------------------
    # Buchungsjahre (Vorjahr abgeschlossen, akt. Jahr offen)
    # ------------------------------------------------------------------
    # clear-db --full leert fiscal_years; nichts kollidiert. Vorjahr ist
    # "closed" (mit closed_at + closed_by_id=admin), aktuelles Jahr offen.
    fy_prev = FiscalYear(
        year=prev_year,
        start_date=date(prev_year, 1, 1),
        end_date=date(prev_year, 12, 31),
        closed=True,
        closed_at=datetime(current_year, 1, 31, 12, 0, 0),
        closed_by_id=admin.id,
        is_vat_liable=False,
    )
    fy_curr = FiscalYear(
        year=current_year,
        start_date=date(current_year, 1, 1),
        end_date=date(current_year, 12, 31),
        closed=False,
        is_vat_liable=False,
    )
    db.session.add_all([fy_prev, fy_curr])
    db.session.flush()
    counts["fiscal_years"] = 2

    # ------------------------------------------------------------------
    # Tarife (je 1 pro Jahr, unterschiedliche Preise)
    # ------------------------------------------------------------------
    t_prev = WaterTariff(
        name=f"Tarif {prev_year}", valid_from=prev_year, valid_to=prev_year,
        base_fee=Decimal("32.00"), additional_fee=Decimal("8.00"),
        price_per_m3=Decimal("1.40"),
        notes=f"Tarif fuer Periode {prev_year}.",
    )
    t_curr = WaterTariff(
        name=f"Tarif {current_year}", valid_from=current_year, valid_to=None,
        base_fee=Decimal("36.00"), additional_fee=Decimal("9.00"),
        price_per_m3=Decimal("1.55"),
        notes="Preisanpassung wegen gestiegener Betriebskosten.",
    )
    db.session.add_all([t_prev, t_curr])
    db.session.flush()
    counts["tariffs"] = 2

    # ------------------------------------------------------------------
    # Konten (3 Einnahme, 3 Ausgabe)
    # ------------------------------------------------------------------
    acc_wasser = Account(code="100", name="Wasserumsatz", description="Wassergebuehren")
    acc_anschluss = Account(code="110", name="Anschlussgebuehren", description="Einmalige Anschlussgebuehren")
    acc_mahn = Account(code="120", name="Mahngebuehren", description="Vereinnahmte Mahnspesen")
    acc_reparatur = Account(code="200", name="Reparaturen", description="Reparatur- und Wartungskosten")
    acc_buero = Account(code="210", name="Buerobedarf", description="Verwaltungs-Material")
    acc_bank = Account(code="220", name="Bankkosten", description="Kontofuehrungs- und Kreditzinsen")
    db.session.add_all([acc_wasser, acc_anschluss, acc_mahn, acc_reparatur, acc_buero, acc_bank])
    db.session.flush()
    counts["accounts"] = 6

    # ------------------------------------------------------------------
    # Projekte (5 mit Farben)
    # ------------------------------------------------------------------
    project_data = [
        ("QSA", "Quellsanierung 2025", "#2ecc71", "Sanierung Hauptquelle Nord"),
        ("LTN", "Leitungstausch Nord", "#3498db", "Erneuerung Hauptleitung Nord-Süd"),
        ("HYD", "Hydrantenwartung", "#e67e22", "Jaehrliche Hydrantenwartung"),
        ("VER", "Verwaltung", "#95a5a6", "Allgemeine Verwaltungstaetigkeit"),
        ("PMW", "Pumpwerk-Modernisierung", "#9b59b6", "Tausch Druckkessel + Steuerung"),
    ]
    projects = []
    for code, name, color, desc in project_data:
        p = Project(code=code, name=name, color=color, description=desc, closed=False)
        db.session.add(p)
        projects.append(p)
    db.session.flush()
    counts["projects"] = len(projects)

    # ------------------------------------------------------------------
    # Bankkonten (Giro Default + Kredit)
    # ------------------------------------------------------------------
    giro = RealAccount(
        name="Girokonto Raika",
        description="Hauptkonto WG",
        iban="AT12 3456 7890 1111 0000",
        opening_balance=Decimal("8500.00"),
        is_default=True,
        active=True,
        icon="fa-university",
    )
    kredit = RealAccount(
        name="Kreditkonto Bauspar",
        description="Investitionskredit Quellsanierung",
        iban="AT12 3456 7890 2222 0000",
        opening_balance=Decimal("-25000.00"),
        is_default=False,
        active=True,
        icon="fa-credit-card",
    )
    db.session.add_all([giro, kredit])
    db.session.flush()
    counts["real_accounts"] = 2

    # ------------------------------------------------------------------
    # Kunden (100) — deterministische Namen / Adressen / IBAN-Felder
    # ------------------------------------------------------------------
    customers: list[Customer] = []
    member_since_base = date(prev_year - 10, 1, 1)
    for i in range(1, 101):
        vorname = rng.choice(_VORNAMEN)
        nachname = rng.choice(_NACHNAMEN)
        plz, ort = rng.choice(_ORTE)
        strasse = rng.choice(_STRASSEN)
        hausnummer = str(rng.randint(1, 120))
        # 90% mit Email, 60% mit Telefon
        email = f"{vorname.lower()}.{nachname.lower()}{i:03d}@example.at" if rng.random() < 0.9 else None
        phone = f"0664 {rng.randint(1000000, 9999999)}" if rng.random() < 0.6 else None
        member_since = member_since_base + timedelta(days=rng.randint(0, 365 * 10))
        c = Customer(
            customer_number=i,
            name=f"{vorname} {nachname}",
            is_customer=True,
            is_supplier=False,
            strasse=strasse, hausnummer=hausnummer,
            plz=plz, ort=ort, land="Österreich",
            email=email, phone=phone,
            member_since=member_since,
            rechnung_per_email=bool(email) and rng.random() < 0.4,
            active=True,
        )
        db.session.add(c)
        customers.append(c)
    db.session.flush()
    counts["customers"] = len(customers)

    # ------------------------------------------------------------------
    # Objekte (120): 80 Kunden bekommen 1, 20 Kunden bekommen 2
    # ------------------------------------------------------------------
    properties: list[Property] = []
    ownership_map: dict[int, Customer] = {}  # property.id -> customer (fuer Rechnungen)
    obj_counter = 0
    # zwei Objekte fuer Kunden 1..20, eins fuer den Rest
    for idx, customer in enumerate(customers):
        obj_count = 2 if idx < 20 else 1
        for k in range(obj_count):
            obj_counter += 1
            obj_type = rng.choice(Property.TYPES)
            # Adresse: meist gleich wie Kunde, aber bei Zweit-Objekt anders
            if k == 0:
                p = Property(
                    object_number=f"OBJ-{obj_counter:04d}",
                    object_type=obj_type,
                    strasse=customer.strasse, hausnummer=customer.hausnummer,
                    plz=customer.plz, ort=customer.ort, land="Österreich",
                    active=True,
                )
            else:
                plz, ort = rng.choice(_ORTE)
                p = Property(
                    object_number=f"OBJ-{obj_counter:04d}",
                    object_type=obj_type,
                    strasse=rng.choice(_STRASSEN),
                    hausnummer=str(rng.randint(1, 120)),
                    plz=plz, ort=ort, land="Österreich",
                    notes="Zweitobjekt",
                    active=True,
                )
            db.session.add(p)
            properties.append(p)
    db.session.flush()
    counts["properties"] = len(properties)

    # PropertyOwnerships
    for prop, cust_idx in _zip_props_to_owners(properties):
        customer = customers[cust_idx]
        owner = PropertyOwnership(
            property_id=prop.id,
            customer_id=customer.id,
            valid_from=customer.member_since or date(prev_year - 5, 1, 1),
            valid_to=None,
        )
        db.session.add(owner)
        ownership_map[prop.id] = customer
    db.session.flush()
    counts["ownerships"] = len(properties)

    # ------------------------------------------------------------------
    # Zaehler (150): 90 Objekte mit 1 Hauptzaehler, 30 Objekte mit Haupt + Sub
    # ------------------------------------------------------------------
    meters: list[WaterMeter] = []
    main_meter_for_prop: dict[int, WaterMeter] = {}
    meter_counter = 0

    # Erste 90 Objekte: nur Hauptzaehler
    for prop in properties[:90]:
        meter_counter += 1
        m = WaterMeter(
            property_id=prop.id,
            meter_number=f"M-{meter_counter:05d}",
            location=rng.choice(["Keller", "Außen", "Schacht", "Garage"]),
            installed_from=date(prev_year - 5, 1, 1),
            initial_value=Decimal("0.000"),
            eichjahr=prev_year - rng.randint(0, 6),
            meter_type="main",
            active=True,
        )
        db.session.add(m)
        meters.append(m)
        main_meter_for_prop[prop.id] = m

    # Naechste 30 Objekte: Hauptzaehler + Subzaehler
    for prop in properties[90:120]:
        meter_counter += 1
        m_main = WaterMeter(
            property_id=prop.id,
            meter_number=f"M-{meter_counter:05d}",
            location="Keller",
            installed_from=date(prev_year - 5, 1, 1),
            initial_value=Decimal("0.000"),
            eichjahr=prev_year - rng.randint(0, 6),
            meter_type="main",
            active=True,
        )
        db.session.add(m_main)
        db.session.flush()
        meters.append(m_main)
        main_meter_for_prop[prop.id] = m_main

        meter_counter += 1
        m_sub = WaterMeter(
            property_id=prop.id,
            meter_number=f"M-{meter_counter:05d}",
            location="Garten",
            installed_from=date(prev_year - 3, 4, 1),
            initial_value=Decimal("0.000"),
            eichjahr=prev_year - rng.randint(0, 6),
            meter_type="sub",
            parent_meter_id=m_main.id,
            active=True,
        )
        db.session.add(m_sub)
        meters.append(m_sub)
    db.session.flush()
    counts["meters"] = len(meters)

    # ------------------------------------------------------------------
    # Ablesungen Vorjahr (alle 150 Zaehler)
    # ------------------------------------------------------------------
    # Pro Zaehler: Stand 31.12.prev_year mit deterministischem Verbrauch
    meter_prev_value: dict[int, Decimal] = {}
    for m in meters:
        # Verbrauch im Vorjahr: 60..280 m3 (Subzaehler eher weniger)
        if m.is_sub():
            verbrauch = Decimal(rng.randint(15, 80))
        else:
            verbrauch = Decimal(rng.randint(60, 280))
        start_val = Decimal(rng.randint(50, 500))  # Bestand am Anfang Vorjahr
        end_val = start_val + verbrauch
        # initial_value ist Stand bei Einbau — wir setzen ihn auf start_val
        m.initial_value = start_val
        r = MeterReading(
            meter_id=m.id,
            billing_period_id=p_prev.id,
            reading_date=date(prev_year, 12, 31),
            value=end_val,
            consumption=verbrauch,
            created_by_id=admin.id,
        )
        db.session.add(r)
        meter_prev_value[m.id] = end_val
    db.session.flush()
    counts["readings_prev"] = len(meters)

    # ------------------------------------------------------------------
    # Aktuelle Periode + 20 Zaehlertausche
    # ------------------------------------------------------------------
    # Auswahl von 20 Hauptzaehlern, die in der aktuellen Periode getauscht werden
    main_meters_only = [m for m in meters if m.is_main()]
    swap_meters = rng.sample(main_meters_only, 20)
    swap_date = date(current_year, 6, 30)
    new_install = date(current_year, 7, 1)

    swap_count = 0
    new_meters: list[WaterMeter] = []
    for old_meter in swap_meters:
        # Endstand alt am swap_date
        prev_val = meter_prev_value[old_meter.id]
        verbrauch_h1 = Decimal(rng.randint(30, 140))
        end_old = prev_val + verbrauch_h1

        # alten Zaehler deaktivieren + Ausbau-Reading
        old_meter.installed_to = swap_date
        old_meter.active = False
        r_old = MeterReading(
            meter_id=old_meter.id,
            billing_period_id=p_curr.id,
            reading_date=swap_date,
            value=end_old,
            consumption=verbrauch_h1,
            created_by_id=admin.id,
        )
        db.session.add(r_old)

        # neuen Zaehler anlegen
        meter_counter += 1
        m_new = WaterMeter(
            property_id=old_meter.property_id,
            meter_number=f"M-{meter_counter:05d}",
            location=old_meter.location,
            installed_from=new_install,
            initial_value=Decimal("0.000"),
            eichjahr=current_year,
            meter_type="main",
            active=True,
            notes=f"Tausch fuer alten Zaehler {old_meter.meter_number}",
        )
        db.session.add(m_new)
        db.session.flush()
        new_meters.append(m_new)
        # Reading per Stichtag fuer den neuen Zaehler
        verbrauch_h2 = Decimal(rng.randint(15, 80))
        r_new = MeterReading(
            meter_id=m_new.id,
            billing_period_id=p_curr.id,
            reading_date=today,
            value=verbrauch_h2,
            consumption=verbrauch_h2,
            created_by_id=admin.id,
        )
        db.session.add(r_new)
        # Update main_meter_for_prop, falls Folge-Aktion noch zugreift
        main_meter_for_prop[old_meter.property_id] = m_new
        swap_count += 1
    counts["meter_swaps"] = swap_count

    # Nicht getauschte Zaehler: ein Reading per Stichtag fuer die aktuelle Periode
    swapped_ids = {m.id for m in swap_meters}
    for m in meters:
        if m.id in swapped_ids:
            continue
        prev_val = meter_prev_value[m.id]
        verbrauch = Decimal(rng.randint(40, 220)) if m.is_main() else Decimal(rng.randint(10, 60))
        new_val = prev_val + verbrauch
        r = MeterReading(
            meter_id=m.id,
            billing_period_id=p_curr.id,
            reading_date=today,
            value=new_val,
            consumption=verbrauch,
            created_by_id=admin.id,
        )
        db.session.add(r)
    db.session.flush()
    counts["readings_curr"] = len(meters) + swap_count  # alte+neue zusaetzlich

    # ------------------------------------------------------------------
    # Rechnungen Vorjahr (20 Stueck, gemischte Status)
    # ------------------------------------------------------------------
    # Status-Verteilung: 4 DRAFT, 8 PAID, 4 SENT, 2 CANCELLED, 2 CREDIT
    invoice_status_plan = (
        [Invoice.STATUS_DRAFT] * 4
        + [Invoice.STATUS_PAID] * 8
        + [Invoice.STATUS_SENT] * 4
        + [Invoice.STATUS_CANCELLED] * 2
        + [Invoice.STATUS_CREDIT] * 2
    )
    rng.shuffle(invoice_status_plan)

    invoice_customers = rng.sample(customers, 20)
    invoices: list[Invoice] = []
    sent_invoices: list[Invoice] = []  # fuer Mahnungen
    for idx, (cust, status) in enumerate(zip(invoice_customers, invoice_status_plan), start=1):
        # Property des Kunden suchen (erstes aus ownership_map)
        prop = next((p for p, c in ownership_map.items() if c is cust), None)
        prop_obj = db.session.get(Property, prop) if prop else None

        # Datum + Faelligkeit so waehlen, dass SENT-Rechnungen die Mahnstufen treffen
        if status == Invoice.STATUS_SENT:
            # gestaffelte Faelligkeiten: today - 18, -34, -50, -70 Tage
            stage_offsets = [18, 34, 50, 70]
            offset = stage_offsets[len(sent_invoices) % len(stage_offsets)]
            inv_date = today - timedelta(days=offset + 14)
            due = today - timedelta(days=offset)
        else:
            inv_date = date(current_year, 1, 15) + timedelta(days=idx * 3)
            due = inv_date + timedelta(days=30)

        verbrauch = Decimal(rng.randint(70, 240))
        base = t_prev.base_fee
        wasser = (t_prev.price_per_m3 * verbrauch).quantize(Decimal("0.01"))
        # Reparatur-Position fuer 20% steuersatz auf manche Rechnungen
        with_repair = rng.random() < 0.35
        repair_net = Decimal(str(rng.randint(40, 150))) if with_repair else Decimal("0.00")

        inv = Invoice(
            invoice_number=f"RE-{prev_year}-{idx:04d}",
            customer_id=cust.id,
            property_id=prop_obj.id if prop_obj else None,
            billing_period_id=p_prev.id,
            date=inv_date,
            due_date=due,
            status=status,
            total_amount=Decimal("0.00"),
            created_by_id=admin.id,
        )
        db.session.add(inv)
        db.session.flush()

        # Positionen: Grundgebuehr (10%), Wasser (10%), evtl. Reparatur (20%)
        db.session.add(InvoiceItem(
            invoice_id=inv.id,
            description="Grundgebuehr Wasserversorgung",
            quantity=Decimal("1"), unit="Stk",
            unit_price=base, amount=base,
            tax_rate=Decimal("10.00"),
        ))
        db.session.add(InvoiceItem(
            invoice_id=inv.id,
            description=f"Wasserverbrauch {prev_year} ({verbrauch} m³)",
            quantity=verbrauch, unit="m³",
            unit_price=t_prev.price_per_m3, amount=wasser,
            tax_rate=Decimal("10.00"),
        ))
        if with_repair:
            db.session.add(InvoiceItem(
                invoice_id=inv.id,
                description="Anteilige Reparatur Hausanschluss",
                quantity=Decimal("1"), unit="Stk",
                unit_price=repair_net, amount=repair_net,
                tax_rate=Decimal("20.00"),
                project_id=projects[rng.randint(0, len(projects) - 1)].id,
            ))
        db.session.flush()
        inv.recalculate_total()
        invoices.append(inv)
        if status == Invoice.STATUS_SENT:
            sent_invoices.append(inv)

        # Fuer PAID-Rechnungen: Zahlungsbuchung
        if status == Invoice.STATUS_PAID:
            grp = BookingGroup(
                date=inv_date + timedelta(days=14),
                description=f"Zahlung {inv.invoice_number}",
                reference=inv.invoice_number,
                invoice_id=inv.id,
                customer_id=cust.id,
                total_amount=inv.total_amount,
                status=BookingGroup.STATUS_AKTIV,
                created_by_id=admin.id,
            )
            db.session.add(grp)
            db.session.flush()
            b = Booking(
                date=grp.date,
                account_id=acc_wasser.id,
                amount=inv.total_amount,
                description=grp.description,
                reference=inv.invoice_number,
                invoice_id=inv.id,
                customer_id=cust.id,
                real_account_id=giro.id,
                group_id=grp.id,
                tax_rate=Decimal("10.00"),
                status=Booking.STATUS_VERBUCHT,
                created_by_id=admin.id,
            )
            db.session.add(b)
    db.session.flush()
    counts["invoices"] = len(invoices)

    # ------------------------------------------------------------------
    # Offene Posten (manuell, ohne invoice_id) + via run_data_migrations
    # die fuer SENT-Rechnungen werden vom Caller nachgezogen
    # ------------------------------------------------------------------
    extra_op1 = OpenItem(
        customer_id=customers[5].id,
        description="Anschlussgebuehr Gartenzaehler Nachruestung",
        amount=Decimal("120.00"),
        date=today - timedelta(days=45),
        due_date=today + timedelta(days=15),
        period_year=current_year,
        status=OpenItem.STATUS_OPEN,
        account_id=acc_anschluss.id,
        created_by_id=admin.id,
    )
    extra_op2 = OpenItem(
        customer_id=customers[12].id,
        description="Saeumniszuschlag manuell",
        amount=Decimal("8.50"),
        date=today - timedelta(days=20),
        due_date=today + timedelta(days=10),
        period_year=current_year,
        status=OpenItem.STATUS_OPEN,
        account_id=acc_mahn.id,
        created_by_id=admin.id,
    )
    db.session.add_all([extra_op1, extra_op2])
    db.session.flush()
    counts["open_items_manual"] = 2

    # ------------------------------------------------------------------
    # Mahnungen — fuer die 4 SENT-Rechnungen je eine Stufe (1..4)
    # ------------------------------------------------------------------
    policy = DunningPolicy.query.filter_by(is_default=True).first()
    stages = sorted(policy.stages, key=lambda s: s.level) if policy else []
    if stages and sent_invoices:
        # SENT-Rechnungen sind in Reihenfolge der stage_offsets [18,34,50,70]
        # angelegt — das matched genau die Stufen 1..4 (>=14, >=30, >=45, >=60).
        for inv, stage in zip(sent_invoices, stages):
            fee = stage.fee_fixed or Decimal("0.00")
            issued = inv.due_date + timedelta(days=stage.days_after_due)
            new_due = issued + timedelta(days=stage.new_due_days or 14)
            fee_item = None
            if fee and fee > 0:
                fee_item = InvoiceItem(
                    invoice_id=inv.id,
                    description=f"Mahngebuehr {stage.name}",
                    quantity=Decimal("1"), unit="Stk",
                    unit_price=fee, amount=fee,
                    tax_rate=None,
                    is_dunning_fee=1,
                )
                db.session.add(fee_item)
                db.session.flush()
            notice = DunningNotice(
                invoice_id=inv.id,
                stage_id=stage.id,
                level_snapshot=stage.level,
                name_snapshot=stage.name,
                print_title_snapshot=stage.print_title,
                issued_date=issued,
                new_due_date=new_due,
                fee_amount=fee,
                fee_invoice_item_id=fee_item.id if fee_item else None,
                status=DunningNotice.STATUS_AKTIV,
                created_by_id=admin.id,
            )
            db.session.add(notice)
            if fee_item:
                # back-ref im Item setzen
                db.session.flush()
                fee_item.dunning_notice_id = notice.id
    db.session.flush()
    counts["dunning_notices"] = len(sent_invoices)

    # ------------------------------------------------------------------
    # Sammelbuchungen (12) — gemischt Projekte / Steuersaetze / Bankkonten
    # ------------------------------------------------------------------
    # 3 davon auf Kreditkonto (Reparatur / Bankgebuehren / Wartung)
    booking_plan = [
        # (date, description, account, project_index, tax_rate, amount, real_account)
        (date(prev_year, 3, 10), "Reparatur Hydrant Dorfstraße", acc_reparatur, 2, Decimal("20.00"), Decimal("-450.00"), giro),
        (date(prev_year, 5, 22), "Bueromaterial Q2", acc_buero, 3, Decimal("20.00"), Decimal("-89.50"), giro),
        (date(prev_year, 6, 15), "Wartung Druckkessel", acc_reparatur, 4, Decimal("20.00"), Decimal("-1250.00"), kredit),
        (date(prev_year, 9, 5), "Bankkosten Q3", acc_bank, None, None, Decimal("-32.40"), giro),
        (date(prev_year, 11, 18), "Anschlussgebuehr Neuanlage", acc_anschluss, 1, Decimal("20.00"), Decimal("450.00"), giro),
        (date(prev_year, 12, 28), "Kreditzinsen Q4", acc_bank, None, None, Decimal("-185.00"), kredit),
        (date(current_year, 2, 11), "Leitungstausch Material", acc_reparatur, 1, Decimal("20.00"), Decimal("-2350.00"), kredit),
        (date(current_year, 3, 14), "Bueromaterial Toner", acc_buero, 3, Decimal("20.00"), Decimal("-65.00"), giro),
        (date(current_year, 5, 9), "Quellsanierung Vorarbeiten", acc_reparatur, 0, Decimal("20.00"), Decimal("-1820.00"), giro),
        (date(current_year, 6, 21), "Bankkosten Q2", acc_bank, None, None, Decimal("-28.90"), giro),
        (date(current_year, 7, 2), "Hydrantenwartung Sommer", acc_reparatur, 2, Decimal("20.00"), Decimal("-540.00"), giro),
        (date(current_year, 8, 30), "Sonder-Anschluss Bauplatz", acc_anschluss, 1, Decimal("20.00"), Decimal("900.00"), giro),
    ]
    for bdate, desc, account, proj_idx, tax, amt, ra in booking_plan:
        proj_id = projects[proj_idx].id if proj_idx is not None else None
        grp = BookingGroup(
            date=bdate, description=desc, reference=f"BG-{bdate.year}-{bdate.month:02d}{bdate.day:02d}",
            total_amount=amt, status=BookingGroup.STATUS_AKTIV,
            created_by_id=admin.id,
        )
        db.session.add(grp)
        db.session.flush()
        b = Booking(
            date=bdate, account_id=account.id, amount=amt,
            description=desc, reference=grp.reference,
            project_id=proj_id, real_account_id=ra.id,
            group_id=grp.id, tax_rate=tax,
            status=Booking.STATUS_VERBUCHT,
            created_by_id=admin.id,
        )
        db.session.add(b)
    db.session.flush()
    counts["booking_groups"] = len(booking_plan)

    # ------------------------------------------------------------------
    # Umbuchungen (3 Stueck Giro <-> Kredit)
    # ------------------------------------------------------------------
    transfers = [
        Transfer(
            date=date(prev_year, 12, 31), amount=Decimal("3000.00"),
            description="Tilgung Investitionskredit",
            from_real_account_id=giro.id, to_real_account_id=kredit.id,
            created_by_id=admin.id,
        ),
        Transfer(
            date=date(current_year, 3, 31), amount=Decimal("2000.00"),
            description="Quartalstilgung Q1",
            from_real_account_id=giro.id, to_real_account_id=kredit.id,
            created_by_id=admin.id,
        ),
        Transfer(
            date=date(current_year, 7, 15), amount=Decimal("5000.00"),
            description="Kreditauszahlung fuer Quellsanierung",
            from_real_account_id=kredit.id, to_real_account_id=giro.id,
            created_by_id=admin.id,
        ),
    ]
    db.session.add_all(transfers)
    db.session.flush()
    counts["transfers"] = 3

    if verbose:
        print("Demo-Daten-Counts:")
        for k, v in counts.items():
            print(f"  {k:24s} {v}")

    return counts


def _zip_props_to_owners(properties):
    """Ordnet Properties den Kundenindexen zu (Kunde 0..19 hat 2, Rest 1).

    Yields (property, customer_index).
    """
    idx = 0
    prop_iter = iter(properties)
    # Kunden 0..19: zwei Objekte
    for cust_idx in range(20):
        for _ in range(2):
            yield next(prop_iter), cust_idx
    # Kunden 20..99: ein Objekt
    for cust_idx in range(20, 100):
        yield next(prop_iter), cust_idx
