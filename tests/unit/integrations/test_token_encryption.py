"""Tests for token encryption — Fernet symmetric encryption."""
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet


# Generate a real test key for the test module
_TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _patch_settings():
    """Inject test encryption key for all tests."""
    with patch(
        "backend.integrations.token_encryption.settings"
    ) as mock_settings:
        mock_settings.platform_token_encryption_key = _TEST_KEY
        yield


def test_encrypt_decrypt_roundtrip():
    from backend.integrations.token_encryption import (
        decrypt_token, encrypt_token,
    )
    original = "test_token_xyz_123"
    encrypted = encrypt_token(original)
    assert encrypted != original
    assert decrypt_token(encrypted) == original


def test_empty_token_returns_empty():
    from backend.integrations.token_encryption import (
        decrypt_token, encrypt_token,
    )
    assert encrypt_token("") == ""
    assert decrypt_token("") == ""


def test_wrong_key_raises_value_error():
    from backend.integrations.token_encryption import encrypt_token

    encrypted = encrypt_token("secret_value")

    # Change the key — decryption should fail
    wrong_key = Fernet.generate_key().decode()
    with patch(
        "backend.integrations.token_encryption.settings"
    ) as mock_settings:
        mock_settings.platform_token_encryption_key = wrong_key
        from backend.integrations.token_encryption import decrypt_token
        with pytest.raises(ValueError, match="decryption failed"):
            decrypt_token(encrypted)


def test_encrypt_produces_different_ciphertext_each_time():
    from backend.integrations.token_encryption import encrypt_token
    token = "same_token"
    a = encrypt_token(token)
    b = encrypt_token(token)
    # Fernet uses random IV — ciphertexts should differ
    assert a != b


def test_no_key_raises_runtime_error():
    with patch(
        "backend.integrations.token_encryption.settings"
    ) as mock_settings:
        mock_settings.platform_token_encryption_key = ""
        from backend.integrations.token_encryption import encrypt_token
        with pytest.raises(RuntimeError, match="not set"):
            encrypt_token("test")
