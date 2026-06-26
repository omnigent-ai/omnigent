"""Tests for ``omnigent/onboarding/cursor_auth.py`` — the Cursor API-key store.

Cursor's ``CURSOR_API_KEY`` lives in a dedicated top-level ``cursor:`` config
block (not the shared global ``auth:``) and the omnigent secret store, resolved
with the same ``resolve_secret`` resolver the provider families use. These
tests isolate the config + secret store to a tmp dir (file backend, no OS
keychain) and assert the read/resolve/configured helpers behave — including the
**soft** resolution that returns ``None`` on a dangling reference instead of
raising, so a run / setup readout falls back rather than crashing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding import cursor_auth, extra_install
from omnigent.onboarding import secrets as secret_store
from omnigent.onboarding.cursor_auth import (
    CURSOR_SECRET_NAME,
    cursor_api_key_configured,
    cursor_api_key_ref,
    cursor_api_key_settings,
    cursor_install_command,
    cursor_sdk_installed,
    install_cursor_sdk,
    looks_like_cursor_api_key,
    resolve_cursor_api_key,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config + secrets to tmp with the file secret backend.

    :returns: The tmp config-home dir, so a test can write a ``config.yaml``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    return tmp_path


def _write_config(tmp_path: Path, block: dict[str, object]) -> None:
    """Write *block* as the isolated ``config.yaml``."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(block))


def test_looks_like_cursor_api_key() -> None:
    """The soft prefix check accepts ``crsr_`` keys and rejects others."""
    assert looks_like_cursor_api_key("crsr_AbC123")
    assert not looks_like_cursor_api_key("sk-ant-123")
    assert not looks_like_cursor_api_key("")


def test_unconfigured_reads_as_none(_isolate: Path) -> None:
    """With no ``cursor:`` block, every accessor reports "not configured"."""
    assert cursor_api_key_ref() is None
    assert resolve_cursor_api_key() is None
    assert cursor_api_key_configured() is False


def test_keychain_ref_resolves(_isolate: Path) -> None:
    """A ``keychain:`` ref resolves to the secret stored under that name."""
    secret_store.store_secret(CURSOR_SECRET_NAME, "crsr_stored")
    _write_config(_isolate, {"cursor": {"api_key_ref": "keychain:cursor"}})
    assert cursor_api_key_ref() == "keychain:cursor"
    assert resolve_cursor_api_key() == "crsr_stored"
    assert cursor_api_key_configured() is True


