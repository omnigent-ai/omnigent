"""End-to-end test: native Codex sessions preserve ``max``/``ultra`` effort.

Regression guard: launching a native Codex session (``codex-native`` harness,
e.g. ``omnigent codex``) copies the user's ``config.toml`` into the per-session
``CODEX_HOME`` via ``_populate_codex_home_config``, and
``_normalize_copied_codex_effort`` used to validate the copied top-level
``model_reasoning_effort`` against ``CODEX_EFFORTS`` (the in-process Responses
API ladder, capped at ``xhigh``) instead of ``CODEX_NATIVE_EFFORTS`` (which
carries ``max`` and ``ultra``). A user with ``model_reasoning_effort = "max"``
(or ``"ultra"``) therefore had their reasoning effort silently clamped to
``"xhigh"`` in the session copy at
``~/.omnigent/codex-native/<session>/codex-home/config.toml``.

Unlike the sibling
``test_codex_native_session_config_preserves_effort_e2e.py`` (which covers the
default ``~/.codex`` source resolved from ``HOME`` with ``CODEX_HOME`` unset),
this test pins the copy source explicitly via the ``CODEX_HOME`` environment
variable, so both config-source resolution paths stay guarded.

This drives the real user journey end-to-end: a real ``omnigent server``, the
real ``omnigent codex --server`` CLI wrapper (which spawns its own runner and a
``codex-native-ui`` session), and the real per-session ``CODEX_HOME``
materialization — then asserts the copied effort survived verbatim and the
user's real config was never touched.

Environment requirements
------------------------
* A ``codex`` CLI on ``PATH``. No Codex **login** is required (unlike the
  sibling ``OMNIGENT_E2E_CODEX_NATIVE``-gated turn-driving tests): the bug
  fires while the runner materializes the session ``CODEX_HOME`` at launch,
  before any Codex authentication or model turn, so this test never waits on
  the TUI becoming interactive.
* No LLM key: the launch path under test runs before any model call
  (``resume_test_server`` boots with the default mock key).

Run::

    .venv/bin/python -m pytest tests/e2e/test_codex_native_effort_clamp_e2e.py -v
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
import tomllib

from tests.e2e._native_resume_helpers import (
    cli_env,
    omnigent_console_script,
    spawn_cli_background,
)

# ``resume_test_server`` is provided by tests/e2e/conftest.py (the allow-list-
# free server the CLI wrapper's self-spawned host daemon can register against).

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex-native effort e2e needs the `codex` CLI on PATH (no login required)",
)

# The per-session CODEX_HOME appears once the runner launches the harness;
# covers CLI boot + runner spawn + session creation on a busy CI box.
_SESSION_HOME_TIMEOUT_S = 240.0
# After the copy first appears the launch sequence keeps rewriting the file
# (effort normalization, MCP inject, model pinning), so settle before reading.
_POST_COPY_SETTLE_S = 5.0
_POLL_S = 0.5


def _wait_for_session_config(home_dir: Path, timeout: float) -> Path:
    """
    Poll for the per-session ``codex-home/config.toml`` under *home_dir*.

    :param home_dir: The (isolated) ``$HOME`` the CLI and its runner use; the
        bridge root is ``<home>/.omnigent/codex-native/``.
    :param timeout: Max seconds to wait for the copy to materialize.
    :returns: Path to the session's private ``config.toml``.
    :raises AssertionError: If no session config appears within *timeout*.
    """
    root = home_dir / ".omnigent" / "codex-native"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = sorted(root.glob("*/codex-home/config.toml"))
        if matches:
            return matches[0]
        time.sleep(_POLL_S)
    raise AssertionError(
        f"per-session codex-home/config.toml never appeared under {root} within {timeout}s"
    )


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_codex_native_session_config_preserves_native_effort(
    resume_test_server: str,
    tmp_path: Path,
    effort: str,
) -> None:
    """
    ``omnigent codex`` keeps ``model_reasoning_effort = "max"/"ultra"`` intact.

    Stages a user home whose ``~/.codex/config.toml`` sets a codex-native
    reasoning effort above ``xhigh``, launches the real ``omnigent codex
    --server`` CLI against a real server, waits for the runner to materialize
    the per-session ``CODEX_HOME`` copy, and asserts the copied top-level
    ``model_reasoning_effort`` is the configured value — not clamped to
    ``"xhigh"`` — and that the user's real config file was left untouched.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; hosts the isolated ``$HOME``.
    :param effort: The configured native-ladder effort (``max`` / ``ultra``).
    :returns: None.
    """
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    codex_src = home_dir / ".codex"
    codex_src.mkdir()
    source_text = f'model = "gpt-5.2-codex"\nmodel_reasoning_effort = "{effort}"\n'
    (codex_src / "config.toml").write_text(source_text, encoding="utf-8")
    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()

    env = cli_env()
    # Isolate the journey from the invoking machine: the bridge root
    # (~/.omnigent/codex-native) derives from HOME, and the copy *source* from
    # CODEX_HOME (defaulted to ~/.codex — pinned explicitly for determinism).
    env["HOME"] = str(home_dir)
    env["CODEX_HOME"] = str(codex_src)

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "codex", "--server", resume_test_server],
        env=env,
        cwd=str(pwd_dir),
    )
    try:
        try:
            session_config = _wait_for_session_config(home_dir, _SESSION_HOME_TIMEOUT_S)
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nCLI output tail:\n{handle.output()[-2000:]}") from exc
        # The clamp rewrite happens milliseconds after the copy lands, so an
        # immediate read could still see the pre-normalization value and pass
        # on a buggy build. Settle past the whole launch-sequence rewrite
        # window (normalize + MCP inject + model pin) before asserting.
        time.sleep(_POST_COPY_SETTLE_S)

        copied = tomllib.loads(session_config.read_text(encoding="utf-8"))
        copied_effort = copied.get("model_reasoning_effort")
        assert copied_effort == effort, (
            f"native Codex session config clamped model_reasoning_effort: expected "
            f"{effort!r} to be preserved in {session_config}, found {copied_effort!r} "
            "(codex-native drives the real Codex CLI, whose ladder includes max/ultra; "
            "the copy must be validated against CODEX_NATIVE_EFFORTS, not CODEX_EFFORTS)"
        )
        assert (codex_src / "config.toml").read_text(encoding="utf-8") == source_text, (
            "the user's real ~/.codex/config.toml must never be modified by session launch"
        )
    finally:
        handle.terminate()
