"""[oss-v1.50.0] Anteilsbuch der Liegenschaft

* ``property_share_entries``: datierte Bewegungen der Anteile einer
  Liegenschaft (Anfangsbestand, Zubau/Neuanschluss/Stilllegung/Abbruch,
  Teilung/Zusammenlegung, Korrektur) mit Flaeche danach und Zahler.
  ``property_wg_profiles.shares`` ist ab jetzt der Cache der Summe.
* ``wg_share_entries.property_id``: Bezug eines Mitglieder-Eintrags zu einer
  Liegenschaft (nur Information).
* Bestand: je Liegenschaft mit Anteilen oder Flaeche ein Anfangsbestand
  (Datum = frueheste Eigentuemerschaft, sonst Anlagedatum, sonst heute) —
  der Cache stimmt damit sofort.

Regeln in ``app/property_shares.py``. Alle FKs/Indizes benannt.

Revision ID: f6d2b8a4c0e7
Revises: e5c9a3f7b1d4
Create Date: 2026-11-01
"""
from datetime import date, datetime

from alembic import op
import sqlalchemy as sa


revision = "f6d2b8a4c0e7"
down_revision = "e5c9a3f7b1d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "property_share_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=True),
        sa.Column("area_m2", sa.Integer(), nullable=True),
        sa.Column("payer_customer_id", sa.Integer(), nullable=True),
        sa.Column("counterparty_property_id", sa.Integer(), nullable=True),
        sa.Column("pair_entry_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default=sa.text("'manual'")),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"],
                                name="fk_property_share_entries_property"),
        sa.ForeignKeyConstraint(["payer_customer_id"], ["customers.id"],
                                name="fk_property_share_entries_payer",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["counterparty_property_id"], ["properties.id"],
                                name="fk_property_share_entries_counterparty",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pair_entry_id"], ["property_share_entries.id"],
                                name="fk_property_share_entries_pair",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"],
                                name="fk_property_share_entries_created_by",
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("property_share_entries", schema=None) as batch_op:
        batch_op.create_index("ix_property_share_entries_property_date",
                              ["property_id", "entry_date"], unique=False)
        batch_op.create_index("ix_property_share_entries_payer_customer_id",
                              ["payer_customer_id"], unique=False)
        batch_op.create_index("ix_property_share_entries_counterparty_property_id",
                              ["counterparty_property_id"], unique=False)
        batch_op.create_index("ix_property_share_entries_pair_entry_id",
                              ["pair_entry_id"], unique=False)

    with op.batch_alter_table("wg_share_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("property_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_wg_share_entries_property", "properties",
                                    ["property_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_wg_share_entries_property_id", ["property_id"],
                              unique=False)

    _take_over_existing_shares()


def _take_over_existing_shares():
    bind = op.get_bind()
    meta = sa.MetaData()
    profiles = sa.Table("property_wg_profiles", meta,
                        sa.Column("property_id", sa.Integer),
                        sa.Column("shares", sa.Integer),
                        sa.Column("area_m2", sa.Integer))
    properties = sa.Table("properties", meta,
                          sa.Column("id", sa.Integer),
                          sa.Column("created_at", sa.DateTime))
    ownerships = sa.Table("property_ownerships", meta,
                          sa.Column("property_id", sa.Integer),
                          sa.Column("valid_from", sa.Date))
    entries = sa.Table("property_share_entries", meta,
                       sa.Column("property_id", sa.Integer),
                       sa.Column("entry_date", sa.Date),
                       sa.Column("quantity", sa.Integer),
                       sa.Column("kind", sa.String(20)),
                       sa.Column("area_m2", sa.Integer),
                       sa.Column("source", sa.String(20)),
                       sa.Column("note", sa.String(255)),
                       sa.Column("created_at", sa.DateTime))

    first_owner = dict(bind.execute(
        sa.select(ownerships.c.property_id, sa.func.min(ownerships.c.valid_from))
        .group_by(ownerships.c.property_id)).all())
    created = dict(bind.execute(
        sa.select(properties.c.id, properties.c.created_at)).all())
    today, now = date.today(), datetime.utcnow()
    rows = []
    for pid, shares, area in bind.execute(
            sa.select(profiles.c.property_id, profiles.c.shares, profiles.c.area_m2)).all():
        shares = int(shares or 0)
        if shares <= 0 and area is None:
            continue
        day = _as_date(first_owner.get(pid)) or _as_date(created.get(pid)) or today
        rows.append({
            "property_id": pid, "entry_date": min(day, today), "quantity": max(shares, 0),
            "kind": "opening", "area_m2": area, "source": "migration",
            "note": "Übernahme des bisherigen Bestands", "created_at": now,
        })
    if rows:
        bind.execute(entries.insert(), rows)


def _as_date(value):
    """SQLite liefert in einer Roh-Abfrage ggf. Strings statt date/datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def downgrade():
    with op.batch_alter_table("wg_share_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_wg_share_entries_property_id")
        batch_op.drop_constraint("fk_wg_share_entries_property", type_="foreignkey")
        batch_op.drop_column("property_id")
    with op.batch_alter_table("property_share_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_property_share_entries_pair_entry_id")
        batch_op.drop_index("ix_property_share_entries_counterparty_property_id")
        batch_op.drop_index("ix_property_share_entries_payer_customer_id")
        batch_op.drop_index("ix_property_share_entries_property_date")
    op.drop_table("property_share_entries")
