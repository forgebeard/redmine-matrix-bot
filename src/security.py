"""Security primitives for auth and encrypted secrets."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PASSWORD_HASHER = PasswordHasher()
logger = logging.getLogger(__name__)


class SecurityError(RuntimeError):
    """Raised for security configuration/runtime errors."""


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except InvalidHashError:
        logger.warning("password verify: stored hash is invalid or corrupted")
        return False
    except HashingError as e:
        logger.warning("password verify: hashing error (%s): %s", type(e).__name__, e)
        return False
    except Exception as e:
        logger.warning("password verify: unexpected error (%s): %s", type(e).__name__, e)
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
    """Returns (ok, reason). login — нормализованный логин админа (без пробелов, lower)."""
    if len(password or "") < 12:
        return False, "Пароль должен содержать минимум 12 символов"
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        return False, "Пароль слишком простой"
    lo = (login or "").strip().lower()
    if lo and len(lo) >= 3 and lo in password.lower():
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


def _default_auto_master_key_path() -> Path:
    custom = (os.getenv("AUTO_MASTER_KEY_FILE") or "").strip()
    if custom:
        return Path(custom)
    root = Path(__file__).resolve().parent.parent
    return root / "data" / ".app_master_key"


def load_master_key() -> bytes:
    """
    Ключ 32 байта (UTF-8): файл ``APP_MASTER_KEY_FILE``, затем ``APP_MASTER_KEY``,
    затем автоматический файл ``data/.app_master_key`` (создаётся при первом запуске).
    """
    p = os.getenv("APP_MASTER_KEY_FILE", "/run/secrets/app_master_key")
    fp = Path(p)
    key = b""
    if fp.exists() and fp.is_file():
        raw = fp.read_text(encoding="utf-8").strip()
        key = raw.encode("utf-8")
    if not key:
        raw = (os.getenv("APP_MASTER_KEY") or "").strip()
        key = raw.encode("utf-8")
    if len(key) == 32:
        return key
    if key:
        raise SecurityError("Master key must be exactly 32 bytes")
    auto_path = _default_auto_master_key_path()
    auto_path.parent.mkdir(parents=True, exist_ok=True)
    if not auto_path.exists():
        auto_path.write_text(secrets.token_hex(16) + "\n", encoding="utf-8")
        try:
            auto_path.chmod(0o600)
        except OSError:
            pass
        logger.info("Создан автоматический master key: %s", auto_path)
    raw_auto = auto_path.read_text(encoding="utf-8").strip()
    key = raw_auto.encode("utf-8")
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

