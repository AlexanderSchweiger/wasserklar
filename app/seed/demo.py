"""Deterministischer Demo-Datensatz fuer Test- und Screenshot-Zwecke.

Erzeugt 100 Kunden, 120 Objekte, 150 Zaehler, vollstaendige Vorjahres-Abrechnung
mit Zaehlerstaenden, laufende Periode mit ~20 Zaehlertauschen, gemischte
Rechnungen / offene Posten / Mahnungen, 2 Bankkonten + Umbuchungen,
Sammelbuchungen mit Projekten und verschiedenen Steuersaetzen, passende Tarife.

Dazu ein kompletter **Leitungsnetz**-Datensatz: ein aktiver Leitungsplan mit
3 Quellen (inkl. historischer Schuettungs-Messreihen mit Trockenperioden),
Hochbehaelter, Zubringer-/Haupt-/Versorgungsleitungen, ~30 Hausanschluessen
(grossteils Liegenschaften zugeordnet + geocodet, einige bewusst unzugeordnet
zum Testen der Zuordnen-Funktion), Hydranten/Schiebern mit Wartungs-/Pruef-Logs
(teils faellig), drei **Probenahmestellen** mit quartalsweisen **Wasserproben/
Laborwerten** (groesstenteils unauffaellig, mit einigen Grenzwert-Ueberschreitungen:
Nitrat-Trend, mikrobiologische Beanstandung, Eisen/Mangan) sowie ein gefuelltes
**Stoerungsjournal** (Rohrbrueche, Lecks, Druckverlust ...).

Dazu eine gefuellte **Schriftfuehrung** (nur im WG-Modus sichtbar): mehrere
**Vorstandssitzungen** und **Hauptversammlungen** ueber zwei Jahre — mit
Tagesordnung, Anwesenheit, Protokoll (inkl. Quorum, eine HV nach Wartefrist
erneut eroeffnet) und einem Register an **Beschluessen** (angenommen / abgelehnt
/ vertagt mit Stimmenzahlen); je eine geplante Sitzung ohne Protokoll.

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


def seed_demo_data(db, *, today: date = date(2025, 9, 15), now: date = None,
                   verbose: bool = True, author=None) -> dict:
    """Seedet den vollstaendigen Demo-Datensatz.

    Voraussetzung: DB hat die Defaults (TaxRates, DunningPolicy "Standard",
    Rollen) aber keine Geschaeftsdaten — d.h. Customer/Property/Invoice/...
    sind leer. Caller ruft ``clear-db --full`` o. Aequivalent davor auf.

    Parameter:
        db: SQLAlchemy-Instanz (`app.extensions.db`).
        today: Historischer Anker fuer den Hauptdatensatz (Perioden,
            Ablesungen, Zaehlertausche, Rechnungen). Default 2025-09-15 —
            bewusst fix, weil viele Stichtage darauf kalibriert sind (u.a. die
            statischen Bank-Sample-Dateien). NICHT auf ``date.today()`` setzen.
        now: Echtes „heute" (vom CLI = ``date.today()``). Liegt es in einem
            spaeteren Jahr als ``today``, wird die Buchhaltung bis ``now``
            fortgeschrieben: Vorjahre abgeschlossen, ein offenes Buchungsjahr
            im aktuellen Jahr, laufende Buchungen/Posten/Umbuchungen bis ``now``.
            ``None`` -> faellt auf ``today`` zurueck (Tests bleiben deterministisch
            und das Bestandsverhalten unveraendert).
        verbose: Print-Output.
        author: Optionaler ``User``, der als Autor (``created_by_id`` etc.) aller
            erzeugten Eintraege verwendet wird. Wird er gesetzt (Web-/Self-
            Service-Pfad, z.B. SaaS-Danger-Zone), legt der Seeder KEINE Demo-
            Logins an. ``None`` (CLI-Pfad) -> die Demo-User ``admin``/``kassier``
            mit Passwort ``demo1234`` werden wie gehabt angelegt.

    Rueckgabe: Dict mit Counts pro Entitaet (fuer Tests / Smoke-Asserts).
    """
    from datetime import datetime
    from app.models import (
        Role, User, Customer, Property, PropertyOwnership, WaterMeter,
        MeterReading, MeterReplacement, BillingPeriod, WaterTariff, Account,
        Project, RealAccount,
        Transfer, Invoice, InvoiceItem, BookingGroup, Booking, OpenItem,
        DunningPolicy, DunningStage, DunningNotice, FiscalYear,
        NetworkPlan, NetworkFeature, MaintenanceLog, SpringYield, Incident,
        WaterSample, LabResult,
        Meeting, MeetingAgendaItem, MeetingResolution, MeetingAttendance,
        MeetingProtocol,
    )

    if now is None:
        now = today
    rng = random.Random(42)
    current_year = today.year
    prev_year = current_year - 1
    counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Autor-User (fuer created_by_id auf allen Folge-Eintraegen)
    # ------------------------------------------------------------------
    # Web-/Self-Service-Pfad reicht ``author`` herein: dann ist der eingeloggte
    # (Tenant-)Admin der Autor und es werden KEINE Demo-Logins angelegt — die
    # haetten in einem echten Mandanten ein Konto mit dem schwachen Passwort
    # "demo1234" erzeugt. Der CLI-Pfad (author=None) legt sie wie bisher an.
    if author is not None:
        admin = author
    else:
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
        # Explizites Tausch-Event (alt->neu-Paarung + Snapshot)
        db.session.add(MeterReplacement(
            property_id=old_meter.property_id,
            old_meter_id=old_meter.id,
            new_meter_id=m_new.id,
            billing_period_id=p_curr.id,
            replacement_date=swap_date,
            final_value=end_old,
            new_initial_value=Decimal("0.000"),
            created_by_id=admin.id,
        ))
        # Update main_meter_for_prop, falls Folge-Aktion noch zugreift
        main_meter_for_prop[old_meter.property_id] = m_new
        swap_count += 1
    counts["meter_swaps"] = swap_count
    counts["meter_replacements"] = swap_count

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

    # ==================================================================
    # Leitungsnetz (network) — Plan, Anlagen, Hausanschluesse, Quellen,
    # Schuettungs-Messreihen, Wartung; danach Stoerungsjournal.
    # ==================================================================
    import json
    import math
    from app.network import services as net_svc

    # Koordinaten-Helfer: Meter-Offsets (Nord/Ost) ab einem Ortsmittelpunkt in
    # WGS84 umrechnen. Synthetischer Ort rund um Hagenberg im Muehlkreis.
    BASE_LAT, BASE_LNG = 48.3680, 14.5120
    M_PER_DEG_LAT = 111320.0
    _cos_lat = math.cos(math.radians(BASE_LAT))

    def ll(north_m, east_m):
        """(Nord-, Ost-Offset in m) -> (lat, lng)."""
        lat = BASE_LAT + north_m / M_PER_DEG_LAT
        lng = BASE_LNG + east_m / (M_PER_DEG_LAT * _cos_lat)
        return lat, lng

    plan = NetworkPlan(
        name="Leitungsnetz (Demo)",
        status=NetworkPlan.STATUS_ACTIVE,
        maintenance_enabled=True,
        description="Demonstrations-Leitungsplan: Quellen, Hochbehaelter, "
                    "Versorgungsnetz, Hausanschluesse, Hydranten und Schieber "
                    "rund um den Demo-Ort.",
        created_by_id=admin.id, updated_by_id=admin.id,
    )
    db.session.add(plan)
    db.session.flush()
    counts["network_plans"] = 1

    net_features: list = []

    def add_point(ftype, name, north, east, **kw):
        lat, lng = ll(north, east)
        f = NetworkFeature(
            plan_id=plan.id, feature_type=ftype, name=name, created_by_id=admin.id,
        )
        net_svc.apply_geometry(f, {"type": "Point", "coordinates": [lng, lat]})
        for k, v in kw.items():
            setattr(f, k, v)
        db.session.add(f)
        net_features.append(f)
        return f

    def add_line(ftype, name, waypoints, **kw):
        coords = []
        for (n, e) in waypoints:
            la, lo = ll(n, e)
            coords.append([lo, la])
        f = NetworkFeature(
            plan_id=plan.id, feature_type=ftype, name=name, created_by_id=admin.id,
        )
        net_svc.apply_geometry(f, {"type": "LineString", "coordinates": coords})
        for k, v in kw.items():
            setattr(f, k, v)
        db.session.add(f)
        net_features.append(f)
        return f

    # --- Anlagen (Punkte) ---------------------------------------------
    behaelter_pos = (520, 90)
    behaelter = add_point(
        "behaelter", "Hochbehälter Sonnberg", *behaelter_pos,
        accuracy="exakt", material="Beton", year_built=1987,
        ground_level_m=512.0, notes="Nutzinhalt 150 m³, zwei Kammern.",
    )
    add_point("pumpe", "Druckerhöhung Sonnberg", 500, 78,
              accuracy="exakt", year_built=2009, manufacturer="Grundfos")
    pumpe = net_features[-1]
    # Drei Probenahmestellen (Rohwasser an der Quelle, Behälterabgang, Ortsnetz)
    # — bewusst ohne rng-Parameter, damit die nachfolgenden rng-Ziehungen
    # (Quellen/Strassen/Hydranten) unveraendert deterministisch bleiben.
    probe_behaelter = add_point(
        "probenahme", "Probenahmestelle Behälterabgang", 514, 92, accuracy="gut",
        notes="Reinwasser-Beprobung am Behälterabgang.")
    add_point("verteiler", "Ortsverteiler", 10, 120, accuracy="gut")
    probe_ortsnetz = add_point(
        "probenahme", "Probenahmestelle Ortsnetz", 8, 150, accuracy="gut",
        notes="Zapfstelle am Ende des Versorgungsstrangs (Ortsnetz).")
    probe_quelle = add_point(
        "probenahme", "Probenahmestelle Quelle Brunnertal", 895, -290, accuracy="gut",
        notes="Rohwasser-Beprobung an der Quellfassung.")

    # 3 Quellen mit je (Position, Basis-Schuettung l/s, Saison-Amplitude,
    # Sommer-Trockenheitsfaktor) — Steinbründl faellt im Trockensommer fast trocken.
    spring_cfg = [
        ("Quelle Brunnertal",  (900, -300), 2.40, 0.28, 0.62),
        ("Quelle Lärchwald",   (1010, 220), 1.10, 0.34, 0.48),
        ("Quelle Steinbründl", (840, 600),  0.50, 0.42, 0.28),
    ]
    springs = []
    for sp_name, sp_pos, base, amp, drought in spring_cfg:
        sp = add_point(
            "quelle", sp_name, *sp_pos, accuracy="exakt",
            year_built=rng.randint(1958, 1992), notes="Gefasste Hangquelle.",
        )
        springs.append({"f": sp, "pos": sp_pos, "base": base, "amp": amp,
                        "drought": drought})

    # --- Transport-/Versorgungsleitungen (Linien) ---------------------
    for sp in springs:
        add_line("zubringer", f"Zubringer {sp['f'].name}",
                 [sp["pos"], (700, behaelter_pos[1]), behaelter_pos],
                 accuracy="geschaetzt", material="PE",
                 dimension_dn=rng.choice([80, 100]), year_built=rng.randint(1985, 2010),
                 pressure_rating="PN 10")

    hauptleitung = add_line(
        "hauptleitung", "Hauptleitung Hochbehälter–Ort",
        [behaelter_pos, (300, 108), (10, 120)],
        accuracy="gut", material="Duktilguss (GGG)", dimension_dn=150,
        year_built=1992, pressure_rating="PN 10",
    )

    # --- Versorgungsstraenge + Hausanschluesse ------------------------
    # Je Strasse eine Versorgungsleitung; entlang sechs Hausanschluesse, jeweils
    # mit Stichleitung und einer geocodeten Liegenschaft (BEV-Treffer simuliert).
    streets = [
        ("Dorfstraße",  (10, 120),  (0, 27)),     # nach Osten
        ("Hauptstraße", (-40, 116), (-26, 3)),    # nach Suedwesten
        ("Birkenweg",   (16, 124),  (21, 21)),    # nach Nordosten
        ("Quellweg",    (4, 116),   (9, -25)),    # nach Westen
    ]
    assigned_props = properties[:24]
    versorg_lines = []
    ha_stub_example = None
    ha_idx = 0
    geocoded_count = 0
    geocode_ts = datetime(current_year, 3, 15, 9, 0, 0)

    for st_name, (s_n, s_e), (d_n, d_e) in streets:
        n_houses = 6
        end = (s_n + d_n * (n_houses + 1), s_e + d_e * (n_houses + 1))
        vleitung = add_line(
            "versorgungsleitung", f"Versorgungsleitung {st_name}",
            [(s_n, s_e), end], accuracy="gut",
            material=rng.choice(["PE", "Guss (GG)", "PVC"]),
            dimension_dn=rng.choice([80, 100, 100, 125]),
            year_built=rng.randint(1978, 2016), pressure_rating="PN 10",
        )
        versorg_lines.append(vleitung)
        # Einheits-Perpendikular (Meter) fuer den seitlichen Hausversatz.
        mag = math.hypot(d_n, d_e)
        pn, pe = d_e / mag, -d_n / mag
        for j in range(1, n_houses + 1):
            base_n, base_e = s_n + d_n * j, s_e + d_e * j
            side = 1 if j % 2 else -1
            ha_n, ha_e = base_n + side * 14 * pn, base_e + side * 14 * pe
            prop = assigned_props[ha_idx]
            add_point("hausanschluss", None, ha_n, ha_e, accuracy="gut",
                      material="PE", dimension_dn=rng.choice([25, 32, 40]),
                      year_built=rng.randint(1980, 2020), property_id=prop.id)
            stub = add_line("hausanschlussleitung", None,
                            [(base_n, base_e), (ha_n, ha_e)],
                            accuracy="geschaetzt", material="PE", dimension_dn=25)
            if ha_stub_example is None:
                ha_stub_example = stub
            # Liegenschaft ~4 m neben dem Hausanschluss geocoden (BEV-Treffer).
            plat, plng = ll(ha_n + rng.uniform(-4, 4), ha_e + rng.uniform(-4, 4))
            prop.lat, prop.lng = round(plat, 6), round(plng, 6)
            prop.geocoded_at = geocode_ts
            geocoded_count += 1
            ha_idx += 1

    # 3 unzugeordnete Hausanschluesse NAHE je einer freien geocodeten
    # Liegenschaft -> per „Zuordnen"-Button (assign-hausanschluss) loesbar.
    for k, prop in enumerate(properties[24:27]):
        base_n, base_e = [(-30, 60), (-58, 72), (44, -78)][k]
        plat, plng = ll(base_n, base_e)
        prop.lat, prop.lng = round(plat, 6), round(plng, 6)
        prop.geocoded_at = geocode_ts
        geocoded_count += 1
        add_point("hausanschluss", None, base_n + 12, base_e + 4, accuracy="gut",
                  material="PE", dimension_dn=32, property_id=None)

    # 3 unzugeordnete Hausanschluesse ohne Liegenschaft im Umkreis -> bleiben
    # auch nach dem Zuordnen-Lauf grell markiert (kein Kandidat im Radius).
    for k in range(3):
        add_point("hausanschluss", None, -380 - k * 25, 540 + k * 18,
                  accuracy="geschaetzt", material="PE", dimension_dn=25,
                  property_id=None)

    # --- Hydranten & Schieber -----------------------------------------
    hydranten = []
    for i, (hn, he) in enumerate(
            [(0, 180), (-30, 60), (90, 175), (130, 165), (300, 108), (12, 122)], start=1):
        hydranten.append(add_point(
            "hydrant", f"Hydrant H{i:02d}", hn, he, accuracy="gut",
            year_built=rng.randint(1990, 2021),
            manufacturer=rng.choice(["HAWLE", "VONROLL", "Düker"])))

    schieber = []
    for i, (sn, se) in enumerate(
            [(260, 104), (0, 250), (-26, 40), (35, 130), (520, 86)], start=1):
        schieber.append(add_point(
            "schieber", f"Schieber S{i:02d}", sn, se, accuracy="gut",
            dimension_dn=rng.choice([80, 100, 125]),
            manufacturer=rng.choice(["HAWLE", "VONROLL"])))

    db.session.flush()  # alle Features -> IDs
    counts["network_features"] = len(net_features)
    counts["properties_geocoded"] = geocoded_count
    counts["hausanschluss_unassigned"] = net_svc.count_unassigned_hausanschluss(plan.id)

    # --- Wartungs-/Pruef-Logs (teils faellig) -------------------------
    # (feature, Art, letzte Durchfuehrung, Intervall Monate, Ergebnis)
    maint_plan = [
        (hydranten[0], MaintenanceLog.KIND_FLUSH, date(prev_year, 5, 12), 12, "ok"),
        (hydranten[1], MaintenanceLog.KIND_FLUSH, date(current_year, 4, 8), 12, "ok"),
        (hydranten[2], MaintenanceLog.KIND_FLUSH, date(current_year, 8, 20), 12, "mangel"),
        (hydranten[3], MaintenanceLog.KIND_FLUSH, date(prev_year, 9, 3), 12, "ok"),
        (hydranten[4], MaintenanceLog.KIND_FLUSH, date(current_year, 6, 30), 12, "ok"),
        (hydranten[5], MaintenanceLog.KIND_FLUSH, date(current_year, 7, 15), 12, "ok"),
        (schieber[0], MaintenanceLog.KIND_FUNCTION_TEST, date(prev_year - 1, 10, 5), 24, "ok"),
        (schieber[1], MaintenanceLog.KIND_FUNCTION_TEST, date(current_year, 3, 18), 24, "ok"),
        (schieber[2], MaintenanceLog.KIND_FUNCTION_TEST, date(prev_year, 11, 2), 24, "mangel"),
        (schieber[3], MaintenanceLog.KIND_FUNCTION_TEST, date(prev_year - 1, 6, 14), 24, "ok"),
        (schieber[4], MaintenanceLog.KIND_FUNCTION_TEST, date(current_year, 5, 9), 24, "ok"),
        (behaelter, MaintenanceLog.KIND_INSPECTION, date(current_year, 4, 2), 12, "ok"),
        (springs[0]["f"], MaintenanceLog.KIND_INSPECTION, date(prev_year, 8, 1), 12, "ok"),
        (springs[1]["f"], MaintenanceLog.KIND_INSPECTION, date(current_year, 5, 20), 24, "ok"),
        (springs[2]["f"], MaintenanceLog.KIND_INSPECTION, date(current_year, 7, 1), 12, "ok"),
    ]
    for feat, kind, last_date, interval, result in maint_plan:
        db.session.add(MaintenanceLog(
            feature_id=feat.id, date=last_date, kind=kind, result=result,
            interval_months=interval,
            next_due=net_svc.add_months(last_date, interval),
            performed_by="Wassermeister Huber", created_by_id=admin.id,
            notes=("Mangel dokumentiert, Nacharbeit veranlasst." if result == "mangel"
                   else None),
        ))
    counts["maintenance_logs"] = len(maint_plan)

    # --- Quellschuettung (historisch, mit Trockenperioden) ------------
    def _month_grid(start_year):
        """Mid-Month-Messdaten von Jan ``start_year`` bis zum Monat von ``today``."""
        out, y, m = [], start_year, 1
        while (y < today.year) or (y == today.year and m <= today.month):
            out.append(date(y, m, 15))
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out

    yield_count = 0
    grid = _month_grid(current_year - 2)
    for sp in springs:
        base, amp, drought = sp["base"], sp["amp"], sp["drought"]
        for d in grid:
            # Saison: Maximum ~April (Schneeschmelze), Minimum ~Oktober.
            seasonal = math.cos((d.month - 4) / 12.0 * 2 * math.pi)
            val = base * (1 + amp * seasonal)
            note = None
            if d.year == current_year and d.month in (6, 7, 8, 9, 10):
                val *= drought                     # schwerer Trockensommer akt. Jahr
                if d.month in (8, 9):
                    note = "Ausgeprägte Trockenperiode."
            elif d.year == prev_year and d.month in (7, 8, 9):
                val *= 0.78                         # milder Trockensommer Vorjahr
            if d.year == current_year - 2 and d.month in (3, 4, 5):
                val *= 1.18                         # nasses Fruehjahr vor zwei Jahren
            val += rng.uniform(-0.05, 0.05) * base  # Messrauschen
            val = max(0.03, val)
            db.session.add(SpringYield(
                feature_id=sp["f"].id, measurement_date=d,
                flow_rate_lps=Decimal(str(round(val, 3))),
                notes=note, created_by_id=admin.id,
            ))
            yield_count += 1
    counts["spring_yields"] = yield_count

    # ------------------------------------------------------------------
    # Stoerungsjournal (incidents)
    # ------------------------------------------------------------------
    def _rep_point(f):
        """Repraesentativer (lat, lng) eines Features (Punkt -> selbst, Linie -> Mitte)."""
        if f.lat is not None and f.lng is not None:
            return f.lat, f.lng
        coords = json.loads(f.geometry)["coordinates"]
        mid = coords[len(coords) // 2]
        return mid[1], mid[0]

    def add_incident(*, title, itype, sev, status, cause, detected_off, repair_days,
                     feature=None, water_loss=None, affected=None, cost=None,
                     performed_by=None, desc=None, repair=None, customer_id=None,
                     property_id=None, loc_desc=None):
        det = today - timedelta(days=detected_off)
        res = (det + timedelta(days=repair_days)
               if (status == Incident.STATUS_RESOLVED and repair_days is not None) else None)
        lat = lng = geo = None
        if feature is not None:
            rlat, rlng = _rep_point(feature)
            rlat += rng.uniform(-0.00010, 0.00010)
            rlng += rng.uniform(-0.00012, 0.00012)
            lat, lng = round(rlat, 6), round(rlng, 6)
            geo = json.dumps({"type": "Point", "coordinates": [lng, lat]})
        db.session.add(Incident(
            title=title, incident_type=itype, severity=sev, status=status, cause=cause,
            detected_at=det, resolved_at=res, location_geojson=geo, lat=lat, lng=lng,
            location_description=loc_desc, water_loss_m3=water_loss,
            affected_count=affected, cost=cost, performed_by=performed_by,
            description=desc, repair_notes=repair, customer_id=customer_id,
            property_id=property_id,
            feature_id=(feature.id if feature is not None else None),
            created_by_id=admin.id,
        ))

    p_inc = assigned_props[2]
    c_inc = ownership_map.get(p_inc.id)

    add_incident(
        title="Rohrbruch Hauptleitung Hochbehälter", itype=Incident.TYPE_ROHRBRUCH,
        sev=Incident.SEVERITY_CRITICAL, status=Incident.STATUS_RESOLVED,
        cause="frostschaden", detected_off=420, repair_days=1, feature=hauptleitung,
        water_loss=Decimal("85.00"), affected=42, cost=Decimal("3200.00"),
        performed_by="Tiefbau Mayr GmbH", loc_desc="Böschung unterhalb Hochbehälter",
        desc="Längsriss an der Gussleitung nach Frostperiode, großflächiger "
             "Wasseraustritt an der Böschung.",
        repair="Rohrabschnitt (3 m) getauscht, Bettung erneuert, Fahrbahn "
               "provisorisch verschlossen.")
    add_incident(
        title="Undichtheit Versorgungsleitung Dorfstraße", itype=Incident.TYPE_UNDICHTHEIT,
        sev=Incident.SEVERITY_MEDIUM, status=Incident.STATUS_RESOLVED,
        cause="korrosion", detected_off=300, repair_days=3, feature=versorg_lines[0],
        water_loss=Decimal("22.50"), affected=0, cost=Decimal("780.00"),
        performed_by="Eigene Crew", loc_desc="Muffe Höhe Dorfstraße 14",
        desc="Schleichendes Muffenleck, durch feuchte Stelle im Belag aufgefallen.",
        repair="Muffe nachgezogen und abgedichtet.")
    add_incident(
        title="Druckverlust Netzbereich Ost", itype=Incident.TYPE_DRUCKVERLUST,
        sev=Incident.SEVERITY_HIGH, status=Incident.STATUS_IN_PROGRESS,
        cause="ueberdruck", detected_off=12, repair_days=None, feature=versorg_lines[2],
        affected=0, loc_desc="Birkenweg",
        desc="Wiederkehrender Druckabfall in den Abendstunden, Ursache wird "
             "eingegrenzt (Schieberstellung / verdeckte Leckage).")
    add_incident(
        title="Trübung nach Starkregen", itype=Incident.TYPE_VERSCHMUTZUNG,
        sev=Incident.SEVERITY_HIGH, status=Incident.STATUS_RESOLVED,
        cause="unbekannt", detected_off=210, repair_days=5, feature=behaelter,
        affected=60, cost=Decimal("450.00"), performed_by="Eigene Crew + Labor",
        loc_desc="Hochbehälter Sonnberg",
        desc="Eintrübung im Zulauf nach Starkregen, Verdacht Oberflächenwasser-"
             "eintrag an der Quellfassung.",
        repair="Behälter gespült, Beprobung veranlasst (Befund unauffällig), "
               "Quellschacht abgedichtet.")
    add_incident(
        title="Baggerschaden Hausanschluss", itype=Incident.TYPE_ROHRBRUCH,
        sev=Incident.SEVERITY_MEDIUM, status=Incident.STATUS_RESOLVED,
        cause="fremdeinwirkung", detected_off=150, repair_days=1, feature=ha_stub_example,
        water_loss=Decimal("6.00"), affected=1, cost=Decimal("540.00"),
        performed_by="Tiefbau Mayr GmbH",
        customer_id=(c_inc.id if c_inc else None), property_id=p_inc.id,
        loc_desc="Grundstückszufahrt",
        desc="Hausanschlussleitung bei Erdarbeiten eines Anrainers beschädigt.",
        repair="Leitung auf 2 m erneuert, Anschluss wiederhergestellt.")
    add_incident(
        title="Versorgungsausfall durch Stromausfall", itype=Incident.TYPE_AUSFALL,
        sev=Incident.SEVERITY_MEDIUM, status=Incident.STATUS_RESOLVED,
        cause="unbekannt", detected_off=95, repair_days=1, feature=pumpe,
        affected=120, performed_by="Eigene Crew", loc_desc="Druckerhöhung Sonnberg",
        desc="Stromausfall legte die Druckerhöhung lahm, Druckabfall in Hochzonen.",
        repair="Notstrom angeschlossen, nach Netzwiederkehr Normalbetrieb.")
    add_incident(
        title="Schleichendes Leck Hauptstraße", itype=Incident.TYPE_UNDICHTHEIT,
        sev=Incident.SEVERITY_LOW, status=Incident.STATUS_OPEN,
        cause="materialermuedung", detected_off=8, repair_days=None,
        feature=versorg_lines[1], loc_desc="Hauptstraße",
        desc="Geringe Dauerleckage anhand der Nachtmengenmessung vermutet, "
             "Ortung steht aus.")
    add_incident(
        title="Rohrbruch Quellweg (Setzung)", itype=Incident.TYPE_ROHRBRUCH,
        sev=Incident.SEVERITY_HIGH, status=Incident.STATUS_IN_PROGRESS,
        cause="erddruck", detected_off=20, repair_days=None, feature=versorg_lines[3],
        water_loss=Decimal("40.00"), affected=8, loc_desc="Quellweg",
        desc="Rohrbruch nach Hangsetzung, Versorgung über Schieber umgeleitet.")
    add_incident(
        title="Hydrant undicht", itype=Incident.TYPE_SONSTIGES,
        sev=Incident.SEVERITY_LOW, status=Incident.STATUS_RESOLVED,
        cause="montagefehler", detected_off=60, repair_days=2, feature=hydranten[1],
        water_loss=Decimal("1.50"), affected=0, cost=Decimal("120.00"),
        performed_by="Eigene Crew", loc_desc="Hydrant H02",
        desc="Entwässerung des Hydranten undicht, ständiger Wasseraustritt.",
        repair="Dichtung getauscht, Funktionsprüfung ok.")
    db.session.flush()
    counts["incidents"] = 9

    # ==================================================================
    # Wasserproben / Laborwerte (TWV-Beprobung)
    # ==================================================================
    # Quartalsweise Befunde je Probenahmestelle, groesstenteils unauffaellig,
    # mit einigen bewusst gesetzten Grenzwert-Ueberschreitungen: mikrobiologische
    # Beanstandung an der Quelle nach Starkregen, Nitrat-Trend ueber den
    # Grenzwert im Ortsnetz, Eisen/Mangan-Event am Behaelter. Status/Einheit/
    # Grenzwert werden — wie in der Route ``network.sample_add`` — zur
    # Erfassungszeit per ``water_quality`` gesnapshotet. Steht am Ende der
    # rng-nutzenden Abschnitte, damit fruehere Ziehungen unveraendert bleiben.
    from app.network import water_quality as wq

    def _panel(**ov):
        """Unauffaelliger TWV-Standardbefund; ``ov`` ueberschreibt einzelne Werte."""
        base = {
            "e_coli": 0, "enterokokken": 0, "coliforme": 0,
            "koloniezahl_22": 5, "koloniezahl_37": 1,
            "nitrat": 12.0, "nitrit": 0.01, "ammonium": 0.02,
            "ph": 7.4, "leitfaehigkeit": 470, "truebung": 0.20,
            "eisen": 0.02, "mangan": 0.01, "gesamthaerte": 12,
        }
        base.update(ov)
        return base

    probe_dates = []
    for _y in (prev_year, current_year):
        for _mo in (2, 5, 8, 11):
            _d = date(_y, _mo, 10)
            if _d <= today:
                probe_dates.append(_d)

    _labs = ["Landeslabor OÖ", "AGES Linz", "Hydro-Labor GmbH"]
    ws_count = lr_count = ws_alarm = befund_seq = 0

    def add_sample(feature, when, values, *, notes=None, sample_type="Routine"):
        nonlocal ws_count, lr_count, ws_alarm, befund_seq
        befund_seq += 1
        s = WaterSample(
            feature_id=feature.id, sample_date=when, lab_name=rng.choice(_labs),
            sample_no=f"B-{when.year}-{befund_seq:03d}", sample_type=sample_type,
            notes=notes, created_by_id=admin.id,
        )
        had_alarm = False
        for key, val in values.items():
            num = Decimal(str(val))
            status = wq.assess(key, num)
            had_alarm = had_alarm or (status == wq.STATUS_ALARM)
            s.results.append(LabResult(
                parameter_key=key, value_num=num,
                unit=wq.parameter_unit(key) or None,
                limit_text=wq.limit_display(key) or None,
                status=status,
            ))
            lr_count += 1
        db.session.add(s)
        ws_count += 1
        if had_alarm:
            ws_alarm += 1

    n_dates = len(probe_dates)
    for i, d in enumerate(probe_dates):
        # --- Behälterabgang: meist sauber; einmal Eisen/Mangan erhoeht. ---
        ov = {"nitrat": round(10 + rng.uniform(-2, 3), 1),
              "ph": round(7.3 + rng.uniform(-0.2, 0.3), 2),
              "leitfaehigkeit": rng.randint(440, 520)}
        notes = None
        if d.year == current_year and d.month == 2:
            ov.update(eisen=0.28, mangan=0.07, truebung=0.9)
            notes = ("Erhöhte Eisen-/Manganwerte nach Stagnation — Behälter "
                     "gespült, Nachprobe unauffällig.")
        add_sample(probe_behaelter, d, _panel(**ov), notes=notes)

        # --- Ortsnetz: Nitrat-Trend nach oben (20 -> 60 mg/l). ---
        nitrat = 20 + (i / max(1, n_dates - 1)) * 40
        ov = {"nitrat": round(nitrat, 1),
              "ph": round(7.2 + rng.uniform(-0.2, 0.3), 2),
              "leitfaehigkeit": rng.randint(520, 720),
              "gesamthaerte": rng.randint(14, 22)}
        notes = ("Nitrat über Parameterwert (50 mg/l) — landwirtschaftlicher "
                 "Eintrag, Beobachtung läuft." if nitrat > 50 else None)
        add_sample(probe_ortsnetz, d, _panel(**ov), notes=notes)

        # --- Quelle: ein mikrobiologischer Befund nach Starkregen. ---
        ov = {"nitrat": round(6 + rng.uniform(-1, 2), 1),
              "ph": round(7.0 + rng.uniform(-0.2, 0.3), 2),
              "leitfaehigkeit": rng.randint(360, 460),
              "truebung": round(rng.uniform(0.1, 0.4), 2)}
        notes, stype = None, "Routine"
        if d.year == current_year and d.month == 5:
            ov.update(e_coli=8, coliforme=22, enterokokken=3, truebung=3.1,
                      koloniezahl_22=180)
            notes = ("Mikrobiologische Beanstandung nach Starkregen "
                     "(Oberflächenwassereinfluss) — Abkochgebot, Desinfektion "
                     "und Nachprobe veranlasst.")
            stype = "Anlassbezogen"
        add_sample(probe_quelle, d, _panel(**ov), notes=notes, sample_type=stype)

    db.session.flush()
    counts["water_samples"] = ws_count
    counts["lab_results"] = lr_count
    counts["water_samples_with_alarm"] = ws_alarm

    # ==================================================================
    # Buchhaltung — Fortschreibung bis zum aktuellen Datum (``now``)
    # ==================================================================
    # Liegt ``now`` (CLI: date.today()) in einem spaeteren Jahr als der
    # historische Anker ``today``, reicht der Hauptdatensatz nur bis ``today``.
    # Damit die Buchhaltung bis in die Gegenwart laeuft: das bisher offene
    # aktuelle Jahr (``current_year``) abschliessen, fuer jedes Jahr bis
    # ``now.year`` ein Buchungsjahr anlegen (nur das letzte offen) und ein paar
    # laufende Buchungen / Offene Posten / eine Umbuchung bis ``now`` erzeugen.
    # ``now == today`` (Default/Tests) -> Block uebersprungen, alles unveraendert.
    if now.year > current_year:
        # 1) Bisher offenes aktuelles Jahr abschliessen.
        fy_curr.closed = True
        fy_curr.closed_at = datetime(current_year + 1, 1, 31, 12, 0, 0)
        fy_curr.closed_by_id = admin.id

        # 2) Buchungsjahre current_year+1 .. now.year — nur das letzte offen.
        bridge_years = list(range(current_year + 1, now.year + 1))
        for y in bridge_years:
            is_latest = (y == now.year)
            db.session.add(FiscalYear(
                year=y, start_date=date(y, 1, 1), end_date=date(y, 12, 31),
                closed=not is_latest,
                closed_at=(None if is_latest else datetime(y + 1, 1, 31, 12, 0, 0)),
                closed_by_id=(None if is_latest else admin.id),
                is_vat_liable=False,
            ))
        db.session.flush()
        counts["fiscal_years"] += len(bridge_years)
        counts["current_fiscal_year"] = now.year

        # 3) Laufende Buchungen, ein Eintrag je Monat ab Jan des ersten neuen
        #    Jahres bis ``now`` (Mitte des Monats). Plan wird zyklisch genutzt;
        #    Buchungen der letzten ~40 Tage bleiben „Offen" (noch nicht verbucht).
        bridge_plan = [
            (acc_reparatur, 1, Decimal("20.00"), Decimal("-380.00"),  giro,   "Reparatur Schieber Dorfstraße"),
            (acc_buero,     3, Decimal("20.00"), Decimal("-54.90"),   giro,   "Büromaterial"),
            (acc_bank,   None, None,             Decimal("-26.50"),   giro,   "Bankkosten"),
            (acc_wasser, None, Decimal("10.00"), Decimal("1240.00"),  giro,   "Sammel-Zahlungseingang Wassergebühren"),
            (acc_reparatur, 2, Decimal("20.00"), Decimal("-210.00"),  giro,   "Hydrantenwartung Frühjahr"),
            (acc_anschluss, 1, Decimal("20.00"), Decimal("450.00"),   giro,   "Anschlussgebühr Neubau"),
            (acc_reparatur, 4, Decimal("20.00"), Decimal("-1340.00"), kredit, "Pumpentausch Material"),
            (acc_buero,     3, Decimal("20.00"), Decimal("-72.00"),   giro,   "Toner / Porto"),
            (acc_bank,   None, None,             Decimal("-185.00"),  kredit, "Kreditzinsen"),
            (acc_reparatur, 0, Decimal("20.00"), Decimal("-560.00"),  giro,   "Quellschacht-Sanierung Material"),
        ]
        offen_cutoff = now - timedelta(days=40)
        bridge_bookings = 0
        gy, gm = current_year + 1, 1
        plan_i = 0
        while (gy < now.year) or (gy == now.year and gm <= now.month):
            bdate = date(gy, gm, 14)
            account, proj_idx, tax, amt, ra, desc = bridge_plan[plan_i % len(bridge_plan)]
            status_b = (Booking.STATUS_OFFEN if bdate > offen_cutoff
                        else Booking.STATUS_VERBUCHT)
            ref = f"BG-{bdate.year}-{bdate.month:02d}{bdate.day:02d}"
            grp = BookingGroup(
                date=bdate, description=desc, reference=ref, total_amount=amt,
                status=BookingGroup.STATUS_AKTIV, created_by_id=admin.id,
            )
            db.session.add(grp)
            db.session.flush()
            db.session.add(Booking(
                date=bdate, account_id=account.id, amount=amt, description=desc,
                reference=ref, project_id=(projects[proj_idx].id if proj_idx is not None else None),
                real_account_id=ra.id, group_id=grp.id, tax_rate=tax,
                status=status_b, created_by_id=admin.id,
            ))
            bridge_bookings += 1
            plan_i += 1
            gm += 1
            if gm > 12:
                gm, gy = 1, gy + 1
        counts["current_year_bookings"] = bridge_bookings

        # 4) Ein paar offene Posten (laufende Forderungen) im aktuellen Jahr.
        bridge_open_items = [
            OpenItem(
                customer_id=customers[7].id,
                description="Anschlussgebühr Erweiterung Gartenzähler",
                amount=Decimal("260.00"), date=now - timedelta(days=24),
                due_date=now + timedelta(days=6), period_year=now.year,
                status=OpenItem.STATUS_OPEN, account_id=acc_anschluss.id,
                created_by_id=admin.id),
            OpenItem(
                customer_id=customers[31].id,
                description="Akontozahlung Wasser offen",
                amount=Decimal("95.00"), date=now - timedelta(days=12),
                due_date=now + timedelta(days=18), period_year=now.year,
                status=OpenItem.STATUS_OPEN, account_id=acc_wasser.id,
                created_by_id=admin.id),
        ]
        db.session.add_all(bridge_open_items)
        counts["current_year_open_items"] = len(bridge_open_items)

        # 5) Eine Umbuchung (Quartalstilgung) im aktuellen Jahr.
        db.session.add(Transfer(
            date=now - timedelta(days=70), amount=Decimal("2000.00"),
            description=f"Quartalstilgung {now.year}",
            from_real_account_id=giro.id, to_real_account_id=kredit.id,
            created_by_id=admin.id,
        ))
        counts["current_year_transfers"] = 1
        db.session.flush()

    # ==================================================================
    # Schriftfuehrung — Vorstandssitzungen, Hauptversammlungen, Beschluesse
    # ==================================================================
    # Nur im WG-Modus (org.type=cooperative) in der UI sichtbar; die Daten werden
    # aber immer angelegt (die Tabellen existieren unabhaengig vom Mandant-Typ —
    # org.type wird vom Seed bewusst NICHT gesetzt). Voll deterministisch (kein
    # rng), steht ohnehin am Ende. Anwesenheit + Beschluesse referenzieren die
    # geseedeten Kunden als Mitglieder; Autor ist ``admin`` (= ``author`` im Web-Pfad).
    from datetime import time as _time

    board_members = customers[:6]        # Vorstand
    assembly_members = customers[:55]    # stimmberechtigte Mitglieder (HV)

    m_meetings = m_agenda = m_resolutions = m_attend = m_protocols = 0

    def _seed_attendance(meeting, members, present_ratio):
        """Deterministische Anwesenheit; gibt die Zahl der Anwesenden zurueck."""
        nonlocal m_attend
        threshold = int(round(present_ratio * 10))
        present_n = 0
        for k, cust in enumerate(members):
            r = (k * 7 + meeting.meeting_date.day) % 10
            if r < threshold:
                st, present_n = MeetingAttendance.STATUS_PRESENT, present_n + 1
            elif r < threshold + 1:
                st = MeetingAttendance.STATUS_EXCUSED
            else:
                st = MeetingAttendance.STATUS_ABSENT
            db.session.add(MeetingAttendance(
                meeting_id=meeting.id, customer_id=cust.id, status=st,
                is_member=True, weight=1,
            ))
            m_attend += 1
        return present_n

    def add_meeting(*, mtype, title, when, start, end, status, agenda,
                    location="Vereinslokal / Genossenschaftsbüro",
                    resolutions=(), attendees=None, present_ratio=0.8,
                    quorum_total=None, reconvened=False, reconvene_wait=None,
                    protocol_html=None, intro=None, closing=None):
        nonlocal m_meetings, m_agenda, m_resolutions, m_protocols
        m = Meeting(
            meeting_type=mtype, title=title, meeting_date=when,
            start_time=start, end_time=end, location=location, status=status,
            intro_text=intro, closing_text=closing,
            created_at=datetime(when.year, when.month, when.day, 8, 0, 0),
            created_by_id=admin.id,
        )
        db.session.add(m)
        db.session.flush()
        m_meetings += 1

        items = []
        for pos, (it_title, it_desc, it_vote) in enumerate(agenda, start=1):
            ai = MeetingAgendaItem(meeting_id=m.id, position=pos, title=it_title,
                                   description=it_desc, requires_vote=it_vote)
            db.session.add(ai)
            items.append(ai)
            m_agenda += 1
        db.session.flush()

        for (r_title, r_status, vf, va, vab, r_notes, ai_idx) in resolutions:
            db.session.add(MeetingResolution(
                meeting_id=m.id,
                agenda_item_id=(items[ai_idx].id if ai_idx is not None
                                and ai_idx < len(items) else None),
                title=r_title, status=r_status,
                votes_for=vf, votes_against=va, votes_abstain=vab,
                notes=r_notes, decided_on=when,
                created_at=datetime(when.year, when.month, when.day, 20, 30, 0),
                created_by_id=admin.id,
            ))
            m_resolutions += 1

        # Anwesenheit + Protokoll nur bei abgehaltenen Sitzungen.
        if status == Meeting.STATUS_HELD:
            present_n = _seed_attendance(m, attendees or [], present_ratio)
            qtot = quorum_total if quorum_total is not None else len(attendees or [])
            # Beschlussfaehig bei einfacher Mehrheit der Anwesenden — ODER nach
            # erneuter Eroeffnung der HV (Wartefrist) mit den dann Anwesenden.
            quorate = True if reconvened else (qtot > 0 and present_n * 2 > qtot)
            db.session.add(MeetingProtocol(
                meeting_id=m.id, source_type=MeetingProtocol.SOURCE_RICHTEXT,
                content_html=protocol_html, status=MeetingProtocol.STATUS_FINAL,
                quorum_present=present_n, quorum_total=qtot, is_quorate=quorate,
                attendance_mode=MeetingProtocol.ATTENDANCE_LIST,
                reconvened=reconvened, reconvene_wait_minutes=reconvene_wait,
                present_headcount=(present_n if reconvened else None),
                finalized_at=datetime(when.year, when.month, when.day, 21, 30, 0),
                created_at=datetime(when.year, when.month, when.day, 21, 0, 0),
                created_by_id=admin.id,
            ))
            m_protocols += 1
        return m

    R = MeetingResolution  # kuerzere Statuskonstanten unten
    _board_agenda = [
        ("Begrüßung und Feststellung der Beschlussfähigkeit", None, False),
        ("Genehmigung des Protokolls der letzten Sitzung", None, True),
        ("Kassabericht", "Aktueller Kontostand, offene Posten, Mahnstand.", False),
        ("Anstehende Investitionen und Reparaturen", "Beschlussfassung über Vergaben.", True),
        ("Allfälliges", None, False),
    ]
    _board_intro = ("<p>Der Obmann begrüßt die Anwesenden und stellt die "
                    "fristgerechte Einladung sowie die Beschlussfähigkeit fest.</p>")

    # --- Vorstandssitzungen (abgehalten) ------------------------------
    add_meeting(
        mtype=Meeting.TYPE_BOARD, title=f"Vorstandssitzung März {prev_year}",
        when=date(prev_year, 3, 12), start=_time(19, 0), end=_time(20, 45),
        status=Meeting.STATUS_HELD, agenda=_board_agenda,
        attendees=board_members, present_ratio=0.83, intro=_board_intro,
        protocol_html="<p>Die laufenden Geschäfte wurden besprochen; der Kassier "
                      "berichtet einen ausgeglichenen Kontostand.</p>",
        resolutions=[
            ("Genehmigung des Protokolls der letzten Sitzung",
             R.STATUS_ACCEPTED, 6, 0, 0, "Einstimmig angenommen.", 1),
            ("Beauftragung Frühjahrs-Hydrantenwartung",
             R.STATUS_ACCEPTED, 5, 0, 1,
             "Vergabe an die eigene Crew, Materialbudget 500 €.", 3),
        ])
    add_meeting(
        mtype=Meeting.TYPE_BOARD, title=f"Vorstandssitzung Juni {prev_year}",
        when=date(prev_year, 6, 18), start=_time(19, 0), end=_time(20, 30),
        status=Meeting.STATUS_HELD, agenda=_board_agenda,
        attendees=board_members, present_ratio=0.83, intro=_board_intro,
        protocol_html="<p>Vorbereitung der Hauptversammlung und Beschluss über "
                      "die Anschaffung von Betriebsmitteln.</p>",
        resolutions=[
            ("Anschaffung eines neuen Standrohrzählers für Bautätigkeit",
             R.STATUS_ACCEPTED, 4, 1, 1, "Mehrheitlich angenommen.", 3),
        ])
    add_meeting(
        mtype=Meeting.TYPE_BOARD, title=f"Vorstandssitzung Oktober {prev_year}",
        when=date(prev_year, 10, 8), start=_time(19, 0), end=_time(21, 0),
        status=Meeting.STATUS_HELD, agenda=_board_agenda,
        attendees=board_members, present_ratio=1.0, intro=_board_intro,
        protocol_html="<p>Investitionsplanung für das kommende Jahr; Vergabe des "
                      "Pumpentauschs beschlossen, Leitungstausch vertagt.</p>",
        resolutions=[
            ("Vergabe Pumpentausch Druckerhöhung Sonnberg",
             R.STATUS_ACCEPTED, 6, 0, 0, "Angebot Grundfos einstimmig angenommen.", 3),
            ("Leitungstausch Quellweg",
             R.STATUS_POSTPONED, 0, 0, 0,
             "Vertagt bis zur Klärung der Förderzusage des Landes.", 3),
        ])
    add_meeting(
        mtype=Meeting.TYPE_BOARD, title=f"Vorstandssitzung März {current_year}",
        when=date(current_year, 3, 11), start=_time(19, 0), end=_time(20, 40),
        status=Meeting.STATUS_HELD, agenda=_board_agenda,
        attendees=board_members, present_ratio=0.83, intro=_board_intro,
        protocol_html="<p>Nachbereitung der Hauptversammlung; Beauftragung der "
                      "Quellschacht-Sanierung.</p>",
        resolutions=[
            ("Genehmigung des Protokolls der Hauptversammlung",
             R.STATUS_ACCEPTED, 6, 0, 0, "Einstimmig angenommen.", 1),
            ("Beauftragung Sanierung Quellschacht Brunnertal",
             R.STATUS_ACCEPTED, 5, 1, 0, "Angebot Tiefbau Mayr GmbH angenommen.", 3),
        ])
    add_meeting(
        mtype=Meeting.TYPE_BOARD, title=f"Vorstandssitzung Juni {current_year}",
        when=date(current_year, 6, 17), start=_time(19, 0), end=_time(20, 20),
        status=Meeting.STATUS_HELD, agenda=_board_agenda,
        attendees=board_members, present_ratio=0.66, intro=_board_intro,
        protocol_html="<p>Diskussion über die Beschaffung eines Notstromaggregats; "
                      "Antrag abgelehnt, zunächst Mietlösung prüfen.</p>",
        resolutions=[
            ("Anschaffung eines Notstromaggregats für die Druckerhöhung",
             R.STATUS_REJECTED, 2, 4, 0,
             "Abgelehnt — kostengünstigere Mietlösung wird geprüft.", 3),
        ])
    # --- Vorstandssitzung (geplant, ohne Protokoll) -------------------
    add_meeting(
        mtype=Meeting.TYPE_BOARD, title="Vorstandssitzung (geplant)",
        when=now + timedelta(days=21), start=_time(19, 0), end=None,
        status=Meeting.STATUS_PLANNING, agenda=_board_agenda,
        intro=_board_intro)

    # --- Hauptversammlungen -------------------------------------------
    _assembly_agenda = [
        ("Begrüßung und Feststellung der ordnungsgemäßen Einladung", None, False),
        ("Bericht des Obmanns über das abgelaufene Geschäftsjahr", None, False),
        ("Kassabericht und Bericht der Rechnungsprüfer", None, False),
        ("Entlastung des Vorstands", None, True),
        ("Beschluss über die Wassergebühren",
         "Anpassung der Grund- und Verbrauchsgebühren.", True),
        ("Allfälliges", None, False),
    ]
    _assembly_close = ("<p>Der Obmann dankt den Mitgliedern für ihr Erscheinen und "
                       "schließt die Versammlung.</p>")

    add_meeting(
        mtype=Meeting.TYPE_ASSEMBLY,
        title=f"Ordentliche Hauptversammlung {prev_year}",
        when=date(prev_year, 5, 6), start=_time(19, 30), end=_time(22, 0),
        location="Gasthaus zur Quelle, Saal", status=Meeting.STATUS_HELD,
        agenda=_assembly_agenda, attendees=assembly_members, present_ratio=0.62,
        quorum_total=len(assembly_members), closing=_assembly_close,
        protocol_html="<p>Die Versammlung war beschlussfähig. Jahresabschluss "
                      "genehmigt, Vorstand entlastet.</p>",
        resolutions=[
            ("Genehmigung des Jahresabschlusses",
             R.STATUS_ACCEPTED, 32, 0, 2, "Mit großer Mehrheit angenommen.", 2),
            ("Entlastung des Vorstands",
             R.STATUS_ACCEPTED, 31, 1, 2, "Angenommen.", 3),
            ("Beibehaltung der Wassergebühren",
             R.STATUS_ACCEPTED, 30, 3, 1,
             "Keine Anpassung im laufenden Jahr.", 4),
        ])
    add_meeting(
        mtype=Meeting.TYPE_ASSEMBLY,
        title=f"Ordentliche Hauptversammlung {current_year}",
        when=date(current_year, 5, 13), start=_time(19, 30), end=_time(22, 15),
        location="Gasthaus zur Quelle, Saal", status=Meeting.STATUS_HELD,
        agenda=_assembly_agenda + [("Neuwahl des Vorstands", None, True)],
        attendees=assembly_members, present_ratio=0.44,
        quorum_total=len(assembly_members), reconvened=True, reconvene_wait=30,
        closing=_assembly_close,
        protocol_html="<p>Zu Beginn nicht beschlussfähig; nach 30 Minuten "
                      "Wartefrist mit den Anwesenden ordnungsgemäß eröffnet. "
                      "Gebührenanpassung und Neuwahl beschlossen.</p>",
        resolutions=[
            ("Genehmigung des Jahresabschlusses",
             R.STATUS_ACCEPTED, 23, 0, 1, "Angenommen.", 2),
            ("Entlastung des Vorstands",
             R.STATUS_ACCEPTED, 22, 1, 1, "Angenommen.", 3),
            ("Anhebung der Verbrauchsgebühr auf 1,55 €/m³",
             R.STATUS_ACCEPTED, 18, 5, 1,
             "Nach Diskussion mehrheitlich angenommen.", 4),
            ("Neuwahl des Vorstands für die laufende Funktionsperiode",
             R.STATUS_ACCEPTED, 24, 0, 0, "Einstimmig wiedergewählt.", 6),
        ])
    # --- Hauptversammlung (geplant, ohne Protokoll) -------------------
    add_meeting(
        mtype=Meeting.TYPE_ASSEMBLY,
        title="Außerordentliche Hauptversammlung (geplant)",
        when=now + timedelta(days=45), start=_time(19, 30), end=None,
        location="Gasthaus zur Quelle, Saal", status=Meeting.STATUS_PLANNING,
        agenda=[
            ("Begrüßung und Feststellung der Beschlussfähigkeit", None, False),
            ("Beschluss über eine Sonderumlage für die Quellsanierung",
             "Außerordentliche Investition.", True),
            ("Allfälliges", None, False),
        ])

    db.session.flush()
    counts["meetings"] = m_meetings
    counts["meeting_agenda_items"] = m_agenda
    counts["meeting_resolutions"] = m_resolutions
    counts["meeting_attendances"] = m_attend
    counts["meeting_protocols"] = m_protocols

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
