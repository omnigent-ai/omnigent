"""UI regression: cursor-native worker reported as dead in ``sys_list_models``.

Journey (what the user does and sees in the SPA):

1. Configure an orchestrator agent with a ``cursor-native`` sub-agent.
2. Ask it for its workers' model availability — the brain calls
   ``sys_list_models`` and the tool call renders in the transcript.
3. Expand the tool call: the ``cursor`` worker's row reads
   ``"source": "none"`` with an empty model list — the dead-worker shape
   whose note tells the (human and driving) agent the worker cannot run
   here, even though cursor-native is fully dispatchable via its own
   stored login.

The pre-launch listing-probe failure is the trigger: a stub
``cursor-agent`` is placed on ``PATH`` (launchable, but its ``models``
subcommand errors) before the suite's shared runner spawns, mirroring an
installed-but-unlistable CLI. When the suite's runner started before this
module (full-suite runs), the CLI is simply absent from the probe
environment — a different probe failure with the same buggy ``"none"``
outcome, so the assertion holds either way.

Sibling subscription-CLI workers (claude-native / codex-native) degrade to
a usable ``source: "static"`` row in the same pre-launch state; the cursor
row collapsing to ``"none"`` is the regression. The test FAILS on un-fixed
code and must PASS once the cursor listing failure degrades to a usable
row (``"static"`` or ``"cli"``).

Run (spawns its own local server + runner; build the SPA first)::

    pytest tests/e2e_ui/chat/test_cursor_native_list_models_row.py -v
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)

# Model key routing the parent brain to its scripted mock queue.
_BRAIN_MODEL = "mock-cursor-catalog-brain"
_PARENT_NAME = "cursor_catalog_orch"

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# One scripted tool turn + one text turn; catalog enumeration is local and
# fast, but CI boxes are slow, so waits get a generous budget.
_TURN_TIMEOUT_MS = 180_000


@pytest.fixture(scope="session", autouse=True)
def _cursor_agent_stub_on_path(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Put a launchable-but-unlistable ``cursor-agent`` stub on ``PATH``.

    The suite's shared runner inherits ``os.environ`` when it spawns, so
    prepending the stub dir here (before ``live_server`` first runs) makes
    the runner resolve a cursor-agent that launches fine but fails its
    ``models`` listing probe — the dispatchable-worker / failing-probe
    state from the report. Best-effort: when the runner already spawned
    (full-suite runs) the CLI is absent there instead, which reproduces the
    same buggy ``source: "none"`` row.

    :param tmp_path_factory: Pytest temp path factory for the stub dir.
    """
    if os.name == "nt":
        # cursor-native is gated off Windows; the bash stub can't run there.
        yield
        return
    stub_dir = tmp_path_factory.mktemp("cursor_probe_fail_stub")
    stub = stub_dir / "cursor-agent"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "models" ]; then\n'
        '  echo "Error: unable to list models" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "stub cursor-agent: $*"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{stub_dir}{os.pathsep}{old_path}"
    try:
        yield
    finally:
        os.environ["PATH"] = old_path


@dataclass(frozen=True)
class _CatalogSession:
    """Handle for the orchestrator-with-cursor-worker session.

    :param base_url: Spawned server base URL.
    :param session_id: The runner-bound parent session id.
    :param routing_token: Per-run token that selects the brain's mock queue.
    """

    base_url: str
    session_id: str
    routing_token: str


def _orchestrator_yaml(mock_llm_server_url: str) -> str:
    """Build the orchestrator spec: an openai-agents brain + cursor-native worker.

    Omnigent-flavored single-file YAML with an inline ``type: agent`` tool
    (the compat-adapter shape, same as the two-agent chat fixture), so the
    ``cursor`` sub-agent registers ``sys_session_send`` / ``sys_list_models``
    on the brain. An explicit ``auth`` block pins the brain to the mock LLM
    server so an ambient provider config (e.g. a CI gateway in
    ``OMNIGENT_CONFIG_HOME``) can't shadow the mock routing.

    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: YAML text ready for bundle upload.
    """
    return f"""\
name: {_PARENT_NAME}
prompt: |
  You are a coding orchestrator with one `cursor` sub-agent. When the
  user asks which models your workers can run, call `sys_list_models`
  and then summarize the result.

executor:
  model: {_BRAIN_MODEL}
  harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_llm_server_url}/v1

tools:
  cursor:
    type: agent
    description: Cursor coding sub-agent (native cursor-agent TUI).
    executor:
      model: grok-4.5
      harness: cursor-native
    prompt: |
      You are the Cursor coding sub-agent.
"""


