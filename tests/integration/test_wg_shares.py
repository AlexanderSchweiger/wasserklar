"""Anteilsbuch (Anteile am Mitglied): Service, Umstellung, Mitgliederliste,
Importe und data_transfer."""
from datetime import date, timedelta

import pytest

from app import wg_shares
from app.extensions import db
from app.members import member_list
from app.models import (AppSetting, Customer, CustomerWgProfile, Property,
                        PropertyOwnership, PropertyWgProfile, WgShareEntry)

TODAY = date.today()
D1 = date(2026, 1, 15)
D2 = date(2026, 3, 1)
D3 = date(2026, 6, 1)


def _person_mode():
    AppSetting.set("org.type", "cooperative")
    AppSetting.set("org.share_binding", "person")
    db.session.commit()


def _customer(name, *, status="member", is_customer=True, active=True, member_since=None,
              member_until=None):
    c = Customer(name=name, is_customer=is_customer, active=active, member_since=member_since)
    db.session.add(c)
    db.session.flush()
    if is_customer:
        c.wg_profile = CustomerWgProfile(status=status, member_until=member_until)
    db.session.commit()
    return c


def _property(nr, shares, owners, valid_from=date(2020, 1, 1)):
    """Liegenschaft mit Anteilen — gebucht ueber das Anteilsbuch der
    Liegenschaft (der Cache PropertyWgProfile.shares folgt daraus)."""
    from app import property_shares
    p = Property(object_number=nr, object_type="Haus")
    db.session.add(p)
    db.session.flush()
    property_shares.record_opening(p, shares, valid_from, area=120)
    for c in owners:
        db.session.add(PropertyOwnership(property_id=p.id, customer_id=c.id,
                                         valid_from=valid_from))
    db.session.commit()
    return p


@pytest.fixture
def person(app):
    _person_mode()


class TestBinding:
    def test_default_is_property(self, app):
        from app.settings_service import share_binding, shares_on_person
        assert share_binding() == "property"
        assert shares_on_person() is False

    def test_utility_mode_never_person(self, app):
        from app.settings_service import shares_on_person
        AppSetting.set("org.type", "utility")
        AppSetting.set("org.share_binding", "person")
        db.session.commit()
        assert shares_on_person() is False

    def test_unknown_value_falls_back(self, app):
        from app.settings_service import share_binding
        AppSetting.set("org.share_binding", "kaputt")
        db.session.commit()
        assert share_binding() == "property"


