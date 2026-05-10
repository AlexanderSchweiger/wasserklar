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

    from app.dunning import bp as dunning_bp
    app.register_blueprint(dunning_bp)

    from app.data_transfer import bp as data_transfer_bp
    app.register_blueprint(data_transfer_bp)

    # hx-boost-Navigationen (base.html: <body hx-boost="true">) senden
    # sowohl "HX-Request: true" als auch "HX-Boosted: true". Unsere Routen
    # verwenden "HX-Request" als Signal fuer Partial-Fragment-Antworten
    # (z.B. _table.html bei Filter-Input). Bei geboosteten Voll-Navigationen
    # muss aber die komplette Seite inkl. base.html-Layout zurueckkommen,
    # sonst verschwindet das Sidebar-Menu. Daher "HX-Request" entfernen,
    # wenn "HX-Boosted" gesetzt ist — existierende Route-Checks sehen dann
    # einen normalen GET und rendern das volle Template.
    from flask import request as _request

    @app.before_request
    def _strip_hx_request_on_boost():
        if _request.headers.get("HX-Boosted"):
            _request.environ.pop("HTTP_HX_REQUEST", None)

    # Jinja2-Filter für deutsche Zahlenformatierung
    def de_number(value, decimals=2, signed=False):
        """Formatiert eine Zahl im deutschen Format (z. B. 1.250,90).

        signed=True erzwingt ein explizites +/- Vorzeichen.
        """
        try:
            num = float(value)
            if signed and num >= 0:
                formatted = f"+{num:,.{decimals}f}"
            else:
                formatted = f"{num:,.{decimals}f}"
            # Python verwendet Komma als Tausender und Punkt als Dezimal → tauschen
            return formatted.replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return value

    app.jinja_env.filters["de_number"] = de_number

    # Jinja-Global fuer Pagination: erzeugt URLs mit ueberlagerten Query-Args
    # (siehe app/pagination.py + templates/_pagination.html).
    from app.pagination import page_url as _page_url
    app.jinja_env.globals["page_url"] = _page_url

    # Context Processor: WG-Einstellungen in alle Templates injizieren
    @app.context_processor
    def inject_wg_settings():
        from app.settings_service import wg_settings
        try:
            return dict(wg=wg_settings())
        except Exception:
            return dict(wg={})

    # Context Processor: OSS-Version fuer Footer/About — SaaS-Layer
    # ueberschreibt den Footer-Block selbst und kombiniert mit saas_version.
    @app.context_processor
    def inject_oss_version():
        from app.__version__ import __version__
        return dict(oss_version=__version__)

    # Context Processor: flag setzen, wenn mindestens ein USt-pflichtiges Jahr existiert
    @app.context_processor
    def inject_vat_flag():
        try:
            from app.models import FiscalYear
            has_vat = FiscalYear.query.filter_by(is_vat_liable=True).first() is not None
            return dict(has_vat_fiscal_year=has_vat)
        except Exception:
            return dict(has_vat_fiscal_year=False)

    # Context Processor: kontextuelle Hilfe-URL fuer den Help-Button im Header.
    # Mapping in app/help_links.py; Default-Base-URL via HELP_BASE_URL-Config.
    @app.context_processor
    def inject_help_url():
        from flask import request
        from app.help_links import help_url_for
        return dict(help_url=help_url_for(request.endpoint, app.config.get("HELP_BASE_URL", "")))

    # Mail-Einstellungen aus DB laden (überschreibt .env-Werte)
    with app.app_context():
        try:
            from app.settings_service import apply_mail_settings
            apply_mail_settings()
        except Exception:
            pass  # DB noch nicht initialisiert (Erststart)

        # Offene Buchungen der Vortage als "Verbucht" markieren (Catch-up beim Start,
        # falls der Scheduler-Container um Mitternacht nicht lief)
        try:
            from app.accounting.services import auto_post_bookings
            auto_post_bookings()
        except Exception:
            pass  # DB noch nicht initialisiert (Erststart)

    # CLI-Befehle
    from cli import register_commands
    register_commands(app)

    return app
