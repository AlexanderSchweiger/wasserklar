"""Deutscher Teil des Demo-Datensatzes (Habach, Oberbayern).

Aufgerufen aus ``demo.seed_demo_data``, wenn das Land des Mandanten Deutschland ist:

* ``create_customers`` / ``create_properties`` — Kunden mit Vor-/Nachname und
  Anrede; jede Liegenschaft liegt an der Straße ihres Hausanschlusses
  (``demo_geo_de.HOUSE_ADDRESSES``), der Kunde wohnt in seinem ersten Objekt.
* ``seed_einvoice`` — E-Rechnung: Unternehmer-Kunden (USt-IdNr., ZUGFeRD),
  Gemeinde und Schulverband mit XRechnung + Leitweg-ID und die zugehörigen
  Ausgangsrechnungen (versendet, bezahlt, ein Entwurf, der nur elektronisch
  zugestellt werden darf).

Alles erfunden — siehe ``demo_locale.py``. Kein Commit.
"""
from __future__ import annotations

import unicodedata
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.seed.demo_locale import (DE_BUSINESSES, DE_FIRST_NAMES, DE_LAST_NAMES, DE_PUBLIC,
                                  de_vat_id, leitweg_id)

_CENT = Decimal("0.01")


def mail_slug(text):
    """``Kögl`` -> ``koegl`` (E-Mail-tauglich, ohne Umlaute/Leerzeichen)."""
    text = (text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
            .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue"))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch in ".-")


def property_addresses(rng, geo, count):
    """Adresse je Objekt-Index: erst die Hausanschluesse, dann die drei geocodeten Objekte
    ohne Zuordnung, dann Objekte ohne Lage (Gärten/Sonstiges an einer Ortsstraße)."""
    rows = list(geo.HOUSE_ADDRESSES) + list(geo.UNASSIGNED_NEAR_ADDRESSES)
    while len(rows) < count:
        rows.append((rng.choice(geo.STREETS), str(rng.randint(60, 99))))
    return rows[:count]


def first_property_index(cust_idx, two_obj):
    return cust_idx * 2 if cust_idx < two_obj else cust_idx + two_obj


def create_customers(db, rng, loc, addresses, *, two_obj, member_since_base):
    """100 Kunden; Adresse = erstes Objekt (``addresses`` aus ``property_addresses``)."""
    from app.models import Customer

    geo = loc.geo
    domain = loc.names["email_domain"]
    customers = []
    for i in range(1, 101):
        first, salutation = rng.choice(DE_FIRST_NAMES)
        last = rng.choice(DE_LAST_NAMES)
        street, number = addresses[first_property_index(i - 1, two_obj)]
        email = (f"{mail_slug(first)}.{mail_slug(last)}{i:03d}@{domain}"
                 if rng.random() < 0.9 else None)
        phone = f"089 99998 {rng.randint(100, 999)}" if rng.random() < 0.6 else None
        member_since = member_since_base + timedelta(days=rng.randint(0, 365 * 10))
        c = Customer(
            customer_number=i,
            name=f"{last} {first}", first_name=first, last_name=last, salutation=salutation,
            is_company=False, is_customer=True, is_supplier=False,
            strasse=street, hausnummer=number, plz=geo.POSTCODE, ort=geo.TOWN,
            land="Deutschland",
            email=email, phone=phone,
            member_since=member_since,
            rechnung_per_email=bool(email) and rng.random() < 0.4,
            active=True,
        )
        db.session.add(c)
        customers.append(c)
    db.session.flush()
    return customers


def create_properties(db, rng, loc, customers, addresses, *, two_obj):
    """Objekte in der Reihenfolge der Hausanschluesse (Objekt i <-> ``HOUSE_CONNECTIONS[i]``)."""
    from app.models import Property

    geo = loc.geo
    properties = []
    counter = 0
    for idx, _customer in enumerate(customers):
        for k in range(2 if idx < two_obj else 1):
            street, number = addresses[counter]
            counter += 1
            # Das erste Objekt ist das Wohnhaus des Kunden, das zweite irgendetwas
            # (Austragshaus, Stadel, Garten …).
            obj_type = "Haus" if k == 0 else rng.choice(Property.TYPES)
            p = Property(
                object_number=f"OBJ-{counter:04d}", object_type=obj_type,
                strasse=street, hausnummer=number, plz=geo.POSTCODE, ort=geo.TOWN,
                land="Deutschland", notes=("Zweitobjekt" if k else None), active=True,
            )
            db.session.add(p)
            properties.append(p)
    db.session.flush()
    return properties


# ---------------------------------------------------------------------------
# E-Rechnung
# ---------------------------------------------------------------------------

def _pick(customers, ownership, wanted_street, used, start=40):
    """Ersten noch freien Kunden (ab Index ``start``, ein Objekt) an ``wanted_street``."""
    for idx in range(start, len(customers)):
        c = customers[idx]
        if c.id in used:
            continue
        if wanted_street is None or ownership.get(c.id, (None, None))[1] == wanted_street:
            used.add(c.id)
            return c
    return None


def _item(invoice, description, qty, unit, price, rate, *, account, charge_key=None):
    from app.models import InvoiceItem
    qty, price = Decimal(str(qty)), Decimal(str(price))
    return InvoiceItem(invoice_id=invoice.id, description=description, quantity=qty, unit=unit,
                       unit_price=price,
                       amount=(qty * price).quantize(_CENT, rounding=ROUND_HALF_UP),
                       tax_rate=rate, account_id=account.id, charge_key=charge_key)


def seed_einvoice(db, loc, *, customers, ownership_map, exclude_ids, admin, now,
                  current_year, p_curr, accounts, giro):
    """Unternehmer-/Behördenkunden + Ausgangsrechnungen. Gibt Zählwerte zurück."""
    from datetime import date as _date

    from app.einvoice import obligation
    from app.einvoice.model import FORMAT_XRECHNUNG
    from app.models import Booking, Invoice, Property

    domain = loc.names["email_domain"]
    # Kunde -> (Objekt, Straße) seines ersten Objekts
    ownership = {}
    for prop_id, cust in ownership_map.items():
        if cust.id not in ownership:
            prop = db.session.get(Property, prop_id)
            ownership[cust.id] = (prop, prop.strasse)
    used = set(exclude_ids)

    # --- Unternehmer (§ 14 UStG): USt-IdNr., E-Rechnung per E-Mail -------
    business_streets = ("Hauptstraße", "Steinberg", "Höhlmühler Straße", "Auf der Leiten",
                        "St.-Ulrich-Straße", "Hauptstraße")
    businesses = []
    for (name, is_company, note, vat_base), street in zip(DE_BUSINESSES, business_streets):
        c = _pick(customers, ownership, street, used) or _pick(customers, ownership, None, used)
        c.name = name
        c.is_company = is_company
        if is_company:
            c.first_name = c.last_name = c.salutation = None
        else:
            c.last_name, c.first_name = name.split(" ", 1)
            c.salutation = dict(DE_FIRST_NAMES).get(c.first_name, c.salutation)
        c.vat_id = de_vat_id(vat_base)
        c.email = f"{mail_slug(name.split(' ')[0])}.{mail_slug(name.split(' ')[1])}@{domain}"
        c.notes = f"Unternehmer: {note}."
        obligation.set_business(c, True)       # schaltet „Schriftverkehr per E-Mail“ mit ein
        businesses.append(c)

    # --- Behörden: XRechnung über die Leitweg-ID -------------------------
    public = []
    for (name, lw_base, mailbox, order_ref, note), street in zip(DE_PUBLIC, ("Hauptstraße", "Schulweg")):
        c = _pick(customers, ownership, street, used) or _pick(customers, ownership, None, used)
        c.name = name
        c.is_company = True
        c.first_name = c.last_name = c.salutation = None
        c.is_business = False
        c.einvoice_format = FORMAT_XRECHNUNG
        c.buyer_reference = leitweg_id(lw_base)
        c.order_reference = order_ref
        c.email = f"{mailbox}@{domain}" if mailbox else None
        c.rechnung_per_email = bool(mailbox)
        c.phone = None
        c.notes = note
        public.append(c)
    db.session.flush()

    # --- Ausgangsrechnungen ---------------------------------------------
    acc_wasser, acc_grund, acc_anschluss = accounts
    water, std = loc.water_rate, loc.std_rate
    base_fee, _add, price = loc.tariff_curr

    def day(offset):
        """Datum ``offset`` Tage vor ``now``, aber im Jahr von ``now`` (offenes Buchungsjahr)."""
        return min(now, max(_date(now.year, 1, 2), now - timedelta(days=offset)))

    seq = [100]

    def new_invoice(customer, when, status, *, period=None, notes=None):
        seq[0] += 1
        prop = ownership.get(customer.id, (None, None))[0]
        inv = Invoice(invoice_number=f"RE-{when.year}-{seq[0]:04d}", customer_id=customer.id,
                      property_id=prop.id if prop else None,
                      billing_period_id=period.id if period else None,
                      date=when, due_date=when + timedelta(days=30), status=status,
                      total_amount=Decimal("0.00"), notes=notes, created_by_id=admin.id)
        db.session.add(inv)
        db.session.flush()
        return inv

    invoices = []
    gasthof, zimmerei = businesses[0], businesses[1]

    # Zimmerei: Bauwasser über Standrohr — zwei Steuersätze (Wasser 7 %, Miete 19 %)
    inv = new_invoice(zimmerei, day(35), Invoice.STATUS_SENT,
                      notes="Bauwasser für den Neubau Stadel, Standrohr Nr. 2.")
    m3 = Decimal("38")
    db.session.add_all([
        _item(inv, "Bauwasser lt. Standrohrzähler", m3, "m³", price, water,
              account=acc_wasser, charge_key="water"),
        _item(inv, "Miete Standrohr mit Zähler (6 Wochen)", 6, "Wo", Decimal("12.00"), std,
              account=acc_anschluss),
    ])
    if loc.water_levy is not None and inv.date >= loc.water_levy_from:
        db.session.add(_item(inv, "Wassercent (Wasserentnahmeentgelt)", m3, "m³",
                             loc.water_levy, water, account=acc_wasser, charge_key="water_levy"))
    invoices.append(inv)

    # Landgasthof: Hausanschluss für das Gästehaus — Teil der Wasserlieferung (7 %), bezahlt
    inv = new_invoice(gasthof, day(60), Invoice.STATUS_PAID,
                      notes="Herstellung Hausanschluss Gästehaus — ermäßigter Steuersatz "
                            "(Hausanschluss gehört zur Wasserlieferung, BFH V R 61/09).")
    db.session.add_all([
        _item(inv, "Herstellung Hausanschluss DN 40 inkl. Anbohrschelle", 1, "pauschal",
              Decimal("1850.00"), water, account=acc_anschluss),
        _item(inv, "Zählerplatz Montage und Wasserzähler Q3 4", 1, "Stk", Decimal("140.00"),
              water, account=acc_anschluss),
    ])
    invoices.append(inv)
    paid_invoice = inv

    # Gemeinde (XRechnung, E-Mail-Postfach): Jahresverbrauch Rathaus + Feuerwehrhaus, versendet
    gemeinde, schule = public
    inv = new_invoice(gemeinde, day(20), Invoice.STATUS_SENT, period=p_curr,
                      notes="Rathaus und Feuerwehrhaus.")
    db.session.add_all([
        _item(inv, "Grundgebühr Wasserversorgung", 2, "Stk", base_fee, water,
              account=acc_grund, charge_key="base_fee"),
        _item(inv, f"Wasserverbrauch {current_year} (412 m³)", 412, "m³", price, water,
              account=acc_wasser, charge_key="water"),
    ])
    invoices.append(inv)

    # Schulverband (XRechnung nur über die Leitweg-ID, kein E-Mail-Postfach): Entwurf —
    # im Versenden-Dialog „nur elektronisch zustellbar“, Post-Versand druckt ihn nicht.
    inv = new_invoice(schule, day(5), Invoice.STATUS_DRAFT, period=p_curr,
                      notes="Grundschule Habach.")
    db.session.add_all([
        _item(inv, "Grundgebühr Wasserversorgung", 1, "Stk", base_fee, water,
              account=acc_grund, charge_key="base_fee"),
        _item(inv, f"Wasserverbrauch {current_year} (286 m³)", 286, "m³", price, water,
              account=acc_wasser, charge_key="water"),
    ])
    invoices.append(inv)
    db.session.flush()
    for inv in invoices:
        inv.recalculate_total()

    # Zahlungseingang zur bezahlten Rechnung (eine Buchung, ein Satz)
    db.session.add(Booking(
        date=min(paid_invoice.date + timedelta(days=12), now),
        account_id=acc_anschluss.id, amount=paid_invoice.total_amount,
        description=f"Zahlung {paid_invoice.invoice_number}",
        reference=paid_invoice.invoice_number, invoice_id=paid_invoice.id,
        customer_id=gasthof.id, real_account_id=giro.id, tax_rate=water,
        status=Booking.STATUS_OFFEN, created_by_id=admin.id))
    db.session.flush()
    return {"business_customers": len(businesses), "xrechnung_customers": len(public),
            "einvoice_invoices": len(invoices)}
