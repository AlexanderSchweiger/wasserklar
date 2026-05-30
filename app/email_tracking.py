"""Gemeinsame Logik fuer den E-Mail-Versand-Audit-Trail.

``record_email_sent`` schreibt das "Sent"-Event und setzt die denormalisierten
Tracking-Felder am Subjekt (Rechnung, Zugangscode, ...). Wiederverwendet von
``invoices.routes`` und vom SaaS-Self-Service-Mailer — beide setzen danach
``db.session.commit()`` selbst (hier wird bewusst NICHT committet, damit der
Aufrufer die Transaktion kontrolliert).
"""

from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.models import EmailEvent


def record_email_sent(subject, recipient, message_id=None):
    """Vermerkt einen erfolgreichen Versand am ``subject`` (EmailTrackableMixin).

    Setzt die ``last_email_status='sent'``-Felder und legt einen ``EmailEvent``
    vom Typ ``Sent`` an. ``message_id`` ist die Postmark-MessageID, sofern der
    SaaS-Hook sie vorbelegt hat — bei reinem SMTP None (wird dann vom ersten
    eingehenden Webhook nachgetragen).
    """
    now = datetime.utcnow()
    subject.email_sent_at = now
    subject.email_recipient = recipient
    if message_id:
        subject.email_message_id = message_id
    subject.last_email_status = subject.EMAIL_STATUS_SENT
    subject.last_email_status_at = now
    subject.last_email_bounce_detail = None

    db.session.add(EmailEvent(
        subject_type=subject.EMAIL_SUBJECT_TYPE,
        subject_id=subject.id,
        record_type="Sent",
        postmark_message_id=message_id,
        recipient=recipient,
        occurred_at=now,
        received_at=now,
    ))
