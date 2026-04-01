import logging

import pytest

from security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    validate_password_policy,
    verify_password,
)


def test_encryption_unique_nonce():
    key = b"0123456789abcdef0123456789abcdef"
    enc1 = encrypt_secret("secret", key=key)
    enc2 = encrypt_secret("secret", key=key)
    assert enc1.ciphertext != enc2.ciphertext
    assert enc1.nonce != enc2.nonce
    assert decrypt_secret(enc1.ciphertext, enc1.nonce, key) == "secret"
    assert decrypt_secret(enc2.ciphertext, enc2.nonce, key) == "secret"


def test_verify_password_roundtrip():
    h = hash_password("GoodPassword123")
    assert verify_password(h, "GoodPassword123") is True
    assert verify_password(h, "WrongPassword123") is False


def test_validate_password_policy_rejects_login_substring():
    ok, reason = validate_password_policy("StrongPassword123x", login="strong")
    assert ok is False
    assert reason and "логин" in reason.lower()


def test_validate_password_policy_allows_without_login_clash():
    ok, _ = validate_password_policy("StrongPassword123", login="admin")
    assert ok is True


def test_verify_password_invalid_hash_logs_warning(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="security")
    assert verify_password("not-a-valid-argon2-hash", "anything") is False
    assert any("password verify" in r.message for r in caplog.records)

