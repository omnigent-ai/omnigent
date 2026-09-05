"""UI journey: kimi harness sessions must report model token usage.

A headless ``kimi`` harness turn completes and the kimi CLI records real
per-turn token usage in its ``wire.jsonl`` (a ``usage.record`` row with
input / output / cache-read tokens), but the Omnigent ``KimiExecutor`` emits
``TurnComplete(usage=None)`` and never forwards that usage, so it is dropped
before it reaches the session. The web SPA's agent-info popover therefore
shows NO ``Token usage`` breakdown for kimi sessions even though the
underlying accounting existed.

Journey (real web SPA, live server + runner, real kimi 0.40.1 CLI pointed at
the mock OpenAI endpoint):

1. start a headless ``kimi`` session (its provider routed at the mock ``/v1``,
   scripted to return a nonzero OpenAI ``usage`` object + stream-usage chunk)
2. send a message; the turn completes and the kimi CLI records a nonzero
   ``usage.record`` (input / output / cache-read tokens) in its wire log
3. open the agent-info popover
4. observable failure: the popover shows no ``Token usage`` breakdown — the
   recorded usage never reached the session

Regression guard: the final assertion (agent-info ``Token usage`` section
renders for the kimi session) FAILS on the current build and passes once the
kimi executor propagates the recorded usage. The wire-log precondition guard
(kimi DID record nonzero usage) passes both before and after a fix, pinning
the failure to a *dropped* usage rather than an absence of data.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

# The kimi CLI reaches the loopback mock only when loopback is exempt from the
# ambient credential proxy; mirror the codex-native e2e module-import guard so
# the runner-spawned kimi subprocess (which inherits NO_PROXY via the executor's
# env allowlist) can reach the mock without tunnelling through the proxy.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))

# Isolated kimi config home, shared with the runner-spawned kimi subprocess:
# ``KimiExecutor._build_spawn_env`` forwards ``KIMI_*`` names, so a
# ``KIMI_CODE_HOME`` set here (at import, before the session-scoped runner
# spawns) reaches the CLI, which reads ``$KIMI_CODE_HOME/config.toml`` for its
# provider routing. A dedicated temp home keeps the test from touching (or
# depending on) any real ``~/.kimi-code`` config.
_KIMI_CODE_HOME = Path(tempfile.mkdtemp(prefix="kimi_home_usage_e2e_"))
os.environ["KIMI_CODE_HOME"] = str(_KIMI_CODE_HOME)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_KIMI_MODEL = "mock/kimi-k3"

# Scripted token usage on the OpenAI wire kimi consumes. kimi records these as
# ``usage.record`` ``{inputOther: prompt - cached, output, inputCacheRead}`` —
# so ``output`` is nonzero and the total is unambiguously present-then-dropped.
_PROMPT_TOKENS = 1234
_CACHED_TOKENS = 1000
_COMPLETION_TOKENS = 56


def _write_kimi_config(mock_llm_server_url: str) -> None:
    """Point the kimi CLI's provider at the mock's OpenAI endpoint.

    kimi 0.40.1 requires a provider ``type`` (the wire protocol) and a model
    that resolves to it; ``type = "openai"`` selects the chat-completions wire
    the mock serves at ``/v1/chat/completions``.

    :param mock_llm_server_url: Mock server base URL WITHOUT ``/v1`` (the
        provider ``base_url`` appends it; kimi then calls
        ``/v1/chat/completions``).
    """
    _KIMI_CODE_HOME.mkdir(parents=True, exist_ok=True)
    (_KIMI_CODE_HOME / "config.toml").write_text(
        f'default_model = "{_KIMI_MODEL}"\n'
        "\n"
        "[providers.mock]\n"
        'type = "openai"\n'
        f'base_url = "{mock_llm_server_url}/v1"\n'
        'api_key = "mock-key"\n'
        "\n"
        f'[models."{_KIMI_MODEL}"]\n'
        'provider_id = "mock"\n'
        'model = "kimi-k3"\n'
        "max_context_size = 262144\n"
    )


def _build_kimi_bundle(name: str) -> bytes:
    """Build a one-file headless-``kimi`` agent bundle.

    No ``executor.auth`` is set: unlike claude-sdk, the kimi CLI has no
    per-spawn provider override, so routing lives in ``config.toml`` (written
    by :func:`_write_kimi_config`). ``context_window`` is declared so the SPA
    resolves a denominator without a catalog entry for the mock model.

    :param name: Agent name (unique per test run).
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "kimi",
            "model": _KIMI_MODEL,
            "context_window": 262144,
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_kimi_session(base_url: str, runner_id: str) -> str:
    """Create a runner-bound session for a fresh headless-``kimi`` agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :returns: The new session id.
    """
    name = f"kimi-usage-{uuid.uuid4().hex[:8]}"
    bundle = _build_kimi_bundle(name)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wire_usage_records() -> list[dict]:
    """Collect every ``usage.record`` row across the isolated kimi home's wires.

    The kimi CLI writes one ``wire.jsonl`` per session under
    ``$KIMI_CODE_HOME/sessions/<wd_...>/session_.../agents/<id>/wire.jsonl``;
    the isolated home means only this test's turns are present.

    :returns: The parsed ``usage.record`` rows (may be empty if the turn never
        reached the model).
    """
    records: list[dict] = []
    for wire in _KIMI_CODE_HOME.glob("sessions/*/session_*/agents/*/wire.jsonl"):
        for line in wire.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "usage.record":
                records.append(row)
    return records


