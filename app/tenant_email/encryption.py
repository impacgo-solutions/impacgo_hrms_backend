"""Authenticated encryption (Fernet: AES-128-CBC + HMAC-SHA256) for per-tenant
email secrets stored in public.tenant_email_settings.

The only key material lives in the environment (TENANT_EMAIL_ENCRYPTION_KEY),
never in the database, an API response, a log line or an exception message.
Several comma-separated keys are accepted for rotation: the FIRST encrypts,
every key can decrypt. If the key is missing or invalid, encrypt/decrypt
raise EncryptionConfigError -- the app never falls back to plaintext.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from ..config import settings

_PREFIX = "enc:v1:"


class EncryptionConfigError(RuntimeError):
    """TENANT_EMAIL_ENCRYPTION_KEY is missing/invalid, or a value cannot be decrypted."""


def _fernet() -> MultiFernet:
    raw = (settings.tenant_email_encryption_key or "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise EncryptionConfigError(
            "TENANT_EMAIL_ENCRYPTION_KEY is not set; tenant email secrets cannot be stored or read."
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise EncryptionConfigError(
            "TENANT_EMAIL_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)."
        ) from exc


def is_configured() -> bool:
    try:
        _fernet()
        return True
    except EncryptionConfigError:
        return False


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_secret(value: str) -> str:
    if value is None or value == "":
        raise ValueError("Cannot encrypt an empty secret.")
    return _PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    """Plaintext of [value]. Raises EncryptionConfigError (never echoing the
    ciphertext) if it is not ours or the key changed."""
    if not value or not value.startswith(_PREFIX):
        raise EncryptionConfigError("Stored secret is not in the expected encrypted format.")
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionConfigError(
            "A stored email secret could not be decrypted (encryption key changed?). Re-enter it."
        ) from None