class TestRecord:
    def test_acquisition_and_holding_at_dates(self, person):
        c = _customer("Anna")
        wg_shares.record_acquisition(c, 2, D2)
        wg_shares.record_return(c, 1, D3)
        db.session.commit()
        assert wg_shares.holding(c.id, D1) == 0
        assert wg_shares.holding(c.id, D2) == 2       # inkl. Buchungen des Tages
        assert wg_shares.holding(c.id, D3) == 1
        assert wg_shares.holdings_map([c.id], D2) == {c.id: 2}

    def test_return_more_than_held_rejected(self, person):
        c = _customer("Anna")
        wg_shares.record_acquisition(c, 1, D1)
        db.session.commit()
        with pytest.raises(wg_shares.ShareError):
            wg_shares.record_return(c, 2, D2)
        db.session.rollback()
        assert WgShareEntry.query.count() == 1

    def test_backdated_return_before_acquisition_rejected(self, person):
        c = _customer("Anna")
        wg_shares.record_acquisition(c, 1, D2)
        db.session.commit()
        with pytest.raises(wg_shares.ShareError):
            wg_shares.record_return(c, 1, D1)
        db.session.rollback()

    def test_backdated_return_breaking_later_transfer_rejected(self, person):
        a, b = _customer("Anna"), _customer("Bert")
        wg_shares.record_acquisition(a, 1, D1)
        wg_shares.record_transfer(a, b, 1, D3)
        db.session.commit()
        with pytest.raises(wg_shares.ShareError):
            wg_shares.record_return(a, 1, D2)
        db.session.rollback()

    def test_transfer_writes_pair(self, person):
        a, b = _customer("Anna"), _customer("Bert")
        wg_shares.record_acquisition(a, 3, D1)
        out, inn = wg_shares.record_transfer(a, b, 2, D2, note="Verkauf")
        db.session.commit()
        assert (out.quantity, inn.quantity) == (-2, 2)
        assert inn.pair_entry_id == out.id
        assert out.counterparty_customer_id == b.id and inn.counterparty_customer_id == a.id
        assert wg_shares.holdings_map([a.id, b.id], D2) == {a.id: 1, b.id: 2}
        assert wg_shares.partner_of(out).id == inn.id
        assert wg_shares.partner_of(inn).id == out.id

    def test_transfer_targets_validated(self, person):
        a = _customer("Anna")
        supplier = _customer("Lieferant", is_customer=False)
        archived = _customer("Archiv", active=False)
        wg_shares.record_acquisition(a, 2, D1)
        db.session.commit()
        for target in (a, supplier, archived, None):
            with pytest.raises(wg_shares.ShareError):
                wg_shares.record_transfer(a, target, 1, D2)
            db.session.rollback()

    def test_supplier_cannot_hold(self, person):
        supplier = _customer("Lieferant", is_customer=False)
        with pytest.raises(wg_shares.ShareError):
            wg_shares.record_acquisition(supplier, 1, D1)

    def test_correction_needs_note_and_nonzero(self, person):
        c = _customer("Anna")
        with pytest.raises(wg_shares.ShareError):
            wg_shares.record_correction(c, 1, D1, "")
        with pytest.raises(wg_shares.ShareError):
            wg_shares.record_correction(c, 0, D1, "Grund")
        wg_shares.record_correction(c, 2, D1, "Altbestand")
        db.session.commit()
        assert wg_shares.holding(c.id, D1) == 2

    def test_delete_transfer_removes_both_halves(self, person):
        a, b = _customer("Anna"), _customer("Bert")
        wg_shares.record_acquisition(a, 2, D1)
        out, _ = wg_shares.record_transfer(a, b, 2, D2)
        db.session.commit()
        affected = wg_shares.delete_entry(out)
        db.session.commit()
        assert affected == {a.id, b.id}
        assert WgShareEntry.query.count() == 1
        assert wg_shares.holding(a.id, D3) == 2

    def test_delete_acquisition_breaking_transfer_rejected(self, person):
        a, b = _customer("Anna"), _customer("Bert")
        acq = wg_shares.record_acquisition(a, 1, D1)
        wg_shares.record_transfer(a, b, 1, D2)
        db.session.commit()
        with pytest.raises(wg_shares.ShareError):
            wg_shares.delete_entry(acq)
        db.session.rollback()
        assert WgShareEntry.query.count() == 3

    def test_planned_changes_and_history(self, person):
        c = _customer("Anna")
        wg_shares.record_acquisition(c, 2, D1)
        future = TODAY + timedelta(days=30)
        wg_shares.record_return(c, 2, future)
        db.session.commit()
        assert wg_shares.planned_changes(c.id) == [(future, 0)]
        assert [bal for _, bal in wg_shares.history(c.id)] == [2, 0]


class TestMembership:
    def test_has_paid_shares_dispatch(self, app):
        c = _customer("Anna")
        p = _property("1", 2, [c])
        assert c.has_paid_shares() is True          # Liegenschafts-Modus
        _person_mode()
        assert c.has_paid_shares() is False         # Anteilsbuch leer
        wg_shares.record_acquisition(c, 1, D1)
        db.session.commit()
        assert c.has_paid_shares() is True
        assert p.wg_shares == 2                     # Liegenschaft unberuehrt

    def test_apply_membership(self, person):
        c = _customer("Anna", status="prospect")
        assert wg_shares.apply_membership(c, D2, 1) is True
        assert c.wg_status == "member" and c.member_since == D2
        assert wg_shares.apply_membership(c, D3, 0) is True
        assert c.wg_status == "resigned"
        assert c.wg_member_until == D3 - timedelta(days=1)


