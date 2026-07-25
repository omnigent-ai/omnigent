"""Tests for ``omnigent/onboarding/copilot_auth.py`` — the Copilot token store.

Copilot's GitHub token lives in a dedicated top-level ``copilot:`` config block
(not the shared global ``auth:``) and the omnigent secret store, resolved with
the same ``resolve_secret`` resolver the provider families use. These tests
isolate the config + secret store to a tmp dir (file backend, no OS keychain)
and assert the read/resolve/configured helpers behave — including the **soft**
resolution that returns ``None`` on a dangling reference instead of raising, so
a run / setup readout falls back rather than crashing. Mirrors
``test_cursor_auth.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.onboarding import copilot_auth, extra_install
from omnigent.onboarding import secrets as secret_store
from omnigent.onboarding.copilot_auth import (
    COPILOT_SECRET_NAME,
    copilot_github_token_configured,
    copilot_github_token_ref,
    copilot_github_token_settings,
    copilot_install_command,
    copilot_sdk_installed,
    install_copilot_sdk,
    looks_like_github_copilot_token,
    resolve_copilot_github_token,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config + secrets to tmp with the file secret backend.

    :returns: The tmp config-home dir, so a test can write a ``config.yaml``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write_config(tmp_path: Path, block: dict[str, object]) -> None:
    """Write *block* as the isolated ``config.yaml``."""
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(block))


# ---------------------------------------------------------------------------
# Token-shape check
# ---------------------------------------------------------------------------


def test_looks_like_github_copilot_token_accepts_pat_and_oauth() -> None:
    assert looks_like_github_copilot_token("github_pat_ABC123")
    assert looks_like_github_copilot_token("gho_ABC123")
    assert looks_like_github_copilot_token("ghu_ABC123")
    assert looks_like_github_copilot_token("ghs_ABC123")


def test_looks_like_github_copilot_token_rejects_classic_pat_and_junk() -> None:
    # Classic ``ghp_`` PATs are not accepted by Copilot.
    assert not looks_like_github_copilot_token("ghp_ABC123")
    assert not looks_like_github_copilot_token("")
    assert not looks_like_github_copilot_token("sk-not-a-github-token")


# ---------------------------------------------------------------------------
# ref / resolve / configured
# ---------------------------------------------------------------------------


def test_ref_and_resolve_none_when_no_block(tmp_path: Path) -> None:
    assert copilot_github_token_ref() is None
    assert resolve_copilot_github_token() is None
    assert copilot_github_token_configured() is False


def test_ref_prefers_token_ref_over_inline(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"copilot": {"github_token_ref": "env:GH_TOKEN", "github_token": "literal"}},
    )
    assert copilot_github_token_ref() == "env:GH_TOKEN"


def test_resolve_from_env_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, {"copilot": {"github_token_ref": "env:GH_TOKEN"}})
    monkeypatch.setenv("GH_TOKEN", "gho_resolved")
    assert resolve_copilot_github_token() == "gho_resolved"
    assert copilot_github_token_configured() is True


def test_resolve_from_keychain_reference(tmp_path: Path) -> None:
    secret_store.store_secret(COPILOT_SECRET_NAME, "gho_stored")
    _write_config(tmp_path, {"copilot": {"github_token_ref": f"keychain:{COPILOT_SECRET_NAME}"}})
    assert resolve_copilot_github_token() == "gho_stored"
    assert copilot_github_token_configured() is True


def test_dangling_reference_is_soft_none(tmp_path: Path) -> None:
    # An ``env:`` ref to an unset var resolves to None (never raises) and reads
    # as not-configured, so a run / readout falls back instead of crashing.
    _write_config(tmp_path, {"copilot": {"github_token_ref": "env:GH_TOKEN_MISSING"}})
    assert resolve_copilot_github_token() is None
    assert copilot_github_token_configured() is False


def test_resolve_from_inline_github_token_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hand-edited config with only the inline ``github_token`` (no
    # ``github_token_ref``) still resolves via the right-hand ``or`` fallback.
    _write_config(tmp_path, {"copilot": {"github_token": "env:GH_TOKEN"}})
    monkeypatch.setenv("GH_TOKEN", "gho_inline")
    assert copilot_github_token_ref() == "env:GH_TOKEN"
    assert resolve_copilot_github_token() == "gho_inline"
    assert copilot_github_token_configured() is True


def test_dangling_keychain_reference_is_soft_none(tmp_path: Path) -> None:
    # A keychain ref to a name that was never stored resolves to None (never
    # raises) and reads as not-configured — the realistic "deleted keychain
    # entry" case, distinct from the env-var dangling case above.
    _write_config(tmp_path, {"copilot": {"github_token_ref": "keychain:copilot-never-stored"}})
    assert resolve_copilot_github_token() is None
    assert copilot_github_token_configured() is False


def test_settings_shape() -> None:
    assert copilot_github_token_settings("keychain:copilot") == {
        "copilot": {"github_token_ref": "keychain:copilot"}
    }


# ---------------------------------------------------------------------------
# Optional-SDK extra (parity with cursor / antigravity)
# ---------------------------------------------------------------------------


def test_copilot_extra_install_command_targets_extra() -> None:
    """The install command targets the optional ``copilot`` extra."""
    cmd = copilot_install_command()
    assert "omnigent[copilot]" in cmd
    assert "install" in cmd


def test_copilot_sdk_installed_true_when_spec_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection returns True when ``find_spec`` resolves ``copilot``."""
    monkeypatch.setattr(copilot_auth.importlib.util, "find_spec", lambda name: object())
    assert copilot_sdk_installed() is True


