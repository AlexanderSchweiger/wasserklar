"""
Einstellungs-Service: liefert WG-Kontaktdaten und Mail-Konfiguration,
wobei Datenbankwerte (AppSetting) Vorrang vor .env-Variablen haben.

Mail-Passwort wird Fernet-verschluesselt in der DB gespeichert. Der Schluessel
``WASSERKLAR_MAIL_KEY`` kommt aus der .env (separat vom SECRET_KEY: andere
Vertrauenszone als Session-Cookies / Reset-Tokens). Comma-separated mehrere
Keys = MultiFernet fuer Key-Rotation; erster Key = primary (encrypt), weitere
= Decrypt-Fallback.
"""
import re
from html import escape
from html.parser import HTMLParser

from flask import current_app


# Mapping: Attributname → Flask-Config-Key
_WG_MAP = {
    'name':           'WG_NAME',
    'address':        'WG_ADDRESS',
    'iban':           'WG_IBAN',
    'bic':            'WG_BIC',
    'account_holder': 'WG_ACCOUNT_HOLDER',
    'email':          'WG_EMAIL',
    'phone':          'WG_PHONE',
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

# (State-Attribut, Flask-Config-Key) — Reset des Flask-Mail-States auf die
# app.config-Defaults, bevor per-Tenant-Overrides angewendet werden.
_MAIL_RESET = [
    ('server',         'MAIL_SERVER'),
    ('port',           'MAIL_PORT'),
    ('use_tls',        'MAIL_USE_TLS'),
    ('username',       'MAIL_USERNAME'),
    ('password',       'MAIL_PASSWORD'),
    ('default_sender', 'MAIL_DEFAULT_SENDER'),
]


def _fernet():
    """Liefert eine MultiFernet-/Fernet-Instanz aus WASSERKLAR_MAIL_KEY.

    Comma-separated mehrere Keys -> MultiFernet (erster encrypt, alle decrypt).
    Fehlt der Key komplett, faellt die App lieber laut auf die Nase, als ein
    Passwort gar nicht oder mit einer schwachen Ableitung zu speichern.
    """
    from cryptography.fernet import Fernet, MultiFernet
    raw = current_app.config.get('WASSERKLAR_MAIL_KEY')
    if not raw:
        raise RuntimeError(
            "WASSERKLAR_MAIL_KEY ist nicht gesetzt. Erzeugen: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    parts = [k.strip().encode() for k in str(raw).split(',') if k.strip()]
    if not parts:
        raise RuntimeError("WASSERKLAR_MAIL_KEY ist leer oder nur Whitespace.")
    fernets = [Fernet(k) for k in parts]
    return MultiFernet(fernets) if len(fernets) > 1 else fernets[0]


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
    # Logo (Data-URI) hat keinen .env-Fallback, daher separat — nicht in _WG_MAP,
    # das auch im Settings-POST-Loop iteriert wird und Config-Keys voraussetzt.
    result['logo'] = AppSetting.get('wg.logo') or ''
    return result


# Maximale dekodierte Logo-Groesse (Safety-Net — der Cropper verkleinert bereits
# client-seitig; der Server lehnt nur exzessiv grosse oder falsch typisierte
# Uploads ab).
LOGO_MAX_BYTES = 256 * 1024
_LOGO_DATA_URI_RE = re.compile(r'^data:image/(?:png|jpeg|webp);base64,')


def validate_logo_data_uri(raw):
    """Validiert ein Logo-Data-URI aus dem Cropper.

    Gibt ``(data_uri, None)`` bei Erfolg zurück, sonst ``(None, fehlermeldung)``.
    Ein leerer/whitespace-Input liefert ``(None, None)`` (= nichts zu tun).
    """
    import base64
    import binascii

    if not raw or not raw.strip():
        return None, None
    data_uri = raw.strip()
    if not _LOGO_DATA_URI_RE.match(data_uri):
        return None, 'Logo muss ein PNG-, JPEG- oder WebP-Bild sein.'
    try:
        b64 = data_uri.split(',', 1)[1]
    except IndexError:
        return None, 'Ungültiges Logo-Format.'
    try:
        raw_bytes = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None, 'Logo konnte nicht dekodiert werden.'
    if not raw_bytes:
        return None, 'Logo ist leer.'
    if len(raw_bytes) > LOGO_MAX_BYTES:
        return None, f'Logo ist zu groß (max. {LOGO_MAX_BYTES // 1024} KB).'
    return data_uri, None


def get_wg(key):
    """Gibt einen einzelnen WG-Wert zurück (z.B. 'iban')."""
    from app.models import AppSetting
    db_val = AppSetting.get(f'wg.{key}')
    if db_val is not None:
        return db_val
    return current_app.config.get(_WG_MAP.get(key, ''), '')


def platform_relay_active():
    """True, wenn Mails über den Plattform-Relay (app.config-SMTP) statt über
    per-Tenant-SMTP laufen.

    Der AppSetting-Wert hat Vorrang vor dem Config-Default MAIL_PLATFORM_RELAY.
    Der DB-Zugriff ist abgesichert: beim App-Start (in der SaaS-Variante hat das
    Schema 'public' keine app_settings-Tabelle) greift der Config-Default.
    """
    from app.models import AppSetting
    try:
        val = AppSetting.get('mail.use_platform_relay')
    except Exception:
        val = None
    if val is None or val == '':
        return bool(current_app.config.get('MAIL_PLATFORM_RELAY'))
    return str(val).lower() in ('true', '1', 'yes')


# ── Rechnungs-Kontakttext (einfaches Rich-Text) ──────────────────────────────
#
# Der Kontakttext fuer Rechnungen/Briefe wird als stark reduziertes HTML
# gespeichert: nur <b>/<i>/<u>/<br>. Beim Speichern wird beliebiges HTML aus dem
# contenteditable-Editor auf genau diese Tags normalisiert (Attribute weg,
# unbekannte Tags weg, Block-Elemente -> Zeilenumbruch). Das normalisierte
# Format ist sowohl im PDF (direkt) als auch im DOCX (geparst) renderbar.

_RT_INLINE = {'b': 'b', 'strong': 'b', 'i': 'i', 'em': 'i', 'u': 'u'}
_RT_BLOCK = {'p', 'div', 'li'}
_FONT_SIZE_STYLE_RE = re.compile(r'font-size\s*:\s*(\d+(?:\.\d+)?)\s*pt', re.IGNORECASE)


def _span_font_size(attrs) -> 'int | None':
    """Extrahiert font-size (pt) aus span style; None wenn ungültig oder außerhalb Grenzen."""
    style = dict(attrs).get('style', '')
    m = _FONT_SIZE_STYLE_RE.search(style)
    if not m:
        return None
    try:
        pt = int(float(m.group(1)))
    except ValueError:
        return None
    return pt if CONTACT_INFO_FONT_MIN <= pt <= CONTACT_INFO_FONT_MAX else None


class _RichTextSanitizer(HTMLParser):
    """Reduziert eingehendes HTML auf <b>/<i>/<u>/<br> und <span style="font-size:Xpt">."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._open = []  # Stack offener (normalisierter) Inline-Tags

    def _line_break(self):
        if self.parts and not self.parts[-1].endswith('<br>'):
            self.parts.append('<br>')

    def _close_to(self, norm):
        if norm in self._open:
            while self._open:
                top = self._open.pop()
                self.parts.append(f'</{top}>')
                if top == norm:
                    break

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('<br>')
        elif tag in _RT_INLINE:
            norm = _RT_INLINE[tag]
            self.parts.append(f'<{norm}>')
            self._open.append(norm)
        elif tag == 'span':
            pt = _span_font_size(attrs)
            if pt is not None:
                self.parts.append(f'<span style="font-size:{pt}pt">')
                self._open.append('span')
        elif tag in _RT_BLOCK:
            self._line_break()

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('<br>')

    def handle_endtag(self, tag):
        if tag in _RT_INLINE:
            self._close_to(_RT_INLINE[tag])
        elif tag == 'span':
            self._close_to('span')

    def handle_data(self, data):
        self.parts.append(escape(data))

    def result(self):
        while self._open:
            self.parts.append(f'</{self._open.pop()}>')
        html = ''.join(self.parts)
        html = re.sub(r'(?:<br>\s*){3,}', '<br><br>', html)
        html = re.sub(r'^(?:<br>\s*)+', '', html)
        html = re.sub(r'(?:\s*<br>)+$', '', html)
        return html.strip()


def sanitize_rich_text(raw: str) -> str:
    """Normalisiert HTML aus dem Kontakttext-Editor auf <b>/<i>/<u>/<br>."""
    if not raw or not raw.strip():
        return ''
    parser = _RichTextSanitizer()
    parser.feed(raw)
    return parser.result()


def get_contact_info() -> str:
    """Gibt den gespeicherten Rechnungs-Kontakttext (sanitisiertes HTML) zurueck."""
    from app.models import AppSetting
    try:
        return AppSetting.get('invoice.contact_info') or ''
    except Exception:
        return ''


# Schriftgroessen-Grenzen fuer den Rechnungs-Kontakttext (Punkt). Wirkt auf PDF
# (inline font-size) und DOCX (Pt). Werte ausserhalb werden auf den Default
# zurueckgesetzt.
CONTACT_INFO_FONT_MIN = 6
CONTACT_INFO_FONT_MAX = 24
CONTACT_INFO_FONT_DEFAULT = 10


def get_contact_info_font_size(default: int = CONTACT_INFO_FONT_DEFAULT) -> int:
    """Gibt die konfigurierte Schriftgroesse des Rechnungs-Kontakttexts (Pt) zurueck."""
    from app.models import AppSetting
    try:
        raw = AppSetting.get('invoice.contact_info_font_size')
    except Exception:
        raw = None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    if val < CONTACT_INFO_FONT_MIN or val > CONTACT_INFO_FONT_MAX:
        return default
    return val


def meter_replacement_interval() -> int:
    """Tausch-Intervall fuer Wasserzaehler in Jahren (Default 5)."""
    from app.models import AppSetting
    try:
        raw = AppSetting.get('meters.replacement_interval_years')
    except Exception:
        raw = None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 5
    return val if val >= 1 else 5


def apply_mail_settings():
    """Konfiguriert den (prozess-globalen) Flask-Mail-State für den aktuellen
    Request bzw. Tenant.

    Idempotent: setzt den State zuerst auf die app.config-Defaults zurück und
    wendet — sofern kein Plattform-Relay aktiv ist — die mail.*-Overrides an.
    Der Reset ist im Multi-Tenant-Betrieb load-bearing, weil der Flask-Mail-State
    prozess-global ist und sonst Werte eines anderen Tenants übrig blieben.
    """
    from app.models import AppSetting

    state = current_app.extensions['mail']

    # 1. Reset auf die app.config-Defaults (kein DB-Zugriff nötig).
    for attr, cfg_key in _MAIL_RESET:
        setattr(state, attr, current_app.config.get(cfg_key))

    # 2. Plattform-Relay: keine per-Tenant-Overrides.
    if platform_relay_active():
        return

    # 3. Eigener SMTP: mail.*-Overrides anwenden.
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
    """Sendet eine flask_mail.Message.

    Wendet vorab die aktuell gültige Mail-Konfiguration an — wichtig im
    Multi-Tenant-Betrieb, da der Flask-Mail-State prozess-global ist und sonst
    der zuletzt gespeicherte Tenant gewinnen würde.
    """
    from app.extensions import mail
    from app.models import AppSetting

    apply_mail_settings()

    # Absender wird immer aus den Einstellungen gesetzt.
    if platform_relay_active():
        msg.sender = current_app.config.get('MAIL_DEFAULT_SENDER') or ''
    else:
        msg.sender = (
            AppSetting.get('mail.default_sender')
            or current_app.config.get('MAIL_DEFAULT_SENDER')
            or AppSetting.get('mail.username')
            or current_app.config.get('MAIL_USERNAME')
            or ''
        )

    msg.extra_headers = getattr(msg, 'extra_headers', {}) or {}
    msg.extra_headers['X-PM-Message-Stream'] = 'outbound'

    mail.send(msg)
