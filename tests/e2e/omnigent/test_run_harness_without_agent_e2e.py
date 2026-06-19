"""Live REPL e2e for ``omnigent run --harness`` without AGENT.

Migrated to use the mock LLM server. This test drives the user-facing
launcher shape::

    omnigent run --harness <harness> -p <prompt>

under a real pseudo-TTY against the mock LLM server. It waits for the
REPL banner, lets the ``-p`` startup hook submit a real user turn, and
asserts the mock model returns the expected marker. No real Databricks
credentials are required.
"""

from __future__ import annotations

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
from tests.e2e.omnigent.conftest import configure_mock_llm

_PROMPT_TEMPLATE = (
    "Reply with exactly the identifier between <answer> tags, but omit the tags: "
    "<answer>{marker}</answer>. Do not include any other text."
)
_SPAWN_TIMEOUT = 120.0
_COMPLETION_TIMEOUT = 240.0
_EXIT_TIMEOUT = 20.0


@pytest.mark.parametrize("probe", HARNESS_PROBES, ids=HARNESS_IDS)
def test_run_harness_without_agent_live_repl_round_trip(
    probe: HarnessProbe,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """``omnigent run --harness`` boots and answers via each wrapped harness.

    Uses the mock LLM server for deterministic responses. The no-AGENT
    launcher should behave like a first-class agent: it should render the
    selected harness banner, auto-submit the provided ``-p`` prompt,
    stream a mock reply, and exit cleanly. A missing marker means either
    the launch path did not reach the model or the response was garbled
    before the REPL rendered it.

    :param probe: Harness probe with model and marker.
    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL.
    :param tmp_path: Per-test temp directory.
    """
    skip_if_harness_cli_missing(probe.harness)

    model = f"mock-harness-no-agent-{probe.harness}"
    marker = f"{probe.marker}_RUN_HARNESS_WITHOUT_AGENT"
    prompt = _PROMPT_TEMPLATE.format(marker=marker)

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": marker}],
        key=model,
    )

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=None,
        model=model,
        harness=probe.harness,
        env=mock_credentials_env,
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

    ``cursor`` is excluded because this matrix authenticates through
    the Databricks gateway/profile, while cursor-agent talks only to
    Cursor's own backend and rejects gateway model ids.

    ``antigravity`` is excluded for the same reason as ``cursor``: it is
    Gemini-native and its SDK launches a native binary needing a modern
    glibc.

    ``cursor-native`` is excluded for the union of both reasons above.
    """
    expected_live_harnesses = set(OMNIGENT_HARNESSES).intersection(_HARNESS_MODULES) - {
        "claude-native",
        "codex-native",
        "pi-native",
        "cursor",
        "cursor-native",
        "antigravity",
    }
    assert {probe.harness for probe in HARNESS_PROBES} == expected_live_harnesses
