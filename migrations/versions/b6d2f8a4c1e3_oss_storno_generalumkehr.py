"""[oss] Storno als Generalumkehr: bestehende Storno-Paare angleichen

Seit dieser Version zählen Auswertungen Original **und** Gegenbuchung (jede an
ihrem Datum) statt beide wegzulassen. Damit sich dadurch keine bestehende Zahl
ändert, gleicht die Migration die vorhandenen Paare an — nur Daten, kein Schema:

1. Gegenbuchungen aus dem früheren Einzel-Storno trugen weder Bank/Kasse noch
   Kontakt noch Belegnummer. Ohne Bank/Kasse würde das Original den Kontostand
   ändern, die Gegenbuchung aber nicht → Werte vom Original übernehmen.
2. Liegt eine Gegenbuchung in einem anderen Kalenderjahr als ihr Original
   (Rechnungs-Storno einer Sammelbuchung über den Jahreswechsel), bekommt sie
   das Datum des Originals. Das Paar wurde bisher in keinem Jahr gezählt; so
   ergibt es in seinem Jahr weiter Null und kein Jahresabschluss-Saldo
   (``real_account_year_balances``) verschiebt sich.
3. Storno-Datum und -Grund am Original nachtragen, wo sie fehlen (das frühere
   Einzel-Storno setzte sie nur an der Gegenbuchung).

Zeilenweise in Python statt UPDATE … FROM: MySQL/MariaDB erlaubt im UPDATE keine
Unterabfrage auf dieselbe Tabelle.

Revision ID: b6d2f8a4c1e3
Revises: c5e1a9d3f7b2
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa


revision = "b6d2f8a4c1e3"
down_revision = "c5e1a9d3f7b2"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    pairs = conn.execute(sa.text(
        "SELECT p.id, p.date, p.real_account_id, p.customer_id, p.reference, "
        "       p.storno_date, p.storno_reason, "
        "       o.id, o.date, o.real_account_id, o.customer_id, o.reference, "
        "       o.storno_date, o.storno_reason "
        "FROM bookings p JOIN bookings o ON o.id = p.storno_of_id"
    )).fetchall()

    for row in pairs:
        (p_id, p_date, p_ra, p_cust, p_ref, p_sdate, p_reason,
         o_id, o_date, o_ra, o_cust, o_ref, o_sdate, o_reason) = row
        p_date, o_date = _as_date(p_date), _as_date(o_date)

        partner = {}
        if p_ra is None and o_ra is not None:
            partner["real_account_id"] = o_ra
        if p_cust is None and o_cust is not None:
            partner["customer_id"] = o_cust
        if not p_ref and o_ref:
            partner["reference"] = o_ref
        if p_date is not None and o_date is not None and p_date.year != o_date.year:
            partner["date"] = o_date
        if partner:
            sets = ", ".join(f"{k} = :{k}" for k in partner)
            conn.execute(sa.text(f"UPDATE bookings SET {sets} WHERE id = :id"),
                         {**partner, "id": p_id})

        original = {}
        if o_sdate is None and (p_sdate is not None or p_date is not None):
            original["storno_date"] = p_sdate or p_date
        if not o_reason and p_reason:
            original["storno_reason"] = p_reason
        if original:
            sets = ", ".join(f"{k} = :{k}" for k in original)
            conn.execute(sa.text(f"UPDATE bookings SET {sets} WHERE id = :id"),
                         {**original, "id": o_id})


def downgrade():
    # Reine Datenangleichung; die übernommenen Werte sind auch unter der alten
    # Regel korrekt (dort zählte das Paar gar nicht).
    pass


def _as_date(value):
    """SQLite liefert Datumsspalten im Roh-SQL als Text."""
    if value is None or hasattr(value, "year"):
        return value
    from datetime import date
    return date.fromisoformat(str(value)[:10])
