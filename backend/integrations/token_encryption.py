"""
Fernet symmetric encryption for OAuth tokens and cookies.
Never store plaintext credentials in the database.
"""
from cryptography.fernet import Fernet, InvalidToken
from backend.config import settings


def _fernet() -> Fernet:
    key = settings.platform_token_encryption_key
    if not key:
        raise RuntimeError("PLATFORM_TOKEN_ENCRYPTION_KEY not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(token: str) -> str:
    if not token:
        return ""
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Token decryption failed — encryption key may have changed"
        ) from e
