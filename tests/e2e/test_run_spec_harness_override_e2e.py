"""
Agent-spec harness vs project ``harness.default`` precedence e2e test.

Reproduces the harness-default precedence bug: when a project-level
``.omnigent/config.yaml`` sets
``harness.default: claude-native``, ``omnigent run <agent-spec>`` is
rejected with the native-TUI "ignores an AGENT spec" error even though
the agent spec declares a compatible headless harness
(``executor.config.harness: pi``). The spec's harness must take
precedence over the project default; the default should only apply when
the spec declares no harness.

Drives the real user journey under a pseudo-TTY (pexpect): a project
directory whose ``.omnigent/config.yaml`` pins the native default, an
agent spec declaring ``pi``, and a real ``omnigent run <spec> -p ...``
invocation from the project directory.

Two tests pin both sides of the contract:

- ``test_spec_harness_overrides_project_native_default`` FAILS while the
  bug is present (the run is rejected at validation) and passes once the
  spec harness wins: the run proceeds into launch ("Starting up…").
- ``test_explicit_native_harness_flag_still_rejected`` passes today and
  must KEEP passing after the fix: an explicit
  ``--harness claude-native`` combined with an AGENT path stays
  rejected — the fix must not remove the intended guard.

Prerequisites:
    - ``pexpect`` installed (4.9+).
    - ``omnigent`` on ``PATH`` resolving to this worktree's entry point
      (the spawned child's ``PYTHONPATH`` is pinned to this worktree so
      a sibling editable install cannot shadow it).

Usage::

    python -m pytest tests/e2e/test_run_spec_harness_override_e2e.py -v --timeout=180
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Relative spec path, exactly as the bug report's journey runs it from
# the project directory.
_SPEC_RELPATH = ".omnigent/agents/my-agent/config.yaml"

# The rejection's stable signature (omnigent/cli.py,
# _reject_agent_with_native_terminal_harness). Anchoring on this phrase
# keeps the test keyed to THIS failure, not incidental launch noise.
_REJECTION_SIGNATURE = "ignores an AGENT spec"

# Launch-phase markers proving validation passed and the run proceeded
# (omnigent/_runner_startup.py STARTUP_PHASE_*). Matched without the
# trailing ellipsis so a wording-adjacent restyle doesn't false-fail.
_LAUNCH_MARKERS = ["Starting up", "Launching your agent", "Omnigent session:"]

# Generous cap for the first observable outcome. Both outcomes print
# within a couple of seconds (the rejection fires before any server
# work; "Starting up…" is the very first launch line), so this only
# bites on a pathologically loaded runner.
_OUTCOME_TIMEOUT = 90


def _require_omnigent_cli() -> str:
    """
    Resolve the ``omnigent`` CLI (or legacy ``ap``) from PATH.

    :returns: Absolute path to the CLI executable.
    """
    path = shutil.which("omnigent") or shutil.which("ap")
    if path is None:
        pytest.skip("Neither omnigent nor ap CLI on PATH")
    return path


@pytest.fixture()
def native_default_project(tmp_path: Path) -> Path:
    """
    Build the bug report's project layout in a temp directory.

    - ``.omnigent/config.yaml`` — project config pinning
      ``harness.default: claude-native`` (the trigger).
    - ``.omnigent/agents/my-agent/config.yaml`` — a minimal agent spec
      declaring ``executor.config.harness: pi`` (the headless harness
      that must win).

    :param tmp_path: Pytest per-test temp directory.
    :returns: The project directory to run ``omnigent run`` from.
    """
    project = tmp_path / "proj"
    agent_dir = project / ".omnigent" / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (project / ".omnigent" / "config.yaml").write_text("harness:\n  default: claude-native\n")
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: my-agent\n"
        "description: Minimal agent for the spec-harness override test.\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: pi\n"
        "prompt: |\n"
        "  You are a minimal test agent. Say hi.\n"
    )
    return project


@pytest.fixture()
def run_env(tmp_path: Path) -> dict[str, str]:
    """
    Child env for the spawned CLI, hermetic to this worktree.

    - ``OMNIGENT_CONFIG_HOME`` points at an isolated dir (with
      ``auto_open_conversation: false``) so the developer's or CI
      machine's user-level ``~/.omnigent/config.yaml`` cannot leak a
      ``harness.default`` into the merged config — the project-level
      file created by :func:`native_default_project` must be the only
      default in play.
    - ``PYTHONPATH`` pins this worktree (plus its sdk packages) so the
      ``omnigent`` entry point imports the code under test even when
      the venv's editable install maps to a sibling checkout.
    - ``OMNIGENT_SKIP_ONBOARD`` guards against first-run prompts: under
      pexpect the child's stdin is a real tty.

    :param tmp_path: Pytest per-test temp directory.
    :returns: Env mapping for ``pexpect.spawn``.
    """
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("auto_open_conversation: false\n")
    pythonpath_parts = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    if os.environ.get("PYTHONPATH"):
        pythonpath_parts.append(os.environ["PYTHONPATH"])
    return {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "PYTHONPATH": os.pathsep.join(pythonpath_parts),
        "TERM": "xterm-256color",
    }


def _drain(child: Any) -> str:
    """
    Best-effort read of whatever the child printed, for failure messages.

    :param child: pexpect child.
    :returns: Buffered output (before + after slices where available).
    """
    parts: list[str] = []
    for chunk in (child.before, getattr(child, "after", None)):
        if isinstance(chunk, str):
            parts.append(chunk)
    with contextlib.suppress(pexpect.EOF, pexpect.TIMEOUT):
        parts.append(child.read_nonblocking(size=65536, timeout=1) or "")
    return "".join(parts)


def test_spec_harness_overrides_project_native_default(
    native_default_project: Path, run_env: dict[str, str]
) -> None:
    """
    ``run <spec>`` must honor the spec's harness over ``harness.default``.

    Journey (verbatim from the report): project config pins
    ``harness.default: claude-native`` → the agent spec declares
    ``executor.config.harness: pi`` → ``omnigent run <spec> -p 'say hi'``
    from the project dir, with NO ``--harness`` flag.

    While the bug is present, the CLI resolves the project default
    before consulting the spec and rejects the run with the native-TUI
    "ignores an AGENT spec" error — this test fails on that signature.
    Once fixed, validation passes and the run proceeds into the launch
    phase ("Starting up…"), at which point the test succeeds and kills
    the child — the later pi turn (credentials, model) is out of scope.
    """
    cli = _require_omnigent_cli()
    child = pexpect.spawn(
        cli,
        ["run", _SPEC_RELPATH, "-p", "say hi"],
        cwd=str(native_default_project),
        env=run_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=_OUTCOME_TIMEOUT,
    )
    try:
        outcome = child.expect(
            [_REJECTION_SIGNATURE, *_LAUNCH_MARKERS, pexpect.EOF],
            timeout=_OUTCOME_TIMEOUT,
        )
        if outcome == 0:
            pytest.fail(
                "project harness.default: claude-native overrode the agent "
                "spec's executor.config.harness: pi — `omnigent run <spec>` "
                "was rejected with the native-TUI 'ignores an AGENT spec' "
                "error instead of launching on the spec's harness"
            )
        if outcome == 1 + len(_LAUNCH_MARKERS):  # EOF before any marker
            pytest.fail(
                "omnigent run exited before reaching the launch phase; "
                f"output was:\n{_drain(child)}"
            )
        # Any launch marker: validation passed with the spec's harness in
        # charge — the regression contract is met.
    finally:
        # The child may be mid-launch (daemon/server bring-up); tear it
        # down unconditionally so a passing run doesn't leak a spinner.
        with contextlib.suppress(Exception):
            child.terminate(force=True)
        with contextlib.suppress(Exception):
            child.close(force=True)


def test_explicit_native_harness_flag_still_rejected(
    native_default_project: Path, run_env: dict[str, str]
) -> None:
    """
    ``run <spec> --harness claude-native`` must STAY rejected.

    The intended guard — a ``*-native`` harness owns its TUI and cannot
    drive an AGENT spec — applies to the explicit flag. The precedence
    fix must only stop the project *default* from masking the spec's
    harness, not delete the guard, so this pins the rejection (and the
    non-zero exit) for the explicit-flag journey.
    """
    cli = _require_omnigent_cli()
    child = pexpect.spawn(
        cli,
        ["run", _SPEC_RELPATH, "--harness", "claude-native", "-p", "say hi"],
        cwd=str(native_default_project),
        env=run_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=_OUTCOME_TIMEOUT,
    )
    try:
        child.expect(_REJECTION_SIGNATURE, timeout=_OUTCOME_TIMEOUT)
        child.expect(pexpect.EOF, timeout=30)
        child.close()
        assert child.exitstatus != 0, (
            "explicit `--harness claude-native` with an AGENT path must exit non-zero"
        )
    finally:
        with contextlib.suppress(Exception):
            child.close(force=True)
