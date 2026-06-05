"""Tests für das Schriftführungs-Modul (Sitzungen, Einladungen, Protokoll,
Beschluss-Register, Schriftverkehr-Archiv).

Deckt ab: Recht- + Mandanttyp-Gate, Sitzungs-CRUD, Agenda, Empfänger-Vorauswahl
(Vorstand ohne Rechnungsprüfer / HV mit allen), Status-/Lock-Workflow, Quorum,
Beschluss-Suche und Upload-Validierung.
"""
import io
from datetime import date, time

import pytest

from app.extensions import db
from app.models import (
    AppSetting, Customer, WgFunction, User,
    Meeting, MeetingAgendaItem, MeetingResolution, MeetingProtocol,
    MeetingInvitation, MeetingAttendance, MeetingDeliveryLog, SchriftverkehrDocument,
)
from app.schriftfuehrung import services, constants
from tests.conftest import _ensure_role


# ── Fixtures / Helpers ───────────────────────────────────────────────────────

@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    # Werkzeug-3 test_client teilt den CookieJar zwischen Instanzen; ohne Logout
    # bleibt eine vorherige Login-Session aktiv und der Login-View kurzschließt.
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": password})


def _mk_customer(name, email=None, wants_email=False, functions=()):
    c = Customer(name=name, email=email, rechnung_per_email=bool(wants_email))
    db.session.add(c)
    db.session.flush()
    for f in functions:
        db.session.add(WgFunction(customer_id=c.id, function=f))
    db.session.commit()
    return c


def _mk_meeting(meeting_type="board", **kw):
    m = Meeting(meeting_type=meeting_type, title=kw.pop("title", "Test-Sitzung"),
                status=kw.pop("status", "planning"), **kw)
    db.session.add(m)
    db.session.commit()
    return m


# ── Gate: Login / Mandanttyp / Recht ─────────────────────────────────────────

