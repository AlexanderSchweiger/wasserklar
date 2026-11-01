"""Anteilsbuch der Liegenschaft (``org.share_binding`` = ``property``).

Einzige Stelle, die ``PropertyShareEntry`` schreibt und den Cache
``PropertyWgProfile.shares`` setzt:

- **Bestand = Summe** ``quantity`` bis zum Stichtag (inkl. des Tages), nie
  negativ — geprueft ueber die ganze Zeitachse (``wg_shares.first_negative``).
- **Keine Zukunftsdaten** (Datum <= heute). Deshalb ist die Summe aller
  Eintraege immer der heutige Bestand und ``PropertyWgProfile.shares`` ein
  verlaesslicher Cache — Liste, Filter, Sortierung und ``has_paid_shares``
  lesen ihn unveraendert. Gesetzt wird er nur von ``sync_cache``.
- **Flaeche:** ein Eintrag kann ``area_m2`` (Flaeche danach) tragen. Ist er der
  juengste Eintrag mit Flaeche, uebernimmt ihn ``PropertyWgProfile.area_m2``;
  ein rueckdatierter Eintrag aendert die aktuelle Flaeche nicht. Eine
  Aenderung um 0 Anteile ist nur mit Flaeche erlaubt (Zubau unter der
  naechsten Schwelle, Flaechenkorrektur).
- **Uebertragung** zwischen Liegenschaften (Teilung/Zusammenlegung) = zwei
  Eintraege, ``transfer_in.pair_entry_id`` → ``transfer_out``; Loeschen nimmt
  beide.
- ``payer_customer_id`` haelt nur fest, wer bezahlt hat — kein Betrag.
- Die Funktionen committen nie; sie flushen, pruefen und aktualisieren den
  Cache. Der Aufrufer committet bzw. rollt bei ``ShareError`` zurueck.
"""
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models import (Customer, Property, PropertyOwnership, PropertyShareEntry,
                        PropertyWgProfile)
from app.wg_shares import MIN_DATE, ShareError, first_negative

# ---------------------------------------------------------------------------
# Arten, Anlaesse, Vorgaenge
# ---------------------------------------------------------------------------
KIND_OPENING = "opening"
KIND_CHANGE = "change"
KIND_TRANSFER_IN = "transfer_in"
KIND_TRANSFER_OUT = "transfer_out"
KIND_CORRECTION = "correction"

KIND_LABELS = {
    KIND_OPENING: "Anfangsbestand",
    KIND_CHANGE: "Änderung",
    KIND_TRANSFER_IN: "Übertragung von",
    KIND_TRANSFER_OUT: "Übertragung an",
    KIND_CORRECTION: "Korrektur",
}

REASON_EXTENSION = "extension"
REASON_NEW_CONNECTION = "new_connection"
REASON_DECOMMISSION = "decommission"
REASON_DEMOLITION = "demolition"
REASON_OTHER = "other"
REASON_SPLIT = "split"
REASON_MERGE = "merge"

REASON_LABELS = {
    REASON_EXTENSION: "Zubau / Erweiterung",
    REASON_NEW_CONNECTION: "Neuanschluss",
    REASON_DECOMMISSION: "Stilllegung",
    REASON_DEMOLITION: "Abbruch / Rückbau",
    REASON_OTHER: "Sonstige Änderung",
    REASON_SPLIT: "Teilung",
    REASON_MERGE: "Zusammenlegung",
}
CHANGE_REASONS = (REASON_EXTENSION, REASON_NEW_CONNECTION, REASON_DECOMMISSION,
                  REASON_DEMOLITION, REASON_OTHER)
TRANSFER_REASONS = (REASON_SPLIT, REASON_MERGE)
INCREASE_REASONS = {REASON_EXTENSION, REASON_NEW_CONNECTION}
DECREASE_REASONS = {REASON_DECOMMISSION, REASON_DEMOLITION}

