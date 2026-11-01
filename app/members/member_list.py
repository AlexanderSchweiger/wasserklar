"""Mitgliederliste zum Stichtag — EIN Builder fuer Seite, CSV und PDF.

Wer zaehlt: Kunden mit WG-Status ``member`` bzw. ``resigned``, deren
Mitgliedschaft den Stichtag umfasst (``member_since`` leer oder <= D,
``member_until`` leer oder >= D). Ein ``resigned`` mit spaeterem
``member_until`` (Kuendigung zum Jahresende) ist am Stichtag noch Mitglied.
Interessenten und Externe stehen nie auf der Liste. Der Status selbst ist
nicht historisiert — ein vergangener Stichtag beruht nur auf seit/bis.

Anteile: bei Anteilen am Mitglied der Bestand im Anteilsbuch zum Stichtag,
sonst die Anteile der am Stichtag besessenen Liegenschaften. Miteigentuemer
sehen dort die vollen Anteile der Liegenschaft mit „gemeinsam mit …“; die
Summe zaehlt jede Liegenschaft nur einmal.
"""
from collections import defaultdict
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import wg
from app.extensions import db
from app.models import (Customer, CustomerWgProfile, Property, PropertyOwnership,
                        WgFunction)

SCOPE_MEMBERS = "members"   # aktuelle Mitglieder (nur aktive Kontakte)
SCOPE_ALL = "all"           # zusaetzlich ehemalige Mitglieder + archivierte Kontakte
SCOPES = (SCOPE_MEMBERS, SCOPE_ALL)

STATE_MEMBER = "member"
STATE_FORMER = "former"

SORTS = ("name", "nr", "shares", "since")


def membership_at(status, member_since, member_until, at):
    """Mitgliedschaft am Stichtag: ``"member"``, ``"former"`` oder ``None``
    (nie Mitglied bzw. noch nicht). Reine Funktion."""
    if status not in (wg.STATUS_MEMBER, wg.STATUS_RESIGNED):
        return None
    if member_since is not None and member_since > at:
        return None
    if status == wg.STATUS_RESIGNED and member_until is None:
        return STATE_FORMER   # ausgeschieden ohne Datum
    if member_until is None or member_until >= at:
        return STATE_MEMBER
    return STATE_FORMER


def _property_shares_at(customer_ids, at):
    """``{customer_id: [(property, shares)]}`` und ``{property_id: [customer_id]}``
    fuer die am Stichtag besessenen Liegenschaften mit Anteilen — die Anteile
    kommen aus dem Anteilsbuch der Liegenschaft zum Stichtag (historisiert)."""
    if not customer_ids:
        return {}, {}
    from app import property_shares
    rows = (db.session.query(PropertyOwnership.customer_id, Property)
            .join(Property, Property.id == PropertyOwnership.property_id)
            .filter(PropertyOwnership.valid_from <= at,
                    or_(PropertyOwnership.valid_to.is_(None),
                        PropertyOwnership.valid_to >= at))
            .order_by(Property.object_number, Property.id)
            .all())
    holdings = property_shares.holdings_map({prop.id for _, prop in rows}, at)
    by_customer, owners = defaultdict(list), defaultdict(list)
    for cid, prop in rows:
        shares = holdings.get(prop.id, 0)
        if shares <= 0:
            continue
        owners[prop.id].append(cid)
        if cid in customer_ids:
            by_customer[cid].append((prop, shares))
    return by_customer, owners