def test_requires_login(client):
    client.get("/auth/logout")
    r = client.get("/schriftfuehrung/board-meetings", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["Location"]


def test_blocked_for_non_cooperative(client, admin):
    AppSetting.set("org.type", "utility")
    db.session.commit()
    _login(client)
    r = client.get("/schriftfuehrung/board-meetings", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" not in r.headers["Location"]  # -> Dashboard, nicht Login


def test_blocked_without_permission(client, app):
    role = _ensure_role("Begrenzt", perms=["stammdaten"])
    u = User(username="limited", email="l@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    _login(client, "limited", "secret")
    r = client.get("/schriftfuehrung/board-meetings", follow_redirects=False)
    assert r.status_code == 302  # -> Dashboard (kein Zugriff)


def test_board_list_renders(client, admin):
    _login(client)
    r = client.get("/schriftfuehrung/board-meetings")
    assert r.status_code == 200
    assert "Vorstandssitzungen" in r.get_data(as_text=True)


# ── Sitzungs-CRUD + Agenda ───────────────────────────────────────────────────

def test_create_meeting(client, admin):
    _login(client)
    r = client.post("/schriftfuehrung/meetings/new", data={
        "meeting_type": "board", "title": "Sitzung Juni",
        "meeting_date": "2026-07-01", "start_time": "19:00", "end_time": "21:00",
        "location": "Saal",
    }, follow_redirects=False)
    assert r.status_code == 302
    m = Meeting.query.filter_by(title="Sitzung Juni").first()
    assert m is not None
    assert m.meeting_type == "board" and m.status == "planning"
    assert m.meeting_date == date(2026, 7, 1)
    assert m.start_time == time(19, 0) and m.end_time == time(21, 0)


def test_meeting_detail_and_agenda_save(client, admin):
    _login(client)
    m = _mk_meeting()
    r = client.get(f"/schriftfuehrung/meetings/{m.id}")
    assert r.status_code == 200
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/agenda", data={
        "agenda[0][title]": "Begrüßung",
        "agenda[1][title]": "Kassabericht", "agenda[1][requires_vote]": "1",
        "agenda[2][title]": "", "agenda[2][description]": "",  # leer -> verworfen
    }, follow_redirects=False)
    assert r.status_code == 302
    items = MeetingAgendaItem.query.filter_by(meeting_id=m.id).order_by(MeetingAgendaItem.position).all()
    assert [i.title for i in items] == ["Begrüßung", "Kassabericht"]
    assert items[0].requires_vote is False and items[1].requires_vote is True


def test_copy_meeting_includes_agenda(client, admin):
    _login(client)
    m = _mk_meeting(title="Original")
    db.session.add(MeetingAgendaItem(meeting_id=m.id, position=0, title="TOP A", requires_vote=True))
    db.session.commit()
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/copy", follow_redirects=False)
    assert r.status_code == 302
    copy = Meeting.query.filter(Meeting.title.like("Original (Kopie)%")).first()
    assert copy is not None
    assert [i.title for i in copy.agenda_items] == ["TOP A"]
    assert copy.agenda_items[0].requires_vote is True


def test_delete_only_while_planning(client, admin):
    _login(client)
    m = _mk_meeting(status="invited")
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/delete", follow_redirects=False)
    assert r.status_code == 302
    assert Meeting.query.get(m.id) is not None  # nicht gelöscht (bereits eingeladen)


# ── Empfänger-Vorauswahl ─────────────────────────────────────────────────────

def test_preselect_board_excludes_auditor(app):
    chairman = _mk_customer("Obmann", functions=["chairman"])
    auditor = _mk_customer("Prüfer", functions=["auditor"])
    _mk_customer("Einfaches Mitglied")  # ohne Funktion
    ids = services.preselect_recipient_ids("board")
    assert ids == {chairman.id}
    assert auditor.id not in ids


def test_preselect_assembly_includes_members_and_auditor(app):
    chairman = _mk_customer("Obmann", functions=["chairman"])
    auditor = _mk_customer("Prüfer", functions=["auditor"])
    member = _mk_customer("Einfaches Mitglied")
    ids = services.preselect_recipient_ids("assembly")
    assert {chairman.id, auditor.id, member.id} <= ids


def test_send_page_renders(client, admin):
    _login(client)
    _mk_customer("Obmann", functions=["chairman"])
    m = _mk_meeting()
    r = client.get(f"/schriftfuehrung/meetings/{m.id}/send")
    assert r.status_code == 200
    assert "Obmann" in r.get_data(as_text=True)


def test_invitation_email_records_and_invites(client, admin):
    _login(client)
    c = _mk_customer("Mitglied", email="m@test.test", wants_email=True)
    m = _mk_meeting(meeting_date=date(2026, 7, 1))
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/invitations/email", data={
        "recipient_ids": str(c.id), f"method_{c.id}": "email",
    }, follow_redirects=False)
    assert r.status_code == 302
    inv = MeetingInvitation.query.filter_by(meeting_id=m.id, customer_id=c.id).first()
    assert inv is not None and inv.last_email_status == "sent"
    assert Meeting.query.get(m.id).status == "invited"


def test_invitation_email_ajax(client, admin):
    _login(client)
    c = _mk_customer("Mitglied", email="m@test.test", wants_email=True)
    m = _mk_meeting(meeting_date=date(2026, 7, 1))
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/invitations/email-ajax",
                    data={"customer_id": str(c.id)})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["test_mode"] is False and j["email"] == "m@test.test"
    inv = MeetingInvitation.query.filter_by(meeting_id=m.id, customer_id=c.id).first()
    assert inv is not None and inv.last_email_status == "sent"
    assert MeetingDeliveryLog.query.filter_by(meeting_id=m.id).count() == 1
    assert Meeting.query.get(m.id).status == "invited"