# Vorgaenge im Menue "Aenderung buchen": die Aenderungs-Anlaesse direkt, dazu
# Uebertragung, Korrektur und Anfangsbestand (nur bei leerem Buch).
ACTION_TRANSFER = "transfer"
ACTION_CORRECTION = "correction"
ACTION_OPENING = "opening"
ACTION_LABELS = {
    REASON_EXTENSION: "Zubau / Erweiterung",
    REASON_NEW_CONNECTION: "Neuanschluss",
    REASON_DECOMMISSION: "Stilllegung",
    REASON_DEMOLITION: "Abbruch / Rückbau",
    REASON_OTHER: "Sonstige Änderung",
    ACTION_TRANSFER: "Übertragung an andere Liegenschaft",
    ACTION_CORRECTION: "Korrektur",
    ACTION_OPENING: "Anfangsbestand",
}
ACTION_ICONS = {
    REASON_EXTENSION: "fa-hammer",
    REASON_NEW_CONNECTION: "fa-plug",
    REASON_DECOMMISSION: "fa-power-off",
    REASON_DEMOLITION: "fa-house-damage",
    REASON_OTHER: "fa-ellipsis-h",
    ACTION_TRANSFER: "fa-exchange-alt",
    ACTION_CORRECTION: "fa-pen",
    ACTION_OPENING: "fa-flag",
}

# Dezente Tabler-Soft-Badges (Konvention: bg-{color}-lt, nie text-white-lt).
_BADGES = {
    KIND_OPENING: "bg-secondary-lt",
    KIND_TRANSFER_IN: "bg-azure-lt",
    KIND_TRANSFER_OUT: "bg-azure-lt",
    KIND_CORRECTION: "bg-yellow-lt",
    REASON_EXTENSION: "bg-success-lt",
    REASON_NEW_CONNECTION: "bg-success-lt",
    REASON_DECOMMISSION: "bg-orange-lt",
    REASON_DEMOLITION: "bg-orange-lt",
    REASON_OTHER: "bg-secondary-lt",
}

SOURCE_MANUAL = "manual"
SOURCE_IMPORT = "import"
SOURCE_MIGRATION = "migration"
SOURCE_DEMO = "demo"
SOURCES = (SOURCE_MANUAL, SOURCE_IMPORT, SOURCE_MIGRATION, SOURCE_DEMO)


def entry_label(entry):
    """Anzeige eines Eintrags: Anlass vor Art (\"Zubau / Erweiterung\")."""
    if entry.kind == KIND_CHANGE and entry.reason in REASON_LABELS:
        return REASON_LABELS[entry.reason]
    label = KIND_LABELS.get(entry.kind, entry.kind)
    if entry.kind in (KIND_TRANSFER_IN, KIND_TRANSFER_OUT) and entry.reason in REASON_LABELS:
        return f"{REASON_LABELS[entry.reason]}: {label}"
    return label


def entry_badge(entry):
    if entry.kind == KIND_CHANGE:
        return _BADGES.get(entry.reason, "bg-secondary-lt")
    return _BADGES.get(entry.kind, "bg-secondary-lt")


def _fmt_date(d):
    return d.strftime("%d.%m.%Y")


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------
def holding(property_id, at=None):
    at = at or date.today()
    total = (db.session.query(func.coalesce(func.sum(PropertyShareEntry.quantity), 0))
             .filter(PropertyShareEntry.property_id == property_id,
                     PropertyShareEntry.entry_date <= at)
             .scalar())
    return int(total or 0)  # MariaDB liefert Decimal fuer SUM(INT)


def holdings_map(property_ids=None, at=None):
    """``{property_id: Bestand}`` zum Stichtag — EINE gruppierte Abfrage."""
    at = at or date.today()
    q = (db.session.query(PropertyShareEntry.property_id,
                          func.sum(PropertyShareEntry.quantity))
         .filter(PropertyShareEntry.entry_date <= at))
    if property_ids is not None:
        ids = [i for i in property_ids if i is not None]
        if not ids:
            return {}
        q = q.filter(PropertyShareEntry.property_id.in_(ids))
    return {pid: int(total or 0)
            for pid, total in q.group_by(PropertyShareEntry.property_id).all()}


