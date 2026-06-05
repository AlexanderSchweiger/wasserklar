"""Geschäftslogik der Schriftführung: Empfänger-Vorauswahl, Anwesenheit/Quorum,
Protokoll-Vorbelegung, Agenda-Parsing.
"""
import re
from datetime import date

from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import (
    AppSetting, Customer, WgFunction,
    Meeting, MeetingAttendance, MeetingResolution, MeetingProtocol,
)
from app.wg import BOARD_FUNCTIONS, FUNCTION_LABELS, function_keys_ordered


def _is_member(customer):
    """Mitglied i.S. der Beschlussfähigkeit (Default 'member', solange kein
    Profil gesetzt ist — siehe Customer.wg_status)."""
    return customer.wg_status == "member"


def all_contacts():
    """Alle aktiven Kontakte (für die Empfängerauswahl), alphabetisch sortiert,
    mit vorgeladenem WG-Profil und Funktionen."""
    return (Customer.query
            .options(joinedload(Customer.wg_profile),
                     selectinload(Customer.wg_functions))
            .filter(Customer.active.is_(True))
            .order_by(Customer.name.asc())
            .all())


def preselect_recipient_ids(meeting_type):
    """IDs der vorausgewählten Empfänger.

    Vorstandssitzung: alle mit einer Vorstandsfunktion (= ``BOARD_FUNCTIONS``,
    also ohne Kassaprüfer/Rechnungsprüfer).
    Hauptversammlung: alle Mitglieder + alle Funktionäre (inkl. Kassaprüfer).
    """
    if meeting_type == Meeting.TYPE_BOARD:
        rows = (Customer.query
                .filter(Customer.active.is_(True),
                        Customer.wg_functions.any(WgFunction.function.in_(BOARD_FUNCTIONS)))
                .all())
        return {c.id for c in rows}

    # Hauptversammlung
    ids = set()
    functionaries = (Customer.query
                     .filter(Customer.active.is_(True), Customer.wg_functions.any())
                     .all())
    ids.update(c.id for c in functionaries)
    members = (Customer.query
               .options(joinedload(Customer.wg_profile))
               .filter(Customer.active.is_(True), Customer.is_customer.is_(True))
               .all())
    ids.update(c.id for c in members if _is_member(c))
    return ids


def customer_function_labels(customer):
    """Deutsche Funktions-Labels eines Kontakts in kanonischer Reihenfolge."""
    keys = function_keys_ordered(customer.function_keys())
    return [FUNCTION_LABELS.get(k, k) for k in keys]


def total_member_count():
    """Anzahl aktiver Mitglieder — Basis für die Beschlussfähigkeit."""
    rows = (Customer.query
            .options(joinedload(Customer.wg_profile))
            .filter(Customer.active.is_(True), Customer.is_customer.is_(True))
            .all())
    return sum(1 for c in rows if _is_member(c))


def quorum_threshold():
    """Schwelle für die Beschlussfähigkeit (Anteil anwesender Mitglieder).
    AppSetting ``schriftfuehrung.quorum_threshold``; Default 0,5 (> 50 %)."""
    raw = AppSetting.get("schriftfuehrung.quorum_threshold")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return val if 0 < val < 1 else 0.5


def list_present_count(meeting):
    """Anwesende Stimmberechtigte laut Personenliste (status=present & Mitglied)."""
    return sum(1 for a in meeting.attendances
               if a.status == MeetingAttendance.STATUS_PRESENT and a.is_member)


def compute_quorum(meeting):
    """``(present, total, is_quorate)`` für die Beschlussfähigkeit.

    Basis (``total``) sind die **eingeladenen** Stimmberechtigten — die als
    Mitglied markierten Einträge der Anwesenheitsliste, nicht mehr alle
    Mitglieder der Genossenschaft. Bei einer Vorstandssitzung sind das nur die
    eingeladenen Vorstandsmitglieder, nicht die gesamte Mitgliederzahl.

    Anwesende (``present``) kommen aus der Personenliste — oder, im Freitext-
    Modus bzw. nach erfolgloser Wartefrist, aus der manuell erfassten Kopfzahl
    ``present_headcount``.

    Wurde die (Haupt-)Versammlung nach einer Wartefrist erneut eröffnet
    (``reconvened``), ist sie unabhängig vom Anteil mit den Anwesenden
    beschlussfähig."""
    protocol = meeting.protocol
    total = sum(1 for a in meeting.attendances if a.is_member)
    list_present = list_present_count(meeting)

    use_headcount = (
        protocol is not None
        and protocol.present_headcount is not None
        and (protocol.attendance_mode == MeetingProtocol.ATTENDANCE_FREETEXT
             or protocol.reconvened))
    present = protocol.present_headcount if use_headcount else list_present

    if protocol is not None and protocol.reconvened:
        is_quorate = present > 0
    else:
        thr = quorum_threshold()
        is_quorate = total > 0 and (present / total) > thr
    return present, total, is_quorate


def prefill_protocol(meeting):
    """Belegt Anwesenheit (aus den Eingeladenen) + Beschlüsse (aus den
    ``requires_vote``-TOPs) vor, sofern noch nicht vorhanden. Kein commit —
    der Aufrufer kontrolliert die Transaktion."""
    # Vorstandssitzung: alle Eingeladenen sind stimmberechtigte Vorstands-
    # mitglieder. Hauptversammlung: stimmberechtigt sind die Mitglieder.
    board = meeting.meeting_type == Meeting.TYPE_BOARD
    existing_attendance = {a.customer_id for a in meeting.attendances}
    for inv in meeting.invitations:
        if inv.customer_id in existing_attendance:
            continue
        c = inv.customer
        db.session.add(MeetingAttendance(
            meeting_id=meeting.id,
            customer_id=inv.customer_id,
            status=MeetingAttendance.STATUS_PRESENT,
            is_member=True if board else (_is_member(c) if c else False),
            weight=1,
        ))

    existing_res = {r.agenda_item_id for r in meeting.resolutions if r.agenda_item_id}
    for item in meeting.agenda_items:
        if not item.requires_vote or item.id in existing_res:
            continue
        db.session.add(MeetingResolution(
            meeting_id=meeting.id,
            agenda_item_id=item.id,
            title=item.title,
            status=MeetingResolution.STATUS_ACCEPTED,
            decided_on=meeting.meeting_date or date.today(),
        ))


_AGENDA_KEY_RE = re.compile(r"^agenda\[(\d+)\]\[")


def parse_agenda_rows(form):
    """Liest die Agenda-Zeilen (``agenda[i][title|description|requires_vote]``)
    aus dem Formular in eine geordnete Liste von Dicts; verwirft leere Zeilen."""
    indices = set()
    for key in form.keys():
        m = _AGENDA_KEY_RE.match(key)
        if m:
            indices.add(int(m.group(1)))
    rows = []
    for i in sorted(indices):
        title = (form.get(f"agenda[{i}][title]") or "").strip()
        desc = (form.get(f"agenda[{i}][description]") or "").strip()
        rv = form.get(f"agenda[{i}][requires_vote]") in ("1", "on", "true", "yes")
        if not title and not desc:
            continue
        rows.append({
            "title": title or "(ohne Titel)",
            "description": desc or None,
            "requires_vote": rv,
        })
    return rows
