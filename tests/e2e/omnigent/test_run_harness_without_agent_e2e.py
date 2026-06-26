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
# Kept UNDER the e2e ``--timeout=180`` cap so a stalled turn fails cleanly here
# (a pexpect TIMEOUT with a captured buffer) instead of tripping the pytest cap
# and crashing the xdist worker with no diagnostic.
_COMPLETION_TIMEOUT = 150.0
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

    # claude-code issues a warmup/title call before the turn that consumes one
    # queued response, so the turn call needs another; queue a few markers.
    responses = [{"text": marker}] * (4 if probe.harness == "claude-sdk" else 1)
    configure_mock_llm(
        mock_llm_server_url,
        responses,
        key=model,
    )

    # claude-sdk speaks the Anthropic wire, not OPENAI_*. Point it at the mock
    # and pass a static gateway token via ANTHROPIC_AUTH_TOKEN (Authorization:
    # Bearer) -- the docs-sanctioned custom-gateway auth. ANTHROPIC_API_KEY
    # (x-api-key) would trigger claude-code's external-key validation, which
    # the mock cannot satisfy ("Invalid API key").
    env = dict(mock_credentials_env)
    if probe.harness == "claude-sdk":
        env["ANTHROPIC_BASE_URL"] = mock_llm_server_url
        env["ANTHROPIC_AUTH_TOKEN"] = "mock-key"

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=None,
        model=model,
        harness=probe.harness,
        env=env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
        initial_prompt=prompt,
    )
    try:
        # Headless one-shot ``-p``: the launcher boots, auto-submits the
        # prompt, and prints the accumulated reply. It does NOT render the
        # interactive ``◆`` assistant-turn glyph (the streaming contract
        # changed in #783), so sync on the marker text the model returns —
        # that landing in stdout is the load-bearing proof the no-AGENT
        # launcher reached the model and rendered the reply.
        child.expect(marker, timeout=_COMPLETION_TIMEOUT)
        output = strip_ansi(child.before or "") + marker
    finally:
        # Drive the exit. The one-shot process does not always terminate
        # promptly under CI load (shutdown/teardown lag — parked tasks,
        # session-log write), so clean_exit sends ``/quit`` and force-kills as
        # a fallback rather than blocking on EOF. Teardown cleanliness is not
        # asserted (it is a known CI-load flake; see clean_exit's docstring).
        clean_exit(child, timeout=_EXIT_TIMEOUT)

    assert marker in output, (
        f"[{probe.harness}] marker {marker!r} missing from REPL output; "
        f"output tail:\n{output[-4000:]}"
    )