def history(property_id):
    """Alle Eintraege chronologisch mit laufendem Saldo: ``[(entry, saldo)]``."""
    entries = (PropertyShareEntry.query
               .filter(PropertyShareEntry.property_id == property_id)
               .order_by(PropertyShareEntry.entry_date, PropertyShareEntry.id)
               .all())
    out, balance = [], 0
    for e in entries:
        balance += e.quantity
        out.append((e, balance))
    return out


def has_entries(property_id):
    return db.session.query(PropertyShareEntry.id).filter(
        PropertyShareEntry.property_id == property_id).first() is not None


def properties_with_entries(property_ids=None):
    q = db.session.query(PropertyShareEntry.property_id).distinct()
    if property_ids is not None:
        ids = [i for i in property_ids if i is not None]
        if not ids:
            return set()
        q = q.filter(PropertyShareEntry.property_id.in_(ids))
    return {row[0] for row in q.all()}


def paid_by(customer_id):
    """Eintraege, die dieser Kontakt bezahlt hat (neueste zuerst)."""
    return (PropertyShareEntry.query
            .filter(PropertyShareEntry.payer_customer_id == customer_id)
            .order_by(PropertyShareEntry.entry_date.desc(), PropertyShareEntry.id.desc())
            .all())


def is_payer(customer_id):
    return db.session.query(PropertyShareEntry.id).filter(
        PropertyShareEntry.payer_customer_id == customer_id).first() is not None


def current_owner_ids(property_id, on=None):
    """Eigentuemer zum Datum (Vorbelegung "bezahlt von"), aelteste zuerst."""
    on = on or date.today()
    rows = (PropertyOwnership.query
            .filter(PropertyOwnership.property_id == property_id,
                    PropertyOwnership.valid_from <= on,
                    db.or_(PropertyOwnership.valid_to.is_(None),
                           PropertyOwnership.valid_to >= on))
            .order_by(PropertyOwnership.valid_from, PropertyOwnership.customer_id)
            .all())
    return [o.customer_id for o in rows]


# ---------------------------------------------------------------------------
# Regeln + Cache
# ---------------------------------------------------------------------------
def _daily_sums(property_id):
    rows = (db.session.query(PropertyShareEntry.entry_date,
                             func.sum(PropertyShareEntry.quantity))
            .filter(PropertyShareEntry.property_id == property_id)
            .group_by(PropertyShareEntry.entry_date)
            .all())
    return {d: int(total or 0) for d, total in rows}


def validate_properties(*property_ids):
    """Nach ``flush()``: ``ShareError``, wenn ein Bestand zu irgendeinem Datum
    negativ wird."""
    for pid in {p for p in property_ids if p is not None}:
        hit = first_negative(_daily_sums(pid))
        if hit:
            prop = db.session.get(Property, pid)
            label = prop.label() if prop else f"#{pid}"
            day, balance = hit
            raise ShareError(f"Liegenschaft {label} hätte am {_fmt_date(day)} {balance} "
                             "Anteile — der Bestand darf zu keinem Datum negativ werden.")


def _latest_area_entry(property_id):
    return (PropertyShareEntry.query
            .filter(PropertyShareEntry.property_id == property_id,
                    PropertyShareEntry.area_m2.isnot(None))
            .order_by(PropertyShareEntry.entry_date.desc(), PropertyShareEntry.id.desc())
            .first())


def sync_cache(*property_ids, area_entry=None, area_removed=False):
    """Setzt ``PropertyWgProfile.shares`` = Summe des Buchs. Flaeche: nur, wenn
    ``area_entry`` der juengste Eintrag mit Flaeche ist, bzw. nach dem Loeschen
    eines solchen (``area_removed``) die Flaeche des nun juengsten."""
    totals = holdings_map([p for p in property_ids if p is not None], at=date.max)
    for pid in {p for p in property_ids if p is not None}:
        prop = db.session.get(Property, pid)
        if prop is None:
            continue
        profile = prop.ensure_wg_profile()
        profile.shares = totals.get(pid, 0)
        if area_entry is not None and area_entry.property_id == pid:
            latest = _latest_area_entry(pid)
            if latest is not None and latest.id == area_entry.id:
                profile.area_m2 = area_entry.area_m2
        elif area_removed:
            latest = _latest_area_entry(pid)
            if latest is not None:
                profile.area_m2 = latest.area_m2


