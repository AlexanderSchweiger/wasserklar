"""Anteilsbuch der Liegenschaft (Anteile an der Liegenschaft): Buchen, Cache,
Flaeche, Uebertragung, Mitgliederliste, Import, Datenuebertragung, Migration."""
import io
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import property_shares as ps
from app.extensions import db
from app.members import member_list
from app.models import (AppSetting, Customer, CustomerWgProfile, Property,
                        PropertyOwnership, PropertyShareEntry, PropertyWgProfile)
from app.wg_shares import ShareError

TODAY = date.today()
D1, D2, D3 = date(2024, 1, 15), date(2025, 3, 1), date(2025, 9, 1)


@pytest.fixture(autouse=True)
def cooperative(app):
    AppSetting.set("org.type", "cooperative")
    db.session.commit()


def _prop(nr="H-1", active=True):
    p = Property(object_number=nr, object_type="Haus", active=active)
    db.session.add(p)
    db.session.commit()
    return p


def _customer(name, status="member"):
    c = Customer(name=name, is_customer=True, active=True)
    db.session.add(c)
    db.session.flush()
    c.wg_profile = CustomerWgProfile(status=status)
    db.session.commit()
    return c


def _own(prop, customer, valid_from=date(2020, 1, 1), valid_to=None):
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=customer.id,
                                     valid_from=valid_from, valid_to=valid_to))
    db.session.commit()


class TestBooking:
    def test_opening_and_extension_with_area_update_cache(self):
        p = _prop()
        payer = _customer("Anna")
        ps.record_opening(p, 2, D1, area=180)
        ps.record_change(p, 1, D2, ps.REASON_EXTENSION, area=260, payer=payer,
                         note="Zubau Wintergarten")
        db.session.commit()
        assert (p.wg_shares, p.wg_area_m2) == (3, 260)
        assert ps.holding(p.id, D1) == 2 and ps.holding(p.id, D2) == 3
        change = PropertyShareEntry.query.filter_by(kind="change").one()
        assert change.payer_customer_id == payer.id and change.reason == "extension"
        assert ps.cache_mismatches() == []

    def test_zero_change_needs_area(self):
        p = _prop()
        ps.record_opening(p, 2, D1, area=150)
        db.session.commit()
        with pytest.raises(ShareError):
            ps.record_change(p, 0, D2, ps.REASON_EXTENSION)
        db.session.rollback()
        ps.record_change(p, 0, D2, ps.REASON_EXTENSION, area=190)   # unter der Schwelle
        db.session.commit()
        assert (p.wg_shares, p.wg_area_m2) == (2, 190)

    def test_future_date_rejected(self):
        p = _prop()
        with pytest.raises(ShareError):
            ps.record_opening(p, 1, TODAY + timedelta(days=1))

    def test_sign_must_match_reason(self):
        p = _prop()
        ps.record_opening(p, 3, D1)
        db.session.commit()
        with pytest.raises(ShareError):
            ps.record_change(p, 1, D2, ps.REASON_DECOMMISSION)
        db.session.rollback()
        with pytest.raises(ShareError):
            ps.record_change(p, -1, D2, ps.REASON_EXTENSION)
        db.session.rollback()
        ps.record_change(p, -3, D2, ps.REASON_DEMOLITION)
        db.session.commit()
        assert p.wg_shares == 0

    def test_never_negative_also_backdated(self):
        p = _prop()
        ps.record_opening(p, 1, D2)
        db.session.commit()
        with pytest.raises(ShareError):
            ps.record_change(p, -1, D1, ps.REASON_DECOMMISSION)   # vor dem Anfangsbestand
        db.session.rollback()
        assert p.wg_shares == 1 and PropertyShareEntry.query.count() == 1

    def test_opening_only_on_empty_book(self):
        p = _prop()
        ps.record_opening(p, 1, D1)
        with pytest.raises(ShareError):
            ps.record_opening(p, 1, D2)

    def test_correction_needs_note(self):
        p = _prop()
        ps.record_opening(p, 1, D1)
        with pytest.raises(ShareError):
            ps.record_correction(p, 1, D2, "")
        ps.record_correction(p, 1, D2, "Tippfehler im Altbestand")
        db.session.commit()
        assert p.wg_shares == 2

    def test_backdated_area_does_not_change_current_area(self):
        p = _prop()
        ps.record_opening(p, 2, D1, area=200)
        ps.record_change(p, 1, D3, ps.REASON_EXTENSION, area=280)
        ps.record_correction(p, 0, D2, "Fläche im Plan korrigiert", area=210)
        db.session.commit()
        assert p.wg_area_m2 == 280

    def test_delete_restores_previous_area_and_cache(self):
        p = _prop()
        ps.record_opening(p, 2, D1, area=200)
        entry = ps.record_change(p, 1, D2, ps.REASON_EXTENSION, area=280)
        db.session.commit()
        ps.delete_entry(entry)
        db.session.commit()
        assert (p.wg_shares, p.wg_area_m2) == (2, 200)

    def test_transfer_split_as_pair(self):
        a, b = _prop("A"), _prop("B")
        ps.record_opening(a, 4, D1)
        out, inn = ps.record_transfer(a, b, 1, D2, ps.REASON_SPLIT)
        db.session.commit()
        assert (a.wg_shares, b.wg_shares) == (3, 1)
        assert inn.pair_entry_id == out.id and out.counterparty_property_id == b.id
        ps.delete_entry(inn)
        db.session.commit()
        assert (a.wg_shares, b.wg_shares) == (4, 0)
        assert PropertyShareEntry.query.count() == 1

    def test_transfer_targets_validated(self):
        a, archived = _prop("A"), _prop("X", active=False)
        ps.record_opening(a, 2, D1)
        db.session.commit()
        for target in (a, archived, None):
            with pytest.raises(ShareError):
                ps.record_transfer(a, target, 1, D2)
            db.session.rollback()

    def test_has_paid_shares_reads_cache(self):
        c = _customer("Anna")
        p = _prop()
        _own(p, c)
        assert c.has_paid_shares() is False
        ps.record_opening(p, 1, D1)
        db.session.commit()
        assert c.has_paid_shares() is True