def test_env_ref_resolves(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref resolves from the environment (no secret-store entry)."""
    monkeypatch.setenv("MY_CURSOR_KEY", "crsr_fromenv")
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:MY_CURSOR_KEY"}})
    assert resolve_cursor_api_key() == "crsr_fromenv"
    assert cursor_api_key_configured() is True


def test_env_ref_strips_surrounding_whitespace(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitespace-padded ``env:`` key validates and resolves cleanly (F103).

    A ``CURSOR_API_KEY`` exported with a stray leading/trailing newline (a
    common ``export $(…)`` mishap) must strip before the ``crsr_`` prefix
    check and before forwarding to the SDK — else the padding fails the
    ``looks_like`` validation and reaches the harness verbatim.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "\ncrsr_test\n")
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:CURSOR_API_KEY"}})
    resolved = resolve_cursor_api_key()
    assert resolved == "crsr_test"
    assert looks_like_cursor_api_key(resolved)
    assert cursor_api_key_configured() is True


def test_empty_env_ref_reads_as_unconfigured(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured ``env:`` ref pointing at an EMPTY var reads as not-configured.

    The shared ``resolve_secret`` ``env:`` branch only raises on an *unset*
    variable, so an exported-but-empty ``CURSOR_API_KEY=""`` (or all-whitespace)
    resolves to ``""``. The cursor layer must fold that to ``None`` so the setup
    readout (``cursor_api_key_configured``) and the spawn-env builder agree —
    both treat it as unset rather than claiming a key the runtime won't forward.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "")
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:CURSOR_API_KEY"}})
    assert resolve_cursor_api_key() is None
    assert cursor_api_key_configured() is False


def test_whitespace_only_env_ref_reads_as_unconfigured(
    _isolate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured ``env:`` ref pointing at an all-whitespace var is not-configured."""
    monkeypatch.setenv("CURSOR_API_KEY", "   \n\t  ")
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:CURSOR_API_KEY"}})
    assert resolve_cursor_api_key() is None
    assert cursor_api_key_configured() is False


def test_inline_api_key_field_accepted(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited inline ``api_key: $VAR`` is honored as a fallback shape."""
    monkeypatch.setenv("INLINE_CURSOR", "crsr_inline")
    _write_config(_isolate, {"cursor": {"api_key": "$INLINE_CURSOR"}})
    assert resolve_cursor_api_key() == "crsr_inline"


def test_dangling_keychain_ref_is_soft_none(_isolate: Path) -> None:
    """A reference to a never-stored keychain entry resolves softly to ``None``.

    Failure (an ``OmnigentError`` escaping) would crash a cursor run / the
    setup readout on a deleted secret instead of falling back to cursor's own
    login.
    """
    _write_config(_isolate, {"cursor": {"api_key_ref": "keychain:cursor"}})
    assert resolve_cursor_api_key() is None
    assert cursor_api_key_configured() is False


def test_unset_env_ref_is_soft_none(_isolate: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``env:`` ref to an unset variable resolves softly to ``None``."""
    monkeypatch.delenv("NOPE_CURSOR_KEY", raising=False)
    _write_config(_isolate, {"cursor": {"api_key_ref": "env:NOPE_CURSOR_KEY"}})
    assert resolve_cursor_api_key() is None


def test_settings_shape() -> None:
    """``cursor_api_key_settings`` builds the dedicated ``cursor:`` block."""
    assert cursor_api_key_settings("keychain:cursor") == {
        "cursor": {"api_key_ref": "keychain:cursor"}
    }


# ── SDK-extra detection + install (the optional ``cursor`` extra) ─────────────
# ``cursor-sdk`` is now an OPTIONAL extra, so setup must detect a missing SDK
# and offer to install it.


def test_cursor_sdk_installed_true_when_spec_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection returns True when ``find_spec`` resolves ``cursor_sdk``."""
    monkeypatch.setattr(
        cursor_auth.importlib.util,
        "find_spec",
        lambda name: object(),
    )
    assert cursor_sdk_installed() is True


def test_cursor_sdk_installed_false_when_spec_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection returns False when ``find_spec`` returns ``None`` (extra absent)."""
    monkeypatch.setattr(cursor_auth.importlib.util, "find_spec", lambda name: None)
    assert cursor_sdk_installed() is False


def test_cursor_sdk_installed_false_when_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ModuleNotFoundError`` from ``find_spec`` reads as not-installed.

    ``find_spec`` can raise (not return ``None``) when a parent package is
    absent; the guard must swallow that rather than crash setup.
    """

    def _raise(name: str) -> object:
        raise ModuleNotFoundError("No module named 'cursor_sdk'")

    monkeypatch.setattr(cursor_auth.importlib.util, "find_spec", _raise)
    assert cursor_sdk_installed() is False


def test_cursor_install_command_prefers_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``uv`` on PATH, the install runs ``uv pip install`` — no index URL."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: "/usr/bin/uv")
    cmd = cursor_install_command()
    assert cmd == ["uv", "pip", "install", "omnigent[cursor]"]
    assert not any("index" in part or "://" in part for part in cmd)


def test_cursor_install_command_falls_back_to_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``uv``, it falls back to this interpreter's pip — still no index."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    cmd = cursor_install_command()
    assert cmd == [
        extra_install.sys.executable,
        "-m",
        "pip",
        "install",
        "omnigent[cursor]",
    ]
    assert not any("index" in part or "://" in part for part in cmd)


def test_cursor_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a ``uv tool`` venv, the install uses ``uv tool install --with``."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: True)
    cmd = cursor_install_command()
    assert cmd == ["uv", "tool", "install", "--with", "omnigent[cursor]", "omnigent", "--force"]


def test_install_cursor_sdk_runs_command_then_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shells the install argv, then reports the post-install detection verdict.

    Mocks the subprocess (never really installs): the SDK "appears" only after
    the install runs, so the function must re-check and return True.
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
    monkeypatch.setattr(cursor_auth.subprocess, "run", _run)
    monkeypatch.setattr(cursor_auth, "cursor_sdk_installed", lambda: state["installed"])

    assert install_cursor_sdk() is True
    assert calls == [[extra_install.sys.executable, "-m", "pip", "install", "omnigent[cursor]"]]


def test_install_cursor_sdk_false_on_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess that can't spawn (OSError) is caught and reported as False."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("no pip")

    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(cursor_auth.subprocess, "run", _boom)
    assert install_cursor_sdk() is False