def cache_mismatches():
    """``[(property_id, cache, summe)]`` fuer Abweichungen (Tests, Pruefung)."""
    totals = holdings_map(at=date.max)
    out = []
    for profile in PropertyWgProfile.query.all():
        expected = totals.get(profile.property_id, 0)
        if (profile.shares or 0) != expected:
            out.append((profile.property_id, profile.shares, expected))
    return out


def _check_date(on):
    if not isinstance(on, date):
        raise ShareError("Bitte ein gültiges Datum angeben.")
    if on < MIN_DATE:
        raise ShareError("Das Datum liegt außerhalb des erlaubten Bereichs.")
    if on > date.today():
        raise ShareError("Das Anteilsbuch der Liegenschaft kennt keine Buchungen in "
                         "der Zukunft — bitte am Tag der Änderung buchen.")
    return on


def _check_int(value, *, label="Anteile"):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ShareError(f"Bitte eine ganze Zahl bei „{label}“ angeben.")


def _check_area(area):
    if area in (None, ""):
        return None
    try:
        area = int(area)
    except (TypeError, ValueError):
        raise ShareError("Die Fläche muss eine ganze Zahl in m² sein.")
    if area < 0:
        raise ShareError("Die Fläche darf nicht negativ sein.")
    return area


def _check_property(prop, *, must_be_active=False):
    if prop is None or prop.id is None:
        raise ShareError("Liegenschaft nicht gefunden.")
    if must_be_active and not prop.active:
        raise ShareError(f"Liegenschaft {prop.label()} ist archiviert.")


def _payer_id(payer):
    if payer is None:
        return None
    if isinstance(payer, Customer):
        return payer.id
    customer = db.session.get(Customer, int(payer))
    if customer is None:
        raise ShareError("Der gewählte Zahler existiert nicht.")
    return customer.id


def _clean_note(note):
    note = (note or "").strip()
    return note[:255] or None


def _add(prop, kind, quantity, on, *, reason=None, area=None, payer=None,
         counterparty=None, pair=None, note=None, source=SOURCE_MANUAL, user_id=None):
    entry = PropertyShareEntry(
        property_id=prop.id, kind=kind, quantity=quantity, entry_date=on,
        reason=reason, area_m2=area, payer_customer_id=_payer_id(payer),
        counterparty_property_id=counterparty.id if counterparty else None,
        pair_entry_id=pair.id if pair else None, note=_clean_note(note),
        source=source if source in SOURCES else SOURCE_MANUAL, created_by_id=user_id)
    db.session.add(entry)
    return entry


def _finish(entry, *property_ids):
    db.session.flush()
    validate_properties(*property_ids)
    sync_cache(*property_ids, area_entry=entry if entry.area_m2 is not None else None)
    return entry


# ---------------------------------------------------------------------------
# Schreiben (flushen + pruefen + Cache, nie committen)
# ---------------------------------------------------------------------------
def record_opening(prop, qty, on, *, area=None, payer=None, note=None,
                   source=SOURCE_MANUAL, user_id=None):
    """Anfangsbestand — nur, solange das Buch der Liegenschaft leer ist."""
    _check_property(prop)
    qty, on, area = _check_int(qty), _check_date(on), _check_area(area)
    if qty < 0:
        raise ShareError("Der Anfangsbestand darf nicht negativ sein.")
    if qty == 0 and area is None:
        raise ShareError("Bitte Anteile oder eine Fläche angeben.")
    if has_entries(prop.id):
        raise ShareError("Die Liegenschaft hat schon ein Anteilsbuch — bitte eine "
                         "Änderung oder Korrektur buchen.")
    entry = _add(prop, KIND_OPENING, qty, on, area=area, payer=payer, note=note,
                 source=source, user_id=user_id)
    return _finish(entry, prop.id)


