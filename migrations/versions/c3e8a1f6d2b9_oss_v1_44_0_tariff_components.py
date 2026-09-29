"""[oss-v1.44.0] Tarifpositionen: charge_types, tariff_components, charge_overrides

Ein Wassertarif war bisher eine feste Spaltengruppe (Preis/m³, Grundgebuehr,
Zusatzgebuehr + Labels + Konten); die Ueberschreibungen standen als feste
Spalten an Kunde/Objekt. Neu:

* ``charge_types`` — Gebuehrenarten-Katalog (System: ``water``,
  ``water_levy`` = Wassercent, ``base_fee``, ``additional_fee``; eigene Arten
  moeglich). Stabiler Anker der Ueberschreibungen.
* ``tariff_components`` — die Positionen eines Tarifs (Betrag, USt-Satz,
  Konto, optional ``valid_from``).
* ``charge_overrides`` — individuelle Gebuehr je Gebuehrenart fuer einen
  Kunden ODER ein Objekt (Betrag ersetzt, NULL = entfaellt).
* ``invoice_items.charge_key``, ``reading_corrections.charge_key/label``,
  ``billing_runs.tariff_snapshot``.

Datenmigration (verlustfrei): je Tarif drei Positionen aus den Altspalten —
Grund-/Zusatzgebuehr AUCH ohne Betrag, damit bestehende Overrides exakt wie
bisher eine Position erzeugen. USt je Position = bisheriges Verhalten (der
Wasser-Satz des Landes; er greift weiter nur in USt-pflichtigen Jahren).
Danach werden die Altspalten entfernt; ``downgrade()`` baut sie aus den
Positionen wieder auf (ein Override „entfällt" ist dort nicht darstellbar und
wird zu „erbt").

Dialekt-portabel: SQLAlchemy Core, alle NOT-NULL-Spalten explizit gesetzt
(Roh-INSERTs ignorieren Python-Defaults), ``batch_alter_table`` fuer SQLite.

Revision ID: c3e8a1f6d2b9
Revises: b7d1e4a9c2f3
Create Date: 2026-09-28 00:00:01.000000

"""
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3e8a1f6d2b9'
down_revision = 'b7d1e4a9c2f3'
branch_labels = None
depends_on = None


# Bewusst hier dupliziert statt aus app.country importiert: eine Migration
# muss auch dann noch laufen, wenn sich der App-Code spaeter aendert.
_WATER_RATE_BY_COUNTRY = {"AT": "10", "DE": "7"}
_LEVY_COUNTRIES = {"DE"}

# (key, label, calc_type, is_levy, overridable, sort_order)
_SYSTEM_CHARGE_TYPES = (
    ("water", "Wasserverbrauch", "per_m3", False, False, 10),
    ("water_levy", "Wasserentnahmeentgelt", "per_m3", True, False, 15),
    ("base_fee", "Grundgebühr", "flat", False, True, 20),
    ("additional_fee", "Zusatzgebühr", "flat", False, True, 30),
)


# Core-Tabellen statt Roh-SQL: ``key`` ist in MySQL/MariaDB ein reserviertes
# Wort — SQLAlchemy quotet es je Dialekt korrekt, ein text()-Statement nicht.
_app_settings = sa.table('app_settings', sa.column('key', sa.String),
                         sa.column('value', sa.Text))
_charge_types = sa.table('charge_types', sa.column('id', sa.Integer),
                         sa.column('key', sa.String))


def _setting(bind, key):
    row = bind.execute(
        sa.select(_app_settings.c.value).where(_app_settings.c.key == key)).first()
    return row[0] if row else None


def _country(bind):
    code = (_setting(bind, "org.country") or os.environ.get("DEFAULT_COUNTRY") or "AT")
    code = code.strip().upper()
    return code if code in _WATER_RATE_BY_COUNTRY else "AT"


