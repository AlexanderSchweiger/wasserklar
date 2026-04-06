"""
Einstellungs-Service: liefert WG-Kontaktdaten und Mail-Konfiguration,
wobei Datenbankwerte (AppSetting) Vorrang vor .env-Variablen haben.
"""
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


def send_mail(msg):
    """Sendet eine flask_mail.Message mit DB-überschriebenen Mail-Einstellungen."""
    from app.extensions import mail
    from app.models import AppSetting

    state = current_app.extensions['mail']
    originals = {}
    for attr, db_key, cast in _MAIL_MAP:
        val = AppSetting.get(db_key)
        if val is not None and val != '':
            try:
                originals[attr] = getattr(state, attr)
                setattr(state, attr, cast(val))
            except (ValueError, TypeError):
                originals.pop(attr, None)
    try:
        mail.send(msg)
    finally:
        for attr, orig_val in originals.items():
            setattr(state, attr, orig_val)