def test_invitation_email_ajax_testmode_changes_nothing(client, admin):
    _login(client)
    c = _mk_customer("Mitglied", email="m@test.test", wants_email=True)
    m = _mk_meeting(meeting_date=date(2026, 7, 1))
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/invitations/email-ajax",
                    data={"customer_id": str(c.id), "test_mode": "1"})
    j = r.get_json()
    assert j["ok"] is True and j["test_mode"] is True
    assert j["email"] == "admin@test.test"  # an den eingeloggten Admin
    assert MeetingInvitation.query.filter_by(meeting_id=m.id).count() == 0
    assert MeetingDeliveryLog.query.filter_by(meeting_id=m.id).count() == 0
    assert Meeting.query.get(m.id).status == "planning"


def test_invitation_email_ajax_rejects_without_optin(client, admin):
    _login(client)
    c = _mk_customer("Ohne Mail")  # keine E-Mail-Freigabe
    m = _mk_meeting()
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/invitations/email-ajax",
                    data={"customer_id": str(c.id)})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_invitation_preview_docx(client, admin):
    _login(client)
    c = _mk_customer("Mitglied")
    m = _mk_meeting(meeting_date=date(2026, 7, 1))
    r = client.get(f"/schriftfuehrung/meetings/{m.id}/invitation-preview"
                   f"?customer_id={c.id}&fmt=docx")
    assert r.status_code == 200
    assert r.data[:2] == b"PK"  # .docx ist ein ZIP-Container


# ── Quorum ───────────────────────────────────────────────────────────────────

def test_compute_quorum(app):
    members = [_mk_customer(f"M{i}") for i in range(3)]
    m = _mk_meeting()
    db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=members[0].id, status="present", is_member=True))
    db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=members[1].id, status="present", is_member=True))
    db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=members[2].id, status="absent", is_member=True))
    db.session.commit()
    present, total, quorate = services.compute_quorum(m)
    assert present == 2 and total == 3 and quorate is True


def test_quorum_basis_only_invited(app):
    """Basis ist die Anwesenheitsliste (Eingeladene), nicht alle Mitglieder:
    weitere Mitglieder außerhalb der Liste zählen nicht mit."""
    invited = [_mk_customer(f"V{i}") for i in range(3)]
    [_mk_customer(f"Sonstig{i}") for i in range(10)]  # nicht eingeladen
    m = _mk_meeting(meeting_type="board")
    db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=invited[0].id, status="present", is_member=True))
    db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=invited[1].id, status="present", is_member=True))
    db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=invited[2].id, status="absent", is_member=True))
    db.session.commit()
    present, total, quorate = services.compute_quorum(m)
    assert present == 2 and total == 3 and quorate is True


def test_quorum_assembly_reconvened_is_quorate(app):
    """Hauptversammlung nach Wartefrist erneut eröffnet → beschlussfähig trotz
    geringer Anwesenheit; die Kopfzahl gilt als Anwesende."""
    members = [_mk_customer(f"M{i}") for i in range(20)]
    m = _mk_meeting(meeting_type="assembly")
    for i, c in enumerate(members):
        db.session.add(MeetingAttendance(
            meeting_id=m.id, customer_id=c.id,
            status="present" if i < 4 else "absent", is_member=True))
    db.session.add(MeetingProtocol(meeting_id=m.id, reconvened=True,
                                   reconvene_wait_minutes=30, present_headcount=4))
    db.session.commit()
    present, total, quorate = services.compute_quorum(m)
    assert total == 20 and present == 4 and quorate is True


def test_quorum_freetext_uses_headcount(app):
    """Freitext-Modus: Anwesende kommen aus der Kopfzahl, nicht aus der Liste."""
    members = [_mk_customer(f"M{i}") for i in range(5)]
    m = _mk_meeting(meeting_type="assembly")
    for c in members:
        db.session.add(MeetingAttendance(meeting_id=m.id, customer_id=c.id, status="present", is_member=True))
    db.session.add(MeetingProtocol(meeting_id=m.id, attendance_mode="freetext", present_headcount=2))
    db.session.commit()
    present, total, quorate = services.compute_quorum(m)
    assert present == 2 and total == 5 and quorate is False