@pytest.fixture
def catalog_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_CatalogSession]:
    """Create a runner-bound session for the catalog journey.

    Same runner-respawn and bind contract as the suite's other agent
    fixtures. The brain's mock queue scripts one ``sys_list_models`` call
    followed by a closing text turn.

    :param live_server: Spawned server fixture.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :yields: A :class:`_CatalogSession` handle.
    """
    routing_token = f"cursor-catalog-{uuid.uuid4().hex[:10]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-lm-{routing_token}",
                        "name": "sys_list_models",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": f"Catalog reported. Marker: {routing_token}"},
        ],
        key=_BRAIN_MODEL,
        match=routing_token,
    )
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = _orchestrator_yaml(mock_llm_server_url).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent` tool.
        info = tarfile.TarInfo(name=f"{_PARENT_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield _CatalogSession(
            base_url=live_server,
            session_id=session_id,
            routing_token=routing_token,
        )
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            try:
                reset_mock_llm(mock_llm_server_url)
            finally:
                if respawned_runner is not None:
                    respawned_runner.terminate()
                    try:
                        respawned_runner.wait(timeout=5)
                    except Exception:
                        respawned_runner.kill()


def _expand_list_models_tool_call(page: Page) -> None:
    """Expand the ``sys_list_models`` tool call in the transcript.

    A lone call renders directly as a ``sys_list_models(...)`` trigger;
    completed multi-step turns fold calls into a collapsed "Called N tools"
    group — expand groups when present, then click the call trigger so its
    output preview (the catalog JSON) is on screen.

    :param page: The Playwright page, on the parent session.
    """
    direct = page.get_by_role("button", name=re.compile(r"^sys_list_models\("))
    if not direct.count():
        groups = page.get_by_text(re.compile(r"^Called \d+ tools?$"))
        expect(groups.first).to_be_visible(timeout=30_000)
        for group in groups.all():
            group.click()
        direct = page.get_by_role("button", name=re.compile(r"sys_list_models"))
    expect(direct.first).to_be_visible(timeout=30_000)
    direct.first.click()


def _cursor_catalog_row(base_url: str, session_id: str) -> dict[str, object]:
    """Fetch the persisted ``sys_list_models`` result's ``cursor`` row.

    :param base_url: Spawned server base URL.
    :param session_id: The parent session id.
    :returns: The cursor worker's catalog row dict.
    """
    items_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=15.0)
    items_resp.raise_for_status()
    items = items_resp.json().get("data", [])
    call_ids = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and item.get("name") == "sys_list_models"
    }
    assert call_ids, "no sys_list_models function_call found in the transcript"
    catalogs = [
        json.loads(item.get("output") or "{}")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
    ]
    assert catalogs, "no sys_list_models tool result found in the transcript"
    row = catalogs[-1].get("cursor")
    assert isinstance(row, dict), f"catalog has no 'cursor' row: {sorted(catalogs[-1])}"
    return row


@pytest.mark.timeout(600)
def test_cursor_native_worker_row_not_source_none(
    page: Page,
    catalog_session: _CatalogSession,
) -> None:
    """The cursor-native worker's catalog row must not be ``source: "none"``.

    Drives the reported journey in the SPA: ask the orchestrator for its
    workers' models, watch the ``sys_list_models`` tool call land in the
    transcript, expand it, and check the cursor worker's row. On un-fixed
    code the row is the dead-worker shape (``source: "none"``, no models,
    a "cannot run / enumeration failed" note) even though cursor-native is
    dispatchable — this test fails there and passes once the row degrades
    to a usable source (``"static"`` / ``"cli"``) like the sibling
    subscription-CLI workers.

    :param page: pytest-playwright page fixture.
    :param catalog_session: The orchestrator session handle.
    """
    chat = catalog_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(
        "Which models can each of your workers run? Call sys_list_models "
        f"and summarize. Routing marker: {chat.routing_token}"
    )
    page.get_by_role("button", name="Send", exact=True).click()

    # The closing text turn proves the tool call completed and persisted.
    expect(
        page.locator(_ASSISTANT, has_text=f"Catalog reported. Marker: {chat.routing_token}").first
    ).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    # The final text item can render just before the terminal response settles
    # the turn and folds its tool calls. Wait for the authoritative session
    # status UI to go idle so the trigger cannot be replaced mid-click.
    expect(page.get_by_test_id("working-indicator")).to_be_hidden(timeout=30_000)

    # Put the catalog on screen the way a user reads it (and the video shows it).
    _expand_list_models_tool_call(page)
    expect(page.get_by_text(re.compile(r'"cursor"')).first).to_be_visible(timeout=30_000)

    row = _cursor_catalog_row(chat.base_url, chat.session_id)

    # THE BUG: a dispatchable cursor-native worker is reported
    # with the dead-worker source "none". Any usable provenance passes.
    assert row.get("source") != "none", (
        "Bug reproduced: sys_list_models reports the dispatchable "
        f"cursor-native worker as source='none' — full row: {row}"
    )
