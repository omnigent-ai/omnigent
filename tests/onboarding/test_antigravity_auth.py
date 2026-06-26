"""Tests for ``omnigent/onboarding/antigravity_auth.py`` — the Gemini key store.

Isolate config + secret store to a tmp dir (file backend) and assert the
read/resolve/configured helpers — including the soft resolution that returns
``None`` on a dangling reference instead of raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding import antigravity_auth, extra_install
from omnigent.onboarding import secrets as secret_store
from omnigent.onboarding.antigravity_auth import (
    ANTIGRAVITY_SECRET_NAME,
    antigravity_api_key_configured,
    antigravity_api_key_ref,
    antigravity_api_key_settings,
    antigravity_install_command,
    antigravity_sdk_installed,
    install_antigravity_sdk,
    looks_like_gemini_api_key,
    resolve_antigravity_api_key,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config + secrets to tmp with the file secret backend.

    :returns: The tmp config-home dir, so a test can write a ``config.yaml``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    return tmp_path


def _write_config(tmp_path: Path, block: dict[str, object]) -> None:
    """Write *block* as the isolated ``config.yaml``."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(block))


def test_looks_like_gemini_api_key() -> None:
    """The soft prefix check accepts ``AIza`` / ``AQ`` keys and rejects others."""
    assert looks_like_gemini_api_key("AIzaSyAbC123")
    assert looks_like_gemini_api_key("AQ.AbC123")
    assert not looks_like_gemini_api_key("sk-ant-123")
    assert not looks_like_gemini_api_key("")


def test_unconfigured_reads_as_none(_isolate: Path) -> None:
    """With no ``antigravity:`` block, every accessor reports "not configured"."""
    assert antigravity_api_key_ref() is None
    assert resolve_antigravity_api_key() is None
    assert antigravity_api_key_configured() is False


def test_keychain_ref_resolves(_isolate: Path) -> None:
    """A ``keychain:`` ref resolves to the secret stored under that name."""
    secret_store.store_secret(ANTIGRAVITY_SECRET_NAME, "AIza_stored")
    _write_config(_isolate, {"antigravity": {"api_key_ref": "keychain:antigravity"}})
    assert antigravity_api_key_ref() == "keychain:antigravity"
    assert resolve_antigravity_api_key() == "AIza_stored"
    assert antigravity_api_key_configured() is True


def test_env_ref_resolves(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref resolves from the environment (no secret-store entry)."""
    monkeypatch.setenv("MY_GEMINI_KEY", "AIza_fromenv")
    _write_config(_isolate, {"antigravity": {"api_key_ref": "env:MY_GEMINI_KEY"}})
    assert resolve_antigravity_api_key() == "AIza_fromenv"
    assert antigravity_api_key_configured() is True


def test_inline_api_key_field_accepted(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited inline ``api_key: $VAR`` is honored as a fallback shape."""
    monkeypatch.setenv("INLINE_GEMINI", "AIza_inline")
    _write_config(_isolate, {"antigravity": {"api_key": "$INLINE_GEMINI"}})
    assert resolve_antigravity_api_key() == "AIza_inline"


def test_dangling_keychain_ref_is_soft_none(_isolate: Path) -> None:
    """A reference to a never-stored keychain entry resolves softly to ``None``.

    Failure (an ``OmnigentError`` escaping) would crash an antigravity run / the
    setup readout on a deleted secret instead of falling back to the SDK's
    ambient / Vertex credentials.
    """
    _write_config(_isolate, {"antigravity": {"api_key_ref": "keychain:antigravity"}})
    assert resolve_antigravity_api_key() is None
    assert antigravity_api_key_configured() is False


def test_unset_env_ref_is_soft_none(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref to an unset variable resolves softly to ``None``."""
    monkeypatch.delenv("NOPE_GEMINI_KEY", raising=False)
    _write_config(_isolate, {"antigravity": {"api_key_ref": "env:NOPE_GEMINI_KEY"}})
    assert resolve_antigravity_api_key() is None


def test_settings_shape() -> None:
    """``antigravity_api_key_settings`` builds the dedicated ``antigravity:`` block."""
    assert antigravity_api_key_settings("keychain:antigravity") == {
        "antigravity": {"api_key_ref": "keychain:antigravity"}
    }


# ── SDK-extra detection + install (the optional ``antigravity`` extra) ────────


def test_antigravity_sdk_installed_true_when_spec_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection returns True when ``find_spec`` resolves ``google.antigravity``."""
    monkeypatch.setattr(
        antigravity_auth.importlib.util,
        "find_spec",
        lambda name: object(),
    )
    assert antigravity_sdk_installed() is True


def test_antigravity_sdk_installed_false_when_spec_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection returns False when ``find_spec`` returns ``None`` (extra absent)."""
    monkeypatch.setattr(antigravity_auth.importlib.util, "find_spec", lambda name: None)
    assert antigravity_sdk_installed() is False


def test_antigravity_sdk_installed_false_when_namespace_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ModuleNotFoundError`` (no ``google`` parent namespace) reads as False.

    ``find_spec`` *raises* (not ``None``) when the parent namespace is absent; the guard
    must swallow that and report not-installed rather than crash setup.
    """

    def _raise(name: str) -> object:
        raise ModuleNotFoundError("No module named 'google'")

    monkeypatch.setattr(antigravity_auth.importlib.util, "find_spec", _raise)
    assert antigravity_sdk_installed() is False


def test_antigravity_install_command_prefers_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``uv`` on PATH, the install runs ``uv pip install`` — no index URL."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: "/usr/bin/uv")
    cmd = antigravity_install_command()
    assert cmd == ["uv", "pip", "install", "omnigent[antigravity]"]
    assert not any("index" in part or "://" in part for part in cmd)


def test_antigravity_install_command_falls_back_to_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``uv``, it falls back to this interpreter's pip — still no index."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    cmd = antigravity_install_command()
    assert cmd == [
        extra_install.sys.executable,
        "-m",
        "pip",
        "install",
        "omnigent[antigravity]",
    ]
    assert not any("index" in part or "://" in part for part in cmd)


def test_antigravity_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a ``uv tool`` venv, the install uses ``uv tool install --with``."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: True)
    cmd = antigravity_install_command()
    assert cmd == [
        "uv",
        "tool",
        "install",
        "--with",
        "omnigent[antigravity]",
        "omnigent",
        "--force",
    ]


def test_install_antigravity_sdk_runs_command_then_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shells the install argv, then reports the post-install detection verdict.

    The mocked SDK "appears" only after the install runs, so the function must re-check
    and return True.
    """
    import subprocess

    calls: list[list[str]] = []
    state = {"installed": False}

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        state["installed"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(antigravity_auth.subprocess, "run", _run)
    monkeypatch.setattr(antigravity_auth, "antigravity_sdk_installed", lambda: state["installed"])

    assert install_antigravity_sdk() is True
    assert calls == [
        [extra_install.sys.executable, "-m", "pip", "install", "omnigent[antigravity]"]
    ]


def test_install_antigravity_sdk_false_on_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess that can't spawn (OSError) is caught and reported as False."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("no pip")

    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(antigravity_auth.subprocess, "run", _boom)
    assert install_antigravity_sdk() is False
