from security import decrypt_secret, encrypt_secret, validate_password_policy


def test_encryption_unique_nonce():
    key = b"0123456789abcdef0123456789abcdef"
    enc1 = encrypt_secret("secret", key=key)
    enc2 = encrypt_secret("secret", key=key)
    assert enc1.ciphertext != enc2.ciphertext
    assert enc1.nonce != enc2.nonce
    assert decrypt_secret(enc1.ciphertext, enc1.nonce, key) == "secret"
    assert decrypt_secret(enc2.ciphertext, enc2.nonce, key) == "secret"


def test_password_policy_rejects_login_substring():
    ok, _ = validate_password_policy("MySecurePass99", login="myuser")
    assert ok
    bad, reason = validate_password_policy("prefix_myuser_suffix99", login="myuser")
    assert not bad
    assert reason and "логин" in reason

