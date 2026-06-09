"""Pro-Tenant-E-Mail-Sperrliste (``EmailSuppression``).

Adressen, die hart gebouncet sind, als Spam markiert oder manuell gesperrt
wurden, werden hier nachgeschlagen, *bevor* eine Kunden-Mail rausgeht — an den
Send-Routen (sofortige, freundliche Fehlermeldung) und im zentralen
``send_mail``-Chokepoint (letztes Netz fuer jeden Versandpfad).

``Customer.wants_email`` bleibt bewusst rein (keine DB-Query) — die
Zustellbarkeit ist eine *separate* Pruefung. Diese Helfer arbeiten auf
``db.session``; der Platform-Webhook spiegelt die Upsert-Logik mit seiner
eigenen Tenant-Session (siehe ``controlplane/webhooks/persist.py``).
"""

from datetime import datetime

from app.extensions import db
from app.models import EmailSuppression


# Reason-Rang fuer die Eskalation: ein staerkerer Grund ueberschreibt einen
# schwaecheren, nie umgekehrt. spam > hard_bounce > manual > smtp_permanent.
_REASON_RANK = {
    EmailSuppression.REASON_SMTP_PERMANENT: 1,
    EmailSuppression.REASON_MANUAL: 2,
    EmailSuppression.REASON_HARD_BOUNCE: 3,
    EmailSuppression.REASON_SPAM: 4,
}


def normalize_email(email):
    """Adresse vergleichbar machen: strip + lowercase. None/leer -> None."""
    if not email:
        return None
    return email.strip().lower() or None


def _escalate_reason(old, new):
    """Liefert den hoeherwertigen der beiden Gruende."""
    if _REASON_RANK.get(new, 0) >= _REASON_RANK.get(old, 0):
        return new
    return old


def is_suppressed(email):
    """True, wenn die (normalisierte) Adresse aktiv gesperrt ist."""
    norm = normalize_email(email)
    if norm is None:
        return False
    return db.session.query(EmailSuppression.id).filter(
        EmailSuppression.email == norm,
        EmailSuppression.active.is_(True),
    ).first() is not None


def get_suppression(email):
    """Die Sperr-Zeile zur Adresse (aktiv oder inaktiv) oder None."""
    norm = normalize_email(email)
    if norm is None:
        return None
    return EmailSuppression.query.filter(EmailSuppression.email == norm).first()


def suppressed_email_set(emails):
    """Set der aktiv gesperrten Adressen aus ``emails`` (1 Query, kein N+1).

    Rueckgabe-Adressen sind normalisiert (lowercase) — im Template also mit
    ``c.email|lower`` vergleichen.
    """
    norms = {n for n in (normalize_email(e) for e in emails) if n}
    if not norms:
        return set()
    rows = db.session.query(EmailSuppression.email).filter(
        EmailSuppression.email.in_(norms),
        EmailSuppression.active.is_(True),
    ).all()
    return {r[0] for r in rows}


def suppression_notice(email):
    """Deutsche Sperr-Meldung fuer Flash/JSON, oder None wenn nicht gesperrt.

    Ein Query (``get_suppression``) deckt Gate-Pruefung *und* Meldungstext ab.
    """
    row = get_suppression(email)
    if row is None or not row.active:
        return None
    return (f"Die E-Mail-Adresse {email} ist als unzustellbar gesperrt "
            f"({row.reason_de}). Bitte korrigieren Sie die Adresse oder geben "
            f"Sie sie im Kontakt wieder frei.")


def suppress(email, reason, *, detail=None, subject=None, occurred_at=None):
    """Idempotenter Upsert einer Sperre. Caller committet.

    Existiert die Adresse schon, wird sie reaktiviert, ``bounce_count`` erhoeht,
    ``last_seen_at`` und ggf. der (hoeherwertige) Grund/Detail nachgezogen.
    ``subject`` ist optional ein ``EmailTrackableMixin``-Objekt (Quelle).
    Gibt die Zeile zurueck oder None, wenn die Adresse leer ist.
    """
    norm = normalize_email(email)
    if norm is None:
        return None
    now = occurred_at or datetime.utcnow()
    clean_detail = (detail or "")[:512] or None
    subj_type = getattr(subject, "EMAIL_SUBJECT_TYPE", None) if subject else None
    subj_id = getattr(subject, "id", None) if subject else None

    row = EmailSuppression.query.filter(EmailSuppression.email == norm).first()
    if row is None:
        row = EmailSuppression(
            email=norm, reason=reason, detail=clean_detail,
            first_seen_at=now, last_seen_at=now, bounce_count=1,
            source_subject_type=subj_type, source_subject_id=subj_id,
            active=True,
        )
        db.session.add(row)
        return row

    row.reason = _escalate_reason(row.reason, reason)
    if clean_detail:
        row.detail = clean_detail
    row.last_seen_at = now
    row.bounce_count = (row.bounce_count or 0) + 1
    row.active = True
    if subj_type and subj_id:
        row.source_subject_type = subj_type
        row.source_subject_id = subj_id
    return row


def unsuppress(email):
    """Markiert die Adresse als freigegeben (``active=False``). Caller committet.

    Gibt True zurueck, wenn eine aktive Sperre aufgehoben wurde. Die Zeile
    bleibt als Audit-Spur erhalten und wird durch einen neuen echten Bounce
    wieder aktiviert.
    """
    norm = normalize_email(email)
    if norm is None:
        return False
    row = EmailSuppression.query.filter(
        EmailSuppression.email == norm,
        EmailSuppression.active.is_(True),
    ).first()
    if row is None:
        return False
    row.active = False
    return True