def build(at=None, scope=SCOPE_MEMBERS, q="", sort="name", direction="asc",
          shares_on_person=False):
    """Liefert ``(rows, totals)``. ``rows``: Liste von dicts (customer, state,
    status, since, until, functions, shares, properties, co_owners, flags)."""
    at = at or date.today()
    scope = scope if scope in SCOPES else SCOPE_MEMBERS
    sort = sort if sort in SORTS else "name"

    query = (Customer.query
             .options(joinedload(Customer.wg_profile))
             .filter(Customer.is_customer.is_(True)))
    if scope == SCOPE_MEMBERS:
        query = query.filter(Customer.active.is_(True))
    # Status-Vorfilter in SQL (kein Profil = Mitglied); die Datumsregel danach.
    query = query.outerjoin(CustomerWgProfile,
                            CustomerWgProfile.customer_id == Customer.id).filter(or_(
        CustomerWgProfile.status.is_(None),
        CustomerWgProfile.status.in_((wg.STATUS_MEMBER, wg.STATUS_RESIGNED)),
    ))
    q = (q or "").strip()
    if q:
        cond = Customer.name.ilike(f"%{q}%")
        if q.isdigit():
            cond = or_(cond, Customer.customer_number == int(q))
        query = query.filter(cond)

    candidates = []
    for c in query.order_by(Customer.name).all():
        state = membership_at(c.wg_status, c.member_since, c.wg_member_until, at)
        if state is None:
            continue
        if state == STATE_FORMER and scope != SCOPE_ALL:
            continue
        candidates.append((c, state))

    ids = [c.id for c, _ in candidates]
    funcs = defaultdict(list)
    if ids:
        for f in WgFunction.query.filter(WgFunction.customer_id.in_(ids)).all():
            funcs[f.customer_id].append(f.function)

    if shares_on_person:
        from app import wg_shares
        holdings = wg_shares.holdings_map(ids, at)
        props_by_customer, owners = {}, {}
    else:
        holdings = {}
        props_by_customer, owners = _property_shares_at(set(ids), at)

    names = {c.id: c.name for c, _ in candidates}
    rows = []
    for c, state in candidates:
        props = props_by_customer.get(c.id, [])
        if shares_on_person:
            shares = holdings.get(c.id, 0)
            co_owners = []
        else:
            shares = sum(n for _, n in props)
            co_owners = sorted({names.get(o) or _name_of(o)
                                for p, _ in props for o in owners.get(p.id, [])
                                if o != c.id})
        flags = []
        if state == STATE_MEMBER and shares <= 0:
            flags.append("no_shares")
        if (state == STATE_FORMER or c.wg_status == wg.STATUS_RESIGNED) and shares > 0:
            flags.append("payout_open")
        if not c.active:
            flags.append("archived")
        rows.append({
            "customer": c, "state": state, "status": c.wg_status,
            "since": c.member_since, "until": c.wg_member_until,
            "functions": wg.function_keys_ordered(funcs.get(c.id, [])),
            "shares": shares, "properties": props, "co_owners": co_owners,
            "flags": flags,
        })

    _sort_rows(rows, sort, direction == "desc")

    member_rows = [r for r in rows if r["state"] == STATE_MEMBER]
    if shares_on_person:
        total_shares = sum(r["shares"] for r in member_rows)
        co_owned = 0
    else:
        # Jede Liegenschaft einmal (Miteigentum nicht doppelt zaehlen).
        distinct = {}
        for r in member_rows:
            for prop, n in r["properties"]:
                distinct[prop.id] = n
        total_shares = sum(distinct.values())
        co_owned = sum(1 for pid in distinct if len(owners.get(pid, [])) > 1)
    totals = {
        "members": len(member_rows),
        "former": len(rows) - len(member_rows),
        "shares": total_shares,
        "co_owned_properties": co_owned,
        "without_shares": sum(1 for r in member_rows if "no_shares" in r["flags"]),
    }
    return rows, totals


def _name_of(customer_id):
    c = db.session.get(Customer, customer_id)
    return c.name if c else f"#{customer_id}"


def _sort_rows(rows, sort, desc):
    def nr_key(r):
        nr = r["customer"].customer_number
        return (0, nr) if nr is not None else (1, 0)

    keys = {
        "name": lambda r: (r["customer"].name or "").lower(),
        "nr": nr_key,
        "shares": lambda r: (r["shares"], (r["customer"].name or "").lower()),
        "since": lambda r: (r["since"] or date.max, (r["customer"].name or "").lower()),
    }
    rows.sort(key=keys[sort], reverse=desc)


CSV_HEADERS = ["Mitgliedsnummer", "Name", "Anschrift", "Status", "Mitglied seit",
               "Mitglied bis", "Funktionen", "Anteile"]


def csv_rows(rows, totals, shares_on_person):
    """Zeilen fuer den CSV-Export (inkl. Summenzeile)."""
    headers = list(CSV_HEADERS)
    if not shares_on_person:
        headers += ["Liegenschaften", "Miteigentümer"]
    out = [headers]
    for r in rows:
        c = r["customer"]
        line = [
            c.customer_number if c.customer_number is not None else "",
            c.name, c.address_display(),
            ("ehemalig" if r["state"] == STATE_FORMER else wg.status_label(r["status"])),
            r["since"].strftime("%d.%m.%Y") if r["since"] else "",
            r["until"].strftime("%d.%m.%Y") if r["until"] else "",
            ", ".join(wg.function_label(f) for f in r["functions"]),
            r["shares"],
        ]
        if not shares_on_person:
            line += [", ".join(p.object_number or p.address_display() for p, _ in r["properties"]),
                     ", ".join(r["co_owners"])]
        out.append(line)
    label = ("Summe" if shares_on_person
             else "Summe (jede Liegenschaft einmal)")
    out.append([label, f"{totals['members']} Mitglieder", "", "", "", "", "", totals["shares"]]
               + ([] if shares_on_person else ["", ""]))
    return out