def test_protocol_prefill_board_members_all_count(client, admin):
    """Vorstandssitzung: alle Eingeladenen werden als stimmberechtigt vorbelegt."""
    _login(client)
    c1, c2 = _mk_customer("V1"), _mk_customer("V2")
    m = _mk_meeting(meeting_type="board", meeting_date=date(2026, 7, 1))
    db.session.add(MeetingInvitation(meeting_id=m.id, customer_id=c1.id, delivery_method="post"))
    db.session.add(MeetingInvitation(meeting_id=m.id, customer_id=c2.id, delivery_method="post"))
    db.session.commit()
    client.get(f"/schriftfuehrung/meetings/{m.id}/protocol")
    atts = MeetingAttendance.query.filter_by(meeting_id=m.id).all()
    assert len(atts) == 2 and all(a.is_member for a in atts)


def test_protocol_save_freetext_and_reconvene(client, admin, tmp_path, monkeypatch):
    monkeypatch.setitem(client.application.config, "PDF_DIR", str(tmp_path / "pdfs"))
    _login(client)
    members = [_mk_customer(f"M{i}") for i in range(3)]
    m = _mk_meeting(meeting_type="assembly", meeting_date=date(2026, 7, 1))
    for c in members:
        db.session.add(MeetingInvitation(meeting_id=m.id, customer_id=c.id, delivery_method="post"))
    db.session.commit()
    client.get(f"/schriftfuehrung/meetings/{m.id}/protocol")  # legt Protokoll an

    r = client.post(f"/schriftfuehrung/meetings/{m.id}/protocol/save", data={
        "content_html": "x",
        "attendance_freetext_mode": "1",
        "attendance_freetext": "12 Personen anwesend.",
        "present_headcount": "12",
        "reconvened": "1",
        "reconvene_wait_minutes": "30",
        "reconvene_headcount": "12",
    }, follow_redirects=False)
    assert r.status_code == 302
    prot = MeetingProtocol.query.filter_by(meeting_id=m.id).first()
    assert prot.is_freetext_attendance
    assert prot.attendance_freetext == "12 Personen anwesend."
    assert prot.present_headcount == 12
    assert prot.reconvened is True and prot.reconvene_wait_minutes == 30
    assert prot.is_quorate is True


def test_protocol_board_ignores_reconvene(client, admin, tmp_path, monkeypatch):
    monkeypatch.setitem(client.application.config, "PDF_DIR", str(tmp_path / "pdfs"))
    _login(client)
    c = _mk_customer("V1")
    m = _mk_meeting(meeting_type="board", meeting_date=date(2026, 7, 1))
    db.session.add(MeetingInvitation(meeting_id=m.id, customer_id=c.id, delivery_method="post"))
    db.session.commit()
    client.get(f"/schriftfuehrung/meetings/{m.id}/protocol")
    client.post(f"/schriftfuehrung/meetings/{m.id}/protocol/save", data={
        "content_html": "x", "reconvened": "1", "reconvene_wait_minutes": "30",
    })
    prot = MeetingProtocol.query.filter_by(meeting_id=m.id).first()
    assert prot.reconvened is False and prot.reconvene_wait_minutes is None


# ── Protokoll: Vorbelegung + Lock ────────────────────────────────────────────

