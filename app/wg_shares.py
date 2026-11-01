"""Anteilsbuch: Genossenschaftsanteile am Mitglied (``org.share_binding`` =
``person``).

Einzige Stelle, die ``WgShareEntry`` schreibt und die Regeln kennt:

- **Bestand = Summe** ``quantity`` aller Eintraege mit ``entry_date <= Stichtag``
  (kein Cache-Feld). Der Bestand am Tag D enthaelt die Buchungen vom Tag D —
  eine Rueckgabe zum Stichtag heisst also: bis zum Vortag gehalten.
- **Nie negativ, zu keinem Datum.** Geprueft wird die ganze Zeitachse je
  Kontakt (Tagesend-Salden), damit auch rueckdatierte und kuenftige Eintraege
  (Kuendigung zum 31.12.) nichts ins Minus druecken.
- **Uebertragung = zwei Eintraege** in einem Flush: ``transfer_out`` beim
  Abgebenden, ``transfer_in`` beim Empfaenger mit ``pair_entry_id`` →
  ``transfer_out``. Loeschen nimmt immer beide.
- Eintraege werden **nicht bearbeitet** — Loeschen und neu buchen, oder eine
  Korrektur (mit Pflicht-Notiz).
- Die Funktionen hier **committen nie**; sie flushen und pruefen. Der Aufrufer
  committet bzw. rollt bei ``ShareError`` zurueck.

Geldwerte (Anteilswert, Einzahlung, Auszahlung) gibt es bewusst noch nicht.
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Customer, WgShareEntry

# ---------------------------------------------------------------------------
# Arten
# ---------------------------------------------------------------------------
KIND_OPENING = "opening"            # Anfangsbestand / Uebernahme
KIND_ACQUISITION = "acquisition"    # Erwerb (Zeichnung)
KIND_TRANSFER_IN = "transfer_in"    # Uebertragung von …
KIND_TRANSFER_OUT = "transfer_out"  # Uebertragung an …
KIND_RETURN = "return"              # Rueckgabe (Kuendigung)
KIND_CORRECTION = "correction"      # Korrektur (+/-)

KIND_LABELS = {
    KIND_OPENING: "Anfangsbestand",
    KIND_ACQUISITION: "Erwerb",
    KIND_TRANSFER_IN: "Übertragung von",
    KIND_TRANSFER_OUT: "Übertragung an",
    KIND_RETURN: "Rückgabe",
    KIND_CORRECTION: "Korrektur",
}

# Dezente Tabler-Soft-Badges (Konvention: bg-{color}-lt, nie text-white-lt).
KIND_BADGE = {
    KIND_OPENING: "bg-secondary-lt",
    KIND_ACQUISITION: "bg-success-lt",
    KIND_TRANSFER_IN: "bg-azure-lt",
    KIND_TRANSFER_OUT: "bg-azure-lt",
    KIND_RETURN: "bg-orange-lt",
    KIND_CORRECTION: "bg-yellow-lt",
}

POSITIVE_KINDS = {KIND_OPENING, KIND_ACQUISITION, KIND_TRANSFER_IN}
NEGATIVE_KINDS = {KIND_TRANSFER_OUT, KIND_RETURN}

# Vorgaenge, die man im Formular waehlt (eine Uebertragung ist EIN Vorgang).
ACTION_OPENING = "opening"
ACTION_ACQUISITION = "acquisition"
ACTION_TRANSFER = "transfer"
ACTION_RETURN = "return"
ACTION_CORRECTION = "correction"

ACTION_LABELS = {
    ACTION_ACQUISITION: "Erwerb (Zeichnung)",
    ACTION_TRANSFER: "Übertragung an anderes Mitglied",
    ACTION_RETURN: "Rückgabe (Kündigung)",
    ACTION_CORRECTION: "Korrektur",
    ACTION_OPENING: "Anfangsbestand",
}

# FA5-Icons fuer das Buchen-Menue.
ACTION_ICONS = {
    ACTION_ACQUISITION: "fa-coins",
    ACTION_TRANSFER: "fa-exchange-alt",
    ACTION_RETURN: "fa-undo",
    ACTION_CORRECTION: "fa-pen",
    ACTION_OPENING: "fa-flag",
}

# Herkunft
SOURCE_MANUAL = "manual"
SOURCE_OWNER_CHANGE = "owner_change"
SOURCE_IMPORT = "import"
SOURCE_MODE_SWITCH = "mode_switch"
SOURCES = (SOURCE_MANUAL, SOURCE_OWNER_CHANGE, SOURCE_IMPORT, SOURCE_MODE_SWITCH)

MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2100, 12, 31)


class ShareError(ValueError):
    """Fachlicher Fehler im Anteilsbuch (deutsche Meldung fuer die UI)."""


def kind_label(kind):
    return KIND_LABELS.get(kind, kind)


def _fmt_date(d):
    return d.strftime("%d.%m.%Y")


def _shares_word(n):
    return "Anteil" if abs(n) == 1 else "Anteile"


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------
def holding(customer_id, at=None):
    """Bestand eines Kontakts zum Stichtag ``at`` (Default heute)."""
    at = at or date.today()
    total = (db.session.query(func.coalesce(func.sum(WgShareEntry.quantity), 0))
             .filter(WgShareEntry.customer_id == customer_id,
                     WgShareEntry.entry_date <= at)
             .scalar())
    return int(total or 0)  # MariaDB liefert Decimal fuer SUM(INT)


def holdings_map(customer_ids=None, at=None):
    """``{customer_id: Bestand}`` zum Stichtag — EINE gruppierte Abfrage.
    ``customer_ids=None`` = alle Kontakte mit Eintraegen. Kontakte ohne
    Eintrag fehlen im Ergebnis (Aufrufer nehmen ``.get(id, 0)``)."""
    at = at or date.today()
    q = (db.session.query(WgShareEntry.customer_id, func.sum(WgShareEntry.quantity))
         .filter(WgShareEntry.entry_date <= at))
    if customer_ids is not None:
        ids = [i for i in customer_ids if i is not None]
        if not ids:
            return {}
        q = q.filter(WgShareEntry.customer_id.in_(ids))
    return {cid: int(total or 0)
            for cid, total in q.group_by(WgShareEntry.customer_id).all()}


def holding_subquery(at=None):
    """Subquery ``(customer_id, shares)`` fuer Sortier-Joins (Outer-Join +
    ``coalesce(shares, 0)``)."""
    at = at or date.today()
    return (db.session.query(WgShareEntry.customer_id.label("customer_id"),
                             func.sum(WgShareEntry.quantity).label("shares"))
            .filter(WgShareEntry.entry_date <= at)
            .group_by(WgShareEntry.customer_id)
            .subquery())


def total_holdings(at=None):
    """Summe aller Anteile zum Stichtag."""
    at = at or date.today()
    total = (db.session.query(func.coalesce(func.sum(WgShareEntry.quantity), 0))
             .filter(WgShareEntry.entry_date <= at).scalar())
    return int(total or 0)


def history(customer_id):
    """Alle Eintraege chronologisch mit laufendem Saldo: ``[(entry, saldo)]``."""
    entries = (WgShareEntry.query
               .filter(WgShareEntry.customer_id == customer_id)
               .order_by(WgShareEntry.entry_date, WgShareEntry.id)
               .all())
    out, balance = [], 0
    for e in entries:
        balance += e.quantity
        out.append((e, balance))
    return out


def planned_changes(customer_id, today=None):
    """Bestand nach jedem kuenftigen Buchungstag: ``[(datum, bestand)]``."""
    today = today or date.today()
    sums = _daily_sums(customer_id)
    out, balance = [], 0
    for d in sorted(sums):
        balance += sums[d]
        if d > today:
            out.append((d, balance))
    return out


def has_entries(customer_id):
    """True, wenn der Kontakt im Anteilsbuch vorkommt — als Inhaber ODER als
    Gegenseite einer Uebertragung (Loesch-Sperre)."""
    return db.session.query(WgShareEntry.id).filter(or_(
        WgShareEntry.customer_id == customer_id,
        WgShareEntry.counterparty_customer_id == customer_id,
    )).first() is not None


def customers_with_entries(customer_ids=None):
    """Menge der Kontakt-IDs, die (als Inhaber) Eintraege haben."""
    q = db.session.query(WgShareEntry.customer_id).distinct()
    if customer_ids is not None:
        ids = [i for i in customer_ids if i is not None]
        if not ids:
            return set()
        q = q.filter(WgShareEntry.customer_id.in_(ids))
    return {row[0] for row in q.all()}


# ---------------------------------------------------------------------------
# Regeln
# ---------------------------------------------------------------------------
def first_negative(deltas_by_date):
    """Erster Tag, an dem der Tagesend-Saldo negativ wird: ``(datum, saldo)``
    oder ``None``. ``deltas_by_date``: ``{datum: summe}`` (oder Paare).

    Gerechnet wird je Tag (Summe aller Eintraege des Tages, dann kumuliert) —
    die Reihenfolge innerhalb eines Tages spielt damit keine Rolle (Bestand am
    Tag D enthaelt alle Buchungen vom D)."""
    items = deltas_by_date.items() if hasattr(deltas_by_date, "items") else deltas_by_date
    per_day = defaultdict(int)
    for d, delta in items:
        per_day[d] += int(delta)
    balance = 0
    for d in sorted(per_day):
        balance += per_day[d]
        if balance < 0:
            return d, balance
    return None


def _daily_sums(customer_id):
    rows = (db.session.query(WgShareEntry.entry_date, func.sum(WgShareEntry.quantity))
            .filter(WgShareEntry.customer_id == customer_id)
            .group_by(WgShareEntry.entry_date)
            .all())
    return {d: int(total or 0) for d, total in rows}


def _negative_message(customer, day, balance):
    name = customer.name if customer is not None else "Kontakt"
    return (f"{name} hätte am {_fmt_date(day)} {balance} {_shares_word(balance)} — "
            "der Bestand darf zu keinem Datum negativ werden.")


def check_changes(changes):
    """Prueft zusaetzliche, noch nicht geschriebene Bewegungen gegen den
    Bestand, ohne zu schreiben. ``changes``: Iterable ``(customer_id, datum,
    delta)``. Wirft ``ShareError`` beim ersten negativen Tagessaldo."""
    extra = defaultdict(list)
    for cid, d, delta in changes:
        extra[cid].append((d, delta))
    for cid, items in extra.items():
        sums = _daily_sums(cid)
        for d, delta in items:
            sums[d] = sums.get(d, 0) + int(delta)
        hit = first_negative(sums)
        if hit:
            raise ShareError(_negative_message(db.session.get(Customer, cid), *hit))


def validate_customers(*customer_ids):
    """Nach ``flush()``: wirft ``ShareError``, wenn der Bestand eines der
    Kontakte zu irgendeinem Datum negativ wird."""
    for cid in {c for c in customer_ids if c is not None}:
        hit = first_negative(_daily_sums(cid))
        if hit:
            raise ShareError(_negative_message(db.session.get(Customer, cid), *hit))


def _check_qty(qty):
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise ShareError("Bitte eine ganze Zahl an Anteilen angeben.")
    if qty < 1:
        raise ShareError("Die Anzahl muss mindestens 1 sein.")
    return qty


def _check_date(on):
    if not isinstance(on, date):
        raise ShareError("Bitte ein gültiges Datum angeben.")
    if on < MIN_DATE or on > MAX_DATE:
        raise ShareError("Das Datum liegt außerhalb des erlaubten Bereichs.")
    return on


def _check_holder(customer, *, must_be_active=False):
    if customer is None or customer.id is None:
        raise ShareError("Kontakt nicht gefunden.")
    if not customer.is_customer:
        raise ShareError(f"{customer.name} ist kein Kunde/Mitglied — "
                         "Anteile können nur Kunden halten.")
    if must_be_active and not customer.active:
        raise ShareError(f"{customer.name} ist archiviert.")


def _check_source(source):
    return source if source in SOURCES else SOURCE_MANUAL


def _clean_note(note):
    note = (note or "").strip()
    return note[:255] or None


# ---------------------------------------------------------------------------
# Schreiben (flushen + pruefen, nie committen)
# ---------------------------------------------------------------------------
def _add(customer, kind, quantity, on, *, counterparty=None, pair=None,
         note=None, source=SOURCE_MANUAL, user_id=None, property_ref=None):
    entry = WgShareEntry(
        customer_id=customer.id, kind=kind, quantity=quantity, entry_date=on,
        counterparty_customer_id=counterparty.id if counterparty else None,
        pair_entry_id=pair.id if pair else None,
        property_id=_property_ref_id(property_ref),
        note=_clean_note(note), source=_check_source(source),
        created_by_id=user_id)
    db.session.add(entry)
    return entry


def _property_ref_id(ref):
    """Optionaler Bezug zu einer Liegenschaft (Property oder ID; nur Information)."""
    if ref in (None, "", 0):
        return None
    from app.models import Property
    prop = ref if isinstance(ref, Property) else db.session.get(Property, int(ref))
    if prop is None:
        raise ShareError("Die gewählte Liegenschaft existiert nicht.")
    return prop.id


def record_acquisition(customer, qty, on, *, kind=KIND_ACQUISITION, note=None,
                       source=SOURCE_MANUAL, user_id=None, property_ref=None):
    """Erwerb (Zeichnung) oder Anfangsbestand (``kind=KIND_OPENING``)."""
    if kind not in (KIND_ACQUISITION, KIND_OPENING):
        raise ShareError("Unbekannte Art.")
    qty, on = _check_qty(qty), _check_date(on)
    _check_holder(customer, must_be_active=(kind == KIND_ACQUISITION
                                            and source == SOURCE_MANUAL))
    entry = _add(customer, kind, qty, on, note=note, source=source, user_id=user_id,
                 property_ref=property_ref)
    db.session.flush()
    validate_customers(customer.id)
    return entry


def record_return(customer, qty, on, *, note=None, source=SOURCE_MANUAL, user_id=None,
                  property_ref=None):
    """Rueckgabe (Kuendigung) — der Bestand sinkt ab ``on``."""
    qty, on = _check_qty(qty), _check_date(on)
    _check_holder(customer)
    entry = _add(customer, KIND_RETURN, -qty, on, note=note, source=source,
                 user_id=user_id, property_ref=property_ref)
    db.session.flush()
    validate_customers(customer.id)
    return entry


def record_correction(customer, delta, on, note, *, source=SOURCE_MANUAL, user_id=None,
                      property_ref=None):
    """Korrektur um ``delta`` (+/-, nie 0) mit Pflicht-Notiz."""
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise ShareError("Bitte eine ganze Zahl angeben (z. B. 2 oder -1).")
    if delta == 0:
        raise ShareError("Eine Korrektur um 0 Anteile ändert nichts.")
    if not (note or "").strip():
        raise ShareError("Bei einer Korrektur bitte den Grund als Notiz angeben.")
    on = _check_date(on)
    _check_holder(customer)
    entry = _add(customer, KIND_CORRECTION, delta, on, note=note, source=source,
                 user_id=user_id, property_ref=property_ref)
    db.session.flush()
    validate_customers(customer.id)
    return entry


def record_transfer(from_customer, to_customer, qty, on, *, note=None,
                    source=SOURCE_MANUAL, user_id=None, property_ref=None):
    """Uebertragung ``qty`` Anteile von ``from_customer`` an ``to_customer``
    zum ``on``. Liefert ``(out_entry, in_entry)``."""
    qty, on = _check_qty(qty), _check_date(on)
    _check_holder(from_customer)
    if to_customer is None:
        raise ShareError("Bitte den Empfänger der Anteile wählen.")
    if to_customer.id == from_customer.id:
        raise ShareError("Anteile können nicht an denselben Kontakt übertragen werden.")
    _check_holder(to_customer, must_be_active=True)
    out = _add(from_customer, KIND_TRANSFER_OUT, -qty, on, counterparty=to_customer,
               note=note, source=source, user_id=user_id, property_ref=property_ref)
    db.session.flush()  # out.id fuer die Gegenhaelfte
    inn = _add(to_customer, KIND_TRANSFER_IN, qty, on, counterparty=from_customer,
               pair=out, note=note, source=source, user_id=user_id,
               property_ref=property_ref)
    db.session.flush()
    validate_customers(from_customer.id, to_customer.id)
    return out, inn


def partner_of(entry):
    """Die andere Haelfte einer Uebertragung (oder ``None``)."""
    if entry.kind == KIND_TRANSFER_IN and entry.pair_entry_id:
        return db.session.get(WgShareEntry, entry.pair_entry_id)
    if entry.kind == KIND_TRANSFER_OUT:
        return WgShareEntry.query.filter_by(pair_entry_id=entry.id).first()
    return None


def delete_entry(entry):
    """Loescht einen Eintrag (bei Uebertragungen beide Haelften) und prueft
    beide Kontakte neu. Liefert die betroffenen Kontakt-IDs."""
    partner = partner_of(entry)
    affected = {entry.customer_id}
    if partner is not None:
        affected.add(partner.customer_id)
        # Erst die transfer_in-Haelfte (traegt den Self-FK), dann die andere.
        first, second = ((partner, entry) if partner.kind == KIND_TRANSFER_IN
                         else (entry, partner))
        db.session.delete(first)
        db.session.flush()
        db.session.delete(second)
    else:
        db.session.delete(entry)
    db.session.flush()
    validate_customers(*affected)
    return affected


# ---------------------------------------------------------------------------
# Mitgliedschaft passend zur Bewegung (Vorschlag aus dem Formular)
# ---------------------------------------------------------------------------
def apply_membership(customer, on, holding_after):
    """Setzt den WG-Status passend zum neuen Bestand: > 0 → Mitglied
    (``member_since`` falls leer, ein altes ``member_until`` faellt weg),
    0 → ausgeschieden mit ``member_until = on − 1`` (bis zum Vortag gehalten,
    gleiche Konvention wie der Eigentuemerwechsel). Liefert True, wenn sich
    etwas geaendert hat."""
    from app import wg
    profile = customer.ensure_wg_profile()
    if holding_after > 0:
        changed = profile.status != wg.STATUS_MEMBER or profile.member_until is not None
        profile.status = wg.STATUS_MEMBER
        profile.member_until = None
        if customer.member_since is None:
            customer.member_since = on
            changed = True
        return changed
    if profile.status == wg.STATUS_RESIGNED and profile.member_until is not None:
        return False
    profile.status = wg.STATUS_RESIGNED
    profile.member_until = on - timedelta(days=1)
    return True


# ---------------------------------------------------------------------------
# Umstellen Liegenschaft → Mitglied
# ---------------------------------------------------------------------------
def _owners_at(property_id, on):
    from app.models import PropertyOwnership
    return (PropertyOwnership.query
            .filter(PropertyOwnership.property_id == property_id,
                    PropertyOwnership.valid_from <= on,
                    or_(PropertyOwnership.valid_to.is_(None),
                        PropertyOwnership.valid_to >= on))
            .order_by(PropertyOwnership.valid_from, PropertyOwnership.customer_id)
            .all())


def switch_preview(on=None):
    """Vorschau fuer die Umstellung auf Anteile am Mitglied (schreibt nichts).

    Je Liegenschaft mit Anteilen die Eigentuemer zum Stichtag ``on``; der
    Vorschlag gibt alle Anteile dem ersten Eigentuemer (fruehestes
    ``valid_from``) — bei Miteigentum bewusst nicht jedem, sonst wuerden
    stillschweigend beide Ehepartner Mitglieder mit voller Anteilszahl."""
    from app.models import Property
    from app import property_shares, wg
    on = on or date.today()
    registered = customers_with_entries()
    # Anteile je Liegenschaft zum Uebernahmedatum aus deren Anteilsbuch.
    held = {pid: n for pid, n in property_shares.holdings_map(at=on).items() if n > 0}
    props = (Property.query.filter(Property.id.in_(held)).order_by(
        Property.object_number, Property.id).all() if held else [])
    rows, unowned = [], []
    for prop in props:
        shares = held[prop.id]
        owners = [o.customer for o in _owners_at(prop.id, on) if o.customer is not None]
        if not owners:
            unowned.append({"property": prop, "shares": shares})
            continue
        default = {c.id: 0 for c in owners}
        # Erster Eigentuemer ohne eigenes Anteilsbuch (die anderen werden
        # beim Uebernehmen uebersprungen, ihre Zeile waere sonst verloren).
        open_owners = [c for c in owners if c.id not in registered]
        if open_owners:
            default[open_owners[0].id] = shares
        rows.append({
            "property": prop, "shares": shares, "owners": owners,
            "co_owned": len(owners) > 1, "default": default,
            "skipped_ids": {c.id for c in owners if c.id in registered},
        })
    allocated = {cid for r in rows for cid, n in r["default"].items()
                 if n > 0 and cid not in registered}
    members = (Customer.query
               .options(joinedload(Customer.wg_profile))
               .filter(Customer.is_customer.is_(True), Customer.active.is_(True))
               .all())
    members_without = [c for c in members
                       if c.wg_status == wg.STATUS_MEMBER
                       and c.id not in allocated and c.id not in registered]
    skipped = (Customer.query.filter(Customer.id.in_(registered)).order_by(Customer.name).all()
               if registered else [])
    return {
        "on": on, "rows": rows, "unowned": unowned, "skipped": skipped,
        "members_without_shares": members_without,
        "total_shares": sum(r["shares"] for r in rows),
    }


def apply_switch_to_person(allocations, on, *, use_member_since=False, user_id=None):
    """Bucht die Uebernahme und stellt die Bindung um (ohne Commit).

    ``allocations``: ``{(property_id, customer_id): anzahl}``. Je Kontakt
    entsteht EIN Anfangsbestand (Notiz nennt die Liegenschaften); Kontakte mit
    vorhandenem Anteilsbuch werden uebersprungen (kein Doppelbuchen beim
    Hin- und Zurueckschalten). ``PropertyWgProfile.shares`` bleibt unberuehrt.
    Liefert ``(anzahl_kontakte, anzahl_anteile)``."""
    from app.models import AppSetting, Property
    from app import wg
    on = _check_date(on)
    registered = customers_with_entries()
    per_customer = defaultdict(lambda: {"qty": 0, "labels": []})
    for (prop_id, cid), qty in allocations.items():
        qty = int(qty or 0)
        if qty < 0:
            raise ShareError("Negative Anteile können nicht übernommen werden.")
        if qty == 0 or cid in registered:
            continue
        prop = db.session.get(Property, prop_id)
        per_customer[cid]["qty"] += qty
        per_customer[cid]["labels"].append(
            (prop.object_number or f"#{prop.id}") if prop else f"#{prop_id}")
    contacts = shares = 0
    for cid, data in per_customer.items():
        customer = db.session.get(Customer, cid)
        if customer is None or not customer.is_customer:
            continue
        entry_on = on
        if use_member_since and customer.member_since and customer.member_since <= on:
            entry_on = customer.member_since
        note = "Übernahme aus Liegenschaft " + ", ".join(data["labels"])
        record_acquisition(customer, data["qty"], entry_on, kind=KIND_OPENING,
                           note=note, source=SOURCE_MODE_SWITCH, user_id=user_id)
        contacts += 1
        shares += data["qty"]
    AppSetting.set(wg.SHARE_BINDING_KEY, wg.SHARES_ON_PERSON)
    return contacts, shares


def apply_switch_to_property():
    """Zurueck auf Anteile an der Liegenschaft — aendert nur die Einstellung,
    das Anteilsbuch bleibt gespeichert (ohne Commit)."""
    from app.models import AppSetting
    from app import wg
    AppSetting.set(wg.SHARE_BINDING_KEY, wg.SHARES_ON_PROPERTY)
