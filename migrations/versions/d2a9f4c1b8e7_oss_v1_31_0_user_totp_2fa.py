"""[oss-v1.31.0] add user totp 2fa columns

Fuegt der ``users``-Tabelle die Felder fuer die optionale, pro User aktivierbare
Zwei-Faktor-Authentifizierung (TOTP, RFC 6238) hinzu:

- ``totp_secret_enc``      Fernet-verschluesseltes Base32-Secret (WASSERKLAR_MAIL_KEY)
- ``totp_enabled``         Schalter, ob 2FA fuer den User aktiv ist
- ``totp_recovery_codes``  JSON-Liste werkzeug-gehashter Einmal-Codes
- ``totp_failed_attempts`` Fehlversuchszaehler fuer den Verify-Schritt
- ``totp_locked_until``    temporaere Sperre nach zu vielen Fehlversuchen

Rein additiv. Die beiden NOT-NULL-Spalten tragen ein ``server_default``
(``false`` / ``0``), damit Bestandszeilen beim Upgrade sauber befuellt werden.
Dialekt-portabel (SQLite/MariaDB/Postgres), ``batch_alter_table`` fuer SQLite.

ACHTUNG bei Key-Rotation: ``totp_secret_enc`` haengt an ``WASSERKLAR_MAIL_KEY``.
Wird der Key rotiert, muss der alte Key in der MultiFernet-Komma-Liste bleiben,
bis ein Re-Encrypt-Pass lief — sonst werden alle TOTP-Secrets undecryptbar
(Recovery-Codes greifen weiterhin, da key-unabhaengig gehasht).

Revision ID: d2a9f4c1b8e7
Revises: c8e4a1f7d9b3
Create Date: 2026-06-19

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2a9f4c1b8e7'
down_revision = 'c8e4a1f7d9b3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret_enc', sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column('totp_recovery_codes', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column('totp_failed_attempts', sa.Integer(), nullable=False, server_default='0')
        )
        batch_op.add_column(sa.Column('totp_locked_until', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('totp_locked_until')
        batch_op.drop_column('totp_failed_attempts')
        batch_op.drop_column('totp_recovery_codes')
        batch_op.drop_column('totp_enabled')
        batch_op.drop_column('totp_secret_enc')
