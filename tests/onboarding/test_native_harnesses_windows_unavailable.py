"""Backend regression tests: native terminal harnesses must be
reported as unavailable on a Windows host.

## Bug

``configured_harness_map()`` (``omnigent/onboarding/harness_readiness.py``)
does not check ``IS_WINDOWS`` before probing CLI binary presence.  On Windows,
a native CLI binary (e.g. ``claude.exe``) may be installed and on ``PATH``, so
the probe returns ``True`` — but launching a native terminal session immediately
fails with ``native_terminal_start_failed`` because tmux/PTY is not available
on Windows.

The host daemon therefore sends ``"claude-native": true`` (and similar ``true``
entries for every other ``*-native`` harness) to the server.  The web picker
reads those values via ``/v1/hosts`` → ``configured_harnesses`` and calls
``harnessUnavailableReasonOnHost("claude-native", host)`` which returns ``null``
for a ``true`` entry → **no warning badge is rendered**.  The user selects
Claude Code → the session is created → it immediately transitions to ``failed``
with ``native_terminal_start_failed``.

## Fix direction

``configured_harness_map()`` should short-circuit for every harness that
requires tmux/PTY (i.e. every member of ``NATIVE_HARNESSES``) and return an
unavailability sentinel (``False`` or a structured string such as
``"unsupported-platform"``) when ``IS_WINDOWS is True``.

SDK harnesses (``claude-sdk``, ``codex``, etc.) must remain unaffected: they
run in-process and have no PTY requirement.

## What these tests pin

1. ``configured_harness_map()`` with ``IS_WINDOWS=True`` and all CLI binaries
   installed returns **a non-True value** for every canonical native harness.
2. ``configured_harness_map()`` with ``IS_WINDOWS=True`` returns **True** for
   SDK harnesses (they are unaffected by the platform gate).

Both tests FAIL on the unpatched codebase because ``harness_readiness.py``
never imports or checks ``IS_WINDOWS``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import omnigent.onboarding.harness_install as hi
from omnigent.harness_aliases import NATIVE_HARNESSES
from omnigent.onboarding.harness_readiness import configured_harness_map

# ---------------------------------------------------------------------------
# Autouse isolation (mirrors tests/onboarding/test_harness_readiness.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate credential sources so readiness is deterministic.

    Matches the autouse fixture in ``test_harness_readiness.py``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    import omnigent.onboarding.copilot_auth as _ca

    monkeypatch.setattr(_ca, "gh_cli_github_token", lambda host=None: None)

    import omnigent._platform as _plat

    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setattr(_plat, "_cli_fallback_dirs", lambda: ())


def _all_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear installed and auth-satisfied."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _stub_run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            if argv[0].endswith("opencode"):
                version = "1.17.7\n"
            elif argv[0].endswith("cursor-agent") or argv[0].endswith("hermes"):
                version = "2026.07.01\n"
            else:
                version = "9.9.9\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=version, stderr="")
        if argv[:3] == ["gh", "auth", "token"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess in readiness tests: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _stub_run)
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness", sorted(NATIVE_HARNESSES))
def test_native_terminal_harness_unavailable_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """Native terminal harnesses must report unavailable on a Windows host.

    Regression: with the host daemon running on Windows,
    ``configured_harness_map()`` must return a falsy / non-True value for
    every member of ``NATIVE_HARNESSES`` — even when the CLI binary is
    present on ``PATH``.  The web picker reads this map and renders a
    warning badge only when the value is not ``True``; a bare ``True``
    silently admits an agent that will always fail.
    """
    import omnigent.onboarding.harness_readiness as readiness_mod

    _all_clis_installed(monkeypatch)

    # Simulate a Windows host: patch IS_WINDOWS in harness_readiness.
    import omnigent._platform as _plat

    monkeypatch.setattr(_plat, "IS_WINDOWS", True)
    # Patch the name in the readiness module's own namespace so calls
    # inside that module see the patched value.
    monkeypatch.setattr(readiness_mod, "IS_WINDOWS", True, raising=False)

    result = configured_harness_map()

    # Any non-True value (False, "binary-missing", "unsupported-platform", …)
    # is acceptable as long as the picker receives a falsy signal.
    assert result.get(harness) is not True, (
        f"configured_harness_map() returned True for {harness!r} on a "
        "Windows host (IS_WINDOWS=True).  Native terminal harnesses require "
        "tmux/PTY, which is not supported on Windows.  The picker will show "
        "no warning badge and the user will select the agent, start the "
        "session, and receive native_terminal_start_failed immediately.  "
        "Fix: return an unavailability sentinel for IS_WINDOWS in "
        "configured_harness_map() / _harness_availability_core()."
    )


def test_sdk_harnesses_remain_available_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK harnesses must stay True on Windows — only native/PTY harnesses are gated.

    The fix must be scoped to harnesses in NATIVE_HARNESSES.  SDK harnesses
    run in-process without tmux and must continue to work on Windows.
    """
    import omnigent.onboarding.harness_readiness as readiness_mod

    _all_clis_installed(monkeypatch)

    import omnigent._platform as _plat

    monkeypatch.setattr(_plat, "IS_WINDOWS", True)
    monkeypatch.setattr(readiness_mod, "IS_WINDOWS", True, raising=False)

    result = configured_harness_map()

    for sdk_harness in ("claude-sdk", "claude_sdk", "openai-agents", "openai-agents-sdk"):
        assert result.get(sdk_harness) is True, (
            f"SDK harness {sdk_harness!r} should remain available on Windows "
            "(it has no PTY requirement), but configured_harness_map() returned "
            f"{result.get(sdk_harness)!r}."
        )