def test_run_harness_live_matrix_covers_registered_coding_harnesses() -> None:
    """The live no-AGENT e2e matrix tracks REPL-launchable harnesses.

    ``OMNIGENT_HARNESSES`` also contains ``open-responses`` for the
    legacy in-process executor path, but that harness is not currently
    registered in the server-backed REPL harness registry. This test
    makes the distinction explicit: when a coding harness is added to
    ``_HARNESS_MODULES``, this file must gain a live round-trip row
    for it.

    ``claude-native``, ``codex-native``, ``pi-native``, and
    ``opencode-native`` are excluded because their inner executors require
    bridge directories plus runner-managed terminal panes to inject keys
    into — both set up by their native launchers, not by
    ``omnigent run --harness <native>``. (``opencode-native`` is a
    terminal-takeover ``native-server`` harness, the same shape as the
    other natives.) Running them through this matrix would hang or crash.
    Their e2e coverage is via native launcher smoke tests (tracked
    separately as native-launcher PTY/REPL smoke tests).

    ``cursor`` is excluded because this matrix authenticates through
    the Databricks gateway/profile, while cursor-agent talks only to
    Cursor's own backend and rejects gateway model ids.

    ``antigravity`` is excluded for the same reason as ``cursor``: it is
    Gemini-native and its SDK launches a native binary needing a modern
    glibc.

    ``copilot`` is excluded for the same reason as ``cursor`` / ``antigravity``:
    the GitHub Copilot SDK authenticates with a GitHub token and talks only to
    GitHub's Copilot backend (no Databricks gateway path), so ``_build_copilot_spawn_env``
    emits none of the shared ``HARNESS_<H>_GATEWAY`` / profile probe vars this
    matrix drives. Its live round-trip is covered by the gated
    ``tests/e2e/test_polly_copilot_e2e.py`` and the ``copilot-sdk-e2e-dev`` skill.

    ``cursor-native`` is excluded for the union of both reasons above.

    ``qwen`` is excluded because it does not follow the shared
    ``HARNESS_<HARNESS>_GATEWAY``/``DATABRICKS_PROFILE`` probe wiring that
    this matrix (and ``test_harness_wrap_e2e.py``) drive harnesses with: its
    wrap routes through ``HARNESS_QWEN_GATEWAY_BASE_URL`` /
    ``HARNESS_QWEN_GATEWAY_AUTH_COMMAND`` instead. Its live round-trip is
    covered by the dedicated ``test_per_harness_qwen.py`` suite.

    ``goose`` (headless ACP) is excluded for the same reason as ``qwen``: it
    authenticates from Goose's own config (``goose configure``), not the shared
    gateway/profile probe wiring, so ``_build_goose_spawn_env`` emits no
    ``HARNESS_GOOSE_GATEWAY*`` vars for this matrix to drive. Its live round-trip
    is covered by the dedicated ``test_goose_acp_e2e.py`` suite.

    ``goose-native`` is excluded for the same reason as ``claude-native`` /
    ``cursor-native``: it is a terminal-first TUI launched via ``omni goose``
    (tmux pane + bridge dir), not ``omnigent run --harness goose-native``.

    ``antigravity-native`` is excluded for the union of both reasons above: it
    is a terminal-first TUI launched via ``omnigent antigravity`` (runner-owned
    agy tmux pane + bridge dir), not ``omnigent run --harness antigravity-native``,
    AND it is Gemini-native (agy authenticates via Google OAuth, not the shared
    Databricks gateway/profile probe wiring this matrix drives).

    ``qwen-native`` is excluded for the same reason as ``goose-native`` /
    ``cursor-native``: it is a terminal-first TUI launched via ``omni qwen``
    (tmux pane + bridge dir, driving qwen's ``--input-file`` / ``--json-file``),
    not ``omnigent run --harness qwen-native``. Its coverage is the dedicated
    qwen-native bridge/executor/forwarder unit tests.

    ``kiro-native`` is excluded for the same reason as ``goose-native`` /
    ``qwen-native`` / ``cursor-native``: it is a terminal-first TUI launched via
    ``omni kiro`` (tmux pane + bridge dir), not ``omnigent run --harness
    kiro-native``. Its coverage is the dedicated kiro-native bridge/executor/
    forwarder unit tests plus the ``test_native_kiro_render_parity`` e2e_ui suite.

    ``kimi`` is excluded for the same reason as ``hermes``: it requires the
    ``kimi`` CLI binary (installed via Moonshot's curl installer) and
    authenticates through ``kimi login`` (OAuth or a Moonshot API key), not the
    shared Databricks gateway/profile probe wiring this matrix drives.

    ``kimi-native`` is excluded for the same reason as ``goose-native`` /
    ``qwen-native`` / ``kiro-native``: it is a terminal-first TUI launched via
    ``omni kimi`` (tmux pane + bridge dir), not ``omnigent run --harness
    kimi-native``. Its coverage is the dedicated kimi-native bridge/executor/
    forwarder/approval unit tests plus the Kimi picker e2e_ui suite.

    ``hermes`` is excluded because it requires the ``hermes`` CLI binary
    (installed separately via Nous Research's install script) and authenticates
    through its own provider config, not the shared gateway/profile probe
    wiring this matrix drives.

    ``hermes-native`` is excluded for the union of both reasons: it is a
    terminal-first TUI launched via ``omni hermes`` (tmux pane + bridge dir), not
    ``omnigent run --harness hermes-native``, AND it wraps the ``hermes`` CLI
    binary. Its coverage is the dedicated hermes-native bridge/executor/forwarder/
    approval-mirror unit tests.
    """
    expected_live_harnesses = set(OMNIGENT_HARNESSES).intersection(_HARNESS_MODULES) - {
        "claude-native",
        "codex-native",
        "pi-native",
        "opencode-native",
        "cursor",
        # ``cursor-cloud`` is excluded for the same reason as ``cursor`` (it
        # authenticates to Cursor's own backend, not the Databricks gateway this
        # matrix drives), and additionally requires a GitHub repo + one-time
        # dashboard environment setup to launch a cloud run. Its coverage is the
        # mocked-SDK cursor-cloud executor/harness/spawn-env unit tests.
        "cursor-cloud",
        "cursor-native",
        "antigravity",
        "antigravity-native",
        "copilot",
        "qwen",
        "qwen-native",
        "goose",
        "goose-native",
        "kiro-native",
        "kimi",
        "kimi-native",
        "hermes",
        "hermes-native",
    }
    assert {probe.harness for probe in HARNESS_PROBES} == expected_live_harnesses
