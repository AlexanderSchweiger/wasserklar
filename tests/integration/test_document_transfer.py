"""Belegablage im Daten-Export/-Import (Voll-Backup eines Mandanten als ZIP).

Die Dateien reisen als ``files/<storage_key>`` im Bundle; der Schluessel steht im JSON, wird aber beim
Import nie blind uebernommen — er wird erst gesetzt, wenn die Datei zur Pruefsumme passt.
"""
import hashlib
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import g

from app.data_transfer.services import (
    export_to_zip, extract_to_temp, import_from_zip, validate_manifest,
)
from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Document, DocumentEvent, DocumentLink, FiscalYear,
    IncomingInvoice, User,
)
from tests.conftest import _ensure_role
from tests.integration.test_document_service import pdf
from tests.unit.test_einvoice_incoming import _fixture

CII = "kosit-01.01a-INVOICE_uncefact.xml"
SELECTION = {"stammdaten": True, "buchungen": True, "mahnwesen": False, "einstellungen": False, "years": []}


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    pdf_dir = tmp_path / "tenants" / "alm" / "pdfs"
    pdf_dir.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(pdf_dir))
    instance = tmp_path / "instance"
    instance.mkdir()
    return SimpleNamespace(pdf_dir=pdf_dir, root=pdf_dir.parent, instance=instance)


@pytest.fixture
def books(app):
    db.session.add_all([FiscalYear(year=y, start_date=date(y, 1, 1), end_date=date(y, 12, 31)) for y in (2015, 2016, 2025)])
    account = Account(name="Aufwand", code="A01")
    user = User(username="kassier", email="k@test.test", role_id=_ensure_role("Admin").id)
    user.set_password("secret")
    db.session.add_all([account, user])
    db.session.commit()
    return account, user


def booking(account, day=date(2025, 3, 1), amount="-10.00"):
    b = Booking(date=day, account_id=account.id, amount=Decimal(amount), description="B", status=Booking.STATUS_VERBUCHT)
    db.session.add(b)
    db.session.commit()
    return b


def export(include_pdfs=True, **selection):
    buf = io.BytesIO()
    export_to_zip({**SELECTION, "include_pdfs": include_pdfs, **selection}, buf, exported_by="test")
    return buf.getvalue()


def fresh_session():
    db.session.remove()
    for attr in ("_login_user", "_flask_login_user"):
        g.pop(attr, None)


def do_import(zip_bytes, tenant, mode="replace", update_existing=False):
    extract_dir, manifest = extract_to_temp(io.BytesIO(zip_bytes), str(tenant.instance))
    errors = validate_manifest(manifest, extract_dir)["errors"]
    assert errors == []
    import_from_zip(extract_dir, manifest, mode=mode, update_existing=update_existing,
                    instance_path=str(tenant.instance))
    fresh_session()


