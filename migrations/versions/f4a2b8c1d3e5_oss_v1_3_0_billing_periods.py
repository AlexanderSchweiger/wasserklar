"""[oss-v1.3.0] Abrechnungsperioden statt Kalenderjahr

Revision ID: f4a2b8c1d3e5
Revises: b4e7d9a1c8f3
Create Date: 2026-05-17 10:00:00.000000

Fuehrt die Tabelle ``billing_periods`` ein und stellt Zaehlerablesungen,
Rechnungslaeufe, Rechnungen und Self-Service-Zugangscodes von einer
Integer-Jahreszahl auf eine FK-Referenz darauf um:

  - ``meter_readings``: ``year`` -> ``billing_period_id`` (NOT NULL);
    ``reading_date`` wird NOT NULL; Unique ``uq_meter_year`` ->
    ``uq_meter_reading_period``.
  - ``billing_runs``:  ``period_year`` -> ``billing_period_id`` (NOT NULL).
  - ``invoices``:      ``period_year`` -> ``billing_period_id`` (nullable —
    manuelle Rechnungen haben keine Periode).
  - ``meter_reading_access_codes``: ``year`` -> ``billing_period_id``
    (NOT NULL); Unique/Index entsprechend.

Backfill: Jedes vorkommende Kalenderjahr wird zu einer ``BillingPeriod``
(Name = Jahreszahl, 01.01.–31.12.); das juengste Jahr wird aktiv gesetzt.
Existieren keine Jahre (frische DB), wird eine Periode fuer das laufende
Kalenderjahr angelegt.

Manuell geschrieben (nicht autogeneriert): Backfill laeuft Python-seitig
(dialekt-portabel SQLite/MariaDB/Postgres), Schema-Aenderungen ueber
``batch_alter_table`` (SQLite-Pflicht). ``downgrade`` ist verlustbehaftet
— Periodennamen wie "2025/26" werden auf das Start-Jahr reduziert.
"""
from datetime import date, datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4a2b8c1d3e5'
down_revision = 'b4e7d9a1c8f3'
branch_labels = None
depends_on = None


def _collect_years(bind) -> set[int]:
    """Distinkte Kalenderjahre aus allen jahresbasierten Spalten sammeln."""
    years: set[int] = set()
    queries = (
        "SELECT DISTINCT year FROM meter_readings WHERE year IS NOT NULL",
        "SELECT DISTINCT period_year FROM billing_runs WHERE period_year IS NOT NULL",
        "SELECT DISTINCT period_year FROM invoices WHERE period_year IS NOT NULL",
        "SELECT DISTINCT year FROM meter_reading_access_codes WHERE year IS NOT NULL",
    )
    for sql in queries:
        for (y,) in bind.execute(sa.text(sql)):
            if y is not None:
                years.add(int(y))
    return years


