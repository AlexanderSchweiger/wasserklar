import os
from flask import Flask
from config import config


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "default")

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config[config_name])

    # Verzeichnisse sicherstellen
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["PDF_DIR"], exist_ok=True)

    # Extensions initialisieren
    from app.extensions import db, login_manager, mail, migrate, csrf
    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    # Blueprints registrieren
    from app.auth import bp as auth_bp
    app.register_blueprint(auth_bp)

    from app.customers import bp as customers_bp
    app.register_blueprint(customers_bp)

    from app.properties import bp as properties_bp
    app.register_blueprint(properties_bp)

    from app.meters import bp as meters_bp
    app.register_blueprint(meters_bp)

    from app.invoices import bp as invoices_bp
    app.register_blueprint(invoices_bp)

    from app.accounting import bp as accounting_bp
    app.register_blueprint(accounting_bp)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    # CLI-Befehle
    from cli import register_commands
    register_commands(app)

    return app
