"""Tests for :mod:`omnigent.util.tls` client-TLS trust resolution."""

from __future__ import annotations

import ssl

import certifi
import pytest

import omnigent.util.tls as tls_module
from omnigent.util.tls import client_ssl_context, resolve_ca_dir, resolve_ca_file


def _verify_paths(
    cafile: str | None, openssl_cafile: str | None, capath: str | None = None
) -> ssl.DefaultVerifyPaths:
    """Build a :class:`ssl.DefaultVerifyPaths` with the fields we read."""
    return ssl.DefaultVerifyPaths(
        cafile=cafile,
        capath=capath,
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


def test_resolve_ca_dir_returns_existing_ssl_cert_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A present ``SSL_CERT_DIR`` (capath) directory is surfaced."""
    capath = tmp_path / "certs"
    capath.mkdir()
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(None, None, capath=str(capath))
    )
    assert resolve_ca_dir() == str(capath)


def test_resolve_ca_dir_ignores_missing_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A stale/missing capath is ignored (never raises), unlike a bare load."""
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: _verify_paths(None, None, capath=str(tmp_path / "gone")),
    )
    assert resolve_ca_dir() is None


def test_resolve_ca_dir_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``SSL_CERT_DIR`` configured -> no capath."""
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    assert resolve_ca_dir() is None


def test_client_ssl_context_honors_ssl_cert_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Capath trust (``SSL_CERT_DIR``) is loaded into the context.

    A corporate CA distributed only as an OpenSSL hashed-cert directory (no
    ``SSL_CERT_FILE``) was trusted by httpx's ``trust_env`` env loading. Routing
    trust through :func:`client_ssl_context` must not silently drop it, or such
    a deployment hits ``CERTIFICATE_VERIFY_FAILED``. Asserts the context calls
    ``load_verify_locations(capath=...)`` with the configured directory.
    """
    capath = tmp_path / "certs"
    capath.mkdir()
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(None, None, capath=str(capath))
    )

    seen: list[str | None] = []
    real_load = ssl.SSLContext.load_verify_locations

    def _spy(self, cafile=None, capath=None, cadata=None):
        seen.append(capath)
        return real_load(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", _spy)
    client_ssl_context()
    assert str(capath) in seen, (
        "SSL_CERT_DIR (capath) trust must be loaded into the client context; "
        f"load_verify_locations was called with capath values {seen}"
    )