def upgrade():
    bind = op.get_bind()

    # 1. billing_periods anlegen.
    op.create_table(
        'billing_periods',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_billing_periods_name'),
    )

    # 2. Pro vorkommendem Kalenderjahr eine Periode anlegen (juengstes aktiv).
    years = sorted(_collect_years(bind))
    if not years:
        years = [date.today().year]
    active_year = max(years)
    bp_table = sa.table(
        'billing_periods',
        sa.column('name', sa.String),
        sa.column('start_date', sa.Date),
        sa.column('end_date', sa.Date),
        sa.column('active', sa.Boolean),
        sa.column('created_at', sa.DateTime),
    )
    now = datetime.utcnow()
    op.bulk_insert(bp_table, [
        {
            'name': str(y),
            'start_date': date(y, 1, 1),
            'end_date': date(y, 12, 31),
            'active': (y == active_year),
            'created_at': now,
        }
        for y in years
    ])

    # year -> billing_period_id Map (Name == Jahreszahl).
    year_to_pid: dict[int, int] = {}
    for (pid, name) in bind.execute(sa.text("SELECT id, name FROM billing_periods")):
        try:
            year_to_pid[int(name)] = pid
        except (TypeError, ValueError):
            pass

    # 3. meter_readings.
    op.add_column('meter_readings',
                  sa.Column('billing_period_id', sa.Integer(), nullable=True))
    for y, pid in year_to_pid.items():
        bind.execute(
            sa.text("UPDATE meter_readings SET billing_period_id = :pid "
                    "WHERE year = :y"),
            {"pid": pid, "y": y},
        )
        bind.execute(
            sa.text("UPDATE meter_readings SET reading_date = :d "
                    "WHERE year = :y AND reading_date IS NULL"),
            {"d": date(y, 12, 31), "y": y},
        )
    # On MySQL, batch_alter_table issues a compound ALTER TABLE where DROP INDEX
    # is evaluated before ADD UNIQUE in the same statement, so uq_meter_year
    # (the FK backing index for meter_id) cannot be dropped even if the new
    # unique is listed first in the batch.  Create the replacement index as a
    # separate DDL statement first; MySQL will then let us drop uq_meter_year.
    op.create_index('uq_meter_reading_period', 'meter_readings',
                    ['meter_id', 'billing_period_id'], unique=True)
    with op.batch_alter_table('meter_readings') as batch_op:
        batch_op.alter_column('reading_date', existing_type=sa.Date(),
                              nullable=False)
        batch_op.alter_column('billing_period_id', existing_type=sa.Integer(),
                              nullable=False)
        batch_op.drop_constraint('uq_meter_year', type_='unique')
        batch_op.create_foreign_key(
            'fk_meter_readings_billing_period_id', 'billing_periods',
            ['billing_period_id'], ['id'])
        batch_op.create_index('ix_meter_readings_billing_period_id',
                              ['billing_period_id'])
        batch_op.drop_column('year')

    # 4. billing_runs.
    op.add_column('billing_runs',
                  sa.Column('billing_period_id', sa.Integer(), nullable=True))
    for y, pid in year_to_pid.items():
        bind.execute(
            sa.text("UPDATE billing_runs SET billing_period_id = :pid "
                    "WHERE period_year = :y"),
            {"pid": pid, "y": y},
        )
    with op.batch_alter_table('billing_runs') as batch_op:
        batch_op.alter_column('billing_period_id', existing_type=sa.Integer(),
                              nullable=False)
        batch_op.create_foreign_key(
            'fk_billing_runs_billing_period_id', 'billing_periods',
            ['billing_period_id'], ['id'])
        batch_op.drop_column('period_year')

    # 5. invoices (billing_period_id bleibt nullable).
    op.add_column('invoices',
                  sa.Column('billing_period_id', sa.Integer(), nullable=True))
    for y, pid in year_to_pid.items():
        bind.execute(
            sa.text("UPDATE invoices SET billing_period_id = :pid "
                    "WHERE period_year = :y"),
            {"pid": pid, "y": y},
        )
    with op.batch_alter_table('invoices') as batch_op:
        batch_op.create_foreign_key(
            'fk_invoices_billing_period_id', 'billing_periods',
            ['billing_period_id'], ['id'])
        batch_op.drop_column('period_year')

    # 6. meter_reading_access_codes.
    op.add_column('meter_reading_access_codes',
                  sa.Column('billing_period_id', sa.Integer(), nullable=True))
    for y, pid in year_to_pid.items():
        bind.execute(
            sa.text("UPDATE meter_reading_access_codes SET billing_period_id = :pid "
                    "WHERE year = :y"),
            {"pid": pid, "y": y},
        )
    op.drop_index('ix_mrac_year_revoked_expires',
                  table_name='meter_reading_access_codes')
    op.drop_index('ix_meter_reading_access_codes_year',
                  table_name='meter_reading_access_codes')
    op.create_index('uq_mrac_customer_period', 'meter_reading_access_codes',
                    ['customer_id', 'billing_period_id'], unique=True)
    with op.batch_alter_table('meter_reading_access_codes') as batch_op:
        batch_op.alter_column('billing_period_id', existing_type=sa.Integer(),
                              nullable=False)
        batch_op.drop_constraint('uq_mrac_customer_year', type_='unique')
        batch_op.create_foreign_key(
            'fk_mrac_billing_period_id', 'billing_periods',
            ['billing_period_id'], ['id'])
        batch_op.create_index('ix_meter_reading_access_codes_billing_period_id',
                              ['billing_period_id'])
        batch_op.create_index(
            'ix_mrac_period_revoked_expires',
            ['billing_period_id', 'revoked_at', 'expires_at'])
        batch_op.drop_column('year')


