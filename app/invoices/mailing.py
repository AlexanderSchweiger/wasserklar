"""Sammelversand: alle versandbereiten Belege eines Kontakts in EINER E-Mail.

Typisch die Jahresabrechnung mit den Teilrechnungen des Folgejahres, aber
auch ein Kontakt mit mehreren Liegenschaften im selben Lauf. Jeder Beleg
bleibt ein eigener Anhang (eigene E-Rechnung, eigenes Archiv-PDF).

Zustell-Tracking: die E-Mail traegt die Kennung des **Hauptbelegs**
(``run_before_send(hauptbeleg, msg)`` — Postmark-Metadaten, Abmelde-Footer
der SaaS). Begleitbelege merken sich ``mailed_with_invoice_id`` und zeigen
den Zustellstatus des Hauptbelegs (``Invoice.mail_status_invoice``). Die
Postmark-MessageID bekommt nur der Hauptbeleg (Unique-Index auf
``email_events(postmark_message_id, record_type)``).
"""
from collections import OrderedDict
from decimal import Decimal

from flask import current_app, render_template

from app.extensions import db
from app.models import AppSetting, Invoice

BUNDLE_SUBJECT_KEY = "invoice.bundle_email_subject"
BUNDLE_BODY_KEY = "invoice.bundle_email_body"
DEFAULT_BUNDLE_SUBJECT = "{{ wg_name }} - Ihre Belege ({{ anzahl }})"

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class BundleError(ValueError):
    """Versand nicht moeglich (deutsche Meldung)."""


def primary_of(invoices):
    """Hauptbeleg: Jahres-/Schlussrechnung vor Teilrechnungen, sonst der
    frueheste faellige."""
    def key(inv):
        rank = 1 if inv.is_installment else 0
        return (rank, inv.due_date or inv.date, inv.invoice_number)
    return sorted(invoices, key=key)[0]


def ordered(invoices):
    """Reihenfolge im Anhang/Kuvert: Hauptbeleg, dann nach Faelligkeit."""
    primary = primary_of(invoices)
    rest = sorted((i for i in invoices if i.id != primary.id),
                  key=lambda i: (1 if i.is_installment else 0, i.due_date or i.date,
                                 i.invoice_number))
    return [primary] + rest


def bundles(invoices):
    """Versandbereite Entwuerfe je Kontakt (nur Kontakte mit Schriftverkehr per
    E-Mail): ``[{"customer", "invoices"}]`` in Reihenfolge des ersten Belegs."""
    groups = OrderedDict()
    for inv in invoices:
        if inv.status != Invoice.STATUS_DRAFT or not inv.customer.wants_email:
            continue
        groups.setdefault(inv.customer_id, []).append(inv)
    return [{"customer": invs[0].customer, "invoices": ordered(invs)}
            for invs in groups.values()]


def post_order(invoices):
    """Druckreihenfolge fuers Kuvert: je Kontakt zusammen (Reihenfolge des
    ersten Belegs des Kontakts), darin wie ``ordered``."""
    groups = OrderedDict()
    for inv in invoices:
        groups.setdefault(inv.customer_id, []).append(inv)
    out = []
    for invs in groups.values():
        out.extend(ordered(invs))
    return out


def run_invoices_with_linked(run):
    """Rechnungen eines Laufs plus die der verknuepften Teilrechnungslaeufe."""
    invoices = run.invoices.order_by(Invoice.invoice_number).all()
    for linked in getattr(run, "linked_runs", None) or []:
        invoices.extend(linked.invoices.order_by(Invoice.invoice_number).all())
    return invoices


def _context(customer, invoices):
    from app.direct_debit.notice import debit_notice
    from app.settings_service import get_wg
    lines = []
    due_total = Decimal("0")
    for inv in invoices:
        total = Decimal(str(inv.total_amount or 0))
        due = inv.due_date.strftime("%d.%m.%Y") if inv.due_date else "—"
        if total > 0:
            due_total += total
            amount = f"{total:.2f}".replace(".", ",") + " €, fällig " + due
        elif total < 0:
            amount = "Guthaben " + f"{-total:.2f}".replace(".", ",") + " €"
        else:
            amount = "nichts zu zahlen"
        lines.append(f"- {inv.document_title} {inv.invoice_number}: {amount}")
    debit = any(debit_notice(i) for i in invoices)
    return dict(
        name=customer.letter_name,
        anzahl=len(invoices),
        belege="\n".join(lines),
        summe=f"{due_total:.2f}".replace(".", ","),
        bankeinzug=debit,
        iban=get_wg("iban"),
        wg_name=get_wg("name"),
    )


def render_subject(customer, invoices):
    from jinja2.sandbox import SandboxedEnvironment
    tpl = AppSetting.get(BUNDLE_SUBJECT_KEY) or DEFAULT_BUNDLE_SUBJECT
    try:
        text = SandboxedEnvironment().from_string(tpl).render(**_context(customer, invoices))
    except Exception:  # noqa: BLE001 — kaputte Vorlage: Standard
        text = ""
    return text.strip() or f"Ihre Belege ({len(invoices)})"


