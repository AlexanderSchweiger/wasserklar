"""Unit-Tests fuer die Passwort-Policy (app/auth/password_policy.py)."""
from app.auth.password_policy import validate_password


def test_too_short_is_rejected():
    errors = validate_password("Kurz1")
    assert any("Zeichen" in e for e in errors)


def test_eleven_chars_too_short():
    assert validate_password("Tal9z-Quk7m")  # 11 Zeichen


def test_twelve_clean_chars_pass():
    assert validate_password("Tal9z-Quk7mx") == []  # 12 Zeichen


def test_common_password_is_rejected():
    errors = validate_password("administrator")
    assert any("verbreitet" in e for e in errors)


def test_trivial_repetition_is_rejected():
    assert validate_password("aaaaaaaaaaaaaa")


def test_password_containing_username_is_rejected():
    errors = validate_password("schweiger-wasser-2026", username="schweiger")
    assert any("Benutzernamen" in e for e in errors)


def test_password_containing_email_local_part_is_rejected():
    errors = validate_password("alexander-mag-wasser", email="alexander@alm.test")
    assert any("Benutzernamen" in e for e in errors)


def test_strong_passphrase_passes():
    assert validate_password("Korrekt-Pferd-Batterie-Klammer") == []


def test_strong_password_with_identity_args_passes():
    errors = validate_password(
        "Gartenschlauch-blau-92", username="tester", email="tester@example.com"
    )
    assert errors == []