def tamper(zip_bytes, table, edit, drop_files=False, extra_files=None):
    """ZIP wie ein Angreifer umbauen (Tabellen-JSON aendern, Checksumme neu berechnen)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(files.pop("manifest.json"))
    records = json.loads(files[f"tables/{table}.json"])
    edit(records)
    files[f"tables/{table}.json"] = json.dumps(records).encode("utf-8")
    if drop_files:
        files = {n: d for n, d in files.items() if not n.startswith("files/")}
    files.update(extra_files or {})
    checksum = hashlib.sha256()
    for tm in manifest["tables"]:
        checksum.update(files.get(f"tables/{tm['name']}.json", b""))
    manifest["checksum_sha256"] = checksum.hexdigest()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest))
    return out.getvalue()


def store(data, name="beleg.pdf"):
    return svc.store_upload(name, data, None)


class TestExport:
    def test_the_files_travel_in_the_bundle_uncompressed(self, app, tenant, books):
        d = store(pdf(1))
        zf = zipfile.ZipFile(io.BytesIO(export()))
        info = zf.getinfo(f"files/{d.storage_key}")
        assert zf.read(info.filename) == pdf(1) and info.compress_type == zipfile.ZIP_STORED
        records = json.loads(zf.read("tables/documents.json"))
        assert records[0]["storage_key"] == d.storage_key and records[0]["sha256"] == d.sha256

    def test_without_the_file_bundle_the_key_stays_in_the_json(self, app, tenant, books):
        d = store(pdf(1))
        zf = zipfile.ZipFile(io.BytesIO(export(include_pdfs=False)))
        assert not [n for n in zf.namelist() if n.startswith("files/")]
        assert json.loads(zf.read("tables/documents.json"))[0]["storage_key"] == d.storage_key

    def test_a_missing_file_is_simply_left_out(self, app, tenant, books):
        d = store(pdf(1))
        storage.delete(d.storage_key)
        zf = zipfile.ZipFile(io.BytesIO(export()))
        assert not [n for n in zf.namelist() if n.startswith("files/")]
        assert len(json.loads(zf.read("tables/documents.json"))) == 1

    def test_a_tampered_key_in_the_database_never_pulls_foreign_files(self, app, tenant, books):
        foreign = tenant.root.parent / "other" / "secret.txt"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"GEHEIM")
        d = store(pdf(1))
        d.storage_key = "../../other/secret.txt"
        db.session.commit()
        zf = zipfile.ZipFile(io.BytesIO(export()))
        assert b"GEHEIM" not in b"".join(zf.read(n) for n in zf.namelist())

    def test_the_manifest_accepts_a_bundle_with_only_files(self, app, tenant, books):
        store(pdf(1))
        extract_dir, manifest = extract_to_temp(io.BytesIO(export()), str(tenant.instance))
        result = validate_manifest(manifest, extract_dir)
        assert result["errors"] == [] and not [w for w in result["warnings"] if "pdfs/" in w]


class TestRoundTrip:
    def _booked_einvoice(self, books):
        account, user = books
        d = store(_fixture(CII), "rechnung.xml")
        supplier = Customer(name="Lieferant", is_supplier=True, is_customer=False)
        db.session.add(supplier)
        db.session.commit()
        from app.accounting import incoming_service
        incoming_service.book(d, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4), user_id=user.id)
        db.session.commit()
        return d

    def test_replace_restores_rows_links_and_files(self, app, tenant, books):
        account, _user = books
        e = self._booked_einvoice(books)
        plain = store(pdf(2))
        svc.link(plain, booking=booking(account))
        db.session.commit()
        keys = {e.sha256: e.storage_key, plain.sha256: plain.storage_key}
        contents = {sha: storage.read(key) for sha, key in keys.items()}
        events_before = DocumentEvent.query.count()
        bundle = export()
        for key in keys.values():                                   # die Dateien sind weg — das Bundle bringt sie zurueck
            storage.delete(key)
        do_import(bundle, tenant)

        assert Document.query.count() == 2 and IncomingInvoice.query.count() == 1 and DocumentLink.query.count() == 2
        assert DocumentEvent.query.count() == events_before
        for row in Document.query:
            assert row.storage_key == keys[row.sha256]              # im Vollersatz bleibt der Schluessel
            assert storage.read(row.storage_key) == contents[row.sha256]
        again = Document.query.filter_by(sha256=e.sha256).one()
        assert again.einvoice.parsed.number == "123456XX" and svc.is_booked(again) and again.supplier_id
        assert svc.is_booked(Document.query.filter_by(sha256=plain.sha256).one())

    def test_the_read_text_and_the_unconfirmed_flag_survive(self, app, tenant, books):
        data = (Path(__file__).resolve().parent.parent / "fixtures" / "documents" / "zeilen.pdf").read_bytes()
        sha = store(data, "rechnung.pdf").sha256
        do_import(export(), tenant)
        again = Document.query.filter_by(sha256=sha).one()
        assert again.text_status == "ok" and "Pumpenhaus" in again.text_content       # sofort durchsuchbar
        assert again.meta_auto is True and again.number == "RE-2026-0042"

    def test_without_the_bundle_existing_files_are_found_again(self, app, tenant, books):
        d = store(pdf(1))
        key = d.storage_key
        do_import(export(include_pdfs=False), tenant)
        row = Document.query.one()
        assert row.storage_key == key and storage.read(key) == pdf(1)

    def test_without_the_bundle_and_without_the_file_the_key_stays_empty(self, app, tenant, books):
        d = store(pdf(1))
        bundle = export(include_pdfs=False)
        storage.delete(d.storage_key)
        do_import(bundle, tenant)
        row = Document.query.one()
        assert row.storage_key is None and row.original_name == "beleg.pdf"            # Daten bleiben, Datei fehlt

    def test_a_file_that_is_already_in_place_is_never_overwritten(self, app, tenant, books):
        d = store(pdf(1))
        bundle = export()
        do_import(bundle, tenant)
        row = Document.query.one()
        before = Path(storage.path_for(row.storage_key)).stat().st_mtime_ns
        do_import(bundle, tenant)
        assert Path(storage.path_for(Document.query.one().storage_key)).stat().st_mtime_ns == before

    def test_the_quota_usage_survives(self, app, tenant, books):
        store(pdf(1))
        store(pdf(2))
        used = svc.quota_status()["used"]
        do_import(export(), tenant)
        assert svc.quota_status()["used"] == used

    def test_the_documents_of_another_tenant_directory_are_untouched(self, app, tenant, books, tmp_path, monkeypatch):
        """Restore schreibt nur in den Dateibaum des importierenden Mandanten."""
        other = tmp_path / "tenants" / "other"
        (other / "pdfs").mkdir(parents=True)
        (other / "documents" / "2025" / "03").mkdir(parents=True)
        mine = store(pdf(3))
        bundle = export()
        storage.delete(mine.storage_key)
        do_import(bundle, tenant)
        assert not any(other.rglob("*.pdf"))


class TestMerge:
    def test_a_document_is_never_duplicated_and_follows_the_copied_bookings(self, app, tenant, books):
        """Buchungen haben keinen natuerlichen Schluessel (Merge legt sie neu an) — der Beleg bleibt
        einmalig (Pruefsumme), die Kopie der Buchung bekommt ihre eigene Verknuepfung."""
        account, _ = books
        d = store(pdf(1))
        key = d.storage_key
        svc.link(d, booking=booking(account))
        svc.link(d, group=self._group(account))
        db.session.commit()
        do_import(export(), tenant, mode="merge")
        assert Document.query.count() == 1 and Document.query.one().storage_key == key
        assert DocumentLink.query.count() == 4                                           # 2 alte + 2 fuer die Kopien
        assert all(l.booking_id or l.booking_group_id for l in DocumentLink.query)
        assert len(list((tenant.root / "documents").rglob("*.pdf"))) == 1                # Datei nicht verdoppelt

    def _group(self, account):
        gr = BookingGroup(date=date(2025, 3, 1), description="Sammel", total_amount=Decimal("-30"), status="Aktiv")
        db.session.add(gr)
        db.session.flush()
        for amount in ("-10", "-20"):
            db.session.add(Booking(date=gr.date, account_id=account.id, amount=Decimal(amount), description="Z",
                                   group_id=gr.id, status=Booking.STATUS_VERBUCHT))
        db.session.commit()
        return gr

    def test_a_missing_document_comes_back_with_a_key_from_its_new_id(self, app, tenant, books):
        keep, gone = store(pdf(1)), store(pdf(2))
        data, sha, keep_key, keep_sha = pdf(2), gone.sha256, keep.storage_key, keep.sha256
        bundle = export()
        svc.delete(gone)                                          # Zeile + Datei weg
        do_import(bundle, tenant, mode="merge")
        assert Document.query.count() == 2
        back = Document.query.filter_by(sha256=sha).one()
        assert back.storage_key == f"documents/{back.created_at:%Y/%m}/{back.id}_{sha[:8]}.pdf" and storage.read(back.storage_key) == data
        assert Document.query.filter_by(sha256=keep_sha).one().storage_key == keep_key

    def test_a_file_missing_for_an_existing_document_is_restored(self, app, tenant, books):
        d = store(pdf(1))
        key, bundle = d.storage_key, export()
        storage.delete(key)
        do_import(bundle, tenant, mode="merge")
        assert Document.query.count() == 1 and storage.read(Document.query.one().storage_key) == pdf(1)


class TestHostileBundles:
    def _bundle(self, books):
        store(pdf(1))
        return export()

    @pytest.mark.parametrize("key", [
        "/etc/passwd", "../../other/secret.pdf", "C:\\Windows\\win.ini", "documents/../../x.pdf",
        "documents/2025/03/1_aaaaaaaa.pdf\n", "incoming/2025/1_aaaaaaaa_..", "", None,
    ])
    def test_a_crafted_key_is_never_adopted(self, app, tenant, books, key):
        bundle = tamper(self._bundle(books), "documents", lambda recs: recs[0].update(storage_key=key))
        do_import(bundle, tenant)
        row = Document.query.one()
        assert row.storage_key is None or (storage.is_valid_key(row.storage_key) and storage.exists(row.storage_key))
        assert not list(tenant.root.parent.glob("etc")) and not (tenant.root.parent / "other").exists()

    def test_a_bundle_file_with_the_wrong_checksum_is_not_restored(self, app, tenant, books):
        d = store(pdf(1))
        key = d.storage_key
        bundle = tamper(export(), "documents", lambda recs: None,
                        extra_files={f"files/{key}": b"%PDF-1.7 ganz etwas anderes"})
        storage.delete(key)
        do_import(bundle, tenant)
        assert Document.query.one().storage_key is None and not storage.exists(key)

    def test_a_bundle_file_outside_the_files_folder_is_ignored(self, app, tenant, books):
        d = store(pdf(1))
        key = d.storage_key
        bundle = tamper(export(), "documents",
                        lambda recs: recs[0].update(storage_key="documents/../../tables/documents.json"),
                        drop_files=True)
        storage.delete(key)
        do_import(bundle, tenant)
        assert Document.query.one().storage_key is None

    def test_the_restored_key_is_valid_even_if_the_json_key_was_a_different_valid_one(self, app, tenant, books):
        d = store(pdf(1))
        key = d.storage_key
        bundle = tamper(export(), "documents",
                        lambda recs: recs[0].update(storage_key="documents/2020/01/99_aaaaaaaa.pdf"))
        storage.delete(key)
        do_import(bundle, tenant)
        # die Datei im Bundle liegt unter dem Originalschluessel, nicht unter dem manipulierten → nichts zu holen
        assert Document.query.one().storage_key is None

    def test_a_forged_hash_cannot_claim_a_foreign_file(self, app, tenant, books):
        """Ein Beleg mit erfundener Pruefsumme findet keine zufaellig passende Datei."""
        d = store(pdf(1))
        bundle = tamper(export(), "documents", lambda recs: recs[0].update(sha256="f" * 64))
        do_import(bundle, tenant)
        assert Document.query.one().storage_key is None


class TestYearFilter:
    def test_documents_follow_their_bookings(self, app, tenant, books):
        account, _ = books
        d2025, d2016, unlinked, both = store(pdf(1)), store(pdf(2)), store(pdf(3)), store(pdf(4))
        b2025, b2016 = booking(account, date(2025, 3, 1)), booking(account, date(2016, 3, 1))
        svc.link(d2025, booking=b2025)
        svc.link(d2016, booking=b2016)
        svc.link(both, booking=b2025)
        svc.link(both, booking=b2016)
        db.session.commit()
        unlinked.created_at = datetime(2016, 5, 1)
        db.session.commit()
        zf = zipfile.ZipFile(io.BytesIO(export(years=[2025])))
        docs = {r["sha256"] for r in json.loads(zf.read("tables/documents.json"))}
        assert docs == {d2025.sha256, both.sha256}
        links = json.loads(zf.read("tables/document_links.json"))
        assert {(l["document_id"], l["booking_id"]) for l in links} == {(d2025.id, b2025.id), (both.id, b2025.id)}
        zf = zipfile.ZipFile(io.BytesIO(export(years=[2016])))
        assert {r["sha256"] for r in json.loads(zf.read("tables/documents.json"))} == {d2016.sha256, both.sha256, unlinked.sha256}
        names = [n for n in zf.namelist() if n.startswith("files/")]
        assert len(names) == 3

    def test_a_filtered_export_merges_cleanly_into_another_database(self, app, tenant, books):
        account, _ = books
        d = store(pdf(1))
        svc.link(d, booking=booking(account, date(2025, 3, 1)))
        svc.link(d, booking=booking(account, date(2016, 3, 1)))
        db.session.commit()
        do_import(export(years=[2025]), tenant, mode="merge")          # kein FK-Ziel fehlt
        assert Document.query.count() == 1 and DocumentLink.query.count() == 3      # 2 alte + die der kopierten 2025-Buchung
        assert all(l.booking_id and db.session.get(Booking, l.booking_id) for l in DocumentLink.query)


class TestRegisterAreas:
    """Ausgangsrechnungen, Mahnungen und Schriftfuehrung im Export: die Datei reist einmal (files/<key>),
    die Pfadspalten verweisen auf das Dokument und werden beim Import wieder gesetzt."""

    def _invoice_with_archive(self, data=b"%PDF-1.7 rechnung"):
        from app.invoices.archive import archive_invoice_file
        from app.models import Invoice
        customer = Customer(name="Kunde", customer_number=1)
        db.session.add(customer)
        db.session.flush()
        invoice = Invoice(invoice_number="2025-00001", customer_id=customer.id, date=date(2025, 2, 1),
                          status=Invoice.STATUS_SENT, total_amount=Decimal("10"))
        db.session.add(invoice)
        db.session.flush()
        archive_invoice_file(invoice, data, "pdf")
        db.session.commit()
        return invoice

    def _letter(self):
        from app.models import SchriftverkehrDocument
        doc = svc.store_record_upload("brief.txt", b"Sehr geehrte Damen und Herren", None,
                                      kind=Document.KIND_CORRESPONDENCE, title="Brief")
        entry = SchriftverkehrDocument(year=2025, title="Brief", doc_type="incoming", document_id=doc.id,
                                       file_path=str(storage.path_for(doc.storage_key)))
        db.session.add(entry)
        db.session.commit()
        return entry

    def test_the_invoice_file_travels_once_and_its_path_comes_back(self, app, tenant, books):
        from app.models import Invoice
        number = self._invoice_with_archive().invoice_number
        key = Document.query.one().storage_key
        bundle = export()
        names = zipfile.ZipFile(io.BytesIO(bundle)).namelist()
        assert f"files/{key}" in names and not [n for n in names if n.startswith("pdfs/invoices/")]
        record = json.loads(zipfile.ZipFile(io.BytesIO(bundle)).read("tables/invoices.json"))[0]
        assert record["pdf_path"] == "document:" + Document.query.one().sha256
        storage.delete(key)
        do_import(bundle, tenant)
        again = Invoice.query.filter_by(invoice_number=number).one()
        assert again.pdf_path == str(storage.path_for(key)) and storage.read(key) == b"%PDF-1.7 rechnung"
        assert [l.invoice_id for l in DocumentLink.query.all()] == [again.id]

    def test_merge_into_another_database_relinks_the_invoice(self, app, tenant, books):
        from app.models import Invoice
        self._invoice_with_archive()
        bundle = export()
        db.session.query(DocumentLink).delete()
        db.session.query(DocumentEvent).delete()
        db.session.query(Document).delete()
        db.session.query(Invoice).delete()
        db.session.commit()
        do_import(bundle, tenant, mode="merge")
        invoice = Invoice.query.one()
        doc = Document.query.one()
        assert invoice.pdf_path == str(storage.path_for(doc.storage_key))
        assert [l.invoice_id for l in doc.links] == [invoice.id]

    def test_correspondence_survives_a_round_trip(self, app, tenant, books):
        from app.models import SchriftverkehrDocument
        entry = self._letter()
        key = entry.document.storage_key
        do_import(export(), tenant)
        again = SchriftverkehrDocument.query.one()
        assert again.document is not None and again.document.storage_key == key
        assert again.file_path == str(storage.path_for(key))

    def test_without_master_data_no_meeting_links_travel(self, app, tenant, books):
        from app.models import Meeting
        meeting = Meeting(meeting_type="board", title="Vorstand", status="held")
        db.session.add(meeting)
        db.session.commit()
        doc = svc.store_record_upload("protokoll.pdf", pdf(9), None, kind=Document.KIND_PROTOCOL, meeting=meeting)
        zf = zipfile.ZipFile(io.BytesIO(export(stammdaten=False)))
        assert json.loads(zf.read("tables/document_links.json")) == []
        assert doc.sha256 in {r["sha256"] for r in json.loads(zf.read("tables/documents.json"))}
        zf = zipfile.ZipFile(io.BytesIO(export()))
        assert [l["meeting_id"] for l in json.loads(zf.read("tables/document_links.json"))] == [meeting.id]

    def test_master_data_alone_drops_the_register_reference(self, app, tenant, books):
        self._letter()
        zf = zipfile.ZipFile(io.BytesIO(export(buchungen=False)))
        records = json.loads(zf.read("tables/schriftverkehr_documents.json"))
        assert records[0]["document_id"] is None and "tables/documents.json" not in zf.namelist()

    def test_the_year_filter_keeps_invoice_archives_of_that_year(self, app, tenant, books):
        invoice = self._invoice_with_archive()
        doc = Document.query.one()
        letter = self._letter()
        zf = zipfile.ZipFile(io.BytesIO(export(years=[2025])))
        shas = {r["sha256"] for r in json.loads(zf.read("tables/documents.json"))}
        assert {doc.sha256, letter.document.sha256} <= shas
        zf = zipfile.ZipFile(io.BytesIO(export(years=[2016])))
        shas = {r["sha256"] for r in json.loads(zf.read("tables/documents.json"))}
        assert doc.sha256 not in shas and letter.document.sha256 in shas      # Schriftfuehrung immer
        assert invoice.id