def record_change(prop, delta, on, reason, *, area=None, payer=None, note=None,
                  source=SOURCE_MANUAL, user_id=None):
    """Aenderung aus einem Anlass (Zubau, Neuanschluss, Stilllegung, Abbruch,
    Sonstiges) um ``delta`` Anteile (0 nur mit Flaeche)."""
    _check_property(prop)
    if reason not in CHANGE_REASONS:
        raise ShareError("Bitte den Anlass der Änderung wählen.")
    delta, on, area = _check_int(delta), _check_date(on), _check_area(area)
    if delta == 0 and area is None:
        raise ShareError("Bitte eine Änderung der Anteile oder die neue Fläche angeben.")
    if reason in INCREASE_REASONS and delta < 0:
        raise ShareError(f"{REASON_LABELS[reason]} erhöht die Anteile — für weniger "
                         "Anteile „Stilllegung“, „Abbruch“ oder „Korrektur“ wählen.")
    if reason in DECREASE_REASONS and delta > 0:
        raise ShareError(f"{REASON_LABELS[reason]} verringert die Anteile — für mehr "
                         "Anteile „Zubau“ oder „Neuanschluss“ wählen.")
    entry = _add(prop, KIND_CHANGE, delta, on, reason=reason, area=area, payer=payer,
                 note=note, source=source, user_id=user_id)
    return _finish(entry, prop.id)


def record_correction(prop, delta, on, note, *, area=None, payer=None,
                      source=SOURCE_MANUAL, user_id=None):
    """Korrektur (Fehler beheben) mit Pflicht-Notiz; 0 nur mit Flaeche."""
    _check_property(prop)
    delta, on, area = _check_int(delta), _check_date(on), _check_area(area)
    if delta == 0 and area is None:
        raise ShareError("Eine Korrektur braucht eine Änderung der Anteile oder eine Fläche.")
    if not (note or "").strip():
        raise ShareError("Bei einer Korrektur bitte den Grund als Notiz angeben.")
    entry = _add(prop, KIND_CORRECTION, delta, on, area=area, payer=payer, note=note,
                 source=source, user_id=user_id)
    return _finish(entry, prop.id)


def record_transfer(from_prop, to_prop, qty, on, reason=None, *, payer=None, note=None,
                    source=SOURCE_MANUAL, user_id=None):
    """Uebertragung ``qty`` Anteile von ``from_prop`` an ``to_prop`` (Teilung,
    Zusammenlegung). Liefert ``(out_entry, in_entry)``."""
    _check_property(from_prop)
    if to_prop is None:
        raise ShareError("Bitte die Ziel-Liegenschaft wählen.")
    if to_prop.id == from_prop.id:
        raise ShareError("Anteile können nicht an dieselbe Liegenschaft übertragen werden.")
    _check_property(to_prop, must_be_active=True)
    if reason not in (None, "") and reason not in TRANSFER_REASONS:
        raise ShareError("Unbekannter Anlass der Übertragung.")
    qty, on = _check_int(qty), _check_date(on)
    if qty < 1:
        raise ShareError("Die Anzahl muss mindestens 1 sein.")
    reason = reason or None
    out = _add(from_prop, KIND_TRANSFER_OUT, -qty, on, reason=reason, payer=payer,
               counterparty=to_prop, note=note, source=source, user_id=user_id)
    db.session.flush()
    inn = _add(to_prop, KIND_TRANSFER_IN, qty, on, reason=reason, payer=payer,
               counterparty=from_prop, pair=out, note=note, source=source,
               user_id=user_id)
    db.session.flush()
    validate_properties(from_prop.id, to_prop.id)
    sync_cache(from_prop.id, to_prop.id)
    return out, inn


def partner_of(entry):
    if entry.kind == KIND_TRANSFER_IN and entry.pair_entry_id:
        return db.session.get(PropertyShareEntry, entry.pair_entry_id)
    if entry.kind == KIND_TRANSFER_OUT:
        return PropertyShareEntry.query.filter_by(pair_entry_id=entry.id).first()
    return None


