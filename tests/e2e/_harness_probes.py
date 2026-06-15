"""
Shared parametrize probes for e2e tests that should run against
every wrapped harness.

Each :class:`HarnessProbe` describes one harness + model + marker
tuple. e2e tests import :data:`HARNESS_PROBES` and use it as the
``parametrize`` argvalues so a single test ID per harness shows
up (``[claude-sdk]``, ``[codex]``, etc.) and a per-harness failure
is visible without re-reading the parametrize tuple.

Add a new entry here when a new harness wrap lands in
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`. Every
parametrized e2e test then picks up the new harness without
per-file edits.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import cache

import pytest

from tests._model_pools import resolve_model


@dataclass(frozen=True)
class HarnessProbe:
    """One row of the harness parametrize matrix.

    :param harness: The harness name as registered in
        ``_HARNESS_MODULES`` and used by
        :meth:`HarnessProcessManager.get_client`, e.g.
        ``"claude-sdk"``.
    :param model: The model identifier the inner executor
        receives via the harness's ``HARNESS_<HARNESS>_MODEL``
        env var (or, for AP-level tests, the model field in
        the agent spec). Must be a real model the Databricks
        gateway exposes for the user's profile.
    :param env_prefix: The env-var prefix the wrap reads
        (e.g. ``HARNESS_CLAUDE_SDK_``). The model-routing env
        vars are derived as ``{prefix}MODEL``,
        ``{prefix}GATEWAY``, ``{prefix}DATABRICKS_PROFILE``.
        Used by the harness-wrap smoke test that talks
        directly to ``HarnessProcessManager``.
    :param marker: The exact literal string the LLM is asked
        to echo back in marker-based tests. Per-harness
        markers keep concurrent runs of different harnesses
        from cross-matching against each other in the
        assertion path.
    :param cli_binary: Name of the CLI binary the inner
        executor requires on PATH (e.g. ``"claude"``,
        ``"codex"``, ``"pi"``). ``None`` when no binary check
        is meaningful (the inner executor's failure path
        surfaces a clear error of its own). Tests can call
        :func:`skip_if_harness_cli_missing` to skip the
        harness's parametrize row when its CLI isn't
        installed locally.
    """

    harness: str
    model: str
    env_prefix: str
    marker: str
    cli_binary: str | None = None


# Probes for every wrapped harness. Add a new entry when
# ``_HARNESS_MODULES`` gains a new key (currently 4b: claude-sdk,
# 4c: codex, 4d: pi; 4e is pending).
#
# Probe models resolve at import time with a stable per-harness key,
# so the OMNIGENT_TEST_MODEL_* env vars can rebalance a harness's
# rows without code edits; pools stay within the API style each
# harness supports.
HARNESS_PROBES: list[HarnessProbe] = [
    HarnessProbe(
        harness="claude-sdk",
        model=resolve_model("databricks-claude-opus-4-6", key="probe:claude-sdk"),
        env_prefix="HARNESS_CLAUDE_SDK_",
        marker="CLAUDE_E2E_OK",
        cli_binary="claude",
    ),
    HarnessProbe(
        harness="codex",
        # Per CLAUDE.md guidance: ``databricks-gpt-5-4-mini`` is
        # the OpenAI-style model exposed via the Databricks
        # gateway. Codex's executor speaks the OpenAI Responses
        # API, so this model lights up via the
        # ``HARNESS_CODEX_GATEWAY`` route.
        model=resolve_model("databricks-gpt-5-4-mini", key="probe:codex"),
        env_prefix="HARNESS_CODEX_",
        marker="CODEX_E2E_OK",
        cli_binary="codex",
    ),
    HarnessProbe(
        harness="pi",
        # Pi speaks the OpenAI Responses API and the Databricks
        # gateway exposes Claude through that endpoint too. Per
        # CLAUDE.md, ``databricks-claude-sonnet-4-6`` is the
        # default Claude-via-Databricks model the per-harness
        # pi suite uses.
        model=resolve_model("databricks-claude-sonnet-4-6", key="probe:pi"),
        env_prefix="HARNESS_PI_",
        marker="PI_E2E_OK",
        cli_binary="pi",
    ),
    HarnessProbe(
        harness="openai-agents",
        # The openai-agents SDK speaks the OpenAI Responses API
        # via the Databricks gateway; the GPT model is the
        # natural fit per CLAUDE.md (``databricks-gpt-5-4-mini``
        # is the OpenAI-style Databricks model). Registry key is
        # ``openai-agents`` (not ``-sdk``) to match the
        # Omnigent YAML ``executor.harness`` spelling.
        model=resolve_model("databricks-gpt-5-4-mini", key="probe:openai-agents"),
        env_prefix="HARNESS_OPENAI_AGENTS_",
        marker="OPENAI_AGENTS_E2E_OK",
        # Pure-Python ``openai-agents`` package; no CLI binary
        # to skip on. ``cli_binary=None`` means
        # :func:`skip_if_harness_cli_missing` is a no-op for
        # this row.
        cli_binary=None,
    ),
    HarnessProbe(
        # Registry key matches ``_HARNESS_MODULES`` (note the
        # underscore, unlike ``claude-sdk`` / ``openai-agents``).
        harness="databricks_supervisor",
        # Per the harness's docstring at
        # ``omnigent/inner/databricks_supervisor_harness.py``,
        # the gateway expects a Databricks-hosted Claude model;
        # ``databricks-claude-sonnet-4-6`` is the default per
        # CLAUDE.md and matches ``examples/databricks_supervisor``.
        model=resolve_model("databricks-claude-sonnet-4-6", key="probe:databricks_supervisor"),
        env_prefix="HARNESS_SUPERVISOR_",
        marker="SUPERVISOR_E2E_OK",
        # Supervisor is an API, not a CLI; no binary to skip on.
        # Workspace-level setup (Supervisor preview enabled) is
        # required for the row to pass, analogous to how
        # ``openai-agents`` needs a gateway-reachable model.
        cli_binary=None,
    ),
    HarnessProbe(
        harness="cursor",
        # Cursor Agent CLI uses Composer 2.5 Fast as its default
        # model. Registry key is ``cursor`` to match the Omnigent
        # YAML ``executor.harness`` spelling.
        model=resolve_model("composer-2.5-fast", key="probe:cursor"),
        env_prefix="HARNESS_CURSOR_",
        marker="CURSOR_E2E_OK",
        cli_binary="cursor-agent",
    ),
    HarnessProbe(
        harness="mimo",
        # Mimo's ACP bridge is CLI-backed like Cursor; use the
        # harness default unless overridden by OMNIGENT_TEST_MODEL_*.
        model=resolve_model("auto", key="probe:mimo"),
        env_prefix="HARNESS_MIMO_",
        marker="MIMO_E2E_OK",
        cli_binary="mimo",
    ),
]


# Convenience: list of just (harness, model) tuples for tests
# that don't need the env-var prefix or marker. Pass to
# ``pytest.mark.parametrize("harness,model", HARNESS_HARNESS_MODELS)``.
HARNESS_HARNESS_MODELS: list[tuple[str, str]] = [(p.harness, p.model) for p in HARNESS_PROBES]


# IDs for parametrize calls — keeps test names like
# ``test_foo[claude-sdk]`` / ``test_foo[codex]`` / ``test_foo[pi]``.
HARNESS_IDS: list[str] = [p.harness for p in HARNESS_PROBES]


# ── CLI availability gate ───────────────────────────────────


# Look up table from harness name to its required CLI binary.
# Built once at import time from ``HARNESS_PROBES`` so the
# helper below doesn't reconstruct it on every call.
_CLI_BINARY_BY_HARNESS: dict[str, str | None] = {p.harness: p.cli_binary for p in HARNESS_PROBES}


def _cli_probe_args(binary: str) -> list[str]:
    """Return a cheap command that proves *binary* is runnable."""
    if binary == "pi":
        # ``shutil.which("pi")`` alone is not enough: pi's npm package
        # may be installed under an older Node version than the package
        # supports. ``pi --help`` exercises module loading without making
        # model/network calls, so it catches broken installs early and lets
        # e2e rows skip instead of failing deep inside ``PiExecutor``.
        return [binary, "--help"]
    return [binary, "--version"]


@cache
def cli_unavailable_reason(binary: str) -> str | None:
    """
    Return ``None`` when *binary* exists and starts, else a skip reason.

    The result is cached because e2e suites call the harness gate from many
    parametrized rows and CLI startup can be non-trivial.
    """
    path = shutil.which(binary)
    if path is None:
        return f"{binary!r} CLI is not on PATH"

    try:
        proc = subprocess.run(
            _cli_probe_args(binary),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{binary!r} CLI at {path!r} is not runnable: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return f"{binary!r} CLI at {path!r} exits {proc.returncode}{suffix}"
    return None


def skip_if_harness_cli_missing(harness: str) -> None:
    """
    Skip the current pytest test when the harness's CLI binary
    isn't installed and runnable.

    Call from the top of a parametrized test body when the test
    drives a real harness subprocess and the CLI is required for
    the inner executor to start. Local dev environments often
    have only some of the harnesses installed; this helper keeps
    missing or broken binaries from surfacing as confusing
    executor errors in the middle of a test.

    No-op for harnesses with ``cli_binary=None`` and for unknown
    harnesses (so an old test that drops in a new harness name
    without updating this table doesn't break unexpectedly).

    :param harness: The harness name from the parametrize row,
        e.g. ``"pi"``. Looked up in :data:`_CLI_BINARY_BY_HARNESS`.
    """
    binary = _CLI_BINARY_BY_HARNESS.get(harness)
    if binary is None:
        return
    reason = cli_unavailable_reason(binary)
    if reason is not None:
        pytest.skip(
            f"{harness!r} harness requires a runnable {binary!r} CLI; "
            f"{reason}. Install/fix it to run this row. Other harness rows continue."
        )
