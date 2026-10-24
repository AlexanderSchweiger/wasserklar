"""SEPA-Lastschrift: Rechnungshinweis, Einzugslauf, Bankdatei, Verbuchen, Ruecklastschrift.

Die pain.008-Datei wird gegen das offizielle ISO-20022-Schema geprueft
(``tests/fixtures/sepa/pain.008.001.08.xsd``). Alle Datumswerte kommen ueber
``today=`` herein, damit der Test nicht vom Kalender abhaengt.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree

from app.bank_import import services as bank_services
from app.bank_import.matching import match_line
from app.direct_debit import export_csv, notice, pain008
from app.direct_debit import services as dd
from app.einvoice import cii, ubl
from app.einvoice.mapper import build_einvoice
from app.einvoice.model import PROFILE_PEPPOL_UBL
from app.einvoice.rules import check
from app.extensions import db
from app.invoices.services import create_or_update_open_item
from app.models import (
    Account, AppSetting, BankStatement, BankStatementLine, BillingPeriod, Customer,
    DirectDebitItem, DirectDebitRun, FiscalYear, Invoice, InvoiceItem, OpenItem, RealAccount,
)

XSD = Path(__file__).resolve().parents[1] / "fixtures" / "sepa" / "pain.008.001.08.xsd"
NS = {"p": pain008.NS}
TODAY = date(2026, 10, 5)                 # Montag → frueheste Abbuchung Mi 07.10.
DUE = date(2026, 10, 14)
IBAN = "AT611904300234573201"
IBAN_OTHER_BANK = "AT483200000012345864"
SELLER = {
    "org.country": "AT",
    "wg.name": "Wassergenossenschaft Treffling",
    "wg.address": "Treffling 3\n9871 Seeboden",
    "wg.vat_id": "ATU12345678",
    "wg.email": "wg@treffling.example",
    "wg.iban": "AT94 2070 6045 0005 0440",
    "wg.bic": "KSPKAT2KXXX",
    "wg.account_holder": "WG Treffling",
    "sepa.creditor_id": "AT61ZZZ01234567890",
}


@pytest.fixture
def setup(app):
    for key, value in SELLER.items():
        AppSetting.set(key, value)
    db.session.add(FiscalYear(year=2026, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)))
    db.session.add(BillingPeriod(name="2025/26", start_date=date(2025, 10, 1),
                                 end_date=date(2026, 9, 30), active=True))
    acc = Account(name="Wassergebühren", code="W01")
    ra = RealAccount(name="Raiffeisen", iban="AT942070604500050440", opening_balance=0, is_default=True)
    db.session.add_all([acc, ra])
    db.session.commit()
    return {"account": acc, "real_account": ra}


def _customer(number=31, *, debit=True, iban=IBAN, signed=date(2024, 1, 15), name="Beispiel Anna"):
    c = Customer(name=name, first_name=name.split()[-1], last_name=name.split()[0],
                 customer_number=number, strasse="Hauptstraße", hausnummer="5", plz="9871",
                 ort="Seeboden", is_customer=True, active=True)
    if debit:
        c.payment_method = Customer.PAYMENT_DIRECT_DEBIT
        c.iban = iban
        c.mandate_signed_on = signed
        notice.ensure_mandate_reference(c)
    db.session.add(c)
    db.session.flush()
    return c


def _invoice(customer, account, *, number="2026-00042", amount="100.00", send=True):
    period = BillingPeriod.query.first()
    inv = Invoice(invoice_number=number, customer_id=customer.id, billing_period_id=period.id,
                  date=date(2026, 9, 30), due_date=DUE, status=Invoice.STATUS_DRAFT)
    db.session.add(inv)
    db.session.flush()
    inv.items.append(InvoiceItem(description="Wasserbezug", quantity=Decimal("1"), unit="Pauschal",
                                 unit_price=Decimal(amount), amount=Decimal(amount),
                                 account_id=account.id))
    db.session.flush()
    inv.recalculate_total()
    if send:
        inv.status = Invoice.STATUS_SENT
        create_or_update_open_item(inv)
    db.session.commit()
    return inv


def _run(*ops, today=TODAY):
    run = dd.create_run([op.id for op in ops], today=today)
    db.session.commit()
    return run


def _dump(name, data):
    """XML fuer den Mustang-Validator ablegen (``EINVOICE_DUMP_DIR``, siehe test_einvoice.py)."""
    import os
    dump_dir = os.environ.get("EINVOICE_DUMP_DIR")
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
        with open(os.path.join(dump_dir, f"{name}.xml"), "wb") as fh:
            fh.write(data)


def _xsd_valid(data):
    schema = etree.XMLSchema(etree.parse(str(XSD)))
    doc = etree.fromstring(data)
    assert schema.validate(doc), schema.error_log
    return doc


# ---------------------------------------------------------------------------
# Rechnung
# ---------------------------------------------------------------------------

class TestNotice:
    def test_sent_invoice_freezes_the_mandate(self, setup):
        c = _customer()
        inv = _invoice(c, setup["account"])
        assert inv.payment_method == Customer.PAYMENT_DIRECT_DEBIT
        assert inv.debit_mandate_reference == "31"
        assert inv.debit_iban == IBAN
        # Spaeter neue Bankverbindung → die versendete Rechnung bleibt beim alten Konto.
        c.iban = IBAN_OTHER_BANK
        db.session.commit()
        assert notice.debit_notice(inv)["iban_masked"].endswith("3201")

    def test_default_text_is_the_pre_notification(self, setup):
        inv = _invoice(_customer(), setup["account"])
        n = notice.debit_notice(inv)
        assert n["title"] == "Bitte nicht einzahlen."
        assert "100,00 €" in n["text"] and "am 14.10.2026" in n["text"]
        assert "AT61 •••• •••• •••• 3201" in n["text"]
        assert n["creditor_id"] == "AT61ZZZ01234567890" and n["mandate_reference"] == "31"

    def test_own_text_and_broken_text(self, setup):
        inv = _invoice(_customer(), setup["account"])
        AppSetting.set(notice.NOTICE_TITLE_KEY, "Achtung")
        AppSetting.set(notice.NOTICE_TEXT_KEY, "Abbuchung {{ abbuchungstermin }} ({{ mandatsreferenz }})")
        db.session.commit()
        n = notice.debit_notice(inv)
        assert n["title"] == "Achtung" and n["text"] == "Abbuchung am 14.10.2026 (31)"
        AppSetting.set(notice.NOTICE_TEXT_KEY, "kaputt {{ betrag ")
        db.session.commit()
        assert "SEPA-Lastschriftverfahren" in notice.debit_notice(inv)["text"]   # Laender-Standard

    def test_transfer_and_credit_notes_have_no_notice(self, setup):
        transfer = _invoice(_customer(32, debit=False), setup["account"], number="2026-00043")
        assert transfer.payment_method == Customer.PAYMENT_TRANSFER
        assert notice.debit_notice(transfer) is None
        debit = _invoice(_customer(33), setup["account"], number="2026-00044")
        debit.invoice_kind = Invoice.KIND_CREDIT_NOTE
        assert notice.debit_notice(debit) is None

    def test_invoice_html_shows_notice_instead_of_payment_request(self, setup):
        from app.invoices.pdf_service import render_invoice_html
        inv = _invoice(_customer(), setup["account"])
        html = render_invoice_html(inv)
        assert "Bitte nicht einzahlen." in html
        assert "Gläubiger-ID: AT61ZZZ01234567890" in html
        assert "Wir ersuchen Sie" not in html
        plain = _invoice(_customer(34, debit=False), setup["account"], number="2026-00045")
        assert "Wir ersuchen Sie" in render_invoice_html(plain)

    def test_word_document_shows_notice(self, setup):
        import io

        from docx import Document as DocxDocument

        from app.invoices.document_service import generate_docx
        from app.settings_service import wg_settings
        inv = _invoice(_customer(), setup["account"])
        doc = DocxDocument(io.BytesIO(generate_docx(inv, wg_settings())))
        text = "\n".join(cell.text for t in doc.tables for row in t.rows for cell in row.cells)
        assert "Bitte nicht einzahlen." in text and "Mandatsreferenz: 31" in text
        assert "Wir ersuchen Sie" not in text


# ---------------------------------------------------------------------------
# Auswahl + Bankdatei
# ---------------------------------------------------------------------------

class TestRun:
    def test_candidates_split_ready_and_excluded(self, setup):
        ok = _invoice(_customer(), setup["account"])
        missing = _invoice(_customer(35, signed=None, name="Ohne Datum"), setup["account"],
                           number="2026-00046")
        ready, excluded = dd.candidates(today=TODAY)
        assert [c.invoice.id for c in ready] == [ok.id]
        assert ready[0].collection_date == DUE and ready[0].preselected
        assert [c.invoice.id for c in excluded] == [missing.id]
        assert "Datum der Mandatsunterschrift fehlt" in excluded[0].issues

    def test_not_listed_counts_drafts_and_later_due_dates(self, setup):
        c = _customer()
        _invoice(c, setup["account"], send=False)
        later = _invoice(c, setup["account"], number="2026-00050")
        later.due_date = later.open_item.due_date = date(2026, 11, 22)
        _invoice(_customer(39, debit=False, name="Ueberweiser"), setup["account"],
                 number="2026-00051", send=False)
        db.session.commit()
        hidden = dd.not_listed(due_until=date(2026, 11, 6))
        assert (hidden.drafts, hidden.due_later, hidden.last_due) == (1, 1, date(2026, 11, 22))
        assert dd.candidates(due_until=date(2026, 11, 6), today=TODAY)[0] == []
        assert not dd.not_listed(due_until=date(2026, 11, 30)).due_later

    def test_overdue_item_is_collected_at_the_earliest_date(self, setup):
        _invoice(_customer(), setup["account"])
        ready, _ = dd.candidates(today=date(2026, 10, 23))          # Freitag, Faelligkeit vorbei
        assert ready[0].collection_date == date(2026, 10, 27)

    def test_invoice_sent_as_transfer_is_not_preselected(self, setup):
        c = _customer(36, debit=False, name="Spaet Umgestellt")
        inv = _invoice(c, setup["account"], number="2026-00047")
        c.payment_method, c.iban, c.mandate_signed_on = Customer.PAYMENT_DIRECT_DEBIT, IBAN, date(2025, 1, 2)
        notice.ensure_mandate_reference(c)
        db.session.commit()
        ready, _ = dd.candidates(today=TODAY)
        cand = next(x for x in ready if x.invoice.id == inv.id)
        assert not cand.preselected and "Überweisung" in cand.warnings[0]

    def test_pain008_is_schema_valid(self, setup):
        a = _invoice(_customer(), setup["account"])
        b = _invoice(_customer(37, iban=IBAN_OTHER_BANK, name="Müller Søren"), setup["account"],
                     number="2026-00048", amount="45.50")
        run = _run(a.open_item, b.open_item)
        assert run.item_count == 2 and run.total_amount == Decimal("145.50")
        doc = _xsd_valid(pain008.build(run))
        assert doc.findtext("p:CstmrDrctDbtInitn/p:GrpHdr/p:CtrlSum", namespaces=NS) == "145.50"
        pmt = doc.find("p:CstmrDrctDbtInitn/p:PmtInf", NS)
        assert pmt.findtext("p:ReqdColltnDt", namespaces=NS) == "2026-10-14"
        assert pmt.findtext("p:PmtTpInf/p:SeqTp", namespaces=NS) == "RCUR"
        assert pmt.findtext("p:CdtrSchmeId/p:Id/p:PrvtId/p:Othr/p:Id", namespaces=NS) == "AT61ZZZ01234567890"
        names = pmt.xpath("p:DrctDbtTxInf/p:Dbtr/p:Nm/text()", namespaces=NS)
        assert "Soren Mueller" in names               # SEPA-Zeichensatz
        e2e = pmt.xpath("p:DrctDbtTxInf/p:PmtId/p:EndToEndId/text()", namespaces=NS)
        assert sorted(e2e) == ["2026-00042", "2026-00048"]
        # Datei ist reproduzierbar
        assert pain008.build(run) == pain008.build(run)

    def test_one_payment_info_per_collection_date(self, setup):
        a = _invoice(_customer(), setup["account"])
        b = _invoice(_customer(38, name="Zweite Frist"), setup["account"], number="2026-00049")
        b.due_date = b.open_item.due_date = date(2026, 11, 2)
        db.session.commit()
        doc = _xsd_valid(pain008.build(_run(a.open_item, b.open_item)))
        dates = doc.xpath("//p:PmtInf/p:ReqdColltnDt/text()", namespaces=NS)
        assert dates == ["2026-10-14", "2026-11-02"]

    def test_never_collected_twice_and_cancel_frees(self, setup):
        inv = _invoice(_customer(), setup["account"])
        run = _run(inv.open_item)
        assert dd.candidates(today=TODAY)[0] == []
        with pytest.raises(dd.DirectDebitError):
            dd.create_run([inv.open_item.id], today=TODAY)
        dd.cancel_run(run)
        db.session.commit()
        assert [c.invoice.id for c in dd.candidates(today=TODAY)[0]] == [inv.id]

    def test_missing_creditor_id_blocks(self, setup):
        inv = _invoice(_customer(), setup["account"])
        AppSetting.set(notice.CREDITOR_ID_KEY, None)
        db.session.commit()
        with pytest.raises(dd.DirectDebitError, match="Gläubiger-ID"):
            dd.create_run([inv.open_item.id], today=TODAY)

    def test_changed_account_is_flagged_as_amendment(self, setup):
        c = _customer()
        first = _invoice(c, setup["account"])
        _run(first.open_item)
        c.iban = IBAN_OTHER_BANK                 # anderes Kreditinstitut → SMNDA
        db.session.commit()
        second = _invoice(c, setup["account"], number="2026-00050")
        run = _run(second.open_item)
        item = run.items[0]
        assert item.amended_from_iban == IBAN
        doc = _xsd_valid(pain008.build(run))
        info = doc.find(".//p:MndtRltdInf", NS)
        assert info.findtext("p:AmdmntInd", namespaces=NS) == "true"
        assert info.findtext("p:AmdmntInfDtls/p:OrgnlDbtrAgt/p:FinInstnId/p:Othr/p:Id",
                             namespaces=NS) == "SMNDA"

    def test_csv_control_list(self, setup):
        inv = _invoice(_customer(), setup["account"], amount="123.45")
        text = export_csv.build(_run(inv.open_item))
        assert text.startswith("﻿Einzugsdatum;")
        assert "AT61 1904 3002 3457 3201" in text and "123,45" in text and "14.10.2026" in text


# ---------------------------------------------------------------------------
# Verbuchen, Ruecklastschrift, Mahnwesen
# ---------------------------------------------------------------------------

class TestBooking:
    def test_book_run_paid_and_return(self, setup):
        inv = _invoice(_customer(), setup["account"])
        run = _run(inv.open_item)
        stats = dd.book_run_paid(run, setup["real_account"].id, user_id=None)
        db.session.commit()
        assert stats["booked"] == 1 and not stats["errors"]
        assert inv.status == Invoice.STATUS_PAID and inv.open_item.open_balance == 0
        assert run.items[0].state == DirectDebitItem.STATE_PAID
        assert dd.cancel_blocker(run)

        amount, created = dd.record_return(run.items[0], date(2026, 10, 20),
                                           setup["real_account"].id, None, reason="Konto erloschen")
        db.session.commit()
        assert amount == Decimal("100.00") and created
        assert inv.status == Invoice.STATUS_SENT
        assert inv.open_item.status == OpenItem.STATUS_OPEN
        assert inv.open_item.open_balance == Decimal("100.00")
        assert run.items[0].state == DirectDebitItem.STATE_RETURNED
        # wieder einziehbar
        assert [c.invoice.id for c in dd.candidates(today=TODAY)[0]] == [inv.id]

    def test_closed_fiscal_year_is_reported_per_item(self, setup):
        inv = _invoice(_customer(), setup["account"])
        run = _run(inv.open_item)
        FiscalYear.query.filter_by(year=2026).one().closed = True
        db.session.commit()
        stats = dd.book_run_paid(run, setup["real_account"].id, user_id=None)
        assert stats["booked"] == 0 and stats["errors"]

    def test_items_in_collection_are_not_dunned(self, setup):
        from app.dunning.services import eligible_invoices_for_stage
        from app.models import DunningPolicy, DunningStage
        policy = DunningPolicy(name="Standard", is_default=True, active=True)
        db.session.add(policy)
        db.session.flush()
        db.session.add(DunningStage(policy_id=policy.id, level=1, name="Erinnerung", active=True,
                                    days_after_due=1, fee_fixed=0, new_due_days=14))
        inv = _invoice(_customer(), setup["account"])
        later = date(2026, 12, 1)
        assert [i.id for i, _s in eligible_invoices_for_stage(policy, today=later)] == [inv.id]
        run = _run(inv.open_item)
        assert eligible_invoices_for_stage(policy, today=later) == []
        dd.cancel_run(run)
        db.session.commit()
        assert [i.id for i, _s in eligible_invoices_for_stage(policy, today=later)] == [inv.id]


# ---------------------------------------------------------------------------
# Bankimport
# ---------------------------------------------------------------------------

def _statement(ra):
    stmt = BankStatement(format="camt053", filename="a.xml", file_hash="h-dd",
                         real_account_id=ra.id, currency="EUR")
    db.session.add(stmt)
    db.session.flush()
    return stmt


def _line(stmt, amount, purpose, booking_date=date(2026, 10, 14), index=0):
    line = BankStatementLine(statement_id=stmt.id, line_index=index, booking_date=booking_date,
                             amount=Decimal(amount), purpose=purpose, line_status="pending")
    db.session.add(line)
    db.session.flush()
    return line


class TestBankImport:
    def test_batch_credit_is_split_over_the_run(self, setup):
        a = _invoice(_customer(), setup["account"])
        b = _invoice(_customer(39, name="Zweiter Kunde"), setup["account"], number="2026-00051",
                     amount="45.50")
        run = _run(a.open_item, b.open_item)
        stmt = _statement(setup["real_account"])
        line = _line(stmt, "145.50", f"SEPA-Lastschrift {run.message_id} Rechnung 2026-00042")
        match_line(line)
        assert line.match_type == BankStatementLine.MATCH_DIRECT_DEBIT
        assert sorted(al.open_item_id for al in line.allocations) == sorted(
            [a.open_item.id, b.open_item.id])
        db.session.commit()
        stats = bank_services.commit_statement(stmt.id, user_id=None)
        assert stats["committed"] == 1 and not stats["errors"]
        assert a.status == Invoice.STATUS_PAID and b.status == Invoice.STATUS_PAID
        assert run.items[0].state == DirectDebitItem.STATE_PAID

    def test_amount_without_reference_needs_a_close_date(self, setup):
        a = _invoice(_customer(), setup["account"])
        _run(a.open_item)
        stmt = _statement(setup["real_account"])
        far = _line(stmt, "100.00", "Gutschrift", booking_date=date(2026, 12, 1))
        match_line(far)
        assert far.match_type != BankStatementLine.MATCH_DIRECT_DEBIT
        near = _line(stmt, "100.00", "Gutschrift", booking_date=date(2026, 10, 15), index=1)
        match_line(near)
        assert near.match_type == BankStatementLine.MATCH_DIRECT_DEBIT

    def test_return_debit_line_reopens_the_invoice(self, setup):
        inv = _invoice(_customer(), setup["account"])
        run = _run(inv.open_item)
        dd.book_run_paid(run, setup["real_account"].id, user_id=None)
        db.session.commit()
        stmt = _statement(setup["real_account"])
        line = _line(stmt, "-100.00", "RUECKLASTSCHRIFT 2026-00042 MS03",
                     booking_date=date(2026, 10, 20))
        match_line(line)
        assert line.match_type == BankStatementLine.MATCH_DIRECT_DEBIT_RETURN
        db.session.commit()
        stats = bank_services.commit_statement(stmt.id, user_id=None)
        assert stats["committed"] == 1 and not stats["errors"]
        assert inv.status == Invoice.STATUS_SENT and inv.open_item.open_balance == Decimal("100.00")
        assert run.items[0].returned_on == date(2026, 10, 20)

    def test_return_with_bank_fee_is_left_for_the_user(self, setup):
        inv = _invoice(_customer(), setup["account"])
        run = _run(inv.open_item)
        dd.book_run_paid(run, setup["real_account"].id, user_id=None)
        db.session.commit()
        stmt = _statement(setup["real_account"])
        line = _line(stmt, "-103.50", "RUECKLASTSCHRIFT 2026-00042")
        match_line(line)
        assert line.match_type != BankStatementLine.MATCH_DIRECT_DEBIT_RETURN


# ---------------------------------------------------------------------------
# E-Rechnung
# ---------------------------------------------------------------------------

class TestEInvoice:
    def test_cii_carries_the_direct_debit(self, setup):
        inv = _invoice(_customer(), setup["account"])
        e = build_einvoice(inv)
        assert check(e) == [], check(e)
        assert e.payment.means_code == "59"
        assert e.payment.iban is None and e.payment.debtor_iban == IBAN
        data = cii.serialize(e)
        _dump("DD1_zugferd_lastschrift", data)
        tree = etree.fromstring(data)
        ns = {"ram": cii.NS_RAM}
        settlement = tree.find(".//ram:ApplicableHeaderTradeSettlement", ns)
        assert settlement[0].tag == f"{{{cii.NS_RAM}}}CreditorReferenceID"
        assert settlement.findtext("ram:CreditorReferenceID", namespaces=ns) == "AT61ZZZ01234567890"
        means = settlement.find("ram:SpecifiedTradeSettlementPaymentMeans", ns)
        assert means.findtext("ram:TypeCode", namespaces=ns) == "59"
        assert means.findtext("ram:PayerPartyDebtorFinancialAccount/ram:IBANID", namespaces=ns) == IBAN
        assert means.find("ram:PayeePartyCreditorFinancialAccount", ns) is None
        assert settlement.findtext("ram:SpecifiedTradePaymentTerms/ram:DirectDebitMandateID",
                                   namespaces=ns) == "31"
        assert "SEPA-Lastschrift" in settlement.findtext(
            "ram:SpecifiedTradePaymentTerms/ram:Description", namespaces=ns)

    def test_ubl_carries_the_mandate(self, setup):
        inv = _invoice(_customer(), setup["account"])
        data = ubl.serialize(build_einvoice(inv, PROFILE_PEPPOL_UBL))
        _dump("DD3_ubl_lastschrift", data)
        tree = etree.fromstring(data)
        ns = {"cac": ubl.NS_CAC, "cbc": ubl.NS_CBC}
        means = tree.find("cac:PaymentMeans", ns)
        assert means.findtext("cbc:PaymentMeansCode", namespaces=ns) == "59"
        assert means.findtext("cac:PaymentMandate/cbc:ID", namespaces=ns) == "31"
        assert means.findtext("cac:PaymentMandate/cac:PayerFinancialAccount/cbc:ID",
                              namespaces=ns) == IBAN
        ids = tree.findall("cac:AccountingSupplierParty/cac:Party/cac:PartyIdentification/cbc:ID", ns)
        assert ids[-1].text == "AT61ZZZ01234567890" and ids[-1].get("schemeID") == "SEPA"

    def test_missing_mandate_reference_is_reported(self, setup):
        c = _customer()
        inv = _invoice(c, setup["account"])
        inv.debit_mandate_reference = None
        db.session.commit()
        assert any("Mandatsreferenz" in issue for issue in check(build_einvoice(inv)))


class TestXRechnungDirectDebit:
    """XRechnung mit Lastschrift: BR-DE-30 (Glaeubiger-ID) und BR-DE-31 (belastetes Konto)."""

    def _setup(self):
        from tests.integration.test_einvoice import _seller
        from tests.integration.test_einvoice_xrechnung import XR_SELLER, _government
        _seller({**XR_SELLER, "sepa.creditor_id": "DE98ZZZ09999999999"})
        db.session.add(BillingPeriod(name="2025/26", start_date=date(2025, 10, 1),
                                     end_date=date(2026, 9, 30), active=True))
        acc = Account(name="Wasser", code="W01")
        db.session.add(acc)
        customer = _government(payment_method=Customer.PAYMENT_DIRECT_DEBIT,
                               iban="DE89370400440532013000", mandate_reference="G-12",
                               mandate_signed_on=date(2024, 1, 2))
        db.session.flush()
        return _invoice(customer, acc)

    def test_xrechnung_is_complete(self, app):
        from app.einvoice.model import PROFILE_XRECHNUNG
        inv = self._setup()
        e = build_einvoice(inv, PROFILE_XRECHNUNG)
        assert check(e) == [], check(e)
        _dump("DD2_xrechnung_lastschrift", cii.serialize(e))

    def test_missing_creditor_id_is_br_de_30(self, app):
        from app.einvoice.model import PROFILE_XRECHNUNG
        inv = self._setup()
        AppSetting.set("sepa.creditor_id", None)
        db.session.commit()
        issues = " | ".join(check(build_einvoice(inv, PROFILE_XRECHNUNG)))
        assert "Gläubiger-ID" in issues
