"""Belegablage Stufe 2: Belegarchiv je Jahr (ZIP + Index + Pruefsummen) und Integritaetspruefung."""
import csv
import hashlib
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.documents import archive
from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import AppSetting, Booking, Document, FiscalYear
from tests.integration.test_document_service import account, booking, group, pdf, store, tenant, user, PNG  # noqa: F401


def dated(doc, *, document_date=None, created=None):
    doc.document_date = document_date
    if created is not None:
        doc.created_at = created
    db.session.commit()
    return doc


def build(year, **kw):
    buf = io.BytesIO()
    result = archive.build(year, buf, **kw)
    return result, zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def index_rows(zf, year):
    text = zf.read(f"belegarchiv-{year}/index.csv").decode("utf-8")
    assert text.startswith("﻿")                               # BOM fuer Excel
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";"))


def ids_of(year):
    return {d.id for d in Document.query.filter(archive.year_filter(year)).all()}


class TestYearMembership:
    def test_a_document_follows_the_dates_of_its_bookings(self, app, tenant, account):
        d = store(pdf(1))
        svc.link(d, booking=booking(account, day=date(2025, 11, 5)))
        db.session.commit()
        assert d.id in ids_of(2025) and d.id not in ids_of(2024) and d.id not in ids_of(2026)

    def test_a_document_for_bookings_of_two_years_is_in_both_archives(self, app, tenant, account):
        d = store(pdf(1))
        svc.link(d, booking=booking(account, day=date(2025, 12, 20)))
        svc.link(d, booking=booking(account, day=date(2026, 1, 5)))
        db.session.commit()
        assert d.id in ids_of(2025) and d.id in ids_of(2026)

    def test_the_booking_date_beats_the_document_date(self, app, tenant, account):
        d = dated(store(pdf(1)), document_date=date(2025, 12, 28))      # Rechnung 2025, gezahlt 2026
        svc.link(d, booking=booking(account, day=date(2026, 1, 10)))
        db.session.commit()
        assert d.id in ids_of(2026) and d.id not in ids_of(2025)

    def test_a_group_counts_with_its_own_date(self, app, tenant, account):
        d = store(pdf(1))
        svc.link(d, group=group(account, day=date(2025, 3, 3)))
        db.session.commit()
        assert d.id in ids_of(2025)

    def test_a_cancelled_booking_still_proves_the_document(self, app, tenant, account):
        d = store(pdf(1))
        b = booking(account, day=date(2025, 3, 3))
        svc.link(d, booking=b)
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        assert d.id in ids_of(2025)

    def test_an_unbooked_document_counts_by_its_date_then_by_the_upload_day(self, app, tenant):
        with_date = dated(store(pdf(1)), document_date=date(2024, 6, 1), created=datetime(2026, 2, 1))
        without = dated(store(pdf(2)), created=datetime(2025, 7, 1))
        assert with_date.id in ids_of(2024) and with_date.id not in ids_of(2026)
        assert without.id in ids_of(2025)

    def test_a_business_year_that_is_not_the_calendar_year_is_respected(self, app, tenant, account):
        db.session.add(FiscalYear(year=2025, start_date=date(2024, 7, 1), end_date=date(2025, 6, 30)))
        d = store(pdf(1))
        svc.link(d, booking=booking(account, day=date(2024, 9, 1)))
        db.session.commit()
        assert archive.period(2025) == (date(2024, 7, 1), date(2025, 6, 30))
        assert d.id in ids_of(2025)

    def test_summary_lists_only_years_with_documents(self, app, tenant, account):
        a = dated(store(pdf(1)), document_date=date(2024, 5, 1))
        b = dated(store(pdf(2)), document_date=date(2025, 5, 1))
        summary = {row["year"]: row for row in archive.year_summary()}
        assert summary[2024]["count"] == 1 and summary[2025]["count"] == 1
        assert summary[2024]["size"] == a.size_bytes and 2023 not in summary
        assert archive.year_size(2025) == b.size_bytes


