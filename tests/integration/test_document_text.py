"""Belegablage Stufe 2: Textauslese beim Upload, Vorbelegung der Angaben, Volltextsuche, Nachlesen."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import AppSetting, Customer, Document, DocumentEvent
from tests.integration.test_document_service import (  # noqa: F401  (Fixtures)
    PNG, account, actions, booking, pdf, store, tenant, user,
)
from tests.unit.test_einvoice_incoming import _fixture

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
CII = "kosit-01.01a-INVOICE_uncefact.xml"


def fixture(name):
    return (FIXTURES / f"{name}.pdf").read_bytes()


def supplier(name, **kw):
    kw.setdefault("is_supplier", True)
    kw.setdefault("is_customer", False)
    s = Customer(name=name, **kw)
    db.session.add(s)
    db.session.commit()
    return s


def event(doc, action):
    return DocumentEvent.query.filter_by(document_id=doc.id, action=action).one()


class TestUploadReadsText:
    def test_a_text_pdf_is_prefilled_and_stays_byte_identical(self, app, tenant, user):
        data = fixture("zeilen")
        doc = store(data, name="rechnung.pdf", user_id=user.id)
        assert storage.read(doc.storage_key) == data                      # die Datei ist unberuehrt
        assert doc.text_status == "ok" and "Pumpenhaus" in doc.text_content
        assert (doc.kind, doc.number, doc.document_date, doc.amount) == (
            "invoice", "RE-2026-0042", date(2026, 1, 12), Decimal("98.40"))
        assert doc.meta_auto is True
        assert actions(doc) == ["uploaded", "autofilled"]
        detail = event(doc, "autofilled").detail_dict
        assert detail["nummer"] == "RE-2026-0042" and detail["betrag"] == "98.40"

    def test_the_supplier_is_found_by_vat_id(self, app, tenant, user):
        hofer = supplier("Brunnenbau Hofer GmbH", vat_id="ATU12345678")
        doc = store(fixture("tabellenkopf"), user_id=user.id)
        assert doc.supplier_id == hofer.id
        assert event(doc, "autofilled").detail_dict["lieferant_erkannt_ueber"] == "USt-IdNr."

    def test_the_supplier_is_found_by_name(self, app, tenant, user):
        gruber = supplier("Elektro Gruber e.U.")
        doc = store(fixture("zeilen"), user_id=user.id)
        assert doc.supplier_id == gruber.id
        assert event(doc, "autofilled").detail_dict["lieferant_erkannt_ueber"] == "Name"

    def test_a_contact_that_is_not_a_supplier_is_never_chosen(self, app, tenant, user):
        supplier("Elektro Gruber e.U.", is_supplier=False, is_customer=True)
        assert store(fixture("zeilen"), user_id=user.id).supplier_id is None

    def test_the_longest_name_wins(self, app, tenant, user):
        supplier("Gruber")                               # steht auch im Text, ist aber kuerzer
        supplier("Wien")                                 # unter 5 Zeichen: zu unspezifisch
        long_name = supplier("Elektro Gruber e.U.")
        assert store(fixture("zeilen"), user_id=user.id).supplier_id == long_name.id

    def test_the_own_vat_id_never_identifies_the_supplier(self, app, tenant, user):
        AppSetting.set("wg.vat_id", "ATU87654321")       # steht als Empfaenger auf der Rechnung
        db.session.commit()
        wrong = supplier("Irgendwer", vat_id="ATU87654321")
        doc = store(fixture("tabellenkopf"), user_id=user.id)
        assert doc.supplier_id != wrong.id and doc.supplier_id is None

    def test_an_explicit_kind_is_kept(self, app, tenant, user):
        doc = store(fixture("zeilen"), user_id=user.id, kind="receipt")
        assert doc.kind == "receipt" and doc.number == "RE-2026-0042"

    def test_a_scan_without_text_layer_is_stored_without_guessing(self, app, tenant, user):
        doc = store(pdf(), user_id=user.id)               # leere Seite = keine Textebene
        assert doc.text_status == "empty" and doc.text_content is None
        assert doc.meta_auto is False and actions(doc) == ["uploaded"]

    def test_images_are_not_read(self, app, tenant):
        doc = store(PNG, name="bon.png")
        assert doc.text_status is None and doc.text_content is None and doc.meta_auto is False

    def test_an_einvoice_keeps_the_data_of_its_original(self, app, tenant, user):
        doc = store(_fixture(CII), name="r.xml", user_id=user.id)
        assert doc.einvoice is not None and doc.meta_auto is False
        doc.text_content = "Rechnungsnummer: FALSCH-1"
        assert svc.autofill(doc, user.id) == {}           # nie ueber die Daten der E-Rechnung

    def test_a_failing_detection_never_blocks_the_upload(self, app, tenant, user, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("Heuristik kaputt")
        monkeypatch.setattr(svc, "detect", boom)
        doc = store(fixture("zeilen"), user_id=user.id)
        assert doc.id and storage.exists(doc.storage_key)
        assert doc.text_status == "ok" and doc.meta_auto is False and doc.number is None

    def test_the_text_is_not_loaded_with_the_list(self, app, tenant):
        store(fixture("zeilen"))
        db.session.expire_all()
        doc = Document.query.first()
        assert "text_content" in sa.inspect(doc).unloaded


class TestConfirm:
    def test_saving_confirms_the_suggestion(self, app, tenant, user):
        doc = store(fixture("zeilen"), user_id=user.id)
        changes = svc.update_meta(
            doc, title=None, number=doc.number, document_date=doc.document_date, amount=doc.amount,
            supplier_id=doc.supplier_id, kind=doc.kind, user_id=user.id)
        db.session.commit()
        assert changes == {} and doc.meta_auto is False
        assert event(doc, "edited").detail_dict == {"bestaetigt": True}

    def test_a_correction_is_logged_with_the_confirmation(self, app, tenant, user):
        doc = store(fixture("zeilen"), user_id=user.id)
        changes = svc.update_meta(
            doc, title=None, number=doc.number, document_date=doc.document_date, amount=Decimal("98.50"),
            supplier_id=None, kind=doc.kind, user_id=user.id)
        db.session.commit()
        assert set(changes) == {"amount"} and doc.meta_auto is False
        assert event(doc, "edited").detail_dict["bestaetigt"] is True

    def test_nothing_is_logged_when_there_was_nothing_to_confirm(self, app, tenant, user):
        doc = store(pdf(), user_id=user.id)
        svc.update_meta(doc, title=None, number=None, document_date=None, amount=None,
                        supplier_id=None, kind=doc.kind, user_id=user.id)
        assert actions(doc) == ["uploaded"]

    def test_after_a_user_edit_nothing_is_prefilled_again(self, app, tenant, user):
        doc = store(fixture("zeilen"), user_id=user.id)
        svc.update_meta(doc, title=None, number=doc.number, document_date=doc.document_date,
                        amount=None, supplier_id=None, kind=doc.kind, user_id=user.id)
        db.session.commit()
        assert doc.amount is None
        assert svc.autofill(doc, user.id) == {} and doc.amount is None and doc.meta_auto is False


class TestIndexText:
    def _old_document(self, **fields):
        """Ein Beleg, wie ihn eine aeltere Version abgelegt haette: ohne Text und Vorbelegung."""
        doc = store(fixture("zeilen"))
        doc.text_status = doc.text_content = None
        doc.meta_auto = False
        for name in ("kind", "number", "document_date", "amount", "supplier_id"):
            setattr(doc, name, fields.get(name, "other" if name == "kind" else None))
        DocumentEvent.query.filter_by(document_id=doc.id, action="autofilled").delete()
        db.session.commit()
        return doc

    def test_old_documents_get_their_text_and_suggestions(self, app, tenant):
        doc = self._old_document()
        assert svc.index_text(doc) == "ok"
        db.session.commit()
        assert doc.text_status == "ok" and doc.number == "RE-2026-0042" and doc.meta_auto is True

    def test_existing_entries_are_never_overwritten(self, app, tenant):
        doc = self._old_document(number="MANUELL", kind="receipt")
        svc.index_text(doc)
        db.session.commit()
        assert doc.number == "MANUELL" and doc.kind == "receipt"          # bleibt
        assert doc.amount == Decimal("98.40")                              # nur Leeres wird gefuellt

    def test_images_and_missing_files_are_skipped(self, app, tenant):
        image = store(PNG, name="foto.png")
        assert svc.index_text(image) is None
        doc = store(fixture("zeilen"))
        storage.delete(doc.storage_key)
        assert svc.index_text(doc) is None


class TestSearchAndUse:
    def test_the_text_is_searched_but_marked_as_a_text_hit(self, app, tenant):
        doc = store(fixture("zeilen"), name="scan0042.pdf")
        found = Document.query.filter(svc.search_filter("Pumpenhaus")).all()
        assert found == [doc] and not svc.matches_meta(doc, "Pumpenhaus")
        assert svc.matches_meta(doc, "scan0042") and svc.matches_meta(doc, "RE-2026-0042")

    def test_other_documents_are_not_found(self, app, tenant):
        store(fixture("zeilen"))
        assert Document.query.filter(svc.search_filter("Hochbehälter")).count() == 0

    def test_detected_identifiers_leave_out_the_own_ones(self, app, tenant):
        AppSetting.set("wg.vat_id", "ATU 8765 4321")
        db.session.commit()
        doc = store(fixture("tabellenkopf"))
        assert svc.detected_identifiers(doc) == (["ATU12345678"], ["AT109999900000067890"])
        assert svc.detected_identifiers(store(pdf(), name="leer.pdf")) == ([], [])

    def test_the_suggested_amount_finds_the_matching_booking(self, app, tenant, account):
        doc = store(fixture("zeilen"))
        fitting = booking(account, amount="-98.40", day=date(2026, 1, 15))
        booking(account, amount="-10.00", day=date(2026, 1, 15))
        assert [obj.id for _kind, obj in svc.candidates(doc)] == [fitting.id]

    def test_the_booking_form_is_prefilled_from_the_suggestion(self, app, tenant):
        doc = store(fixture("zeilen"))
        prefill = svc.booking_prefill(doc)
        assert prefill["amount"] == Decimal("-98.40") and prefill["reference"] == "RE-2026-0042"
        assert prefill["date"] == date(2026, 1, 12)
