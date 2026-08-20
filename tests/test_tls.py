"""Tests for :mod:`omnigent.tls` client-TLS trust resolution."""

from __future__ import annotations

import ssl

import certifi
import pytest

import omnigent.tls as tls_module
from omnigent.tls import client_ssl_context, resolve_ca_file


def _verify_paths(cafile: str | None, openssl_cafile: str | None) -> ssl.DefaultVerifyPaths:
    """Build a :class:`ssl.DefaultVerifyPaths` with the two fields we read."""
    return ssl.DefaultVerifyPaths(
        cafile=cafile,
        capath=None,
        openssl_cafile_env="SSL_CERT_FILE",
        openssl_cafile=openssl_cafile,
        openssl_capath_env="SSL_CERT_DIR",
        openssl_capath=None,
    )


@pytest.fixture(autouse=True)
def _reset_context_cache() -> None:
    """Reset the module-level cached context around each test."""
    tls_module._client_ssl_context = None
    yield
    tls_module._client_ssl_context = None


def test_resolve_ca_file_prefers_os_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A present, non-empty OS bundle (e.g. SSL_CERT_FILE) wins over certifi."""
    bundle = tmp_path / "os-ca.pem"
    bundle.write_text("-----dummy non-empty-----\n")
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(str(bundle), str(bundle))
    )
    assert resolve_ca_file() == str(bundle)


def test_resolve_ca_file_falls_back_to_certifi_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uv / python-build-standalone case: no OS cert path -> certifi."""
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    assert resolve_ca_file() == certifi.where()


def test_resolve_ca_file_skips_empty_os_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A zero-byte OS bundle is ignored in favor of certifi."""
    empty = tmp_path / "empty.pem"
    empty.write_text("")
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(str(empty), str(empty))
    )
    assert resolve_ca_file() == certifi.where()


def test_client_ssl_context_loads_certs_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no OS default path, the context still loads roots and verifies.

    This is the regression: a bare ``ssl.create_default_context()`` would load
    zero certs on the affected interpreters, so handshake verification failed.
    """
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    ctx = client_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert len(ctx.get_ca_certs()) > 0


def test_client_ssl_context_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The context is built once and reused across reconnect attempts."""
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    assert client_ssl_context() is client_ssl_context()
