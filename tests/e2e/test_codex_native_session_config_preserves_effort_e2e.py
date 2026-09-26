"""E2E test: a native Codex session preserves ``max``/``ultra`` reasoning effort.

Regression test: when a native Codex session is created (``omnigent codex``),
Omnigent copies the user's ``~/.codex/config.toml`` into the per-session
private ``CODEX_HOME`` and normalizes the top-level ``model_reasoning_effort``
against ``CODEX_EFFORTS`` (the in-process Responses API ladder, capped at
``xhigh``) instead of ``CODEX_NATIVE_EFFORTS`` (which carries ``max`` and
``ultra``). The result: a native session configured for ``max``/``ultra``
reasoning is silently downgraded to ``xhigh`` in its session config.

The journey driven here is the reported user journey, end to end:

1. configure ``model_reasoning_effort = "max"`` (or ``"ultra"``) in
   ``~/.codex/config.toml``;
2. launch a native Codex session with ``omnigent codex --server <url>``;
3. inspect ``~/.omnigent/codex-native/<session>/codex-home/config.toml``;
4. the top-level ``model_reasoning_effort`` must still be the configured
   native-ladder value — not ``"xhigh"``.

The test runs the real CLI under a PTY against a real ``omnigent server``
(the ``resume_test_server`` fixture) with a **temporary HOME**, so the user's
real ``~/.codex`` and ``~/.omnigent`` are never touched and the per-session
codex-home lands in a test-owned directory. No model turn is needed — the
clamp happens during session preparation, before any LLM traffic — so this
needs no Codex login and no LLM credentials (the fixture falls back to the
mock key).

Run::

    pytest tests/e2e/test_codex_native_session_config_preserves_effort_e2e.py -v
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
import tomllib

from tests.e2e._native_resume_helpers import cli_env, omnigent_console_script, spawn_cli_background
from tests.e2e.helpers import POLL_INTERVAL_S

# Session preparation only needs the codex binary to exist (the populate step
# runs before the TUI could hang on a sign-in screen), so gate on presence
# rather than the OMNIGENT_E2E_CODEX_NATIVE login opt-in.
pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex-native session prep e2e requires the `codex` CLI on PATH",
)

# The CLI chain (CLI -> host daemon -> runner -> codex-home populate) can take
# a couple of minutes on a loaded box; the config copy is the first durable
# artifact of session prep, so a generous wait keeps this non-flaky.
_CONFIG_WAIT_S = 300.0


def _find_session_codex_config(home: Path) -> Path | None:
    """
    Find the per-session ``config.toml`` under a (temp) HOME.

    Native Codex sessions materialize their private ``CODEX_HOME`` at
    ``~/.omnigent/codex-native/<bridge>/codex-home/``; the test HOME is
    fresh, so the first match is this test's session.

    :param home: The temporary HOME directory the CLI ran under.
    :returns: The copied ``config.toml`` path, or ``None`` if not yet created.
    """
    bridge_root = home / ".omnigent" / "codex-native"
    if not bridge_root.is_dir():
        return None
    for candidate in sorted(bridge_root.glob("*/codex-home/config.toml")):
        return candidate
    return None


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_codex_native_session_config_preserves_native_effort(
    resume_test_server: str,
    tmp_path: Path,
    effort: str,
) -> None:
    """
    ``omnigent codex`` keeps a native-ladder top-level effort in the session copy.

    ``max`` and ``ultra`` are valid on the codex-native ladder
    (``CODEX_NATIVE_EFFORTS``): the real Codex CLI validates and supports them
    per-model. The session's private ``config.toml`` copy must therefore carry
    the user's configured value verbatim, and the user's real source config
    must never be mutated.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; hosts the isolated HOME and launch cwd.
    :param effort: The configured native reasoning effort under test.
    :returns: None.
    """
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    user_config = f'model = "gpt-5.6-luna"\nmodel_reasoning_effort = "{effort}"\n'
    (home / ".codex" / "config.toml").write_text(user_config)
    workdir = tmp_path / "pwd"
    workdir.mkdir()

    env = cli_env()
    # Isolate the journey's filesystem state: the source ~/.codex the copy
    # reads and the ~/.omnigent/codex-native bridge root the session's private
    # CODEX_HOME lands in both resolve from HOME. No login is required (session
    # prep never reaches the model), so a fresh HOME is safe here.
    env["HOME"] = str(home)
    env.pop("CODEX_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "codex", "--server", resume_test_server],
        env=env,
        cwd=str(workdir),
    )
    try:
        deadline = time.monotonic() + _CONFIG_WAIT_S
        config_path: Path | None = None
        while time.monotonic() < deadline:
            config_path = _find_session_codex_config(home)
            if config_path is not None:
                break
            time.sleep(POLL_INTERVAL_S)
        assert config_path is not None, (
            f"per-session codex-home config.toml never appeared under "
            f"{home / '.omnigent' / 'codex-native'} within {_CONFIG_WAIT_S}s; "
            f"CLI output tail:\n{handle.output()[-2000:]}"
        )

        # The copy is written before session-prep appends provider/MCP tables,
        # so poll briefly for the top-level key rather than racing the writer.
        copied_effort: object = None
        key_deadline = time.monotonic() + 30.0
        while time.monotonic() < key_deadline:
            try:
                copied_effort = tomllib.loads(config_path.read_text()).get(
                    "model_reasoning_effort"
                )
            except tomllib.TOMLDecodeError:
                copied_effort = None  # mid-write; retry
            if copied_effort is not None:
                break
            time.sleep(POLL_INTERVAL_S)

        assert copied_effort == effort, (
            f"native Codex session config clamped top-level model_reasoning_effort "
            f"from {effort!r} to {copied_effort!r} in {config_path}; codex-native "
            f"drives the real Codex CLI, which supports the full "
            f"CODEX_NATIVE_EFFORTS ladder, so {effort!r} must be preserved"
        )
        # The user's real config must never be mutated by session prep.
        assert (home / ".codex" / "config.toml").read_text() == user_config
    finally:
        handle.terminate()
