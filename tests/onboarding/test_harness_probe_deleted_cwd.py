"""A removed host-daemon cwd must not make a present harness CLI read as
"not configured".

A daemon can outlive the directory it was started from; its readiness probes
then exec the vendor CLI from that deleted cwd, where ``claude`` exits
non-zero. These drive the real probe subprocess against a fake CLI that aborts
from a deleted cwd like the real one.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import omnigent._platform as _platform
from omnigent.onboarding import harness_install as hi
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY

# Fake ``claude`` body run via a short ``/bin/sh`` launcher (see
# ``_install_fake_claude``): aborts from a deleted cwd like the real binary,
# else reports a supported version and a logged-in status.
_FAKE_CLAUDE_IMPL = """\
import json
import os
import sys

try:
    os.getcwd()
except OSError:
    sys.stderr.write("error: The current working directory was deleted\\n")
    sys.exit(1)

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.268 (Claude Code)")
elif args == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
else:
    sys.exit(2)
"""


def _install_fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake ``claude`` first on ``PATH`` and clear the on-disk fallback."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    impl = bin_dir / "claude_impl.py"
    impl.write_text(_FAKE_CLAUDE_IMPL, encoding="utf-8")
    cli = bin_dir / "claude"
    # Quote the interpreter and body paths so a space in either survives; the
    # ``/bin/sh`` shebang itself stays short enough to avoid the length cap.
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # A developer's real claude under a fallback dir must not stand in for the fake.
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())


@contextlib.contextmanager
def _deleted_cwd(tmp_path: Path) -> Iterator[None]:
    """Run the body with the process cwd pointing at a just-removed directory."""
    original = os.getcwd()
    dead = tmp_path / "dead-worktree"
    dead.mkdir()
    os.chdir(dead)
    try:
        dead.rmdir()
        yield
    finally:
        os.chdir(original)


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_missing_harness_cli_survives_deleted_daemon_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch preflight's ``missing_harness_cli`` gate must still see a
    valid claude when the daemon's cwd was removed."""
    _install_fake_claude(tmp_path, monkeypatch)
    with _deleted_cwd(tmp_path):
        assert hi.missing_harness_cli("claude-native") is None
    # Re-probe from scratch so the version read exercises detection under the
    # deleted cwd, not the value the preflight already cached.
    hi._VERSION_PROBE_CACHE.clear()
    with _deleted_cwd(tmp_path):
        assert hi.harness_cli_version(ANTHROPIC_FAMILY)[0] == "2.1.268"


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_login_probe_survives_deleted_daemon_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login/auth-status probe must also tolerate a removed cwd."""
    _install_fake_claude(tmp_path, monkeypatch)
    with _deleted_cwd(tmp_path):
        assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_cli_probe_cwd_only_overrides_when_the_cwd_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid cwd is inherited (``None``); a removed one falls back to home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert hi._cli_probe_cwd() is None
    with _deleted_cwd(tmp_path):
        fallback = hi._cli_probe_cwd()
    assert fallback == str(home)


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_cli_probe_cwd_falls_back_to_root_without_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable home (``Path.home`` raising) must not escape the probe."""

    def _no_home() -> Path:
        raise RuntimeError("home directory not resolvable")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    with _deleted_cwd(tmp_path):
        fallback = hi._cli_probe_cwd()
    assert fallback == os.path.abspath(os.sep)


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_login_probe_execs_discovered_absolute_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status probe must exec the resolved absolute path, so a relative
    PATH entry cannot re-resolve ``claude`` against the fallback cwd."""
    _install_fake_claude(tmp_path, monkeypatch)
    discovered = shutil.which("claude")
    monkeypatch.setenv("PATH", f".{os.pathsep}{os.environ['PATH']}")
    executed: list[str] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        executed.append(argv[0])
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout='{"loggedIn": true}', stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    with _deleted_cwd(tmp_path):
        assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True
    assert executed == [discovered]
    assert os.path.isabs(executed[0])


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_login_probe_refuses_relative_discovery_under_deleted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When discovery yields a relative path (a ``.``-style PATH entry) and the
    daemon's cwd is gone, running it under the fallback cwd would exec a
    different executable. The status probe must refuse rather than probe it."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"./{name}")
    executed: list[str] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        executed.append(argv[0])
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout='{"loggedIn": true}', stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    with _deleted_cwd(tmp_path):
        assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is False
    assert executed == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_version_probe_refuses_relative_discovery_under_deleted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version probe applies the same relative-path protection."""
    monkeypatch.setattr(hi, "resolve_cli_binary", lambda name, **_kwargs: f"./{name}")
    executed: list[str] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        executed.append(argv[0])
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="2.1.268 (Claude Code)", stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    with _deleted_cwd(tmp_path):
        version, _range = hi.harness_cli_version(ANTHROPIC_FAMILY)
    assert version is None
    assert executed == []
