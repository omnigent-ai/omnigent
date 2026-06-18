"""Live REPL e2e for ``omnigent run --harness`` without AGENT.

This test drives the user-facing launcher shape::

    omnigent run --harness <harness> -p <prompt>

under a real pseudo-TTY. It waits for the REPL banner, lets the
``-p`` startup hook submit a real user turn, and asserts the model
returns an exact marker. That covers the integration unit tests
cannot: Click optional-AGENT parsing, synthetic Omnigent YAML
materialization, Omnigent server boot, harness executor selection,
Databricks profile routing, REPL initial-message handling, and
model response rendering.

Run explicitly with Databricks credentials, for example::

    .venv/bin/python -m pytest \
      tests/e2e/omnigent/test_run_harness_without_agent_e2e.py \
      --profile test-profile -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES
from tests.e2e._harness_probes import (
    HARNESS_IDS,
    HARNESS_PROBES,
    HarnessProbe,
    skip_if_harness_cli_missing,
)
from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    strip_ansi,
)

_PROMPT_TEMPLATE = (
    "Reply with exactly the identifier between <answer> tags, but omit the tags: "
    "<answer>{marker}</answer>. Do not include any other text."
)
_SPAWN_TIMEOUT = 120.0
_COMPLETION_TIMEOUT = 240.0
_EXIT_TIMEOUT = 20.0


def _profile_env(repo_root: Path, profile: str, config_home: Path) -> dict[str, str]:
    """Return a subprocess env that resolves credentials via the profile.

    Unlike most e2e tests, this intentionally does not depend on
    ``--llm-api-key`` or a patched ``~/.databrickscfg``. The feature under
    test is the real CLI shape users run locally:
    ``omnigent run --harness <harness>`` with Databricks routing taken
    from the global config's ``auth:`` block (the ``--profile`` CLI flag
    was removed). Harnesses should resolve host/token through the
    Databricks SDK/profile path themselves.
    """
    env = dict(os.environ)
    env["DATABRICKS_CONFIG_PROFILE"] = profile
    # The omnigent CLI no longer accepts ``--profile``; write the
    # supported replacement — an ``auth:`` block in an isolated
    # ``OMNIGENT_CONFIG_HOME`` — so the spawned CLI routes harness
    # model/gateway traffic through the test profile.
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        f"auth:\n  type: databricks\n  profile: {profile}\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    for stale in (
        # Force profile-backed routing instead of accidental direct OpenAI.
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DATABRICKS_TOKEN",
        # Nested Claude/Codex sessions can set these and block child harnesses.
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CODEX",
    ):
        env.pop(stale, None)
    existing_pp = env.get("PYTHONPATH", "")
    omnigent_path = str(repo_root / "omnigent")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), omnigent_path, existing_pp) if p
    )
    return env


@pytest.mark.parametrize("probe", HARNESS_PROBES, ids=HARNESS_IDS)
def test_run_harness_without_agent_live_repl_round_trip(
    probe: HarnessProbe,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    databricks_workspace: tuple[str, str],
    tmp_path: Path,
) -> None:
    """``omnigent run --harness`` boots and answers via each wrapped harness.

    The no-AGENT launcher should behave like a first-class agent:
    it should render the selected harness banner, auto-submit the
    provided ``-p`` prompt, reach the real Databricks-backed model,
    stream a reply, and exit cleanly. A missing marker means either
    the launch path did not reach the model or the response was
    garbled before the REPL rendered it.

    ``HARNESS_PROBES`` covers every coding harness registered in
    ``omnigent.runtime.harnesses`` and accepted by the Omnigent
    compat allowlist. A separate assertion below keeps that matrix in
    lock-step with the runtime registry.
    """
    skip_if_harness_cli_missing(probe.harness)

    profile, _host = databricks_workspace
    env = _profile_env(omnigent_repo_root, profile, tmp_path / "omnigent-config")
    marker = f"{probe.marker}_RUN_HARNESS_WITHOUT_AGENT"
    prompt = _PROMPT_TEMPLATE.format(marker=marker)
    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=None,
        model=probe.model,
        harness=probe.harness,
        env=env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
        initial_prompt=prompt,
    )
    try:
        child.expect("◆", timeout=_COMPLETION_TIMEOUT)
        agent_before = child.before or ""
        child.expect(marker, timeout=_COMPLETION_TIMEOUT)
        marker_before = child.before or ""
        marker_after = child.after or ""
        clean_exit(child, timeout=_EXIT_TIMEOUT)
        exit_code = child.exitstatus
        signal_status = child.signalstatus
    finally:
        if not child.closed:
            child.close(force=True)

    combined_stripped = (
        strip_ansi(agent_before) + "◆" + strip_ansi(marker_before) + str(marker_after)
    )
    assert exit_code == 0, (
        f"[{probe.harness}] REPL exited non-zero: exit={exit_code}, "
        f"signal={signal_status}; output tail:\n{combined_stripped[-4000:]}"
    )
    assert signal_status is None, (
        f"[{probe.harness}] REPL terminated by signal {signal_status}; "
        f"output tail:\n{combined_stripped[-4000:]}"
    )
    assert marker in combined_stripped, (
        f"[{probe.harness}] marker {marker!r} missing from REPL output; "
        f"output tail:\n{combined_stripped[-4000:]}"
    )


def test_run_harness_live_matrix_covers_registered_coding_harnesses() -> None:
    """The live no-AGENT e2e matrix tracks REPL-launchable harnesses.

    ``OMNIGENT_HARNESSES`` also contains ``open-responses`` for the
    legacy in-process executor path, but that harness is not currently
    registered in the server-backed REPL harness registry. This test
    makes the distinction explicit: when a coding harness is added to
    ``_HARNESS_MODULES``, this file must gain a live round-trip row
    for it.

    ``claude-native``, ``codex-native``, and ``pi-native`` are excluded
    because their inner executors require bridge directories plus
    runner-managed terminal panes to inject keys into — both set up by
    their native launchers, not by ``omnigent run --harness <native>``.
    Running them through this matrix would hang or crash. Their e2e
    coverage is via native launcher smoke tests (tracked separately as
    native-launcher PTY/REPL smoke tests).

    ``cursor`` is excluded because this matrix authenticates through
    the Databricks gateway/profile, while cursor-agent talks only to
    Cursor's own backend (``CURSOR_API_KEY``) and rejects gateway
    model ids. Its live coverage is the gated row in
    ``tests/e2e/omnigent/test_per_harness_cursor.py``.

    ``antigravity`` is excluded for the same reason as ``cursor``: it is
    Gemini-native — it authenticates with a Gemini API key (or Vertex AI),
    not the Databricks gateway/profile this matrix uses — and its SDK
    launches a native binary needing a modern glibc, so it cannot round-trip
    through this gateway-backed matrix.

    ``cursor-native`` is excluded for the union of both reasons above: like
    the ``*-native`` harnesses it injects keys into a runner-managed tmux
    pane backed by a bridge dir (set up by ``omnigent cursor``, not by
    ``omnigent run --harness cursor-native``), and like ``cursor`` it drives
    ``cursor-agent``, which talks only to Cursor's own backend and rejects
    gateway model ids. Its live coverage is the gated row in
    ``tests/e2e/omnigent/test_per_harness_cursor.py``.
    """
    expected_live_harnesses = set(OMNIGENT_HARNESSES).intersection(_HARNESS_MODULES) - {
        "claude-native",
        "codex-native",
        "pi-native",
        "cursor",
        "cursor-native",
        "antigravity",
    }
    # The intersection above keeps this expectation tied to the actual
    # runtime registry instead of duplicating the harness list here.
    assert {probe.harness for probe in HARNESS_PROBES} == expected_live_harnesses