class TestModeSwitch:
    def test_preview_is_read_only_and_defaults(self, app):
        a, b, solo = _customer("Anna"), _customer("Bert"), _customer("Solo")
        _property("1", 2, [a, b])                           # Miteigentum
        _property("2", 1, [solo])
        _property("3", 1, [solo])
        lonely = _property("4", 5, [])                      # ohne Eigentuemer
        prev = wg_shares.switch_preview(TODAY)
        assert WgShareEntry.query.count() == 0
        rows = {r["property"].object_number: r for r in prev["rows"]}
        assert rows["1"]["co_owned"] and rows["1"]["default"] == {a.id: 2, b.id: 0}
        assert [u["property"].id for u in prev["unowned"]] == [lonely.id]
        assert prev["total_shares"] == 4

    def test_apply_books_one_opening_per_contact(self, app):
        AppSetting.set("org.type", "cooperative")
        db.session.commit()
        solo = _customer("Solo", member_since=D1)
        p2 = _property("2", 1, [solo])
        p3 = _property("3", 2, [solo])
        contacts, shares = wg_shares.apply_switch_to_person(
            {(p2.id, solo.id): 1, (p3.id, solo.id): 2}, D3, use_member_since=True)
        db.session.commit()
        assert (contacts, shares) == (1, 3)
        entry = WgShareEntry.query.one()
        assert entry.kind == "opening" and entry.quantity == 3
        assert entry.entry_date == D1 and entry.source == "mode_switch"
        assert "2" in entry.note and "3" in entry.note
        assert AppSetting.get("org.share_binding") == "person"
        assert db.session.get(PropertyWgProfile, p2.id).shares == 1  # unberuehrt

    def test_second_switch_skips_existing_registers(self, app):
        AppSetting.set("org.type", "cooperative")
        db.session.commit()
        solo = _customer("Solo")
        p = _property("2", 1, [solo])
        wg_shares.apply_switch_to_person({(p.id, solo.id): 1}, D3)
        db.session.commit()
        wg_shares.apply_switch_to_property()
        db.session.commit()
        assert AppSetting.get("org.share_binding") == "property"
        assert WgShareEntry.query.count() == 1                       # bleibt
        prev = wg_shares.switch_preview(D3)
        assert [c.id for c in prev["skipped"]] == [solo.id]
        wg_shares.apply_switch_to_person({(p.id, solo.id): 1}, D3)
        db.session.commit()
        assert WgShareEntry.query.count() == 1                       # nicht doppelt


class TestMemberList:
    def test_person_mode_holdings_and_flags(self, person):
        a = _customer("Anna", member_since=D1)
        b = _customer("Bert")                                       # Mitglied ohne Anteile
        c = _customer("Cora", status="resigned", member_until=D1)   # ehemalig
        _customer("Ida", status="prospect")
        wg_shares.record_acquisition(a, 2, D1)
        wg_shares.record_acquisition(c, 1, D1)
        db.session.commit()
        rows, totals = member_list.build(D3, shares_on_person=True)
        by_name = {r["customer"].name: r for r in rows}
        assert set(by_name) == {"Anna", "Bert"}
        assert by_name["Anna"]["shares"] == 2
        assert "no_shares" in by_name["Bert"]["flags"]
        assert totals == {"members": 2, "former": 0, "shares": 2,
                          "co_owned_properties": 0, "without_shares": 1}
        rows_all, totals_all = member_list.build(D3, scope="all", shares_on_person=True)
        cora = next(r for r in rows_all if r["customer"].name == "Cora")
        assert cora["state"] == "former" and "payout_open" in cora["flags"]
        assert totals_all["former"] == 1

    def test_future_resignation_counts_until_then(self, person):
        _customer("Anna", status="resigned", member_until=D3)
        rows, _ = member_list.build(D2, shares_on_person=True)
        assert [r["customer"].name for r in rows] == ["Anna"]
        rows, _ = member_list.build(D3 + timedelta(days=1), shares_on_person=True)
        assert rows == []

    def test_property_mode_co_owners_counted_once(self, app):
        a, b, solo = _customer("Anna"), _customer("Bert"), _customer("Solo")
        _property("1", 2, [a, b])
        _property("2", 3, [solo])
        rows, totals = member_list.build(TODAY, shares_on_person=False)
        by_name = {r["customer"].name: r for r in rows}
        assert by_name["Anna"]["shares"] == 2 and by_name["Bert"]["shares"] == 2
        assert by_name["Anna"]["co_owners"] == ["Bert"]
        assert totals["shares"] == 5
        assert totals["co_owned_properties"] == 1

    def test_archived_only_in_scope_all(self, person):
        _customer("Archiv", active=False)
        assert member_list.build(TODAY, shares_on_person=True)[0] == []
        rows, _ = member_list.build(TODAY, scope="all", shares_on_person=True)
        assert "archived" in rows[0]["flags"]

    def test_csv_rows_have_sum_line(self, person):
        a = _customer("Anna")
        wg_shares.record_acquisition(a, 2, D1)
        db.session.commit()
        rows, totals = member_list.build(D3, shares_on_person=True)
        lines = member_list.csv_rows(rows, totals, True)
        assert lines[0][0] == "Mitgliedsnummer"
        assert lines[-1][0] == "Summe" and lines[-1][-1] == 2


