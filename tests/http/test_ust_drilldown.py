"""USt-Voranmeldung: Drill-Down-Links auf die gefilterte Buchungsliste."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import Account, Booking, FiscalYear, Role, User


def test_ust_drilldown_links(client, app):
    client.get("/auth/logout")
    with app.app_context():
        role = Role(name="Admin")
        db.session.add(role)
        db.session.flush()
        u = User(username="tester", email="tester@test.local", role_id=role.id, active=True)
        u.set_password("test1234")
        fy = FiscalYear(year=2026, start_date=date(2026, 1, 1),
                        end_date=date(2026, 12, 31), is_vat_liable=True)
        acc = Account(name="Wassergeld", active=True)
        db.session.add_all([u, fy, acc])
        db.session.flush()
        db.session.add(Booking(date=date(2026, 2, 3), amount=Decimal("120.00"),
                               tax_rate=Decimal("20"), account_id=acc.id,
                               description="Test", status="Offen"))
        db.session.commit()

    r = client.post("/auth/login", data={"username": "tester", "password": "test1234"})
    assert r.status_code in (302, 200), r.status_code

    r = client.get("/accounting/ust?year=2026&quartal=1")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert "/accounting/bookings?year=2026&amp;quarter=1&amp;kind=income&amp;tax=any" in html
    assert "/accounting/bookings?year=2026&amp;quarter=1&amp;kind=expense&amp;tax=any" in html
    assert "/accounting/bookings?year=2026&amp;quarter=1&amp;tax=any" in html
    assert "kind=income&amp;tax=20" in html
    assert "ust-drill-row" in html

    # Gesamtjahr: quarter-Parameter faellt weg
    r = client.get("/accounting/ust?year=2026")
    html = r.get_data(as_text=True)
    assert "/accounting/bookings?year=2026&amp;kind=income&amp;tax=any" in html

    # Zielseite muss den Filter auch annehmen
    r = client.get("/accounting/bookings?year=2026&quarter=1&kind=income&tax=20")
    assert r.status_code == 200, r.status_code
    assert "Test" in r.get_data(as_text=True)