def test_protocol_prefills_and_locks(client, admin, tmp_path, monkeypatch):
    monkeypatch.setitem(client.application.config, "PDF_DIR", str(tmp_path / "pdfs"))
    _login(client)
    c = _mk_customer("Teilnehmer", email="t@test.test", wants_email=True)
    m = _mk_meeting(meeting_date=date(2026, 7, 1))
    db.session.add(MeetingAgendaItem(meeting_id=m.id, position=0, title="Beschluss X", requires_vote=True))
    db.session.add(MeetingInvitation(meeting_id=m.id, customer_id=c.id, delivery_method="email"))
    db.session.commit()

    r = client.get(f"/schriftfuehrung/meetings/{m.id}/protocol")
    assert r.status_code == 200
    m = Meeting.query.get(m.id)
    assert m.status == "held"
    assert m.protocol is not None and m.protocol.status == "draft"
    assert MeetingResolution.query.filter_by(meeting_id=m.id).count() == 1  # aus Vote-TOP
    assert MeetingAttendance.query.filter_by(meeting_id=m.id).count() == 1  # aus Einladung

    # Finalisieren -> gesperrt
    r = client.post(f"/schriftfuehrung/meetings/{m.id}/protocol/finalize", data={
        "content_html": "<b>Protokolltext</b>",
    }, follow_redirects=False)
    assert r.status_code == 302
    prot = MeetingProtocol.query.filter_by(meeting_id=m.id).first()
    assert prot.status == "final" and prot.is_locked

    # Speichern nach Abschluss wird abgelehnt (Inhalt bleibt)
    client.post(f"/schriftfuehrung/meetings/{m.id}/protocol/save", data={"content_html": "<b>geändert</b>"})
    prot = MeetingProtocol.query.filter_by(meeting_id=m.id).first()
    assert prot.status == "final"


# ── Beschluss-Register (Suche) ───────────────────────────────────────────────

def test_resolution_register_search(client, admin):
    _login(client)
    m = _mk_meeting()
    db.session.add(MeetingResolution(meeting_id=m.id, title="Bau Hochbehälter",
                                     status="accepted", decided_on=date(2026, 7, 1)))
    db.session.add(MeetingResolution(meeting_id=m.id, title="Gebührenerhöhung",
                                     status="rejected", decided_on=date(2026, 7, 1)))
    db.session.commit()

    r = client.get("/schriftfuehrung/resolutions")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Bau Hochbehälter" in body and "Gebührenerhöhung" in body  # abgelehnte sichtbar

    r = client.get("/schriftfuehrung/resolutions?q=Hochbehälter")
    body = r.get_data(as_text=True)
    assert "Bau Hochbehälter" in body and "Gebührenerhöhung" not in body

    r = client.get("/schriftfuehrung/resolutions?status=rejected")
    body = r.get_data(as_text=True)
    assert "Gebührenerhöhung" in body and "Bau Hochbehälter" not in body


# ── Schriftverkehr-Archiv: Upload-Validierung ────────────────────────────────

def test_archive_upload_rejects_bad_extension(client, admin):
    _login(client)
    data = {"title": "Test", "doc_type": "incoming",
            "document": (io.BytesIO(b"x"), "schad.exe")}
    client.post("/schriftfuehrung/archive/upload", data=data,
                content_type="multipart/form-data", follow_redirects=False)
    assert SchriftverkehrDocument.query.count() == 0


def test_archive_upload_accepts_txt(client, admin, tmp_path, monkeypatch):
    monkeypatch.setitem(client.application.config, "PDF_DIR", str(tmp_path / "pdfs"))
    _login(client)
    data = {"title": "Aktennotiz", "doc_type": "incoming", "document_date": "2026-07-01",
            "document": (io.BytesIO(b"hallo welt"), "notiz.txt")}
    r = client.post("/schriftfuehrung/archive/upload", data=data,
                    content_type="multipart/form-data", follow_redirects=False)
    assert r.status_code == 302
    doc = SchriftverkehrDocument.query.filter_by(title="Aktennotiz").first()
    assert doc is not None and doc.year == 2026 and doc.doc_type == "incoming"


def test_archive_upload_rejects_oversize(client, admin, monkeypatch):
    _login(client)
    monkeypatch.setattr(constants, "MAX_UPLOAD_BYTES", 5)
    data = {"title": "Groß", "doc_type": "other",
            "document": (io.BytesIO(b"viel zu lang fuer 5 byte"), "gross.txt")}
    client.post("/schriftfuehrung/archive/upload", data=data,
                content_type="multipart/form-data", follow_redirects=False)
    assert SchriftverkehrDocument.query.count() == 0
