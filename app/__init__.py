import os
from flask import Flask
from config import config


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "default")

    # instance_path defaultet auf <package_root>/instance. Im SaaS-Stack liegt
    # das OSS-Paket aber unter /app/wasserklar/, waehrend das persistente Volume
    # bei /app/instance gemountet ist — ohne Override schriebe die App auf das
    # ephemere Container-FS und verloere alle Tenant-PDFs/Backups beim Redeploy.
    # WASSERKLAR_INSTANCE_PATH (absolut) biegt instance_path auf das Volume.
    instance_path = os.environ.get("WASSERKLAR_INSTANCE_PATH") or None
    app = Flask(__name__, instance_relative_config=True, instance_path=instance_path)
    app.config.from_object(config[config_name])

    # SECRET_KEY-Haertung: in produktiven Configs (Staging/Production, d.h. nicht
    # DEBUG und nicht TESTING) darf NICHT der oeffentlich bekannte Dev-Default
    # (oder ein leerer Wert aus fehlgeschlagener Env-Interpolation) greifen.
    # SECRET_KEY signiert Session-Cookies UND alle itsdangerous-Tokens
    # (Passwort-Reset; im SaaS zusaetzlich Invite-/Self-Service-/Opt-In-Tokens).
    # Lokales OSS-Standalone-Dev (DEBUG) und die Tests bleiben unberuehrt.
    _secret = app.config.get("SECRET_KEY")
    if not app.config.get("DEBUG") and not app.config.get("TESTING"):
        if not _secret or _secret == "dev-secret-change-in-production":
            raise RuntimeError(
                "SECRET_KEY ist in Produktion nicht (sicher) gesetzt. Einen "
                "starken Zufallswert via Umgebungsvariable setzen, z.B.:\n"
                "  python -c \"import secrets; print(secrets.token_hex(32))\""
            )

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

    from app.meter_tours import bp as meter_tours_bp
    app.register_blueprint(meter_tours_bp)

    from app.periods import bp as periods_bp
    app.register_blueprint(periods_bp)

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

    from app.bank_import import bp as bank_import_bp
    app.register_blueprint(bank_import_bp)

    from app.network import bp as network_bp
    app.register_blueprint(network_bp)

    from app.incidents import bp as incidents_bp
    app.register_blueprint(incidents_bp)

    from app.schriftfuehrung import bp as schriftfuehrung_bp
    app.register_blueprint(schriftfuehrung_bp)

    from app.circulars import bp as circulars_bp
    app.register_blueprint(circulars_bp)

    from app.notes import bp as notes_bp
    app.register_blueprint(notes_bp)

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

    # Jinja-Globals fuer per-entity import preview macros (ROW_* status vocabulary).
    # The meter-reading import_preview.html passes its own status_badge/status_row_class
    # as render-kwargs and shadows these globals locally — no regression there.
    from app.imports.common import (
        status_badge as _status_badge,
        status_row_class as _status_row_class,
    )
    app.jinja_env.globals["status_badge"] = _status_badge
    app.jinja_env.globals["status_row_class"] = _status_row_class

    # Jinja-Global fuer Pagination: erzeugt URLs mit ueberlagerten Query-Args
    # (siehe app/pagination.py + templates/_pagination.html).
    from app.pagination import page_url as _page_url
    app.jinja_env.globals["page_url"] = _page_url

    # Berechtigungs-Liste fuer Rollen-Formular + Permission-Konstanten in Templates.
    from app.auth.permissions import ALL_PERMISSIONS as _ALL_PERMISSIONS
    app.jinja_env.globals["ALL_PERMISSIONS"] = _ALL_PERMISSIONS

    # Produkt-/Markenname fuer Templates (Footer etc.). OSS-Default aus der
    # Config; der SaaS-Layer setzt APP_BRAND_NAME um und re-injiziert den Global.
    app.jinja_env.globals["app_brand_name"] = app.config["APP_BRAND_NAME"]

    # WG-Domaene (Mandant-Typ Wassergenossenschaft): Label-/Badge-Dicts +
    # Funktions-Label-Helper fuer Formulare und Listen.
    from app.wg import (
        STATUS_LABELS as _wg_status_labels,
        STATUS_BADGE as _wg_status_badge,
        FUNCTION_LABELS as _wg_function_labels,
        function_label as _wg_function_label,
        function_keys_ordered as _wg_function_keys_ordered,
    )
    app.jinja_env.globals["wg_status_labels"] = _wg_status_labels
    app.jinja_env.globals["wg_status_badge"] = _wg_status_badge
    app.jinja_env.globals["wg_function_labels"] = _wg_function_labels
    app.jinja_env.globals["wg_function_label"] = _wg_function_label
    app.jinja_env.globals["wg_function_keys_ordered"] = _wg_function_keys_ordered

    # Notiz-Helfer als Jinja-Globals: Detailseiten/Dashboard rendern ihr Panel
    # via ``hx-trigger=load`` (kein Direkt-Query noetig), aber die Zeilen-Pins in
    # Listen ziehen ihre Notizen ueber ``notes_by_entity_for`` in EINER Query je
    # Tabelle (kein N+1). ``notes_for`` ist der Single-Entity-Fallback fuer den
    # Row-Swap (eine Zeile, keine Map vorhanden). Beide sind reine Lese-Helfer.
    from app.notes import services as _notes_svc
    app.jinja_env.globals["notes_by_entity_for"] = _notes_svc.notes_by_entity_for
    app.jinja_env.globals["notes_for"] = _notes_svc.notes_for

    # Context Processor: WG-Einstellungen in alle Templates injizieren
    @app.context_processor
    def inject_wg_settings():
        from app.settings_service import wg_settings
        try:
            return dict(wg=wg_settings())
        except Exception:
            return dict(wg={})

    # Context Processor: Mandant-Typ in alle Templates injizieren. ``is_wg``
    # schaltet die genossenschafts-spezifischen Felder/Spalten/Filter (Default
    # True = Wassergenossenschaft, da das der Standardfall der App ist).
    @app.context_processor
    def inject_org_type():
        from app.settings_service import org_type, is_wassergenossenschaft
        try:
            return dict(org_type=org_type(), is_wg=is_wassergenossenschaft())
        except Exception:
            from app.wg import ORG_COOPERATIVE
            return dict(org_type=ORG_COOPERATIVE, is_wg=True)

    # Context Processor: OSS-Version fuer Footer/About — SaaS-Layer
    # ueberschreibt den Footer-Block selbst und kombiniert mit saas_version.
    @app.context_processor
    def inject_oss_version():
        from app.__version__ import __version__
        return dict(oss_version=__version__)

    # Context Processor: Massenversand-Pause (ms) fuers frontend-getriebene
    # serielle Massenmailing. 0 beim Plattform-Relay (Vollgas), sonst die
    # konfigurierte Drossel fuer eigenen SMTP. Siehe
    # settings_service.bulk_mail_delay_ms.
    @app.context_processor
    def inject_bulk_mail_delay():
        from app.settings_service import bulk_mail_delay_ms, BULK_MAIL_DELAY_DEFAULT_S
        try:
            return dict(bulk_mail_delay_ms=bulk_mail_delay_ms())
        except Exception:
            return dict(bulk_mail_delay_ms=int(BULK_MAIL_DELAY_DEFAULT_S * 1000))

    # Context Processor: flag setzen, wenn mindestens ein USt-pflichtiges Jahr existiert
    @app.context_processor
    def inject_vat_flag():
        try:
            from app.models import FiscalYear
            has_vat = FiscalYear.query.filter_by(is_vat_liable=True).first() is not None
            return dict(has_vat_fiscal_year=has_vat)
        except Exception:
            return dict(has_vat_fiscal_year=False)

    # WASSERKLAR_MAIL_KEY-Warnung: ohne Key kann das DB-gespeicherte
    # SMTP-Passwort nicht entschluesselt werden. Hart-failen will man nicht
    # (Erststart ohne Mail-Konfig soll laufen), aber lautlos schweigen auch
    # nicht. Wenn in der DB schon ein verschluesseltes Passwort liegt, kommt
    # spaeter beim send_mail() ein RuntimeError aus _fernet() — der ist
    # absichtlich nicht abgefangen.
    if not app.config.get("WASSERKLAR_MAIL_KEY"):
        app.logger.warning(
            "WASSERKLAR_MAIL_KEY ist nicht gesetzt — gespeicherte SMTP-Passwoerter "
            "koennen nicht entschluesselt werden. Generieren: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )

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
