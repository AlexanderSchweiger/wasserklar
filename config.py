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

    # PDF-Ausgabeverzeichnis
    PDF_DIR = os.path.join(BASE_DIR, "instance", "pdfs")

    # Genossenschaft (für Rechnungskopf)
    WG_NAME = os.environ.get("WG_NAME", "Wassergenossenschaft")
    WG_ADDRESS = os.environ.get("WG_ADDRESS", "")
    WG_IBAN = os.environ.get("WG_IBAN", "")
    WG_EMAIL = os.environ.get("WG_EMAIL", "")
    WG_PHONE = os.environ.get("WG_PHONE", "")


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    """Für automatisierte Unit-Tests: SQLite in-memory, kein CSRF."""
    DEBUG = False
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret-key"


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
