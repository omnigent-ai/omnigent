"""A codex-rejected effort pairing must surface an error, not fail silently.

Journey (from the report): create a ``codex-native`` session running
``gpt-6-astra`` -> set ``reasoning_effort: minimal`` via session PATCH
(``CODEX_NATIVE_EFFORTS`` deliberately carries the full ladder because codex is
the per-model authority on levels, so omnigent accepts the pairing: the PATCH
succeeds and a GET reports it back) -> send the first brief -> codex rejects the
``minimal`` + ``gpt-6-astra`` pairing and ends the turn with a bare failed
status (``turn.status: "failed"`` and **no** ``turn.error`` payload) -> the
codex-native forwarder maps that to ``external_session_status {status:
"failed"}`` with **no** ``output`` -> the session flips to failed with zero
token usage and the transcript shows nothing explaining why.

While the bug is live, the server (`routes_events.py`) only builds a
``status_error`` when a native ``failed`` edge carries a non-empty ``output``;
a detail-less rejection leaves ``error`` null on the published status edge, and
the web (`chatStore.ts`) appends an error block only when the edge carries one.
So the session goes ``failed`` and no error pill ever renders -- the user is
left staring at a bare failed session with no reason.

The final expectation below is the fix's contract: a detail-less native
failure still surfaces a readable error pill (so the turn's failure is visible,
not silent). The assertion is pinned to the ``native_turn_error`` pill headline
so only THIS turn's surfaced failure satisfies it -- an unrelated error pill
(say, an ambient codex CLI failing its launch) cannot. While the bug is live
that expectation times out -- no such pill appears -- reproducing the silence.

The turn is driven through the same ``/v1/sessions/{id}/events`` route the real
codex-native forwarder posts to. A live codex process + gpt-6-astra needs real
OpenAI/ChatGPT auth and a proprietary model, neither of which this suite has;
the detail-less ``failed`` edge published here is exactly the payload
``_post_turn_status_edge`` derives from codex's error-less rejection (a
``_CodexTurnStatusEdge`` with ``error=None`` posts ``output=None``). The
config-acceptance half (PATCH succeeds, GET reports it back) is asserted against
the real server, so the reproduction exercises the genuine product code end to
end -- only the codex process's own terminal edge is injected.
"""

from __future__ import annotations

import io
import json
import tarfile

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
)

_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'

_MODEL_ID = "gpt-6-astra"

# The reporter's shape: a label-less custom agent bound to the native codex
# executor, running gpt-6-astra. No ``omnigent.wrapper`` label is stamped --
# this is a genuine ``codex-native`` session, exactly as the report describes.
_CODEX_ASTRA_AGENT_YAML = f"""\
spec_version: 1
name: codex-astra-brief

executor:
  type: omnigent
  model: {_MODEL_ID}
  config:
    harness: codex-native

prompt: |
  You are a focused coding assistant. Answer briefly.
"""


def _create_codex_native_session(base_url: str, runner_id: str) -> str:
    """Register the codex-native agent bundle and bind its session.

    Runner-owned codex terminals hard-require a session workspace, so one is
    pinned in the create metadata (mirrors ``test_custom_codex_native_controls``).
    No labels are passed, so the session carries no ``omnigent.wrapper`` label
    and resolves as a plain ``codex-native`` harness -- the report's shape.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CODEX_ASTRA_AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


def _set_reasoning_effort(base_url: str, session_id: str, effort: str) -> None:
    """PATCH the session's reasoning effort and require the store to accept it.

    ``silent=True`` persists the effort without injecting a slash command into a
    live pane (there is no live turn when a user picks an effort), so the row
    ends up carrying ``minimal`` deterministically -- the config-set half of the
    report's journey ("the PATCH succeeds").

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param effort: Reasoning effort to set, e.g. ``"minimal"``.
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"reasoning_effort": effort, "silent": True},
        timeout=30.0,
    )
    resp.raise_for_status()


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Publish the status payload the codex-native forwarder posts.

    ``_post_turn_status_edge`` sends ``{"status": ..., "response_id": ...}`` and
    attaches ``output`` only when the terminal turn carried an error payload. A
    codex rejection that ends the turn with a bare failed status (no
    ``turn.error``) posts **no** ``output`` at all -- the exact detail-less
    shape driven here.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"running"`` / ``"failed"``.
    :param response_id: In-flight turn id, carried on both the turn-start
        ``running`` edge and the terminal ``failed`` edge.
    :param output: Terminal detail; omitted (``None``) to reproduce the
        error-less codex rejection.
    """
    data: dict[str, str] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_codex_native_detail_less_failed_turn_surfaces_error(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A codex-native turn that fails with no detail must not die silently.

    Reproduces the reported journey: setting ``reasoning_effort: minimal`` on a
    ``codex-native`` session running ``gpt-6-astra`` is accepted by omnigent,
    the model rejects the pairing, and the turn ends ``failed`` with no output
    and no error -- and the transcript shows nothing. The fix's contract, and
    the assertion this test drives fail->pass, is that a detail-less native
    failure still surfaces a readable error pill.

    :param page: Playwright page fixture.
    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: None.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_codex_native_session(live_server, runner_id)
    try:
        # Precondition against the REAL server: this is a genuine codex-native
        # session running gpt-6-astra with no wrapper presentation label.
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
        assert snapshot["harness"] == "codex-native", snapshot["harness"]
        assert "omnigent.wrapper" not in (snapshot.get("labels") or {})

        # The report's first observable: omnigent ACCEPTS minimal for a
        # codex-native session (the full codex ladder is deliberate), so the
        # PATCH succeeds and a GET reports it back.
        _set_reasoning_effort(live_server, session_id, "minimal")
        after = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
        assert after["reasoning_effort"] == "minimal", after.get("reasoning_effort")

        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

        working = page.locator(_WORKING)
        pills = page.locator(_ERROR_PILL)

        # Turn starts: the id-bearing running edge lights the Working indicator.
        _publish_native_status(
            live_server, session_id, "running", response_id="codex_turn_minimal"
        )
        expect(working).to_be_visible(timeout=15_000)

        # Codex rejects minimal/astra and ends the turn failed with NO detail:
        # a bare failed status edge carrying no ``output`` -- the exact payload
        # the codex-native forwarder derives from an error-less rejection.
        _publish_native_status(live_server, session_id, "failed", response_id="codex_turn_minimal")

        # The turn is over -- Working clears...
        expect(working).to_have_count(0, timeout=15_000)

        # ...and the failure must be SURFACED, not swallowed: a readable
        # error pill for THIS turn's detail-less failure. Filter on the
        # ``native_turn_error`` headline (the code the server stamps on a
        # detail-less native failure) so an unrelated error pill -- e.g. a
        # real codex CLI on PATH failing its launch during session adoption --
        # can never satisfy the assertion.
        #
        # THIS IS THE BUG: while it is live, the status edge's ``error`` is
        # null on a detail-less native failure and the web only appends an
        # error block when the edge carries one, so no such pill ever renders
        # and this expectation times out -- the silent failure. After the fix,
        # the fallback error pill surfaces and this passes.
        native_failure_pill = pills.filter(
            has_text="The agent ran into an error during this turn."
        )
        expect(native_failure_pill.first).to_be_visible(timeout=15_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:
                respawned.kill()
                respawned.wait(timeout=5)