class TestBuild:
    def test_the_archive_holds_the_unchanged_files_with_checksums(self, app, tenant, account, user):
        data = pdf(1)
        d = store(data, name="Rechnung Müller (1).pdf", user_id=user.id)
        dated(d, document_date=date(2025, 3, 14))
        svc.link(d, booking=booking(account, amount="-98.40", day=date(2025, 3, 20)), user_id=user.id)
        photo = dated(store(PNG, name="bon.png"), document_date=date(2025, 4, 1))
        db.session.commit()

        result, zf = build(2025, created_by="tester")
        root = "belegarchiv-2025"
        assert result.ok and result.documents == 2
        names = sorted(zf.namelist())
        assert [n for n in names if "/belege/" not in n] == [
            f"{root}/LIESMICH.txt", f"{root}/index.csv", f"{root}/sha256sums.txt"]
        assert [n for n in names if "/belege/" in n] == sorted([
            f"{root}/belege/{d.id:06d}_Rechnung_M_ller_1.pdf",            # Umlaut/Klammern → „_“
            f"{root}/belege/{photo.id:06d}_bon.png"])
        pdf_entry = f"{root}/belege/{d.id:06d}_Rechnung_M_ller_1.pdf"
        assert zf.read(pdf_entry) == data                                 # Byte fuer Byte
        assert zf.getinfo(pdf_entry).compress_type == zipfile.ZIP_STORED  # schon komprimiert
        sums = dict(line.split("  ", 1)[::-1] for line in zf.read(f"{root}/sha256sums.txt").decode().splitlines())
        assert sums[pdf_entry.split(root + "/")[1]] == hashlib.sha256(data).hexdigest()

    def test_the_index_describes_every_document(self, app, tenant, account, user):
        d = dated(store(pdf(1), name="rechnung.pdf", user_id=user.id), document_date=date(2025, 3, 14))
        d.title, d.number, d.amount = "Wartung Pumpe", "RE-42", Decimal("98.40")
        svc.link(d, booking=booking(account, amount="-98.40", day=date(2025, 3, 20)), user_id=user.id)
        db.session.commit()
        _result, zf = build(2025)
        row = index_rows(zf, 2025)[0]
        assert row["Beleg-Nr."] == str(d.id) and row["Titel"] == "Wartung Pumpe" and row["Belegnummer"] == "RE-42"
        assert row["Belegdatum"] == "14.03.2025" and row["Betrag brutto (EUR)"] == "98,40"
        assert row["Status"] == "Verbucht" and row["Prüfung"] == "ok" and row["SHA-256"] == d.sha256
        # die Frist rechnet ab dem spaetesten Datum — hier der Ablagetag (heute), nicht das Belegjahr
        assert row["Abgelegt von"] == "kassier" and row["Aufbewahren bis"] == f"31.12.{date.today().year + 7}"
        assert row["Zugeordnete Buchungen"].startswith("Buchung ") and "20.03.2025" in row["Zugeordnete Buchungen"]
        assert row["Größe (Byte)"] == str(d.size_bytes) and row["Datei im Archiv"].startswith("belege/")

    @pytest.mark.parametrize("evil", ['=HYPERLINK("http://boese.example","klick")', "+1+1", "-2+3", "@SUM(A1)", "\tcmd"])
    def test_foreign_text_cannot_become_a_spreadsheet_formula(self, app, tenant, evil):
        d = dated(store(pdf(1), name="rechnung.pdf"), document_date=date(2025, 3, 14))
        d.title = d.number = evil
        db.session.commit()
        row = index_rows(build(2025)[1], 2025)[0]
        assert row["Titel"] == "'" + evil and row["Belegnummer"] == "'" + evil
        assert row["Betrag brutto (EUR)"] == "" and row["Belegdatum"] == "14.03.2025"      # Zahlen/Daten bleiben unberuehrt

    def test_unconfirmed_entries_are_marked(self, app, tenant):
        d = dated(store(pdf(1)), document_date=date(2025, 3, 14))
        d.meta_auto = True
        db.session.commit()
        assert "nicht bestätigt" in index_rows(build(2025)[1], 2025)[0]["Hinweis"]

    def test_a_missing_file_is_reported_not_hidden(self, app, tenant):
        d = dated(store(pdf(1)), document_date=date(2025, 3, 14))
        storage.delete(d.storage_key)
        result, zf = build(2025)
        row = index_rows(zf, 2025)[0]
        assert result.missing == [d.id] and not result.ok
        assert row["Prüfung"] == "Datei fehlt" and row["Datei im Archiv"] == ""
        assert zf.read("belegarchiv-2025/sha256sums.txt") == b""
        assert "fehlte die Datei" in zf.read("belegarchiv-2025/LIESMICH.txt").decode()

    def test_a_changed_file_is_flagged_and_keeps_the_recorded_checksum(self, app, tenant):
        d = dated(store(pdf(1)), document_date=date(2025, 3, 14))
        storage.path_for(d.storage_key).write_bytes(b"%PDF-1.4 manipuliert")
        result, zf = build(2025)
        assert result.mismatched == [d.id]
        assert index_rows(zf, 2025)[0]["Prüfung"] == "ABWEICHUNG"
        # sha256sum -c schlaegt fuer diese Datei fehl: die Datei passt nicht zur abgelegten Pruefsumme
        assert d.sha256 in zf.read("belegarchiv-2025/sha256sums.txt").decode()

    def test_an_empty_year_is_a_valid_archive(self, app, tenant):
        result, zf = build(2019)
        assert result.documents == 0 and index_rows(zf, 2019) == []
        assert zf.testzip() is None

    def test_the_name_in_the_archive_cannot_escape(self, app, tenant):
        d = store(pdf(1), name="..\\..\\boese/..\\ä ö!.pdf")
        assert d.original_name == "ä ö!.pdf"                               # Pfadanteile sind schon beim Upload weg
        assert archive.archive_name(d) == f"{d.id:06d}_beleg.pdf"          # nichts Brauchbares uebrig → „beleg“
        d.original_name = "a/../b\\..\\c.pdf"
        name = archive.archive_name(d)
        assert "/" not in name and "\\" not in name and ".." not in name

    def test_the_readme_explains_the_year_and_the_check(self, app, tenant):
        AppSetting.set("wg.name", "WG Musterdorf")
        db.session.commit()
        text = build(2025, created_by="tester")[1].read("belegarchiv-2025/LIESMICH.txt").decode()
        assert "Belegarchiv 2025" in text and "WG Musterdorf" in text and "von tester" in text
        assert "sha256sum -c sha256sums.txt" in text and "01.01.2025 bis 31.12.2025" in text
        assert "§ 132 BAO" in text


