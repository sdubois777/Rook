"""
Security utilities — token generation, hashing.
Never store plaintext secrets.
"""
import hashlib
import secrets


def generate_secure_token(length: int = 32) -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """
    SHA-256 hash a token for storage.
    Store the hash, not the raw token.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    """Timing-safe string comparison to prevent timing attacks."""
    return secrets.compare_digest(a.encode(), b.encode())