def render_body(customer, invoices):
    from jinja2.sandbox import SandboxedEnvironment
    custom = AppSetting.get(BUNDLE_BODY_KEY)
    ctx = _context(customer, invoices)
    if custom:
        try:
            return SandboxedEnvironment().from_string(custom).render(**ctx)
        except Exception:  # noqa: BLE001
            current_app.logger.warning("Sammel-Mailvorlage fehlerhaft — Standardtext")
    return render_template("invoices/email_bundle_body.txt", customer=customer,
                           invoices=invoices, **ctx)


def attachments_for(invoice, *, test_mode):
    """``(anhaenge, pdf_bytes, docx_bytes)`` eines Belegs — dieselbe Formatwahl
    wie der Einzelversand (XRechnung/UBL als XML, sonst PDF/Word)."""
    from app.einvoice.service import EInvoiceUnavailable, xml_only_attachment
    from app.invoices.document_service import generate_docx
    from app.invoices.pdf_service import current_design, render_invoice_pdf
    from app.invoices.routes import _mail_document_format
    from app.settings_service import wg_settings

    try:
        xml = xml_only_attachment(invoice, freeze=not test_mode)
    except EInvoiceUnavailable as exc:
        raise BundleError(f"{invoice.invoice_number}: {exc.format_name} nicht möglich: "
                          + "; ".join(exc.issues))
    if xml:
        return [xml], None, None
    fmt = _mail_document_format(invoice)
    files, pdf_bytes, doc_bytes = [], None, None
    if fmt in ("pdf", "both"):
        try:
            pdf_bytes = render_invoice_pdf(invoice, for_email=True, freeze_einvoice=not test_mode)
            files.append((f"{invoice.invoice_number}.pdf", "application/pdf", pdf_bytes))
        except (ImportError, OSError):
            if fmt == "pdf":
                raise BundleError("WeasyPrint nicht verfügbar")
    if fmt == "docx" or (fmt == "both" and pdf_bytes is None):
        doc_bytes = generate_docx(invoice, wg_settings(), design=current_design())
        files.append((f"{invoice.invoice_number}.docx", _DOCX_MIME, doc_bytes))
    if not files:
        raise BundleError(f"{invoice.invoice_number}: kein Dokument erzeugt")
    return files, pdf_bytes, doc_bytes


def send_bundle(invoices, *, test_mode=False, test_recipient=None):
    """Versendet ``invoices`` (ein Kontakt) in einer E-Mail. Committet bei
    Erfolg. Gibt den Empfaenger zurueck; wirft ``BundleError``."""
    from flask_mail import Message
    from app.email_suppression import suppression_notice
    from app.email_tracking import record_email_sent
    from app.invoices.archive import archive_invoice_file
    from app.invoices.send_email_hooks import read_message_id, run_before_send
    from app.invoices.services import create_or_update_open_item
    from app.settings_service import send_mail

    if not invoices:
        raise BundleError("Keine Belege ausgewählt.")
    customers = {inv.customer_id for inv in invoices}
    if len(customers) != 1:
        raise BundleError("Eine Sammel-E-Mail geht immer nur an einen Kontakt.")
    customer = invoices[0].customer
    if not customer.email:
        raise BundleError("Keine E-Mail-Adresse hinterlegt")
    if not customer.rechnung_per_email:
        raise BundleError("E-Mail-Versand nicht aktiviert")
    if test_mode:
        recipient = test_recipient
        if not recipient:
            raise BundleError("Kein Admin-E-Mail für Testmodus hinterlegt")
    else:
        recipient = customer.email
        notice = suppression_notice(recipient)
        if notice:
            raise BundleError(notice)

    invoices = ordered(invoices)
    primary = invoices[0]
    subject = render_subject(customer, invoices)
    body = render_body(customer, invoices)
    if test_mode:
        subject = f"[TEST – an: {customer.email}] {subject}"
        body = f"[TESTMODUS – eigentlicher Empfänger: {customer.email}]\n\n{body}"
    msg = Message(subject=subject, recipients=[recipient], body=body)
    produced = []
    for inv in invoices:
        files, pdf_bytes, doc_bytes = attachments_for(inv, test_mode=test_mode)
        for f in files:
            msg.attach(*f)
        produced.append((inv, pdf_bytes, doc_bytes))

    run_before_send(primary, msg)
    send_mail(msg)
    if test_mode:
        db.session.rollback()          # eingefrorene XML-Pfade usw. nicht behalten
        return recipient

    message_id = read_message_id(msg)
    for inv, pdf_bytes, doc_bytes in produced:
        if inv.status == Invoice.STATUS_DRAFT:
            inv.status = Invoice.STATUS_SENT
        is_primary = inv.id == primary.id
        record_email_sent(inv, recipient, message_id if is_primary else None)
        inv.mailed_with_invoice_id = None if is_primary else primary.id
        if doc_bytes is not None and inv.status != Invoice.STATUS_DRAFT:
            archive_invoice_file(inv, doc_bytes, "docx", strict=False)
        if pdf_bytes is not None:
            archive_invoice_file(inv, pdf_bytes, "pdf", strict=False)
        create_or_update_open_item(inv)
    db.session.commit()
    return recipient
