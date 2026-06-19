"""
End-to-end: an omnigent YAML declaring ``os_env: caller_process``
at the parent level AND ``os_env: inherit`` on an inline
``codex_worker`` sub-agent actually gives the spawned Codex
worker filesystem access under Omnigent mode.

Before the os_env translator landed, the adapter fail-loud-
rejected parent ``os_env`` at spec-load time, and even with it
commented out the sub-agent's ``inherit`` directive was
silently dropped — the worker booted without shell/file tools
and would report "my environment lacked normal file/shell
access" (see the user-reported session from 2026-04-21).

**What breaks if this fails:**

- The translator regresses to the old fail-loud on top-level
  ``os_env``.
- ``_resolve_inline_agent_tool_os_env`` stops propagating the
  parent's concrete :class:`OSEnvSpec` into sub-specs that
  declared ``os_env: inherit``.
- ``OmnigentExecutor.from_spec`` forgets to pass
  ``executor.config["os_env"]`` onto the reconstructed
  :class:`AgentDef` handed to
  :func:`omnigent.executor_factory.create_executor`.
- Codex's Databricks harness loses filesystem access for
  non-os_env reasons (rare — would be a codex-side regression,
  not this PR's).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from tests._model_pools import resolve_model
from tests.e2e._harness_probes import HARNESS_HARNESS_MODELS, HARNESS_IDS

_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)
_HARNESS = "openai-agents"

# Derived from the parametrize harness id. The YAML's inline
# AgentTool is named ``<harness_short>_worker`` to match the
# convention in :file:`examples/coding_supervisor.yaml` (where
# ``claude-sdk`` lives behind ``claude_worker`` and ``codex``
# behind ``codex_worker``).
_WORKER_TYPE_BY_HARNESS: dict[str, str] = {
    "claude-sdk": "claude_worker",
    "codex": "codex_worker",
    # Pi joined the harness probe matrix in commit 9e0f540 (4d).
    # Mirror the same naming convention so the parametrized
    # case has a worker tool name on the AgentTool branch.
    "pi": "pi_worker",
}

# PATH-resolvable executable each harness's outer CLI depends
# on. The test skips cleanly when it's absent so local dev
# boxes without every vendor CLI installed still contribute
# meaningful signal on the tests they CAN run.
_REQUIRED_BINARY_BY_HARNESS: dict[str, str] = {
    "claude-sdk": "claude",
    "codex": "codex",
    "pi": "pi",
}

# Files that must exist at the repo root and thus must appear
# in any honest ``ls`` Codex would produce against the cwd.
# Any ONE of these showing up in stdout proves Codex ran its
# shell tool; checking multiple lets the test tolerate the
# supervisor paraphrasing the listing as long as real content
# survives.
_ROOT_ANCHOR_ENTRIES = ("pyproject.toml", "README.md", "omnigent")

_ONESHOT_TIMEOUT_SEC = 180


@pytest.fixture()
def _os_env_probe_yaml_factory(
    tmp_path: Path,
    omnigent_repo_root: Path,
) -> Callable[[str, str, str], Path]:
    """
    Returns a factory that writes a trimmed os-env probe YAML
    with ONE sub-agent of the caller's choice.

    Keeps the parametrized cases from stepping on each other's
    YAML file and avoids mutating the shared
    ``examples/coding_supervisor.yaml`` fixture (whose os_env
    block is intentionally commented out so other e2e tests
    stay repo-isolated).

    :param tmp_path: pytest-provided temp dir.
    :param omnigent_repo_root: Repo root — the parent's
        ``os_env.cwd`` so ``ls`` sees real files.
    :returns: Factory ``(worker_type, harness, model) -> Path``.
    """

    def _build(worker_type: str, harness: str, model: str) -> Path:
        config = {
            "name": f"os_env_probe_{worker_type}",
            "prompt": (
                f"You coordinate a single {worker_type} sub-agent. "
                "When the user asks to list files, spawn the worker "
                f"via sys_session_send with type='{worker_type}' and "
                "ask it to list files in its working directory, then "
                "include its verbatim listing in your reply."
            ),
            "async": True,
            "cancellable": True,
            "executor": {
                "harness": _HARNESS,
                "model": _MODEL,
            },
            "os_env": {
                "type": "caller_process",
                "cwd": str(omnigent_repo_root),
                # ``OSEnvSandboxSpec.type`` defaults to
                # ``linux_bwrap``, which the Claude SDK
                # rejects outright on macOS ("only available
                # on Linux"). Explicitly declaring ``none``
                # here matches what developers already do in
                # ``examples/coding_supervisor.yaml`` when
                # running locally, and keeps this test
                # portable across the macOS dev boxes most of
                # us run on.
                "sandbox": {"type": "none"},
            },
            "tools": {
                worker_type: {
                    "type": "agent",
                    "description": (f"A {worker_type} with shell / file tools."),
                    "prompt": (
                        "You are a coding worker. Use your shell or "
                        "file-listing tool against the current "
                        "working directory to answer the user's "
                        "question."
                    ),
                    "os_env": "inherit",
                    "executor": {
                        "harness": harness,
                        "model": model,
                    },
                },
            },
        }
        yaml_path = tmp_path / f"os_env_probe_{worker_type}.yaml"
        yaml_path.write_text(yaml.dump(config))
        return yaml_path

    return _build


@pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS, ids=HARNESS_IDS)
def test_run_omnigent_propagates_os_env_inherit_to_spawned_worker(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    databricks_workspace: tuple[str, str],
    _os_env_probe_yaml_factory: Callable[[str, str, str], Path],
    harness: str,
    model: str,
) -> None:
    """
    Spawn a coding worker for the parametrized harness via the
    Omnigent mode supervisor and verify the worker can actually list
    files — proves the parent's ``os_env: caller_process``
    propagated through ``inherit`` on the inline AgentTool down
    into the spawned task's :class:`OmnigentExecutor` and onto
    the underlying harness.

    Parametrized over both harnesses because propagation lives
    in the translator (harness-agnostic) but the binding
    between ``os_env`` and real shell/file tools lives in each
    harness's executor construction. Covering both guards
    against the per-harness paths drifting independently.

    :param omnigent_python: Shared session interpreter.
    :param omnigent_repo_root: Subprocess cwd + the parent's
        ``os_env.cwd``.
    :param omnigent_credentials_env: Env with PAT + profile.
    :param _os_env_probe_yaml_factory: Fixture factory that
        writes a minimal YAML per case.
    :param harness: Omnigent harness identifier from
        :data:`HARNESS_HARNESS_MODELS` — e.g. ``"codex"`` or
        ``"claude-sdk"``.
    :param model: The harness-routed model identifier from
        :data:`HARNESS_HARNESS_MODELS`.
    """
    worker_type = _WORKER_TYPE_BY_HARNESS.get(harness)
    required_binary = _REQUIRED_BINARY_BY_HARNESS.get(harness)
    if worker_type is None or required_binary is None:
        pytest.skip(
            f"{harness!r} has no inline ``<harness>_worker`` AgentTool "
            "convention (only claude-sdk/codex/pi do), so the "
            "os_env-inherit-to-spawned-worker invariant does not apply."
        )
    for entry in _ROOT_ANCHOR_ENTRIES:
        assert (omnigent_repo_root / entry).exists(), (
            f"Expected anchor {entry!r} missing from "
            f"{omnigent_repo_root}. Pick anchors that actually "
            f"exist before asserting on them."
        )

    if shutil.which(required_binary) is None:
        pytest.skip(
            f"{required_binary!r} binary not on PATH — {harness!r} "
            "harness can't boot, the test would fail for an "
            "unrelated reason that'd mask the os_env invariant."
        )

    yaml_path = _os_env_probe_yaml_factory(
        worker_type,
        harness,
        model,
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            # Ephemeral DBOS state — see comment in
            # test_run_omnigent_example_agents.py for the
            # HarnessProcessManager rationale.
            "--no-session",
            "-p",
            (
                f"Spawn the {worker_type} named 'probe' and ask it "
                "to list files in its working directory. Quote its "
                "verbatim output in your reply."
            ),
        ],
        env=omnigent_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_ONESHOT_TIMEOUT_SEC,
    )

    combined = result.stdout + result.stderr
    assert "lacked normal file/shell access" not in combined, (
        f"{harness}: worker reported no filesystem access — "
        f"os_env:inherit didn't propagate from parent to "
        f"sub-spec. Check _resolve_inline_agent_tool_os_env and "
        f"OmnigentExecutor.from_spec's os_env reader. "
        f"stderr tail:\n{result.stderr[-1500:]}"
    )
    assert "unsupported" not in combined.lower() or "os_env" not in combined.lower(), (
        f"{harness}: spec load rejected os_env — the old "
        f"fail-loud in _fail_on_unsupported_concepts_def is "
        f"back. stderr tail:\n{result.stderr[-1500:]}"
    )
    assert result.returncode == 0, (
        f"{harness}: `omnigent run --omnigent` exited "
        f"{result.returncode}. stderr tail:\n"
        f"{result.stderr[-2000:]}\n"
        f"stdout tail:\n{result.stdout[-1500:]}"
    )

    matches = [e for e in _ROOT_ANCHOR_ENTRIES if e in result.stdout]
    assert matches, (
        f"{harness}: none of the repo-root anchors "
        f"{_ROOT_ANCHOR_ENTRIES} appeared in the supervisor's "
        f"reply. Either the worker did not run its shell/file "
        f"tool (os_env didn't propagate), the supervisor "
        f"dropped the listing, or the worker paraphrased every "
        f"anchor away. stdout tail:\n{result.stdout[-2500:]}"
    )
