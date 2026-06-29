"""Tests for the per-user secret vault crypto (#5).

The security keystone: a stable, persisted key; authenticated encryption that
produces ciphertext (never plaintext) and round-trips; and a wrong key cannot
decrypt (so a leaked DB without the key is useless).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from omnigent.server.secret_vault import decrypt_secret, encrypt_secret, load_or_create_vault_key


def test_key_is_generated_then_reused(tmp_path: Path) -> None:
    key_path = tmp_path / "vault.key"
    first = load_or_create_vault_key(key_path)
    assert key_path.exists()
    # A second call reuses the persisted key (stability — else stored secrets
    # would become undecryptable across restarts).
    assert load_or_create_vault_key(key_path) == first


def test_encrypt_decrypt_roundtrip(tmp_path: Path) -> None:
    key = load_or_create_vault_key(tmp_path / "k")
    token = encrypt_secret(key, "ghp_secrettoken")
    assert token != "ghp_secrettoken"  # stored value is ciphertext, not plaintext
    assert decrypt_secret(key, token) == "ghp_secrettoken"


def test_wrong_key_cannot_decrypt(tmp_path: Path) -> None:
    token = encrypt_secret(load_or_create_vault_key(tmp_path / "k1"), "s3cret")
    with pytest.raises(InvalidToken):
        decrypt_secret(load_or_create_vault_key(tmp_path / "k2"), token)
