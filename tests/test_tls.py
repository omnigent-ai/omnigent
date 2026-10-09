"""Tests for :mod:`omnigent.util.tls` client-TLS trust resolution."""

from __future__ import annotations

import logging
import re
import ssl
from pathlib import Path

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
        openssl_capath=capath,
    )


def _one_root_bundle(directory: Path) -> Path:
    """Write a bundle holding exactly one root taken from certifi."""
    match = re.search(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        Path(certifi.where()).read_text(),
        re.S,
    )
    assert match is not None
    bundle = directory / "one-root.pem"
    bundle.write_text(match.group(0) + "\n")
    return bundle


def _spy_verify_locations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str | None]]:
    """Record every ``(cafile, capath)`` loaded into any context."""
    seen: list[tuple[str | None, str | None]] = []
    real_load = ssl.SSLContext.load_verify_locations

    def _spy(self, cafile=None, capath=None, cadata=None):
        seen.append((cafile, capath))
        return real_load(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", _spy)
    return seen


@pytest.fixture(autouse=True)
def _isolated_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test without ambient CA env vars and with an empty context cache."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
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
    """A configured ``SSL_CERT_DIR`` that exists is surfaced."""
    capath = tmp_path / "certs"
    capath.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    assert resolve_ca_dir() == str(capath)


def test_resolve_ca_dir_ignores_missing_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale ``SSL_CERT_DIR`` is logged and ignored, never raised."""
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "gone"))
    with caplog.at_level(logging.WARNING, logger=tls_module.__name__):
        assert resolve_ca_dir() is None
    assert "SSL_CERT_DIR" in caplog.text


def test_resolve_ca_dir_none_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """OpenSSL's compiled-in directory is a default, not configuration."""
    default_dir = tmp_path / "compiled-in-certs"
    default_dir.mkdir()
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(None, None, capath=str(default_dir))
    )
    assert resolve_ca_dir() is None


def test_client_ssl_context_file_only_excludes_default_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A restricted ``SSL_CERT_FILE`` trusts only that bundle.

    The compiled-in certificate directory must not widen an explicitly
    restricted trust set.
    """
    bundle = _one_root_bundle(tmp_path)
    default_dir = tmp_path / "compiled-in-certs"
    default_dir.mkdir()
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: _verify_paths(str(bundle), str(bundle), capath=str(default_dir)),
    )
    seen = _spy_verify_locations(monkeypatch)

    ctx = client_ssl_context()

    assert seen == [(str(bundle), None)]
    assert len(ctx.get_ca_certs()) == 1


def test_client_ssl_context_directory_only_excludes_default_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A corporate CA shipped only as ``SSL_CERT_DIR`` is the sole trust source.

    Neither the OS bundle nor certifi is added alongside it.
    """
    capath = tmp_path / "certs"
    capath.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(None, certifi.where())
    )
    seen = _spy_verify_locations(monkeypatch)

    ctx = client_ssl_context()

    assert seen == [(None, str(capath))]
    assert ctx.get_ca_certs() == []


def test_client_ssl_context_stale_explicit_sources_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rotated-away ``SSL_CERT_FILE``/``SSL_CERT_DIR`` fall back to default roots.

    Construction must succeed with a verifying context instead of raising, and
    the operator is told which configured path was ignored.
    """
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "rotated-away-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "rotated-away-certs"))
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))

    with caplog.at_level(logging.WARNING, logger=tls_module.__name__):
        ctx = client_ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert len(ctx.get_ca_certs()) > 0
    assert "SSL_CERT_FILE" in caplog.text and "SSL_CERT_DIR" in caplog.text
