"""Passwort-Policy: serverseitige Validierung nach NIST SP 800-63B.

Diese Validierung ist maßgeblich. Die Stärkeanzeige im Frontend
(static/js/password-strength.js) ist nur eine UX-Hilfe.
"""

MIN_LENGTH = 12
MAX_LENGTH = 128

# Knappe Liste verbreiteter Passwörter (inkl. deutschsprachiger Klassiker).
# Bewusst klein gehalten — die Mindestlänge von 12 Zeichen fängt den Großteil
# schwacher Passwörter ohnehin ab.
COMMON_PASSWORDS = frozenset({
    "password", "passwort", "12345678", "123456789", "1234567890",
    "111111111", "qwertzuiop", "qwertyuiop", "password123", "passwort123",
    "administrator", "willkommen", "willkommen1", "letmein123",
    "iloveyou123", "geheim123", "test1234", "changeme123", "wasserklar",
    "sommer2024", "sommer2025", "winter2024", "winter2025", "fussball123",
    "computer123", "hallo12345", "passwort1234", "password1234",
})


def _is_trivial(password):
    """True bei reinen Wiederholungen oder simplen Zeichenfolgen."""
    if len(set(password)) <= 2:
        return True
    sequences = "abcdefghijklmnopqrstuvwxyz0123456789"
    low = password.lower()
    return low in sequences or low in sequences[::-1]


def validate_password(password, *, username=None, email=None):
    """Prüft ein Passwort gegen die Policy.

    Gibt eine Liste deutscher Fehlermeldungen zurück; leere Liste = gültig.
    """
    errors = []
    pw = password or ""

    if len(pw) < MIN_LENGTH:
        errors.append(
            f"Das Passwort muss mindestens {MIN_LENGTH} Zeichen lang sein."
        )
    if len(pw) > MAX_LENGTH:
        errors.append(
            f"Das Passwort darf höchstens {MAX_LENGTH} Zeichen lang sein."
        )

    low = pw.lower()
    if low.strip() and low.strip() in COMMON_PASSWORDS:
        errors.append("Dieses Passwort ist zu verbreitet — bitte wähle ein anderes.")
    elif pw and _is_trivial(pw):
        errors.append(
            "Das Passwort ist zu einfach (nur Wiederholungen oder Zeichenfolgen)."
        )

    forbidden = set()
    if username and username.strip():
        forbidden.add(username.strip().lower())
    if email and email.strip():
        addr = email.strip().lower()
        forbidden.add(addr)
        if "@" in addr:
            forbidden.add(addr.split("@", 1)[0])
    if any(len(tok) >= 3 and tok in low for tok in forbidden):
        errors.append(
            "Das Passwort darf nicht den Benutzernamen oder die E-Mail-Adresse enthalten."
        )

    return errors