class TestMemberListStichtag:
    def test_past_stichtag_uses_book(self):
        c = _customer("Anna")
        p = _prop()
        _own(p, c)
        ps.record_opening(p, 2, D1)
        ps.record_change(p, 2, D3, ps.REASON_EXTENSION)
        db.session.commit()
        rows, totals = member_list.build(D2, shares_on_person=False)
        assert rows[0]["shares"] == 2 and totals["shares"] == 2
        rows, totals = member_list.build(D3, shares_on_person=False)
        assert rows[0]["shares"] == 4


class TestCustomerDeleteGuard:
    def test_payer_blocks_delete(self, client):
        from app.models import User
        from tests.conftest import _ensure_role
        u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
        u.set_password("secret")
        db.session.add(u)
        db.session.commit()
        payer = _customer("Zahler")
        p = _prop()
        ps.record_opening(p, 1, D1, payer=payer)
        db.session.commit()
        client.get("/auth/logout")
        client.post("/auth/login", data={"username": "admin", "password": "secret"})
        client.post(f"/customers/{payer.id}/delete")
        assert db.session.get(Customer, payer.id) is not None


class TestImport:
    def test_combined_import_books_opening_and_correction(self):
        import pandas as pd
        from app.import_csv.routes import _run_import
        df = pd.DataFrame([{"Kd": "11", "Name": "Huber Anna", "Objekt": "O-1",
                            "Anteile": "2", "Fläche": "190"}])
        col_map = {"customer_number": "Kd", "customer_name": "Name",
                   "property_name": "Objekt", "property_shares": "Anteile",
                   "property_area": "Fläche"}
        _run_import(df, col_map, "overwrite", dry_run=False, is_wg=True)
        prop = Property.query.filter_by(object_number="O-1").one()
        assert (prop.wg_shares, prop.wg_area_m2) == (2, 190)
        assert PropertyShareEntry.query.one().kind == "opening"
        df2 = pd.DataFrame([{"Kd": "11", "Name": "Huber Anna", "Objekt": "O-1",
                             "Anteile": "3", "Fläche": "260"}])
        _run_import(df2, col_map, "overwrite", dry_run=False, is_wg=True)
        db.session.expire_all()
        prop = Property.query.filter_by(object_number="O-1").one()
        assert (prop.wg_shares, prop.wg_area_m2) == (3, 260)
        corr = PropertyShareEntry.query.filter_by(kind="correction").one()
        assert (corr.quantity, corr.area_m2, corr.source) == (1, 260, "import")

    def test_dry_run_writes_nothing(self):
        import pandas as pd
        from app.import_csv.routes import _run_import
        df = pd.DataFrame([{"Kd": "12", "Name": "Berger Bea", "Objekt": "O-2",
                            "Anteile": "1"}])
        col_map = {"customer_number": "Kd", "customer_name": "Name",
                   "property_name": "Objekt", "property_shares": "Anteile"}
        _run_import(df, col_map, "skip", dry_run=True, is_wg=True)
        assert PropertyShareEntry.query.count() == 0


