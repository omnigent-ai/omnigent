"""Tests for :mod:`omnigent.util.tls` client-TLS trust resolution."""

from __future__ import annotations

import logging
import re
import shutil
import ssl
from pathlib import Path

import certifi
import pytest

import omnigent.util.tls as tls_module
from omnigent.util.tls import client_ssl_context, explicit_trust_sources, resolve_ca_file


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


def _hashed_cert_dir(directory: Path) -> Path:
    """Create a non-empty OpenSSL-style hashed certificate directory."""
    capath = directory / "certs"
    capath.mkdir()
    (capath / "0a1b2c3d.0").write_text("")
    return capath


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


def test_explicit_trust_sources_ignore_compiled_in_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """OpenSSL's compiled-in bundle and directory are defaults, not configuration."""
    default_dir = _hashed_cert_dir(tmp_path)
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: _verify_paths(certifi.where(), certifi.where(), capath=str(default_dir)),
    )
    assert explicit_trust_sources() == (None, None)


def test_client_ssl_context_file_only_excludes_default_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A restricted ``SSL_CERT_FILE`` trusts only that bundle.

    The compiled-in certificate directory must not widen an explicitly
    restricted trust set.
    """
    bundle = _one_root_bundle(tmp_path)
    default_dir = _hashed_cert_dir(tmp_path)
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


def test_client_ssl_context_file_wins_over_configured_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With both variables set, the bundle is the whole explicit trust set.

    An inherited ``SSL_CERT_DIR`` must not widen a restricted ``SSL_CERT_FILE``,
    matching httpx's ``trust_env`` precedence.
    """
    bundle = _one_root_bundle(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setenv("SSL_CERT_DIR", str(_hashed_cert_dir(tmp_path)))
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    seen = _spy_verify_locations(monkeypatch)

    ctx = client_ssl_context()

    assert explicit_trust_sources() == (str(bundle), None)
    assert seen == [(str(bundle), None)]
    assert len(ctx.get_ca_certs()) == 1


def test_client_ssl_context_directory_only_excludes_default_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A corporate CA shipped only as ``SSL_CERT_DIR`` is the sole trust source.

    Neither the OS bundle nor certifi is added alongside it.
    """
    capath = _hashed_cert_dir(tmp_path)
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _verify_paths(None, certifi.where())
    )
    seen = _spy_verify_locations(monkeypatch)

    ctx = client_ssl_context()

    assert explicit_trust_sources() == (None, str(capath))
    assert seen == [(None, str(capath))]
    assert ctx.get_ca_certs() == []


@pytest.mark.parametrize(
    ("variable", "make_source"),
    [
        ("SSL_CERT_FILE", lambda tmp: (tmp / "empty.pem").write_bytes(b"") or tmp / "empty.pem"),
        ("SSL_CERT_FILE", lambda tmp: tmp),
        ("SSL_CERT_DIR", lambda tmp: (tmp / "empty-certs").mkdir() or tmp / "empty-certs"),
        ("SSL_CERT_DIR", lambda tmp: _one_root_bundle(tmp)),
    ],
    ids=["zero-byte-file", "directory-as-file", "empty-directory", "file-as-directory"],
)
def test_client_ssl_context_existing_empty_source_trusts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    variable: str,
    make_source,
) -> None:
    """An existing but empty or malformed explicit source fails closed.

    It must neither raise at construction nor gain default roots; it keeps
    verifying with an empty trust set, and the operator is warned.
    """
    monkeypatch.setenv(variable, str(make_source(tmp_path)))
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))

    with caplog.at_level(logging.WARNING, logger=tls_module.__name__):
        ctx = client_ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.get_ca_certs() == []
    assert "trusting no roots" in caplog.text


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
        assert explicit_trust_sources() == (None, None)
        ctx = client_ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert len(ctx.get_ca_certs()) > 0
    assert "SSL_CERT_FILE" in caplog.text and "SSL_CERT_DIR" in caplog.text


def test_client_ssl_context_survives_directory_vanishing_mid_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A directory removed between the existence check and loading never raises.

    OpenSSL accepts a missing capath without loading anything, so the result is
    a verifying context with no roots plus a warning.
    """
    capath = _hashed_cert_dir(tmp_path)
    monkeypatch.setenv("SSL_CERT_DIR", str(capath))
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    real_create = ssl.create_default_context

    def _vanish_then_create(*args, **kwargs):
        if kwargs.get("capath") == str(capath):
            shutil.rmtree(capath)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", _vanish_then_create)
    with caplog.at_level(logging.WARNING, logger=tls_module.__name__):
        ctx = client_ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.get_ca_certs() == []
    assert "trusting no roots" in caplog.text


def test_client_ssl_context_survives_bundle_vanishing_mid_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bundle rotated away between the existence check and loading still yields a context."""
    bundle = _one_root_bundle(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _verify_paths(None, None))
    real_create = ssl.create_default_context

    def _vanish_then_create(*args, **kwargs):
        if kwargs.get("cafile") == str(bundle):
            bundle.unlink()
        return real_create(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", _vanish_then_create)
    with caplog.at_level(logging.WARNING, logger=tls_module.__name__):
        ctx = client_ssl_context()

    assert len(ctx.get_ca_certs()) > 0
    assert "vanished" in caplog.text
