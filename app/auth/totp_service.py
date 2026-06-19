"""TOTP/2FA-Helfer (RFC 6238) — kontextfrei und unit-testbar.

Die Verschluesselung des Secrets passiert NICHT hier, sondern ueber
``app.settings_service.encrypt_password`` / ``decrypt_password`` (Key
``WASSERKLAR_MAIL_KEY``, MultiFernet). ``pyotp`` und ``segno`` werden lazy
importiert (wie ``cryptography`` in ``settings_service``), damit ein fehlendes
Paket nicht den App-Start, sondern erst die 2FA-Nutzung betrifft.
"""
import json
import secrets

# 6-stellige Codes im 30-s-Fenster (pyotp-Defaults). ``valid_window=1`` toleriert
# +-30 s Uhren-Drift zwischen Handy und Server — 0 ist zu streng, >1 weitet das
# Brute-Force-Fenster unnoetig auf.
TOTP_WINDOW = 1

# Eindeutiges Alphabet fuer Recovery-Codes (ohne 0/O/1/I/L-Verwechsler).
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_RECOVERY_CODE_LEN = 10
_RECOVERY_CODE_COUNT = 10


def new_secret():
    """Neues zufaelliges Base32-TOTP-Secret."""
    import pyotp

    return pyotp.random_base32()


def verify_code(secret_b32, code, window=TOTP_WINDOW):
    """True, wenn ``code`` zum Secret im aktuellen (+-window) Zeitfenster passt."""
    if not secret_b32 or not code:
        return False
    import pyotp

    return pyotp.TOTP(secret_b32).verify(str(code).strip(), valid_window=window)


def provisioning_uri(secret_b32, account_name, issuer):
    """otpauth://-URI fuer QR-Code / manuelle Eingabe in der Authenticator-App."""
    import pyotp

    return pyotp.TOTP(secret_b32).provisioning_uri(
        name=account_name, issuer_name=issuer
    )


def qr_svg(uri):
    """Inline-SVG-Markup des QR-Codes (kein Pillow, kein head-JS — hx-boost-sicher)."""
    import segno

    return segno.make(uri).svg_inline(scale=5)


def normalize_code(raw):
    """Recovery-Code normalisieren: Grossbuchstaben, ohne Bindestriche/Leerzeichen."""
    if not raw:
        return ""
    return "".join(ch for ch in str(raw).upper() if ch.isalnum())


def _format_recovery_code(raw):
    """Gruppiert XXXX-XXXX-XX zur besseren Lesbarkeit."""
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:10]}"


def generate_recovery_codes(n=_RECOVERY_CODE_COUNT):
    """Liefert (plaintext_codes, json_of_hashes).

    ``plaintext_codes`` werden dem User EINMALIG gezeigt; ``json_of_hashes`` ist
    eine JSON-Liste werkzeug-Hashes fuer die DB (bewusst key-unabhaengig, damit
    Recovery auch bei wegrotiertem Fernet-Key noch funktioniert).
    """
    from werkzeug.security import generate_password_hash

    plaintexts = []
    hashes = []
    for _ in range(n):
        raw = "".join(
            secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_CODE_LEN)
        )
        plaintexts.append(_format_recovery_code(raw))
        hashes.append(generate_password_hash(raw))
    return plaintexts, json.dumps(hashes)


def match_and_remove(stored_json, raw_code):
    """Prueft ``raw_code`` gegen die gespeicherten Hashes.

    Liefert (matched: bool, new_json: str|None). Bei Treffer wird der Hash
    entfernt (Einmal-Verbrauch) und das aktualisierte JSON zurueckgegeben.
    """
    from werkzeug.security import check_password_hash

    code = normalize_code(raw_code)
    if not code or not stored_json:
        return False, stored_json
    try:
        hashes = json.loads(stored_json)
    except (ValueError, TypeError):
        return False, stored_json
    for i, h in enumerate(hashes):
        if check_password_hash(h, code):
            del hashes[i]
            return True, json.dumps(hashes)
    return False, stored_json
