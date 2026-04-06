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

    from app.projects import bp as projects_bp
    app.register_blueprint(projects_bp)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.import_csv import bp as import_csv_bp
    app.register_blueprint(import_csv_bp)

    from app.settings import bp as settings_bp
    app.register_blueprint(settings_bp)

    # Context Processor: WG-Einstellungen in alle Templates injizieren
    @app.context_processor
    def inject_wg_settings():
        from app.settings_service import wg_settings
        try:
            return dict(wg=wg_settings())
        except Exception:
            return dict(wg={})

    # CLI-Befehle
    from cli import register_commands
    register_commands(app)

    return app
