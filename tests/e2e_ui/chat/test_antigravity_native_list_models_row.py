"""UI regression: antigravity-native worker reported as dead in ``sys_list_models``.

Journey (what the user does and sees in the SPA):

1. Sign in to ``agy`` — staged as the Linux OAuth token file the CLI writes,
   plus an ambient ``GEMINI_API_KEY`` (the report's "no configuration fixes
   it" variant; both credentials the provider layer can see are present).
2. Configure an orchestrator agent with an ``agy`` sub-agent on the
   ``antigravity-native`` harness.
3. Ask it for its workers' model availability — the brain calls
   ``sys_list_models`` and the tool call renders in the transcript.
4. Expand the tool call: the ``agy`` worker's row reads ``"source": "none"``
   with no models and the "dispatches to this worker cannot run here" note —
   the dead-worker shape — even though the agy CLI carries its own login and
   nothing actually blocks a dispatch, exactly the cursor-native false
   negative this suite already guards against.

The suite's shared runner masks the bug: its mock-LLM env
(``OPENAI_API_KEY`` + ``OPENAI_BASE_URL``) registers an ambient
openai-family provider, and every agy spelling collapses to the
openai-family ``"antigravity"`` harness type, so the worker row resolves
that wrong-family provider instead of reporting ``"none"``. The fixture
therefore replaces the shared runner with one whose env mirrors the
reported install: agy signed in, ``GEMINI_API_KEY`` set, and no
openai-family credential anywhere. The test FAILS on un-fixed code and must
PASS once the agy row degrades to a usable subscription-style readout
(``"static"`` / ``"cli"``) like the sibling CLI-login workers.

Run (spawns its own local server + runner; build the SPA first)::

    pytest tests/e2e_ui/chat/test_antigravity_native_list_models_row.py -v
"""

from __future__ import annotations

import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)

# Model key routing the parent brain to its scripted mock queue.
_BRAIN_MODEL = "mock-agy-catalog-brain"
_PARENT_NAME = "agy_catalog_orch"

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# One scripted tool turn + one text turn; catalog enumeration is local and
# fast, but CI boxes are slow, so waits get a generous budget.
_TURN_TIMEOUT_MS = 180_000

_RUNNER_SWAP_TIMEOUT_S = 60.0

# The Linux agy sign-in artifact (token nested under "token"); values are
# shape-only — Linux login detection is file-based and never calls Google.
_FAKE_OAUTH_TOKEN: dict[str, object] = {
    "auth_method": "oauth",
    "token": {
        "access_token": "ya29.e2e-fake-access-token",
        "refresh_token": "1//0g-e2e-fake-refresh-token",
        "token_type": "Bearer",
        "expiry": "2099-01-01T00:00:00Z",
    },
}


@dataclass(frozen=True)
class _CatalogSession:
    """Handle for the orchestrator-with-agy-worker session.

    :param base_url: Spawned server base URL.
    :param session_id: The runner-bound parent session id.
    :param routing_token: Per-run token that selects the brain's mock queue.
    """

    base_url: str
    session_id: str
    routing_token: str


def _orchestrator_yaml(mock_llm_server_url: str) -> str:
    """Build the orchestrator spec: an openai-agents brain + agy worker.

    Same compat-adapter single-file shape as the cursor-native sibling test.
    The explicit ``auth`` block pins the brain to the mock LLM server — the
    swapped runner's env carries no openai credentials at all, so the brain
    must be fully self-describing.

    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: YAML text ready for bundle upload.
    """
    return f"""\
name: {_PARENT_NAME}
prompt: |
  You are a coding orchestrator with one `agy` sub-agent. When the
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
  agy:
    type: agent
    description: Antigravity coding sub-agent (native agy TUI).
    executor:
      model: gemini-3-pro
      harness: antigravity-native
    prompt: |
      You are the Antigravity coding sub-agent.
"""


def _runner_online(base_url: str, runner_id: str) -> bool:
    """Report whether the token-bound runner is registered and online.

    :param base_url: Spawned server base URL.
    :param runner_id: The suite's token-bound runner id.
    :returns: ``True`` when the status endpoint reports it online.
    """
    try:
        resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200 and resp.json().get("online") is True


def _wait_runner_state(base_url: str, runner_id: str, *, online: bool, what: str) -> None:
    """Poll the runner status endpoint until it reaches the wanted state.

    :param base_url: Spawned server base URL.
    :param runner_id: The suite's token-bound runner id.
    :param online: Target state to wait for.
    :param what: Failure-message label for the transition being awaited.
    :raises RuntimeError: If the state is not reached in time.
    """
    deadline = time.monotonic() + _RUNNER_SWAP_TIMEOUT_S
    while _runner_online(base_url, runner_id) is not online:
        if time.monotonic() > deadline:
            raise RuntimeError(f"runner did not become {what} within {_RUNNER_SWAP_TIMEOUT_S:.0f}s")
        time.sleep(0.5)