def _export(selection=None):
    from app.data_transfer.services import export_to_zip
    buf = io.BytesIO()
    export_to_zip({"stammdaten": True, "buchungen": False, "mahnwesen": False,
                   "einstellungen": False, "years": [], "include_pdfs": False,
                   **(selection or {})}, buf, exported_by="test")
    return buf.getvalue()


def _import(zip_bytes, tmp_path, mode="replace", tamper=None):
    import json
    import zipfile
    from app.data_transfer.services import extract_to_temp, import_from_zip
    if tamper:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            files = {n: zf.read(n) for n in zf.namelist()}
        tamper(files)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for n, data in files.items():
                zf.writestr(n, data)
        zip_bytes = out.getvalue()
    instance = tmp_path / "instance"
    instance.mkdir(exist_ok=True)
    extract_dir, manifest = extract_to_temp(io.BytesIO(zip_bytes), str(instance))
    import_from_zip(extract_dir, manifest, mode=mode, instance_path=str(instance))
    db.session.remove()


class TestDataTransfer:
    def test_roundtrip_keeps_book_and_cache(self, tmp_path):
        a, b = _prop("A"), _prop("B")
        payer = _customer("Zahler")
        ps.record_opening(a, 3, D1, area=300, payer=payer)
        ps.record_transfer(a, b, 1, D2, ps.REASON_SPLIT)
        db.session.commit()
        _import(_export(), tmp_path)
        a = Property.query.filter_by(object_number="A").one()
        b = Property.query.filter_by(object_number="B").one()
        assert (a.wg_shares, b.wg_shares, a.wg_area_m2) == (2, 1, 300)
        inn = PropertyShareEntry.query.filter_by(kind="transfer_in").one()
        out = PropertyShareEntry.query.filter_by(kind="transfer_out").one()
        assert inn.pair_entry_id == out.id
        assert ps.cache_mismatches() == []

    def test_old_export_without_book_gets_opening(self, tmp_path):
        p = _prop("ALT")
        c = _customer("Anna")
        _own(p, c, valid_from=date(2012, 5, 1))
        ps.record_opening(p, 2, D1, area=210)
        db.session.commit()

        def drop_book(files):
            import json
            files.pop("tables/property_share_entries.json", None)
            manifest = json.loads(files["manifest.json"])
            for key in ("tables", "checksums", "files"):
                if isinstance(manifest.get(key), dict):
                    manifest[key].pop("property_share_entries", None)
                    manifest[key].pop("tables/property_share_entries.json", None)
            files["manifest.json"] = json.dumps(manifest).encode()

        _import(_export(), tmp_path, tamper=drop_book)
        p = Property.query.filter_by(object_number="ALT").one()
        entry = PropertyShareEntry.query.filter_by(property_id=p.id).one()
        assert (entry.kind, entry.quantity, entry.area_m2) == ("opening", 2, 210)
        assert entry.entry_date == date(2012, 5, 1)
        assert p.wg_shares == 2

    def test_merge_does_not_double_existing_books(self, tmp_path):
        p = _prop("M-1")
        ps.record_opening(p, 2, D1)
        db.session.commit()
        _import(_export(), tmp_path, mode="merge")
        p = Property.query.filter_by(object_number="M-1").one()
        assert PropertyShareEntry.query.filter_by(property_id=p.id).count() == 1
        assert p.wg_shares == 2


class TestCacheGuard:
    def test_no_direct_cache_writes_outside_service(self):
        """Wer PropertyWgProfile.shares direkt setzt, umgeht das Anteilsbuch."""
        root = Path(__file__).resolve().parents[2] / "app"
        allowed = {root / "property_shares.py"}
        pattern = re.compile(r"(profile|wg_profile)\.shares\s*=(?!=)|PropertyWgProfile\([^)]*shares=")
        offenders = [str(f.relative_to(root)) for f in root.rglob("*.py")
                     if f not in allowed and pattern.search(f.read_text(encoding="utf-8"))]
        assert offenders == []