def test_copilot_sdk_installed_false_when_spec_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection returns False when ``find_spec`` returns ``None`` (extra absent)."""
    monkeypatch.setattr(copilot_auth.importlib.util, "find_spec", lambda name: None)
    assert copilot_sdk_installed() is False


def test_copilot_sdk_installed_false_when_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ModuleNotFoundError`` from ``find_spec`` reads as not-installed.

    ``find_spec`` can raise (not return ``None``) when a parent package is
    absent; the guard must swallow that rather than crash setup.
    """

    def _raise(name: str) -> object:
        raise ModuleNotFoundError("No module named 'copilot'")

    monkeypatch.setattr(copilot_auth.importlib.util, "find_spec", _raise)
    assert copilot_sdk_installed() is False


def test_copilot_install_command_prefers_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``uv`` on PATH, the install runs ``uv pip install`` — no index URL."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: "/usr/bin/uv")
    cmd = copilot_install_command()
    assert cmd == ["uv", "pip", "install", "omnigent[copilot]"]
    assert not any("index" in part or "://" in part for part in cmd)


def test_copilot_install_command_falls_back_to_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``uv``, it falls back to this interpreter's pip — still no index."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    cmd = copilot_install_command()
    assert cmd == [
        extra_install.sys.executable,
        "-m",
        "pip",
        "install",
        "omnigent[copilot]",
    ]
    assert not any("index" in part or "://" in part for part in cmd)


def test_copilot_install_command_uv_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a ``uv tool`` venv, the install uses ``uv tool install --with``."""
    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: True)
    monkeypatch.setattr(extra_install, "_installed_vcs_url", lambda: None)
    cmd = copilot_install_command()
    assert cmd == [
        "uv",
        "tool",
        "install",
        "--with",
        "omnigent[copilot]",
        "omnigent",
        "--force",
    ]


def test_install_copilot_sdk_runs_command_then_rechecks(
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
    monkeypatch.setattr(copilot_auth.subprocess, "run", _run)
    monkeypatch.setattr(copilot_auth, "copilot_sdk_installed", lambda: state["installed"])

    assert install_copilot_sdk() is True
    assert calls == [[extra_install.sys.executable, "-m", "pip", "install", "omnigent[copilot]"]]


def test_install_copilot_sdk_false_on_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess that can't spawn (OSError) is caught and reported as False."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("no pip")

    monkeypatch.setattr(extra_install, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(extra_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(copilot_auth.subprocess, "run", _boom)
    assert install_copilot_sdk() is False
