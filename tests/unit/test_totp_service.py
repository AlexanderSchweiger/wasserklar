"""Unit-Tests fuer app.auth.totp_service (pure Funktionen, ohne DB/Request)."""
import json
import time

import pyotp

from app.auth import totp_service


class TestTotpCodes:
    def test_new_secret_is_usable_base32(self):
        secret = totp_service.new_secret()
        assert isinstance(secret, str) and len(secret) >= 16
        # Laesst sich als TOTP-Secret verwenden
        assert pyotp.TOTP(secret).now()

    def test_verify_accepts_current_code(self):
        secret = totp_service.new_secret()
        assert totp_service.verify_code(secret, pyotp.TOTP(secret).now()) is True

    def test_verify_rejects_wrong_code(self):
        secret = totp_service.new_secret()
        current = pyotp.TOTP(secret).now()
        wrong = "654321" if current != "654321" else "123456"
        assert totp_service.verify_code(secret, wrong) is False

    def test_verify_tolerates_one_step_drift(self):
        secret = totp_service.new_secret()
        prev = pyotp.TOTP(secret).at(time.time() - 30)
        assert totp_service.verify_code(secret, prev) is True

    def test_verify_rejects_two_step_drift(self):
        secret = totp_service.new_secret()
        old = pyotp.TOTP(secret).at(time.time() - 90)
        assert totp_service.verify_code(secret, old) is False

    def test_verify_empty_inputs(self):
        assert totp_service.verify_code("", "123456") is False
        assert totp_service.verify_code(totp_service.new_secret(), "") is False


class TestRecoveryCodes:
    def test_generate_returns_n_distinct_codes(self):
        plaintexts, hashes_json = totp_service.generate_recovery_codes(n=10)
        assert len(plaintexts) == 10
        assert len(set(plaintexts)) == 10
        assert len(json.loads(hashes_json)) == 10
        # Klartext wird NICHT gespeichert (nur Hashes)
        assert all(p not in hashes_json for p in plaintexts)

    def test_match_consumes_code_once(self):
        plaintexts, hashes_json = totp_service.generate_recovery_codes(n=3)
        matched, new_json = totp_service.match_and_remove(hashes_json, plaintexts[0])
        assert matched is True
        assert len(json.loads(new_json)) == 2
        # Zweiter Versuch mit demselben Code scheitert
        matched2, _ = totp_service.match_and_remove(new_json, plaintexts[0])
        assert matched2 is False

    def test_match_accepts_dashed_and_lowercase(self):
        plaintexts, hashes_json = totp_service.generate_recovery_codes(n=2)
        matched, _ = totp_service.match_and_remove(hashes_json, plaintexts[0].lower())
        assert matched is True

    def test_match_unknown_code_returns_input_unchanged(self):
        _, hashes_json = totp_service.generate_recovery_codes(n=2)
        matched, new_json = totp_service.match_and_remove(hashes_json, "ZZZZ-ZZZZ-ZZ")
        assert matched is False
        assert new_json == hashes_json

    def test_match_empty_inputs(self):
        assert totp_service.match_and_remove(None, "ABCD") == (False, None)
        _, hashes_json = totp_service.generate_recovery_codes(n=1)
        assert totp_service.match_and_remove(hashes_json, "") == (False, hashes_json)

    def test_normalize_code_strips_and_uppercases(self):
        assert totp_service.normalize_code(" ab-cd 12 ") == "ABCD12"