class TestVerify:
    def test_an_intact_store_is_clean(self, app, tenant):
        store(pdf(1))
        store(pdf(2))
        report = archive.verify_all(orphans=True)
        assert report.ok and report.checked == 2 and report.orphans == []

    def test_a_missing_file(self, app, tenant):
        d = store(pdf(1))
        storage.delete(d.storage_key)
        report = archive.verify_all()
        assert [(i[0], i[2]) for i in report.issues] == [(d.id, "missing")]

    def test_a_document_without_a_key(self, app, tenant):
        d = store(pdf(1))
        d.storage_key = None
        db.session.commit()
        assert [i[2] for i in archive.verify_all().issues] == ["missing"]

    def test_a_changed_file(self, app, tenant):
        d = store(pdf(1))
        storage.path_for(d.storage_key).write_bytes(b"anders")
        assert [(i[0], i[2]) for i in archive.verify_all().issues] == [(d.id, "mismatch")]

    def test_an_invalid_key(self, app, tenant):
        d = store(pdf(1))
        d.storage_key = "../../etc/passwd"
        db.session.commit()
        assert [i[2] for i in archive.verify_all().issues] == ["invalid_key"]

    def test_files_nobody_knows_are_reported_but_never_removed(self, app, tenant):
        d = store(pdf(1))
        stray = tenant / "documents" / "2020" / "01" / "9_deadbeef.pdf"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"%PDF-1.4")
        report = archive.verify_all(orphans=True)
        assert report.ok and report.orphans == ["documents/2020/01/9_deadbeef.pdf"]
        assert stray.exists() and storage.exists(d.storage_key)
        assert archive.verify_all(orphans=False).orphans == []


class TestCli:
    def test_verify_exits_with_one_when_something_is_wrong(self, app, tenant):
        d = store(pdf(1))
        runner = app.test_cli_runner()
        ok = runner.invoke(args=["documents-verify"])
        assert ok.exit_code == 0 and "1 Beleg(e) geprueft" in ok.output and "stimmen" in ok.output
        storage.path_for(d.storage_key).write_bytes(b"anders")
        bad = runner.invoke(args=["documents-verify"])
        assert bad.exit_code == 1 and "[mismatch]" in bad.output and "1 Auffaelligkeit" in bad.output

    def test_archive_writes_a_zip(self, app, tenant, tmp_path):
        dated(store(pdf(1)), document_date=date(2025, 3, 14))
        out = tmp_path / "archiv.zip"
        result = app.test_cli_runner().invoke(args=["documents-archive", "--year", "2025", "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert "belegarchiv-2025/index.csv" in zipfile.ZipFile(out).namelist()
