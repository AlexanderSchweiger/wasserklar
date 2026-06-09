"""Integration-Tests fuer die E-Mail-Sperrliste (``app.email_suppression``).

Reine Helfer-Logik gegen die SQLite-In-Memory-Test-DB; ``db.create_all()`` zieht
das ``EmailSuppression``-Model automatisch.
"""
from app.extensions import db
from app.models import EmailSuppression
from app.email_suppression import (
    normalize_email, is_suppressed, get_suppression, suppress, unsuppress,
    suppressed_email_set, suppression_notice,
)


class TestNormalize:
    def test_strip_and_lower(self, app):
        assert normalize_email("  Foo@Bar.AT ") == "foo@bar.at"

    def test_empty_is_none(self, app):
        assert normalize_email("") is None
        assert normalize_email("   ") is None
        assert normalize_email(None) is None


class TestSuppress:
    def test_creates_active_row(self, app):
        row = suppress("hans@bad.test", EmailSuppression.REASON_HARD_BOUNCE,
                       detail="no such user")
        db.session.commit()
        assert row.id is not None
        assert row.email == "hans@bad.test"
        assert row.active is True
        assert row.bounce_count == 1
        # Lookup ist case-insensitiv (normalisiert).
        assert is_suppressed("HANS@BAD.test") is True

    def test_idempotent_upsert_increments_count(self, app):
        suppress("a@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        suppress("A@B.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()
        rows = EmailSuppression.query.filter_by(email="a@b.test").all()
        assert len(rows) == 1
        assert rows[0].bounce_count == 2

    def test_reason_escalates_up_not_down(self, app):
        suppress("c@b.test", EmailSuppression.REASON_MANUAL)
        suppress("c@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()
        assert get_suppression("c@b.test").reason == EmailSuppression.REASON_HARD_BOUNCE
        # Schwaecherer Grund stuft NICHT zurueck.
        suppress("c@b.test", EmailSuppression.REASON_MANUAL)
        db.session.commit()
        assert get_suppression("c@b.test").reason == EmailSuppression.REASON_HARD_BOUNCE
        # Spam ist staerker als Hard-Bounce -> stuft hoch.
        suppress("c@b.test", EmailSuppression.REASON_SPAM)
        db.session.commit()
        assert get_suppression("c@b.test").reason == EmailSuppression.REASON_SPAM

    def test_empty_email_is_noop(self, app):
        assert suppress("", EmailSuppression.REASON_MANUAL) is None
        db.session.commit()
        assert EmailSuppression.query.count() == 0


class TestUnsuppress:
    def test_deactivate_keeps_audit_row_and_reactivates(self, app):
        suppress("d@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()
        assert is_suppressed("d@b.test") is True

        assert unsuppress("d@b.test") is True
        db.session.commit()
        assert is_suppressed("d@b.test") is False
        # Zeile bleibt als Audit-Spur erhalten.
        assert get_suppression("d@b.test") is not None

        # Ein neuer echter Bounce reaktiviert die freigegebene Adresse.
        suppress("d@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()
        assert is_suppressed("d@b.test") is True

    def test_unsuppress_unknown_returns_false(self, app):
        assert unsuppress("nope@b.test") is False


class TestSetAndNotice:
    def test_suppressed_email_set_only_active(self, app):
        suppress("x@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        suppress("y@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        unsuppress("y@b.test")
        db.session.commit()
        result = suppressed_email_set(["X@b.test", "y@b.test", "z@b.test"])
        assert result == {"x@b.test"}

    def test_notice_for_active_mentions_block(self, app):
        suppress("n@b.test", EmailSuppression.REASON_SPAM)
        db.session.commit()
        notice = suppression_notice("n@b.test")
        assert notice is not None
        assert "gesperrt" in notice

    def test_no_notice_when_clean_or_released(self, app):
        assert suppression_notice("clean@b.test") is None
        suppress("rel@b.test", EmailSuppression.REASON_HARD_BOUNCE)
        unsuppress("rel@b.test")
        db.session.commit()
        assert suppression_notice("rel@b.test") is None
