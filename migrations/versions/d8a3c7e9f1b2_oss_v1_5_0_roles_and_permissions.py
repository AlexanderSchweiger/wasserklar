"""[oss-v1.5.0] Rollen und Berechtigungen

Revision ID: d8a3c7e9f1b2
Revises: c7e1f9a2b3d4
Create Date: 2026-05-22 12:00:00.000000

Fuehrt eine feingranulare Berechtigungsverwaltung ein:

  - Neue Tabellen ``roles`` und ``role_permissions``.
  - Seed der drei Standard-Rollen: Admin (alle Rechte, ``is_system=True``),
    Kassier (Buchhaltung, Rechnungen/OP, Mahnwesen, Auswertungen),
    Zaehlerverwalter (Zaehler).
  - ``users.role`` (String) wird durch ``users.role_id`` (FK -> roles.id)
    ersetzt. Bestehende User werden alle der Admin-Rolle zugeordnet
    (Entscheidung: kein Bruch fuer bestehende Installationen — der Admin
    weist danach die richtigen Rollen manuell zu).

Dialect-portabel: ALTER COLUMN und DROP COLUMN ueber ``batch_alter_table``
(SQLite-Pflicht). Seed der Standard-Rollen erfolgt mit ``op.bulk_insert``.
``downgrade`` rekonstruiert den ``role``-String aus ``Role.name``
(Admin -> 'admin', alles andere -> 'user').
"""
from datetime import datetime
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8a3c7e9f1b2'
down_revision = 'c7e1f9a2b3d4'
branch_labels = None
depends_on = None


# Permission-Keys (Spiegel von app/auth/permissions.py — bewusst dupliziert,
# damit die Migration unabhaengig vom Applicationscode laeuft).
_PERM_STAMMDATEN = "stammdaten"
_PERM_ZAEHLER = "zaehler"
_PERM_BUCHHALTUNG = "buchhaltung"
_PERM_RECHNUNGEN = "rechnungen_op"
_PERM_MAHNWESEN = "mahnwesen"
_PERM_AUSWERTUNGEN = "auswertungen"
_PERM_VERWALTUNG = "verwaltung"

_ALL_PERMS = [
    _PERM_STAMMDATEN, _PERM_ZAEHLER, _PERM_BUCHHALTUNG, _PERM_RECHNUNGEN,
    _PERM_MAHNWESEN, _PERM_AUSWERTUNGEN, _PERM_VERWALTUNG,
]

_KASSIER_PERMS = [
    _PERM_BUCHHALTUNG, _PERM_RECHNUNGEN, _PERM_MAHNWESEN, _PERM_AUSWERTUNGEN,
]

_ZAEHLERVERWALTER_PERMS = [_PERM_ZAEHLER]


def upgrade():
    # 1) Tabellen anlegen
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_key", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_key"),
    )

    # 2) Standard-Rollen seeden
    bind = op.get_bind()
    now = datetime.utcnow()

    roles_table = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("is_system", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    op.bulk_insert(roles_table, [
        {"name": "Admin", "description": "Vollzugriff auf alle Bereiche",
         "is_system": True, "created_at": now},
        {"name": "Kassier",
         "description": "Buchhaltung, Rechnungen/OP, Mahnwesen und Auswertungen",
         "is_system": False, "created_at": now},
        {"name": "Zählerverwalter",
         "description": "Verwaltung von Zählern und Ablesungen",
         "is_system": False, "created_at": now},
    ])

    def _role_id(name):
        return bind.execute(
            sa.text("SELECT id FROM roles WHERE name = :n"), {"n": name}
        ).scalar()

    admin_id = _role_id("Admin")
    kassier_id = _role_id("Kassier")
    zv_id = _role_id("Zählerverwalter")

    rp_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission_key", sa.String),
    )
    rows = (
        [{"role_id": admin_id, "permission_key": p} for p in _ALL_PERMS]
        + [{"role_id": kassier_id, "permission_key": p} for p in _KASSIER_PERMS]
        + [{"role_id": zv_id, "permission_key": p} for p in _ZAEHLERVERWALTER_PERMS]
    )
    op.bulk_insert(rp_table, rows)

    # 3) users.role_id ergaenzen (nullable temporaer), backfillen, NOT NULL setzen,
    #    alte role-Spalte droppen — alles batched fuer SQLite.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("role_id", sa.Integer(), nullable=True))

    # Backfill: alle bestehenden User -> Admin (Entscheidung)
    bind.execute(
        sa.text("UPDATE users SET role_id = :rid"),
        {"rid": admin_id},
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("role_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            "fk_users_role_id_roles", "roles", ["role_id"], ["id"]
        )
        batch_op.drop_column("role")


def downgrade():
    bind = op.get_bind()

    # 1) role-String wiederherstellen
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=20), nullable=True))

    # Backfill: Admin-Rolle -> 'admin', alles andere -> 'user'
    bind.execute(sa.text(
        "UPDATE users SET role = CASE "
        "WHEN role_id IN (SELECT id FROM roles WHERE name = 'Admin') "
        "THEN 'admin' ELSE 'user' END"
    ))

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_role_id_roles", type_="foreignkey")
        batch_op.drop_column("role_id")

    # 2) Tabellen abbauen
    op.drop_table("role_permissions")
    op.drop_table("roles")