def delete_entry(entry):
    """Loescht einen Eintrag (Uebertragung: beide Haelften), prueft neu und
    zieht den Cache nach. Liefert die betroffenen Liegenschafts-IDs."""
    partner = partner_of(entry)
    affected = {entry.property_id}
    had_area = entry.area_m2 is not None
    if partner is not None:
        affected.add(partner.property_id)
        first, second = ((partner, entry) if partner.kind == KIND_TRANSFER_IN
                         else (entry, partner))
        db.session.delete(first)
        db.session.flush()
        db.session.delete(second)
    else:
        db.session.delete(entry)
    db.session.flush()
    validate_properties(*affected)
    sync_cache(*affected, area_removed=had_area)
    return affected


# ---------------------------------------------------------------------------
# Datenimport / Bestand
# ---------------------------------------------------------------------------
def opening_date(prop):
    """Datum fuer den Anfangsbestand aus Altdaten: frueheste Eigentuemerschaft,
    sonst Anlagedatum der Liegenschaft, sonst heute (nie in der Zukunft)."""
    today = date.today()
    first = (db.session.query(func.min(PropertyOwnership.valid_from))
             .filter(PropertyOwnership.property_id == prop.id).scalar())
    candidate = first or (prop.created_at.date() if prop.created_at else None) or today
    return min(candidate, today)


def ensure_openings_from_cache(*, source=SOURCE_IMPORT):
    """Liegenschaften mit Anteilen/Flaeche, aber ohne Anteilsbuch (z. B. aus
    einem Export vor v1.50.0) bekommen einen Anfangsbestand. Liefert die Anzahl."""
    with_book = properties_with_entries()
    created = 0
    for profile in PropertyWgProfile.query.all():
        if profile.property_id in with_book:
            continue
        if not (profile.shares or 0) > 0 and profile.area_m2 is None:
            continue
        prop = db.session.get(Property, profile.property_id)
        if prop is None:
            continue
        db.session.add(PropertyShareEntry(
            property_id=prop.id, kind=KIND_OPENING, quantity=max(profile.shares or 0, 0),
            entry_date=opening_date(prop), area_m2=profile.area_m2,
            note="Übernahme des bisherigen Bestands", source=source))
        created += 1
    db.session.flush()
    return created


def book_snapshot():
    """Vor einem Zusammenfuehren: ``(hoechste Eintrags-ID, Liegenschaften mit
    Buch)``."""
    max_id = db.session.query(func.max(PropertyShareEntry.id)).scalar() or 0
    return int(max_id), properties_with_entries()


def drop_merged_duplicates(snapshot):
    """Nach einem Zusammenfuehren: neu eingefuegte Eintraege an Liegenschaften,
    die schon vorher ein Buch hatten, wieder entfernen (Uebertragungen immer
    mit beiden Haelften). Liefert die Anzahl entfernter Eintraege."""
    max_id, had_book = snapshot
    if not had_book:
        return 0
    new = PropertyShareEntry.query.filter(PropertyShareEntry.id > max_id).all()
    by_id = {e.id: e for e in new}
    drop = {e.id for e in new if e.property_id in had_book}
    changed = True
    while changed:   # Paare nie halb stehen lassen
        changed = False
        for e in new:
            pair = e.pair_entry_id if e.pair_entry_id in by_id else None
            if e.id in drop and pair and pair not in drop:
                drop.add(pair)
                changed = True
            elif pair in drop and e.id not in drop:
                drop.add(e.id)
                changed = True
    # transfer_in (traegt den Self-FK) zuerst loeschen
    for e in sorted((by_id[i] for i in drop if i in by_id),
                    key=lambda x: 0 if x.kind == KIND_TRANSFER_IN else 1):
        db.session.delete(e)
    db.session.flush()
    return len(drop)


def resync_share_caches():
    """Cache aller Liegenschafts-Profile = Summe des Buchs (z. B. nach einem
    Zusammenfuehren, das Eintraege doppelt einfuegt). Flaeche bleibt."""
    totals = holdings_map(at=date.max)
    changed = 0
    for profile in PropertyWgProfile.query.all():
        expected = totals.get(profile.property_id, 0)
        if (profile.shares or 0) != expected:
            profile.shares = expected
            changed += 1
    db.session.flush()
    return changed