class TestImports:
    def test_customer_import_books_opening_and_respects_register(self, person):
        import pandas as pd
        from app.customers import import_service as svc
        existing = _customer("Bestand")
        existing.customer_number = 7
        wg_shares.record_acquisition(existing, 1, D1)
        db.session.commit()
        df = pd.DataFrame([
            {"Nr": "", "Name": "Neu Mitglied", "Mitglied seit": "01.03.2026", "Anteile": "2"},
            {"Nr": "7", "Name": "Bestand", "Mitglied seit": "", "Anteile": "5"},
        ])
        cfg = svc.suggest_config(list(df.columns))
        cfg.col_customer_number = "Nr"
        cfg.duplicate_mode = "update"
        assert cfg.col_shares == "Anteile"
        rows = svc.build_preview_rows(df, cfg, is_wg=True, shares_on_person=True)
        assert any("Anteilsbuch vorhanden" in w for w in rows[1].warnings)
        svc.commit(rows, cfg, is_wg=True, shares_on_person=True)
        new = Customer.query.filter_by(name="Neu Mitglied").one()
        entry = WgShareEntry.query.filter_by(customer_id=new.id).one()
        assert (entry.kind, entry.quantity, entry.entry_date) == ("opening", 2, D2)
        assert wg_shares.holding(existing.id) == 1                   # nicht ueberschrieben

    def test_customer_import_ignores_shares_in_property_mode(self, app):
        import pandas as pd
        from app.customers import import_service as svc
        df = pd.DataFrame([{"Name": "Neu", "Anteile": "2"}])
        cfg = svc.suggest_config(list(df.columns))
        rows = svc.build_preview_rows(df, cfg, is_wg=True, shares_on_person=False)
        svc.commit(rows, cfg, is_wg=True, shares_on_person=False)
        assert WgShareEntry.query.count() == 0

    def test_combined_import_routes_shares_to_member(self, person):
        import pandas as pd
        from app.import_csv.routes import _run_import
        df = pd.DataFrame([
            {"Kd": "11", "Name": "Huber Anna", "Objekt": "O-1", "Anteile": "2"},
            {"Kd": "11", "Name": "Huber Anna", "Objekt": "O-2", "Anteile": "3"},
        ])
        col_map = {"customer_number": "Kd", "customer_name": "Name",
                   "property_name": "Objekt", "member_shares": "Anteile",
                   "property_shares": "Anteile"}
        dry = _run_import(df, col_map, "skip", dry_run=True, is_wg=True,
                          shares_on_person=True)
        assert WgShareEntry.query.count() == 0                       # Probelauf
        assert any("abweichende Anteile" in w for w in dry["warnings"])
        _run_import(df, col_map, "skip", dry_run=False, is_wg=True, shares_on_person=True)
        anna = Customer.query.filter_by(customer_number=11).one()
        assert wg_shares.holding(anna.id) == 2                       # erster Wert gilt
        assert all(p.wg_shares == 0 for p in Property.query.all())  # nicht an die Liegenschaft


class TestDataTransfer:
    def test_roundtrip_restores_transfer_pair(self, person, tmp_path, monkeypatch):
        import io
        from app.data_transfer.services import (export_to_zip, extract_to_temp,
                                                import_from_zip, validate_manifest)
        from app.models import User
        from tests.conftest import _ensure_role
        user = User(username="kassier", email="k@test.test", role_id=_ensure_role("Admin").id)
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()
        a, b = _customer("Anna"), _customer("Bert")
        wg_shares.record_acquisition(a, 2, D1, user_id=user.id)
        wg_shares.record_transfer(a, b, 1, D2, user_id=user.id)
        db.session.commit()
        buf = io.BytesIO()
        export_to_zip({"stammdaten": True, "buchungen": False, "mahnwesen": False,
                       "einstellungen": False, "years": [], "include_pdfs": False},
                      buf, exported_by="test")
        instance = tmp_path / "instance"
        instance.mkdir()
        extract_dir, manifest = extract_to_temp(io.BytesIO(buf.getvalue()), str(instance))
        assert validate_manifest(manifest, extract_dir)["errors"] == []
        import_from_zip(extract_dir, manifest, mode="replace", instance_path=str(instance))
        db.session.remove()
        entries = WgShareEntry.query.order_by(WgShareEntry.id).all()
        assert len(entries) == 3
        inn = next(e for e in entries if e.kind == "transfer_in")
        out = next(e for e in entries if e.kind == "transfer_out")
        assert inn.pair_entry_id == out.id
        assert all(e.created_by_id is None for e in entries)
