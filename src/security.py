"""Security primitives for auth and encrypted secrets."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PASSWORD_HASHER = PasswordHasher()


class SecurityError(RuntimeError):
    """Raised for security configuration/runtime errors."""


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


_COMMON_WEAK_PASSWORDS = {
    "123456",
    "123456789",
    "qwerty",
    "password",
    "admin",
    "letmein",
    "111111",
    "000000",
    "123123",
}


def validate_password_policy(password: str, login: str = "") -> tuple[bool, str | None]:
    """Returns (ok, reason)."""
    if len(password or "") < 12:
        return False, "Пароль должен содержать минимум 12 символов"
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        return False, "Пароль слишком простой"
    if login and login.lower() in password.lower():
        return False, "Пароль не должен содержать логин"
    if not re.search(r"[A-Za-zА-Яа-я]", password):
        return False, "Пароль должен содержать буквы"
    if not re.search(r"\d", password):
        return False, "Пароль должен содержать цифры"
    return True, None


def token_hash(value: str, salt: str) -> str:
    return hashlib.sha256((value + salt).encode("utf-8")).hexdigest()


def make_reset_token() -> str:
    return secrets.token_urlsafe(32)


def load_master_key() -> bytes:
    """
    Load 32-byte key from Docker secret file.
    In tests/dev can fallback to APP_MASTER_KEY env.
    """
    p = os.getenv("APP_MASTER_KEY_FILE", "/run/secrets/app_master_key")
    fp = Path(p)
    key = b""
    if fp.exists() and fp.is_file():
        raw = fp.read_text(encoding="utf-8").strip()
        key = raw.encode("utf-8")
    # Fallback for local/dev when file secret is not available or is mis-mounted.
    if not key:
        raw = (os.getenv("APP_MASTER_KEY") or "").strip()
        key = raw.encode("utf-8")
    if len(key) != 32:
        raise SecurityError("Master key must be exactly 32 bytes")
    return key


@dataclass
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: int = 1


def encrypt_secret(plaintext: str, key: bytes, key_version: int = 1) -> EncryptedSecret:
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return EncryptedSecret(ciphertext=ciphertext, nonce=nonce, key_version=key_version)


def decrypt_secret(ciphertext: bytes, nonce: bytes, key: bytes) -> str:
    aes = AESGCM(key)
    pt = aes.decrypt(nonce, ciphertext, None)
    return pt.decode("utf-8")

