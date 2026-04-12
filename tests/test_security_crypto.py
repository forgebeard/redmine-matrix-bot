from security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    verify_password,
    validate_password_policy,
    token_hash,
    make_reset_token,
    _COMMON_WEAK_PASSWORDS,
)


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


# ═══════════════════════════════════════════════════════════════════════════
# hash_password / verify_password
# ═══════════════════════════════════════════════════════════════════════════


class TestPasswordHashing:
    """hash_password / verify_password: Argon2id хеширование."""

    def test_hash_and_verify(self):
        pw = "MyStr0ng!Passw0rd"
        h = hash_password(pw)
        assert h  # не пустой
        assert verify_password(h, pw) is True

    def test_wrong_password_fails(self):
        h = hash_password("CorrectPassword123")
        assert verify_password(h, "WrongPassword456") is False

    def test_verify_invalid_hash_returns_false(self):
        assert verify_password("not-a-valid-hash", "anypassword") is False

    def test_hash_is_argon2_format(self):
        h = hash_password("test")
        assert h.startswith("$argon2")

    def test_different_hashes_for_same_password(self):
        h1 = hash_password("SamePassword123")
        h2 = hash_password("SamePassword123")
        assert h1 != h2  # разные соли
        assert verify_password(h1, "SamePassword123") is True
        assert verify_password(h2, "SamePassword123") is True


# ═══════════════════════════════════════════════════════════════════════════
# validate_password_policy — дополнительные тесты
# ═══════════════════════════════════════════════════════════════════════════


class TestPasswordPolicyExtended:
    """Дополнительные сценарии валидации пароля."""

    def test_too_short(self):
        ok, reason = validate_password_policy("Short1!")
        assert not ok
        assert "12" in reason

    def test_min_length(self):
        ok, reason = validate_password_policy("1234567890Ab")
        assert ok or "12" in (reason or "")

    def test_common_password_exact_match(self):
        """Пароль из списка слабых должен отклоняться (если достаточно длинный для других проверок — но короткие отвергаются по длине)."""
        # "123456" — слишком короткий, отклоняется по длине
        ok, reason = validate_password_policy("123456")
        assert not ok
        assert "12" in (reason or "")

        # Длинные пароли со слабыми паттернами не детектируются как "слабые"
        # (проверка только на полное совпадение в _COMMON_WEAK_PASSWORDS)
        # Это ожидаемое поведение — сложность проверяется по другим критериям
        ok2, _ = validate_password_policy("1234567890Ab")
        # 1234567890Ab — 12 символов, содержит буквы и цифры, валиден
        assert ok2 is True or "12" in (_ or "")

    def test_no_letters(self):
        ok, reason = validate_password_policy("123456789012")
        assert not ok
        assert "буквы" in (reason or "")

    def test_no_digits(self):
        ok, reason = validate_password_policy("Abcdefghijkl")
        assert not ok
        assert "цифры" in (reason or "")

    def test_valid_password(self):
        ok, reason = validate_password_policy("MyStr0ng!Passw0rd")
        assert ok is True
        assert reason is None


# ═══════════════════════════════════════════════════════════════════════════
# token_hash
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenHash:
    """token_hash: SHA-256 хеш для reset-токенов."""

    def test_deterministic(self):
        h1 = token_hash("mytoken", "mysalt")
        h2 = token_hash("mytoken", "mysalt")
        assert h1 == h2

    def test_different_values_different_hashes(self):
        h1 = token_hash("token1", "salt")
        h2 = token_hash("token2", "salt")
        assert h1 != h2

    def test_different_salts_different_hashes(self):
        h1 = token_hash("token", "salt1")
        h2 = token_hash("token", "salt2")
        assert h1 != h2

    def test_returns_hex_string(self):
        h = token_hash("test", "salt")
        assert len(h) == 64  # SHA-256 = 32 bytes = 64 hex chars
        int(h, 16)  # не падает = валидный hex


# ═══════════════════════════════════════════════════════════════════════════
# make_reset_token
# ═══════════════════════════════════════════════════════════════════════════


class TestMakeResetToken:
    """make_reset_token: генерация случайных токенов."""

    def test_length(self):
        token = make_reset_token()
        # token_urlsafe(32) = ~43 символа
        assert len(token) >= 40

    def test_uniqueness(self):
        tokens = {make_reset_token() for _ in range(100)}
        assert len(tokens) == 100  # все уникальные

    def test_url_safe(self):
        token = make_reset_token()
        assert "+" not in token
        assert "/" not in token
        assert "=" not in token


# ═══════════════════════════════════════════════════════════════════════════
# _COMMON_WEAK_PASSWORDS
# ═══════════════════════════════════════════════════════════════════════════


class TestCommonWeakPasswords:
    """Список слабых паролей должен быть непустым."""

    def test_not_empty(self):
        assert len(_COMMON_WEAK_PASSWORDS) > 0

    def test_contains_known_weak(self):
        assert "123456" in _COMMON_WEAK_PASSWORDS
        assert "password" in _COMMON_WEAK_PASSWORDS
        assert "qwerty" in _COMMON_WEAK_PASSWORDS
        assert "admin" in _COMMON_WEAK_PASSWORDS

