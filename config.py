import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(BASE_DIR, "instance", "wg.db"),
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Flask-Mail
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "localhost")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "wg@example.com")
    # Plattform-Relay: wenn aktiv, laeuft der Versand ueber den app.config-SMTP
    # statt ueber per-Tenant-mail.*-Overrides. OSS-Standalone: aus, SaaS: an.
    MAIL_PLATFORM_RELAY = os.environ.get("MAIL_PLATFORM_RELAY", "false").lower() == "true"

    # Fernet-Key fuer das in der DB gespeicherte SMTP-Passwort. Bewusst separat
    # vom SECRET_KEY: SMTP-Secrets duerfen nicht entschluesselt werden, wenn
    # nur SECRET_KEY (Session-Cookies, Reset-Tokens) leaked. Comma-separated
    # = Key-Rotation via MultiFernet (erster Key = primary).
    WASSERKLAR_MAIL_KEY = os.environ.get("WASSERKLAR_MAIL_KEY")

    # PDF-Ausgabeverzeichnis
    PDF_DIR = os.path.join(BASE_DIR, "instance", "pdfs")

    # BEV-Adressregister-Geocoding (Liegenschaften -> WGS84-Koordinate).
    # Der aufbereitete Adress-Index liegt als eigenstaendige SQLite-Datei auf
    # der Platte — bewusst NICHT in der App-/Tenant-DB (3,3 Mio Adressen sind
    # geteilte Referenzdaten, nicht Mandanten-Daten; im SaaS zeigt der Pfad auf
    # ein geteiltes Read-only-Volume, das fuer alle Tenants gilt). Der Index
    # wird per `flask bev-refresh` gebaut (OSS-Standalone: gelegentlich/Cron;
    # SaaS: platform-scheduler 2x/Jahr, passend zu den Gratis-Stichtagsdaten).
    BEV_INDEX_PATH = os.environ.get(
        "BEV_INDEX_PATH",
        os.path.join(BASE_DIR, "instance", "bev_addresses.sqlite"),
    )
    # Download-URL der Gratis-Adressregister-Stichtagsdaten (CC BY 4.0). Traegt
    # ein Stichtagsdatum und wechselt 2x/Jahr -> daher per Env konfigurierbar
    # statt hartcodiert. `bev-refresh --file <lokal.zip>` umgeht den Download
    # ganz (manuell geladenes ZIP).
    BEV_DOWNLOAD_URL = os.environ.get("BEV_DOWNLOAD_URL", "")

    # Automatische Hausanschluss->Liegenschaft-Zuordnung im Leitungsnetz
    # (Nearest-Neighbour gegen die geocodeten Liegenschaften, siehe
    # app/network/services.assign_hausanschluss_to_properties). Bewusst ein
    # SaaS-only-Komfortfeature: im OSS-Standalone defaultet es AUS — Selbst-
    # Hoster kennen ihre Anschluesse und ordnen sie im Feature-Formular manuell
    # zu (die grelle Karten-Markierung unzugeordneter Hausanschluesse und der
    # "ohne Liegenschaft"-Zaehler bleiben aktiv und unterstuetzen das). Der
    # SaaS-Layer schaltet das Flag in register_saas_extensions fuer alle Tenants
    # (Basis + Pro) an. Steuert die Route /network/assign-hausanschluss (404,
    # wenn aus) und das Zuordnen-UI auf der Karte.
    FEATURE_HAUSANSCHLUSS_AUTOASSIGN = (
        os.environ.get("FEATURE_HAUSANSCHLUSS_AUTOASSIGN", "false").lower() == "true"
    )

    # Oeffentlicher Hydrantenplan-Freigabe-Link fuer die Feuerwehr (Zugriff ohne
    # Login ueber ein Token, siehe app.models.HydrantShareLink). Steuert NUR die
    # Link-Verwaltung (Anlegen/Widerrufen) + das zugehoerige UI auf der
    # Hydranten-Druckseite — der reine A4/A3-Druck (authentifiziert) ist immer
    # verfuegbar. Bewusst ein SaaS-only-Komfortfeature: im OSS-Standalone
    # defaultet es AUS (ein Selbst-Hoster hat keine einloesende Public-Route;
    # die liegt im SaaS-Layer). Der SaaS-Layer schaltet das Flag in
    # register_saas_extensions fuer alle Tenants (Basis + Pro) an.
    FEATURE_HYDRANT_PUBLIC_SHARE = (
        os.environ.get("FEATURE_HYDRANT_PUBLIC_SHARE", "false").lower() == "true"
    )

    # Pro-API + MCP-Server (versionierte REST-API unter /api/v1 + MCP-Sidecar).
    # Bewusst ein SaaS-only-Feature und nur fuer den Pro-Plan: im OSS-Standalone
    # defaultet es AUS — die REST-/MCP-Schicht, das Tenant-Subdomain-Routing und
    # das Pro-Gating liegen komplett im SaaS-Layer (app/api_keys liefert nur das
    # Model + die Hash-Helfer, damit sie mit der Tenant-DB mitwandern). Der
    # SaaS-Layer schaltet das Flag in register_saas_extensions an und gatet jeden
    # API-/MCP-Request zusaetzlich ueber is_pro(slug). Steuert die Sichtbarkeit der
    # API-Schluessel-Verwaltung; die /api/v1-Routen liegen ohnehin im SaaS-Layer.
    FEATURE_API_ENABLED = (
        os.environ.get("FEATURE_API_ENABLED", "false").lower() == "true"
    )

    # Obergrenze fuer Massendruck/-export von Dokumenten pro Durchgang.
    # Schuetzt vor Speicher-/CPU-Last und Timeouts: WeasyPrint rendert jedes
    # Dokument einzeln in den RAM. Wird die Auswahl groesser, bietet die UI
    # einen Gruppen-Dialog (je BULK_PRINT_MAX Dokumente ein eigener Download);
    # die Server-Routen kappen zusaetzlich hart als Sicherheitsnetz. Im SaaS
    # ggf. strenger per Env setzen.
    BULK_PRINT_MAX = int(os.environ.get("BULK_PRINT_MAX", 100))

    # Genossenschaft (für Rechnungskopf)
    WG_NAME = os.environ.get("WG_NAME", "Wassergenossenschaft")
    WG_ADDRESS = os.environ.get("WG_ADDRESS", "")
    WG_IBAN = os.environ.get("WG_IBAN", "")
    WG_BIC = os.environ.get("WG_BIC", "")
    WG_ACCOUNT_HOLDER = os.environ.get("WG_ACCOUNT_HOLDER", "")
    WG_EMAIL = os.environ.get("WG_EMAIL", "")
    WG_PHONE = os.environ.get("WG_PHONE", "")

    # Produkt-/Markenname fuer nutzersichtbare Stellen (Footer, 2FA-Aussteller,
    # Export-Dateiname, iCal-Metadaten). Default "wasserklar" fuer den
    # OSS-Standalone; der SaaS-Layer ueberschreibt das via register_saas_extensions.
    APP_BRAND_NAME = os.environ.get("APP_BRAND_NAME", "wasserklar")

    # Hilfe-Link im App-Header. Default: oeffentliche Doku auf wasserklar.at.
    # Self-hosted Installationen koennen via .env auf eine eigene Doku zeigen
    # (oder leer setzen, dann ist der Help-Button im Template versteckt).
    HELP_BASE_URL = os.environ.get("HELP_BASE_URL", "https://wasserklar.at/docs")


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    """Für automatisierte Unit-Tests: SQLite in-memory, kein CSRF."""
    DEBUG = False
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key"
    # Fester Test-Key (32-Byte urlsafe-base64) — Tests duerfen nicht von der
    # User-.env abhaengen.
    WASSERKLAR_MAIL_KEY = "Q3hUYjBkbGRZWG41ZXM0SUtYRG1MRzJaRWxudFYzeTI="


class StagingConfig(Config):
    """Docker-Testinstallation: verhält sich wie Production, nutzt DATABASE_URL aus .env.test."""
    DEBUG = False


class ProductionConfig(Config):
    DEBUG = False


config = {
    "development": DevelopmentConfig,
    "testing":     TestingConfig,
    "staging":     StagingConfig,
    "production":  ProductionConfig,
    "default":     DevelopmentConfig,
}