def downgrade():
    """Spiegelbildlich. Verlustbehaftet: Periodennamen werden auf das
    Start-Jahr reduziert; mehrere Perioden im selben Kalenderjahr koennen
    den ``uq_meter_year``/``uq_mrac_customer_year``-Constraint verletzen."""
    bind = op.get_bind()

    pid_to_year: dict[int, int] = {}
    for (pid, sd) in bind.execute(
        sa.text("SELECT id, start_date FROM billing_periods")
    ):
        if hasattr(sd, "year"):
            pid_to_year[pid] = sd.year
        elif sd:
            pid_to_year[pid] = int(str(sd)[:4])

    # meter_reading_access_codes.
    op.add_column('meter_reading_access_codes',
                  sa.Column('year', sa.Integer(), nullable=True))
    for pid, y in pid_to_year.items():
        bind.execute(
            sa.text("UPDATE meter_reading_access_codes SET year = :y "
                    "WHERE billing_period_id = :pid"),
            {"y": y, "pid": pid},
        )
    with op.batch_alter_table('meter_reading_access_codes') as batch_op:
        batch_op.alter_column('year', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_constraint('fk_mrac_billing_period_id', type_='foreignkey')
        batch_op.drop_index('ix_meter_reading_access_codes_billing_period_id')
        batch_op.drop_index('ix_mrac_period_revoked_expires')
        # uq_mrac_customer_period was created as op.create_index (not constraint)
        batch_op.drop_index('uq_mrac_customer_period')
        batch_op.create_unique_constraint(
            'uq_mrac_customer_year', ['customer_id', 'year'])
        batch_op.drop_column('billing_period_id')
    op.create_index('ix_meter_reading_access_codes_year',
                    'meter_reading_access_codes', ['year'])
    op.create_index('ix_mrac_year_revoked_expires',
                    'meter_reading_access_codes',
                    ['year', 'revoked_at', 'expires_at'])

    # invoices.
    op.add_column('invoices',
                  sa.Column('period_year', sa.Integer(), nullable=True))
    for pid, y in pid_to_year.items():
        bind.execute(
            sa.text("UPDATE invoices SET period_year = :y "
                    "WHERE billing_period_id = :pid"),
            {"y": y, "pid": pid},
        )
    with op.batch_alter_table('invoices') as batch_op:
        batch_op.drop_constraint('fk_invoices_billing_period_id',
                                 type_='foreignkey')
        batch_op.drop_column('billing_period_id')

    # billing_runs.
    op.add_column('billing_runs',
                  sa.Column('period_year', sa.Integer(), nullable=True))
    for pid, y in pid_to_year.items():
        bind.execute(
            sa.text("UPDATE billing_runs SET period_year = :y "
                    "WHERE billing_period_id = :pid"),
            {"y": y, "pid": pid},
        )
    with op.batch_alter_table('billing_runs') as batch_op:
        batch_op.alter_column('period_year', existing_type=sa.Integer(),
                              nullable=False)
        batch_op.drop_constraint('fk_billing_runs_billing_period_id',
                                 type_='foreignkey')
        batch_op.drop_column('billing_period_id')

    # meter_readings.
    op.add_column('meter_readings',
                  sa.Column('year', sa.Integer(), nullable=True))
    for pid, y in pid_to_year.items():
        bind.execute(
            sa.text("UPDATE meter_readings SET year = :y "
                    "WHERE billing_period_id = :pid"),
            {"y": y, "pid": pid},
        )
    with op.batch_alter_table('meter_readings') as batch_op:
        batch_op.alter_column('year', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_constraint('fk_meter_readings_billing_period_id',
                                 type_='foreignkey')
        batch_op.drop_index('ix_meter_readings_billing_period_id')
        # uq_meter_reading_period was created as op.create_index (not constraint)
        batch_op.drop_index('uq_meter_reading_period')
        batch_op.create_unique_constraint('uq_meter_year', ['meter_id', 'year'])
        batch_op.alter_column('reading_date', existing_type=sa.Date(),
                              nullable=True)
        batch_op.drop_column('billing_period_id')

    op.drop_table('billing_periods')
