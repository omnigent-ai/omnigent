"""Tests for the at-rest secret encryption helper."""

from __future__ import annotations

import pytest

from omnigent.server.secretbox import (
    CREDENTIAL_ENC_KEY_ENV_VAR,
    SecretBox,
    build_secret_cipher,
)


def test_encrypt_decrypt_roundtrip() -> None:
    box = SecretBox("some-key-material")
    ct = box.encrypt("ghu_secret_token")
    assert ct != "ghu_secret_token"
    assert box.decrypt(ct) == "ghu_secret_token"


def test_same_secret_interops() -> None:
    """Two boxes with the same secret decrypt each other's ciphertext."""
    a = SecretBox("shared")
    b = SecretBox("shared")
    assert b.decrypt(a.encrypt("hello")) == "hello"


def test_wrong_key_returns_none() -> None:
    ct = SecretBox("key-one").encrypt("payload")
    assert SecretBox("key-two").decrypt(ct) is None


def test_garbage_ciphertext_returns_none() -> None:
    assert SecretBox("key").decrypt("not-a-fernet-token") is None


def test_build_secret_cipher_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CREDENTIAL_ENC_KEY_ENV_VAR, raising=False)
    assert build_secret_cipher() is None


def test_build_secret_cipher_from_store_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_ENC_KEY_ENV_VAR, "store-key-material")
    cipher = build_secret_cipher()
    assert cipher is not None
    # The store-level key drives a working Fernet cipher, independent of any
    # provider (the same key material a SecretBox would use directly).
    assert cipher.decrypt(cipher.encrypt("secret")) == "secret"
    assert SecretBox("store-key-material").decrypt(cipher.encrypt("secret")) == "secret"