@pytest.fixture
def agy_install_runner(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
    _recover_shared_runner: Callable[[], None],
) -> Iterator[None]:
    """Swap the shared runner for one matching the reported agy install.

    The replacement runner's env is the reported environment: a HOME whose
    ``~/.gemini/antigravity-cli/antigravity-oauth-token`` marks agy as signed
    in, an ambient ``GEMINI_API_KEY``, an empty ``OMNIGENT_CONFIG_HOME``, and
    none of the suite's mock-pointed openai credentials (which would resolve
    a wrong-family provider for the worker and mask the reported row).
    Teardown restores a standard shared runner via ``_recover_shared_runner``.

    :param live_server: Spawned server fixture.
    :param tmp_path_factory: Pytest temp path factory for HOME and the log.
    :param _recover_shared_runner: Session-scoped restore callable.
    :yields: ``None`` once the replacement runner is online.
    """
    runner_id = str(_server_state["runner_id"])
    if _runner_online(live_server, runner_id):
        os.kill(int(_server_state["runner_pid"]), signal.SIGKILL)
        _wait_runner_state(live_server, runner_id, online=False, what="offline after SIGKILL")

    home = tmp_path_factory.mktemp("agy_install_home")
    token_path = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(json.dumps(_FAKE_OAUTH_TOKEN), encoding="utf-8")
    (home / ".omnigent").mkdir()

    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": str(_server_state["binding_token"]),
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "GEMINI_API_KEY": "AIza-e2e-fake-key",
    }
    for masking_var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"):
        env.pop(masking_var, None)

    log_path = tmp_path_factory.mktemp("agy_install_runner") / "runner.log"
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + _RUNNER_SWAP_TIMEOUT_S
    while not _runner_online(live_server, runner_id):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.terminate()
            raise RuntimeError(
                f"agy-install runner did not register; log:\n{log_path.read_text()[-3000:]}"
            )
        time.sleep(0.5)
    _server_state["runner_pid"] = proc.pid

    try:
        yield
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        _wait_runner_state(live_server, runner_id, online=False, what="offline after teardown")
        _recover_shared_runner()


@pytest.fixture
def catalog_session(
    live_server: str,
    mock_llm_server_url: str,
    agy_install_runner: None,
) -> Iterator[_CatalogSession]:
    """Create a runner-bound session for the catalog journey.

    The brain's mock queue scripts one ``sys_list_models`` call followed by
    a closing text turn.

    :param live_server: Spawned server fixture.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param agy_install_runner: The reported-install replacement runner.
    :yields: A :class:`_CatalogSession` handle.
    """
    routing_token = f"agy-catalog-{uuid.uuid4().hex[:10]}"
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
            reset_mock_llm(mock_llm_server_url)


def _expand_list_models_tool_call(page: Page) -> None:
    """Expand the completed turn, tool group, and model-listing output.

    :param page: The Playwright page, on the parent session.
    """
    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=30_000)
    worked_trigger = worked.get_by_role("button", name=re.compile(r"^Worked"))
    # The fold mounts open to animate shut after idle; wait before opening it.
    expect(worked_trigger).to_have_attribute("aria-expanded", "false", timeout=30_000)
    worked_trigger.click()
    expect(worked_trigger).to_have_attribute("aria-expanded", "true")

    group = worked.get_by_role("button", name="Called 1 tool", exact=True)
    expect(group).to_be_visible(timeout=30_000)
    expect(group).to_have_attribute("aria-expanded", "false")
    group.click()
    expect(group).to_have_attribute("aria-expanded", "true")

    direct = worked.get_by_role("button", name=re.compile(r"^sys_list_models"))
    expect(direct).to_be_visible(timeout=30_000)
    direct.click()
    expect(direct).to_have_attribute("aria-expanded", "true")


def _agy_catalog_row(base_url: str, session_id: str) -> dict[str, object]:
    """Fetch the persisted ``sys_list_models`` result's ``agy`` row.

    :param base_url: Spawned server base URL.
    :param session_id: The parent session id.
    :returns: The agy worker's catalog row dict.
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
    row = catalogs[-1].get("agy")
    assert isinstance(row, dict), f"catalog has no 'agy' row: {sorted(catalogs[-1])}"
    return row


@pytest.mark.timeout(600)
def test_antigravity_native_worker_row_not_source_none(
    page: Page,
    catalog_session: _CatalogSession,
) -> None:
    """The antigravity-native worker's catalog row must not be ``source: "none"``.

    Drives the reported journey in the SPA: ask the orchestrator for its
    workers' models, watch the ``sys_list_models`` tool call land in the
    transcript, expand it, and check the agy worker's row. On un-fixed code
    the row is the dead-worker shape (``source: "none"``, no models, the
    "dispatches to this worker cannot run here" note) despite the signed-in
    agy install and the ambient gemini credential — this test fails there
    and passes once the row degrades to a usable source (``"static"`` /
    ``"cli"``) like the sibling subscription-CLI workers.

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
    # The completed-turn fold appears after the working indicator clears.
    expect(page.get_by_test_id("working-indicator")).to_be_hidden(timeout=30_000)

    # Put the catalog on screen the way a user reads it (and the video shows it).
    _expand_list_models_tool_call(page)
    expect(page.get_by_text(re.compile(r'"agy"')).first).to_be_visible(timeout=30_000)

    row = _agy_catalog_row(chat.base_url, chat.session_id)

    # THE BUG: a dispatchable antigravity-native worker is reported
    # with the dead-worker source "none". Any usable provenance passes.
    assert row.get("source") != "none", (
        "Bug reproduced: sys_list_models reports the dispatchable "
        f"antigravity-native worker as source='none' — full row: {row}"
    )