def upgrade():
    bind = op.get_bind()

    # 1. Gebuehrenarten
    op.create_table(
        'charge_types',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=40), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('calc_type', sa.String(length=10), nullable=False,
                  server_default=sa.text("'flat'")),
        sa.Column('is_levy', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('overridable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default=sa.text('100')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key', name='uq_charge_types_key'),
    )

    # 2. Tarifpositionen
    op.create_table(
        'tariff_components',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tariff_id', sa.Integer(), nullable=False),
        sa.Column('charge_type_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('amount', sa.Numeric(10, 4), nullable=True),
        sa.Column('tax_rate', sa.Numeric(5, 2), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('valid_from', sa.Date(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default=sa.text('100')),
        sa.ForeignKeyConstraint(['tariff_id'], ['water_tariffs.id'], ondelete='CASCADE',
                                name='fk_tariff_components_tariff_id'),
        sa.ForeignKeyConstraint(['charge_type_id'], ['charge_types.id'],
                                name='fk_tariff_components_charge_type_id'),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'],
                                name='fk_tariff_components_account_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tariff_id', 'charge_type_id',
                            name='uq_tariff_components_tariff_charge'),
    )
    op.create_index('ix_tariff_components_tariff_id', 'tariff_components', ['tariff_id'])

    # 3. Individuelle Gebuehren
    op.create_table(
        'charge_overrides',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('charge_type_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('property_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(10, 4), nullable=True),
        sa.Column('note', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['charge_type_id'], ['charge_types.id'], ondelete='CASCADE',
                                name='fk_charge_overrides_charge_type_id'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE',
                                name='fk_charge_overrides_customer_id'),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='CASCADE',
                                name='fk_charge_overrides_property_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('charge_type_id', 'customer_id',
                            name='uq_charge_overrides_customer'),
        sa.UniqueConstraint('charge_type_id', 'property_id',
                            name='uq_charge_overrides_property'),
    )
    op.create_index('ix_charge_overrides_customer_id', 'charge_overrides', ['customer_id'])
    op.create_index('ix_charge_overrides_property_id', 'charge_overrides', ['property_id'])

    # 4. Neue Spalten an bestehenden Tabellen
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('charge_key', sa.String(length=40), nullable=True))
    with op.batch_alter_table('billing_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tariff_snapshot', sa.Text(), nullable=True))
    with op.batch_alter_table('reading_corrections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('charge_key', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('label', sa.String(length=100), nullable=True))

    # 5. Daten
    country = _country(bind)
    try:
        water_rate = Decimal(str(_setting(bind, "tax.water_rate") or ""))
    except (InvalidOperation, ValueError):
        water_rate = Decimal(_WATER_RATE_BY_COUNTRY[country])

    ct_table = sa.table(
        'charge_types',
        sa.column('key', sa.String), sa.column('label', sa.String),
        sa.column('calc_type', sa.String), sa.column('is_levy', sa.Boolean),
        sa.column('overridable', sa.Boolean), sa.column('is_system', sa.Boolean),
        sa.column('active', sa.Boolean), sa.column('sort_order', sa.Integer),
    )
    op.bulk_insert(ct_table, [
        {"key": key, "label": label, "calc_type": calc, "is_levy": levy,
         "overridable": overridable, "is_system": True,
         "active": (country in _LEVY_COUNTRIES) if key == "water_levy" else True,
         "sort_order": sort}
        for key, label, calc, levy, overridable, sort in _SYSTEM_CHARGE_TYPES
    ])
    ct_ids = {k: i for (i, k) in bind.execute(
        sa.select(_charge_types.c.id, _charge_types.c.key))}

    comp_table = sa.table(
        'tariff_components',
        sa.column('tariff_id', sa.Integer), sa.column('charge_type_id', sa.Integer),
        sa.column('label', sa.String), sa.column('amount', sa.Numeric(10, 4)),
        sa.column('tax_rate', sa.Numeric(5, 2)), sa.column('account_id', sa.Integer),
        sa.column('sort_order', sa.Integer),
    )
    comps = []
    tariffs = bind.execute(sa.text(
        "SELECT id, price_per_m3, price_per_m3_account_id, "
        "base_fee, base_fee_label, base_fee_account_id, "
        "additional_fee, additional_fee_label, additional_fee_account_id "
        "FROM water_tariffs")).fetchall()
    for (tid, price, price_acc, base, base_label, base_acc,
         add, add_label, add_acc) in tariffs:
        comps.append({"tariff_id": tid, "charge_type_id": ct_ids["water"],
                      "label": "Wasserverbrauch", "amount": price,
                      "tax_rate": water_rate, "account_id": price_acc, "sort_order": 10})
        comps.append({"tariff_id": tid, "charge_type_id": ct_ids["base_fee"],
                      "label": base_label or "Grundgebühr", "amount": base,
                      "tax_rate": water_rate, "account_id": base_acc, "sort_order": 20})
        comps.append({"tariff_id": tid, "charge_type_id": ct_ids["additional_fee"],
                      "label": add_label or "Zusatzgebühr", "amount": add,
                      "tax_rate": water_rate, "account_id": add_acc, "sort_order": 30})
    if comps:
        op.bulk_insert(comp_table, comps)

    ov_table = sa.table(
        'charge_overrides',
        sa.column('charge_type_id', sa.Integer), sa.column('customer_id', sa.Integer),
        sa.column('property_id', sa.Integer), sa.column('amount', sa.Numeric(10, 4)),
        sa.column('created_at', sa.DateTime),
    )
    now = datetime.utcnow()
    overrides = []
    for table, fk in (("customers", "customer_id"), ("properties", "property_id")):
        rows = bind.execute(sa.text(
            f"SELECT id, base_fee_override, additional_fee_override FROM {table} "
            "WHERE base_fee_override IS NOT NULL OR additional_fee_override IS NOT NULL"
        )).fetchall()
        for (oid, base_ov, add_ov) in rows:
            for key, value in (("base_fee", base_ov), ("additional_fee", add_ov)):
                if value is None:
                    continue
                row = {"charge_type_id": ct_ids[key], "customer_id": None,
                       "property_id": None, "amount": value, "created_at": now}
                row[fk] = oid
                overrides.append(row)
    if overrides:
        op.bulk_insert(ov_table, overrides)

    # 6. Altspalten entfernen
    with op.batch_alter_table('water_tariffs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_water_tariffs_price_per_m3_account_id', type_='foreignkey')
        batch_op.drop_constraint('fk_water_tariffs_additional_fee_account_id', type_='foreignkey')
        batch_op.drop_constraint('fk_water_tariffs_base_fee_account_id', type_='foreignkey')
        batch_op.drop_column('price_per_m3_account_id')
        batch_op.drop_column('additional_fee_account_id')
        batch_op.drop_column('base_fee_account_id')
        batch_op.drop_column('price_per_m3')
        batch_op.drop_column('additional_fee_label')
        batch_op.drop_column('additional_fee')
        batch_op.drop_column('base_fee_label')
        batch_op.drop_column('base_fee')
    for table in ('customers', 'properties'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('additional_fee_override')
            batch_op.drop_column('base_fee_override')


def downgrade():
    bind = op.get_bind()

    # 1. Altspalten wieder anlegen (price_per_m3 erst nullable, nach dem Befuellen NOT NULL)
    with op.batch_alter_table('water_tariffs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('base_fee', sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column('base_fee_label', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('additional_fee', sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column('additional_fee_label', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('price_per_m3', sa.Numeric(10, 4), nullable=True))
        batch_op.add_column(sa.Column('base_fee_account_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('additional_fee_account_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('price_per_m3_account_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_water_tariffs_base_fee_account_id', 'accounts', ['base_fee_account_id'], ['id'])
        batch_op.create_foreign_key(
            'fk_water_tariffs_additional_fee_account_id', 'accounts',
            ['additional_fee_account_id'], ['id'])
        batch_op.create_foreign_key(
            'fk_water_tariffs_price_per_m3_account_id', 'accounts',
            ['price_per_m3_account_id'], ['id'])
    for table in ('customers', 'properties'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('base_fee_override', sa.Numeric(10, 2), nullable=True))
            batch_op.add_column(sa.Column('additional_fee_override', sa.Numeric(10, 2), nullable=True))

    # 2. Aus den Positionen zurueckschreiben
    ct_keys = {i: k for (i, k) in bind.execute(
        sa.select(_charge_types.c.id, _charge_types.c.key))}
    rows = [
        (tid, ct_keys.get(ctid), label, amount, account_id)
        for (tid, ctid, label, amount, account_id) in bind.execute(sa.text(
            "SELECT tariff_id, charge_type_id, label, amount, account_id "
            "FROM tariff_components")).fetchall()
        if ct_keys.get(ctid) in ("water", "base_fee", "additional_fee")
    ]
    for (tid, key, label, amount, account_id) in rows:
        if key == "water":
            bind.execute(sa.text(
                "UPDATE water_tariffs SET price_per_m3 = :a, price_per_m3_account_id = :acc "
                "WHERE id = :t"), {"a": amount, "acc": account_id, "t": tid})
        else:
            bind.execute(sa.text(
                f"UPDATE water_tariffs SET {key} = :a, {key}_label = :l, "
                f"{key}_account_id = :acc WHERE id = :t"),
                {"a": amount, "l": label, "acc": account_id, "t": tid})
    bind.execute(sa.text("UPDATE water_tariffs SET price_per_m3 = 0 WHERE price_per_m3 IS NULL"))

    ov_rows = [
        (cid, pid, ct_keys.get(ctid), amount)
        for (cid, pid, ctid, amount) in bind.execute(sa.text(
            "SELECT customer_id, property_id, charge_type_id, amount "
            "FROM charge_overrides WHERE amount IS NOT NULL")).fetchall()
        if ct_keys.get(ctid) in ("base_fee", "additional_fee")
    ]
    for (cid, pid, key, amount) in ov_rows:
        table, oid = ("customers", cid) if cid is not None else ("properties", pid)
        bind.execute(sa.text(f"UPDATE {table} SET {key}_override = :a WHERE id = :o"),
                     {"a": amount, "o": oid})

    with op.batch_alter_table('water_tariffs', schema=None) as batch_op:
        batch_op.alter_column('price_per_m3', existing_type=sa.Numeric(10, 4), nullable=False)

    # 3. Neue Spalten + Tabellen entfernen
    with op.batch_alter_table('reading_corrections', schema=None) as batch_op:
        batch_op.drop_column('label')
        batch_op.drop_column('charge_key')
    with op.batch_alter_table('billing_runs', schema=None) as batch_op:
        batch_op.drop_column('tariff_snapshot')
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.drop_column('charge_key')

    op.drop_index('ix_charge_overrides_property_id', table_name='charge_overrides')
    op.drop_index('ix_charge_overrides_customer_id', table_name='charge_overrides')
    op.drop_table('charge_overrides')
    op.drop_index('ix_tariff_components_tariff_id', table_name='tariff_components')
    op.drop_table('tariff_components')
    op.drop_table('charge_types')
