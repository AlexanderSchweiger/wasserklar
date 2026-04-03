"""
CLI-Befehle für die Anwendung.
Verwendung:
  flask --app run init-db
  flask --app run create-admin
"""


def register_commands(app):
    from app.extensions import db
    from app.models import User, Account

    @app.cli.command("init-db")
    def init_db():
        """Datenbanktabellen erstellen und Standard-Konten anlegen."""
        db.create_all()
        print("Datenbanktabellen erstellt.")

        # Fehlende Spalten ergänzen (SQLite unterstützt kein ALTER COLUMN,
        # aber ADD COLUMN ist möglich – sicher für Re-Runs).
        import sqlalchemy as sa
        with db.engine.connect() as conn:
            def _add_col_if_missing(table, col_def, col_name):
                cols = [c["name"] for c in sa.inspect(db.engine).get_columns(table)]
                if col_name not in cols:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))

            _add_col_if_missing("water_meters", "installed_from DATE", "installed_from")
            _add_col_if_missing("water_meters", "installed_to DATE", "installed_to")
            _add_col_if_missing("water_meters", "initial_value NUMERIC(12,3)", "initial_value")
            _add_col_if_missing("bookings", "open_item_id INTEGER REFERENCES open_items(id)", "open_item_id")
            _add_col_if_missing("invoice_items", "tax_rate NUMERIC(5,2)", "tax_rate")
            conn.commit()

        if Account.query.count() == 0:
            default_accounts = [
                Account(name="Wassergebühren", type="Einnahme", description="Jährliche Wasserabrechnung"),
                Account(name="Sonstige Einnahmen", type="Einnahme", description=""),
                Account(name="Wartung und Reparatur", type="Ausgabe", description=""),
                Account(name="Strom (Pumpen)", type="Ausgabe", description=""),
                Account(name="Versicherung", type="Ausgabe", description=""),
                Account(name="Verwaltung", type="Ausgabe", description=""),
                Account(name="Sonstige Ausgaben", type="Ausgabe", description=""),
            ]
            for acc in default_accounts:
                db.session.add(acc)
            db.session.commit()
            print(f"{len(default_accounts)} Standard-Konten angelegt.")

    @app.cli.command("upgrade-db")
    def upgrade_db():
        """Fehlende Spalten in bestehender Datenbank ergänzen (für Updates)."""
        db.create_all()
        import sqlalchemy as sa
        with db.engine.connect() as conn:
            def _add_col_if_missing(table, col_def, col_name):
                cols = [c["name"] for c in sa.inspect(db.engine).get_columns(table)]
                if col_name not in cols:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
                    print(f"  + {table}.{col_name} hinzugefügt.")
                else:
                    print(f"  ✓ {table}.{col_name} bereits vorhanden.")

            _add_col_if_missing("water_meters", "installed_from DATE", "installed_from")
            _add_col_if_missing("water_meters", "installed_to DATE", "installed_to")
            _add_col_if_missing("water_meters", "initial_value NUMERIC(12,3)", "initial_value")
            _add_col_if_missing("bookings", "open_item_id INTEGER REFERENCES open_items(id)", "open_item_id")
            _add_col_if_missing("invoice_items", "tax_rate NUMERIC(5,2)", "tax_rate")
            conn.commit()
        print("Datenbank aktualisiert.")

    @app.cli.command("create-admin")
    def create_admin():
        """Admin-Benutzer interaktiv anlegen."""
        import getpass
        db.create_all()
        username = input("Benutzername: ").strip()
        email = input("E-Mail: ").strip()
        password = getpass.getpass("Passwort: ")

        existing = User.query.filter_by(username=username).first()
        if existing:
            print(f"Benutzer '{username}' existiert bereits.")
            return

        user = User(username=username, email=email, role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"Admin '{username}' angelegt.")
