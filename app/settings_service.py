"""
Einstellungs-Service: liefert WG-Kontaktdaten und Mail-Konfiguration,
wobei Datenbankwerte (AppSetting) Vorrang vor .env-Variablen haben.

Mail-Passwort wird verschlüsselt in der DB gespeichert (Fernet/AES128,
Key wird aus SECRET_KEY abgeleitet). Wer nur die DB hat, kann es nicht lesen.
"""
import base64
import hashlib

from flask import current_app


# Mapping: Attributname → Flask-Config-Key
_WG_MAP = {
    'name':    'WG_NAME',
    'address': 'WG_ADDRESS',
    'iban':    'WG_IBAN',
    'bic':     'WG_BIC',
    'email':   'WG_EMAIL',
    'phone':   'WG_PHONE',
}

# (State-Attribut, DB-Key, Cast-Funktion)
_MAIL_MAP = [
    ('server',         'mail.server',         str),
    ('port',           'mail.port',           int),
    ('use_tls',        'mail.use_tls',        lambda v: str(v).lower() in ('true', '1', 'yes')),
    ('username',       'mail.username',       str),
    ('password',       'mail.password',       str),
    ('default_sender', 'mail.default_sender', str),
]


def _fernet():
    """Gibt eine Fernet-Instanz zurück, deren Key aus SECRET_KEY abgeleitet wird."""
    from cryptography.fernet import Fernet
    secret = current_app.config['SECRET_KEY']
    # SHA-256 → 32 Bytes → URL-safe Base64 → gültiger Fernet-Key
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_password(plaintext: str) -> str:
    """Verschlüsselt ein Passwort für die DB-Ablage."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Entschlüsselt ein DB-Passwort. Gibt Leerstring zurück bei Fehler."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ''


def wg_settings():
    """Gibt alle WG-Kontakteinstellungen als Dict zurück (DB > .env)."""
    from app.models import AppSetting
    result = {}
    for attr, config_key in _WG_MAP.items():
        db_val = AppSetting.get(f'wg.{attr}')
        result[attr] = db_val if db_val is not None else current_app.config.get(config_key, '')
    return result


def get_wg(key):
    """Gibt einen einzelnen WG-Wert zurück (z.B. 'iban')."""
    from app.models import AppSetting
    db_val = AppSetting.get(f'wg.{key}')
    if db_val is not None:
        return db_val
    return current_app.config.get(_WG_MAP.get(key, ''), '')


def apply_mail_settings():
    """Liest Mail-Einstellungen aus der DB und schreibt sie in den Flask-Mail-State.

    Beim App-Start und nach jeder Änderung der Mail-Einstellungen aufrufen.
    """
    from app.models import AppSetting

    state = current_app.extensions['mail']
    for attr, db_key, cast in _MAIL_MAP:
        val = AppSetting.get(db_key)
        if val is not None and val != '':
            if attr == 'password':
                val = decrypt_password(val)
                if not val:
                    continue
            try:
                setattr(state, attr, cast(val))
            except (ValueError, TypeError):
                pass


def send_mail(msg):
    """Sendet eine flask_mail.Message. Mail-Einstellungen wurden bereits beim
    App-Start bzw. nach Einstellungsänderungen via apply_mail_settings() gesetzt."""
    from app.extensions import mail
    from app.models import AppSetting

    ##Mail Sender wird immer aus Einstellungen gesetzt
    msg.sender = (
        AppSetting.get('mail.default_sender')
        or current_app.config.get('MAIL_DEFAULT_SENDER')
        or AppSetting.get('mail.username')
        or current_app.config.get('MAIL_USERNAME')
        or ''
    )

    mail.send(msg)
