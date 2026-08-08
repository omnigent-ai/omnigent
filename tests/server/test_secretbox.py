"""Tests for the at-rest secret encryption helper."""

from __future__ import annotations

from omnigent.server.secretbox import SecretBox


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
