"""End-to-end: a headless codex-native sub-agent must not die in the startup timeout.

Polly's bundled codex sub-agent (``examples/polly/agents/codex/config.yaml``,
``executor.config.harness: codex-native``) is dispatched headlessly — no TTY,
nobody at a terminal. On a machine whose native-Codex launch routing resolves
to "Codex CLI login" without a usable stored credential, the runner's
``--remote`` Codex TUI parks on the ChatGPT sign-in / onboarding screen and
never emits ``thread/started``. The sub-agent's first turn then burns the
30s ``wait_for_thread_started`` startup timeout and dies with::

    inner executor error: Codex native thread never started: Codex
    app-server never started a thread (startup timed out: TimeoutError). ...

so the cross-vendor review never runs (runner log: "Codex TUI never started
a thread for conv_...; chat will not forward").

This drives the real stack — server subprocess, real
``omnigent.runner._entry`` runner, real codex CLI — through the exact
sub-agent shape Polly ships (a spec with ``harness: codex-native`` +
``yolo: true``) and asserts the first turn does NOT die in the thread-start
timeout. While the bug is live the test fails on that timeout error; after a
fix the turn must either start a thread or fail fast with a clear
non-timeout error (e.g. a refusal to launch headlessly without a routable
credential)::

    .venv/bin/python -m pytest tests/e2e/test_codex_native_headless_subagent_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Polly's codex sub-agent shape (examples/polly/agents/codex/config.yaml),
# reduced to the fields that pick the launch path under test.
_CODEX_SUBAGENT_YAML = """\
spec_version: 1
name: codex
description: Codex coding sub-agent (Polly cross-vendor reviewer shape).

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

prompt: |
  You are Codex, a coding sub-agent dispatched for a single scoped REVIEW
  task. Judge the given diff against its acceptance contract.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_HEALTH_TIMEOUT_S = 60.0
# The buggy path takes the 30s thread-start timeout plus the executor's
# bridge-state poll before the turn errors; leave generous headroom for the
# fixed path's real turn as well.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_POLL_INTERVAL_S = 3.0

_STARTUP_TIMEOUT_MARKER = "startup timed out"
_THREAD_NEVER_STARTED_MARKER = "never started a thread"


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers (e.g. the conftest session factory) use ambient
# ``httpx`` calls that DO trust env, so also exclude loopback from any forced
# proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


@pytest.fixture
def credential_less_codex_rig(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """Server + runner whose environment has no routable Codex credential.

    Empty ``CODEX_HOME`` (Codex not logged in) and empty
    ``OMNIGENT_CONFIG_HOME`` (no provider configured for the codex harness):
    the launch router resolves to "Codex CLI login" with nothing to show at
    a headless terminal — the routing state in which the reported timeout
    fires.

    :returns: ``(base_url, runner_id, runner_log_path)``.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required for the codex-native headless repro")

    work = tmp_path_factory.mktemp("codex_headless_subagent")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    state_dir = work / "codex-native-state"
    for path in (config_home, codex_home, state_dir):
        path.mkdir(parents=True)
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(codex_home),
    }
    with server_runner(
        work,
        server_cwd=_REPO_ROOT,
        base_env=base_env,
        server_env=env,
        health_timeout=_HEALTH_TIMEOUT_S,
        poll_interval=0.5,
        wait_ready=False,
    ) as stack:
        # Preserve cwd fallback: this rig has no runner-wide workspace.
        stack.start_runner(cwd=_REPO_ROOT, env={**env, "OMNIGENT_RUNNER_WORKSPACE": None})
        yield stack.base_url, stack.runner_id, stack.log_path("runner")


def _spec_bundle() -> bytes:
    """Gzip the codex sub-agent spec as a session bundle (strict parser path)."""
    data = _CODEX_SUBAGENT_YAML.encode()
    return bundle_files({"config.yaml": data})


@pytest.mark.timeout(400)
def test_polly_shaped_codex_subagent_first_turn_survives_headless_dispatch(
    credential_less_codex_rig: tuple[str, str, Path],
) -> None:
    """A headless codex-native sub-agent turn must not die in the startup timeout.

    Journey: create a session from Polly's codex sub-agent spec, bind it to
    the runner (headless — no TTY anywhere), send the review prompt, and
    wait for a terminal outcome. While the bug is live the turn errors with
    the ``startup timed out`` thread-start failure after ~30s; the test
    fails on exactly that marker.
    """
    base_url, runner_id, runner_log = credential_less_codex_rig

    create = post_session_bundle(
        _client.post,
        f"{base_url}/v1/sessions",
        _spec_bundle(),
        metadata={"workspace": str(_REPO_ROOT)},
        filename="codex.tar.gz",
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    try:
        bind_session_runner(_client.patch, base_url, session_id, runner_id, timeout=60.0)

        send = _client.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "REVIEW: judge this (empty) diff against the contract.",
                        }
                    ],
                },
            },
            timeout=30.0,
        )
        assert send.status_code == 202, f"send rejected: {send.status_code} {send.text}"

        # Wait for a terminal outcome: an error item, an assistant message,
        # or the runner stamping the Codex thread id (thread started).
        error_messages: list[str] = []
        thread_started = False
        assistant_replied = False
        deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
        while time.monotonic() < deadline:
            items = _client.get(
                f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0
            )
            if items.status_code == 200:
                data = items.json()["data"]
                error_messages = [
                    str(item.get("message", "")) for item in data if item.get("type") == "error"
                ]
                assistant_replied = any(
                    item.get("type") == "message" and item.get("role") == "assistant"
                    for item in data
                )
            session = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
            if session.status_code == 200 and session.json().get("external_session_id"):
                thread_started = True
            if error_messages or thread_started or assistant_replied:
                break
            time.sleep(_POLL_INTERVAL_S)

        timed_out_errors = [
            message
            for message in error_messages
            if _STARTUP_TIMEOUT_MARKER in message or _THREAD_NEVER_STARTED_MARKER in message
        ]
        assert not timed_out_errors, (
            "headless codex-native sub-agent turn died in the thread-start "
            f"timeout (the reported headless failure): {timed_out_errors[0][:500]}\n"
            f"runner log tail:\n{runner_log.read_text()[-1500:]}"
        )
        assert thread_started or assistant_replied or error_messages, (
            "turn reached no terminal outcome within "
            f"{_TURN_OUTCOME_TIMEOUT_S:.0f}s (no thread, no reply, no error)"
        )
    finally:
        _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