@pytest.mark.timeout(600)
def test_kimi_session_reports_token_usage_in_agent_info(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A completed kimi turn's recorded token usage must reach the agent-info UI.

    The mock scripts a nonzero OpenAI ``usage`` object (and stream-usage chunk)
    for the turn, which the kimi CLI records in ``wire.jsonl``. On the current
    build the ``KimiExecutor`` emits ``TurnComplete(usage=None)``, so the usage
    is dropped and the agent-info popover renders no ``Token usage`` section —
    the final assertion fails. Once the executor propagates the usage, the
    section renders and the assertion passes.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        _write_kimi_config(mock_llm_server_url)

        uid = uuid.uuid4().hex[:6]
        token = f"kimiusage-{uid}"
        # Route by the unique token in the user message so this test's queue
        # can't be drained by another session's calls; several copies cover
        # any incidental extra calls the CLI makes within the turn.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "text": "ack from kimi",
                    "usage": {
                        "prompt_tokens": _PROMPT_TOKENS,
                        "completion_tokens": _COMPLETION_TOKENS,
                        "total_tokens": _PROMPT_TOKENS + _COMPLETION_TOKENS,
                        "prompt_tokens_details": {"cached_tokens": _CACHED_TOKENS},
                    },
                }
            ]
            * 6,
            key=_KIMI_MODEL,
            match=token,
        )

        session_id = _create_kimi_session(live_server, runner_id)
        try:
            page.goto(f"{live_server}/c/{session_id}")

            # Drive the turn to completion: the assistant bubble renders and
            # the working indicator clears.
            _send(page, f"Say ack. {token}")
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=180_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=180_000)

            # Precondition: the kimi CLI DID record nonzero usage for this turn
            # in its wire log, so any missing usage in the UI is a DROP, not an
            # absence of data. This guard passes both before and after a fix.
            records = _wire_usage_records()
            assert records, (
                "no usage.record in kimi wire.jsonl under "
                f"{_KIMI_CODE_HOME} — the mock/kimi wiring broke, not the bug"
            )
            outputs = [int((r.get("usage") or {}).get("output") or 0) for r in records]
            assert max(outputs, default=0) > 0, (
                f"kimi recorded only zero-output usage; records={records!r}"
            )

            # Open the agent-info popover so the (missing) usage section is on
            # screen for the recording.
            page.get_by_test_id("agent-info-trigger").click()

            # Reproduction assertion (FAILS on the current build, passes
            # post-fix): the recorded usage must surface as the agent-info
            # per-model ``Token usage`` breakdown. On the buggy build the
            # executor emits ``TurnComplete(usage=None)`` so the section never
            # renders (the whole usage/cost block is gated on non-empty
            # ``usage_by_model``).
            expect(page.get_by_test_id("agent-info-usage-by-model")).to_be_visible(
                timeout=15_000
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
