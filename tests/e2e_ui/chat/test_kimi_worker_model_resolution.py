"""UI regressions: kimi worker provider-resolution KeyError + omitted-model
dispatch inheriting the parent's foreign-family model.

Covers the two independent facets of the kimi-worker model-resolution
regression:

1. **Provider-resolution KeyError.** With a generic Omnigent provider in play
   (a host ``providers:`` entry marked ``default: pi``, or an ambient-detected
   OpenAI key — both inline-family kinds), ``sys_list_models`` reports the
   ``kimi`` worker as ``no usable model provider (provider resolution failed:
   KeyError) — dispatches to this worker cannot run here``.
   ``model_catalog._provider_from_entry`` indexes
   ``_KEY_AUTH_FAMILY["kimi"]``, a key that intentionally does not exist
   (kimi's provider table is CLI-managed), so resolution crashes instead of
   reporting kimi's CLI-owned catalog.

2. **Omitted-model dispatch inherits the parent's model.** A
   ``sys_session_send`` dispatch to the kimi worker that names no
   ``args.model`` inherits the parent session's model even when that model is
   a foreign family (``claude-opus-4-8``): ``model_family_mismatch`` has no
   family rule for the kimi harness, so
   ``runner/tool_dispatch._inherited_parent_model`` forces the Claude model
   onto the Kimi child instead of keeping the Kimi CLI default — breaking
   cross-vendor review independence.

Journeys (what the user does and sees in the SPA):

- Facet 1: configure an orchestrator with a ``kimi`` sub-agent on a host
  whose provider config carries a generic ``default: pi`` entry → ask it
  which models its workers can run (the brain calls ``sys_list_models``) →
  expand the tool call: the kimi worker's row is the dead-worker shape with
  the ``provider resolution failed: KeyError`` note.
- Facet 2: run the parent session on ``claude-opus-4-8`` (the persisted
  session model selection) → ask the orchestrator to have the kimi worker
  review a change (the brain dispatches ``sys_session_send`` with no
  ``args.model``) → open the kimi child session from the Agents rail: it
  came up on the parent's ``claude-opus-4-8`` instead of the Kimi default.

A stub ``kimi`` CLI (version above the harness floor) is placed on ``PATH``
before the suite's runner spawns so the dispatch gate admits the kimi worker
— mirroring the report, where the kimi CLI itself is healthy. The generic
provider trigger is pinned deterministically via ``OMNIGENT_CONFIG_HOME``
(a ``default: pi`` key provider); when the suite's shared runner already
spawned before this module (full-suite runs), the runner's ambient
``OPENAI_API_KEY``/``OPENAI_BASE_URL`` (set by the e2e conftest) detects an
equivalent generic provider, which reproduces the same KeyError, and the
stub is also dropped into ``~/.local/bin`` (a ``resolve_cli_binary``
fallback dir probed live at dispatch) so the dispatch gate still admits the
worker there.

Both tests FAIL on un-fixed code and must PASS once (1) kimi resolution
stops crashing (no ``provider resolution failed`` note) and (2) an
omitted-model kimi dispatch no longer comes up on a foreign-family parent
model.

Run (spawns its own local server + runner; build the SPA first)::

    pytest tests/e2e_ui/chat/test_kimi_worker_model_resolution.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tarfile
import textwrap
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
)

# Model key routing the parent brain to its scripted mock queue.
_BRAIN_MODEL = "mock-kimi-worker-brain"
_PARENT_NAME = "kimi_worker_orch"
# The parent session's model selection — the foreign-family model the report
# saw omitted-model kimi dispatches come up on.
_PARENT_SESSION_MODEL = "claude-opus-4-8"
# Above the kimi harness's version floor (0.7.0) so the dispatch gate admits
# the worker, matching the report's healthy-CLI premise.
_STUB_KIMI_VERSION = "0.30.0"

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# One scripted tool turn + one text turn; catalog enumeration and dispatch
# are local and fast, but CI boxes are slow, so waits get a generous budget.
_TURN_TIMEOUT_MS = 180_000


@pytest.fixture(scope="session", autouse=True)
def _kimi_worker_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Stage the report's environment before the suite's runner spawns.

    Two mutations, both inherited by the runner (``live_server`` /
    ``_ensure_runner_online`` spawn it with ``os.environ``):

    - A stub ``kimi`` CLI on ``PATH`` whose ``--version`` satisfies the
      harness floor, so the sub-agent dispatch gate admits the kimi worker
      (the report's "kimi CLI itself is healthy" premise). Best-effort for
      already-spawned runners (full-suite runs): the stub is also linked
      into ``~/.local/bin``, one of the ``resolve_cli_binary`` fallback
      dirs probed live at dispatch time, unless something named ``kimi``
      already exists there.
    - ``OMNIGENT_CONFIG_HOME`` pointing at a config whose ``providers:``
      block carries a generic inline-family entry marked ``default: pi`` —
      the report's trigger for the resolution KeyError. When the runner
      already spawned without this config, its ambient
      ``OPENAI_API_KEY``/``OPENAI_BASE_URL`` (set by the e2e conftest)
      detects an equivalent generic provider, which trips the same crash.

    :param tmp_path_factory: Pytest temp path factory for the stub/config.
    """
    if os.name == "nt":
        # The bash stub can't run on Windows; the kimi harness is POSIX-only.
        yield
        return
    stub_dir = tmp_path_factory.mktemp("kimi_stub_bin")
    stub = stub_dir / "kimi"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "kimi {_STUB_KIMI_VERSION}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "stub kimi: not a real TUI" >&2\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    config_home = tmp_path_factory.mktemp("kimi_config_home")
    (config_home / "config.yaml").write_text(
        textwrap.dedent(
            """\
            providers:
              team-gateway:
                kind: key
                default: pi
                openai:
                  base_url: https://gw.example.invalid/v1
                  api_key: sk-e2e-generic-provider
            """
        ),
        encoding="utf-8",
    )

    old_path = os.environ.get("PATH", "")
    old_config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    os.environ["PATH"] = f"{stub_dir}{os.pathsep}{old_path}"
    os.environ["OMNIGENT_CONFIG_HOME"] = str(config_home)

    # Full-suite best effort: an already-running runner probes
    # ``~/.local/bin`` live at dispatch (resolve_cli_binary's fallback
    # ladder), so the stub is visible there without a runner respawn.
    fallback = Path.home() / ".local" / "bin" / "kimi"
    created_fallback = False
    try:
        if not fallback.exists():
            fallback.parent.mkdir(parents=True, exist_ok=True)
            fallback.write_text(stub.read_text(encoding="utf-8"), encoding="utf-8")
            fallback.chmod(0o755)
            created_fallback = True
    except OSError:
        pass

    try:
        yield
    finally:
        os.environ["PATH"] = old_path
        if old_config_home is None:
            os.environ.pop("OMNIGENT_CONFIG_HOME", None)
        else:
            os.environ["OMNIGENT_CONFIG_HOME"] = old_config_home
        if created_fallback:
            with contextlib.suppress(OSError):
                fallback.unlink()


@dataclass(frozen=True)
class _KimiOrchSession:
    """Handle for the orchestrator-with-kimi-worker session.

    :param base_url: Spawned server base URL.
    :param session_id: The runner-bound parent session id.
    :param routing_token: Per-run token that selects the brain's mock queue.
    """

    base_url: str
    session_id: str
    routing_token: str


def _orchestrator_yaml(mock_llm_server_url: str) -> str:
    """Build the orchestrator spec: an openai-agents brain + kimi worker.

    Omnigent-flavored single-file YAML with an inline ``type: agent`` tool
    (the compat-adapter shape, same as the cursor catalog fixture), so the
    ``kimi`` sub-agent registers ``sys_session_send`` / ``sys_list_models``
    on the brain. An explicit ``auth`` block pins the brain to the mock LLM
    server so the staged generic provider can't shadow the mock routing.
    The kimi worker deliberately pins NO model and NO auth — the report's
    shape: its model comes from the Kimi CLI default and its provider table
    is CLI-managed.

    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: YAML text ready for bundle upload.
    """
    return f"""\
name: {_PARENT_NAME}
prompt: |
  You are a coding orchestrator with one `kimi` cross-vendor review
  sub-agent. Use `sys_list_models` to inspect worker model availability
  and `sys_session_send` to dispatch review tasks to the kimi worker.

executor:
  model: {_BRAIN_MODEL}
  harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_llm_server_url}/v1

tools:
  kimi:
    type: agent
    description: Kimi Code cross-vendor review sub-agent.
    executor:
      harness: kimi
    prompt: |
      You are the Kimi review sub-agent.
"""


@pytest.fixture
def kimi_orch_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_KimiOrchSession]:
    """Create a runner-bound session for the kimi-worker journeys.

    Same runner-respawn and bind contract as the suite's other agent
    fixtures. Each test scripts its own mock queue against the shared
    routing token before sending.

    :param live_server: Spawned server fixture.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :yields: A :class:`_KimiOrchSession` handle.
    """
    routing_token = f"kimi-worker-{uuid.uuid4().hex[:10]}"
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
        yield _KimiOrchSession(
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


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _expand_tool_call(page: Page, tool_name: str) -> None:
    """Expand *tool_name*'s call in the transcript so its output is visible.

    A lone call renders directly as a ``<tool>(...)`` trigger; completed
    multi-step turns fold calls into a collapsed "Called N tools" group —
    expand groups when present, then click the call trigger so its output
    preview is on screen (and in the recorded video).

    :param page: The Playwright page, on the parent session.
    :param tool_name: The tool to expand, e.g. ``"sys_list_models"``.
    """
    direct = page.get_by_role("button", name=re.compile(rf"^{tool_name}\("))
    if not direct.count():
        groups = page.get_by_text(re.compile(r"^Called \d+ tools?$"))
        expect(groups.first).to_be_visible(timeout=30_000)
        for group in groups.all():
            group.click()
        direct = page.get_by_role("button", name=re.compile(tool_name))
    expect(direct.first).to_be_visible(timeout=30_000)
    direct.first.click()


def _tool_outputs(base_url: str, session_id: str, tool_name: str) -> list[str]:
    """Fetch the persisted outputs of *tool_name* calls in a session.

    :param base_url: Spawned server base URL.
    :param session_id: The session id whose transcript to read.
    :param tool_name: The function name to collect outputs for.
    :returns: The raw output strings, in transcript order.
    """
    items_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=15.0)
    items_resp.raise_for_status()
    items = items_resp.json().get("data", [])
    call_ids = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and item.get("name") == tool_name
    }
    return [
        str(item.get("output") or "")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
    ]


@pytest.mark.timeout(600)
def test_kimi_worker_row_survives_generic_provider(
    page: Page,
    kimi_orch_session: _KimiOrchSession,
    mock_llm_server_url: str,
) -> None:
    """``sys_list_models`` must not crash resolving the kimi worker.

    Drives the reported journey in the SPA: with a generic provider in play
    (the staged ``default: pi`` entry / the runner's ambient OpenAI key),
    ask the orchestrator which models its workers can run, watch the
    ``sys_list_models`` call land in the transcript, expand it, and check
    the kimi worker's row. On un-fixed code resolution crashes —
    ``_KEY_AUTH_FAMILY["kimi"]`` raises KeyError — and the row reads
    ``no usable model provider (provider resolution failed: KeyError) —
    dispatches to this worker cannot run here``. This test fails there and
    passes once kimi resolution stops crashing (whatever usable shape its
    CLI-owned catalog row takes).

    :param page: pytest-playwright page fixture.
    :param kimi_orch_session: The orchestrator session handle.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    chat = kimi_orch_session
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-lm-{chat.routing_token}",
                        "name": "sys_list_models",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": f"Catalog reported. Marker: {chat.routing_token}"},
            # Spare turns: a child-failure inbox wake must not starve the queue.
            {"text": "Acknowledged."},
            {"text": "Acknowledged."},
        ],
        key=_BRAIN_MODEL,
        match=chat.routing_token,
    )

    page.goto(f"{chat.base_url}/c/{chat.session_id}")
    _send(
        page,
        "Which models can each of your workers run? Call sys_list_models "
        f"and summarize. Routing marker: {chat.routing_token}",
    )

    # The closing text turn proves the tool call completed and persisted.
    expect(
        page.locator(_ASSISTANT, has_text=f"Catalog reported. Marker: {chat.routing_token}").first
    ).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.get_by_test_id("working-indicator")).to_be_hidden(timeout=30_000)

    # Put the catalog on screen the way a user reads it (and the video shows it).
    _expand_tool_call(page, "sys_list_models")
    expect(page.get_by_text(re.compile(r'"kimi"')).first).to_be_visible(timeout=30_000)
    # Best-effort framing for the recorded journey: when the crash note is
    # present (un-fixed builds), scroll it into view so the failure is what
    # the viewer sees; a no-op on fixed builds.
    crash_note = page.get_by_text(re.compile(r"provider resolution failed"))
    if crash_note.count():
        crash_note.first.scroll_into_view_if_needed()
    page.wait_for_timeout(1_200)

    outputs = _tool_outputs(chat.base_url, chat.session_id, "sys_list_models")
    assert outputs, "no sys_list_models tool result found in the transcript"
    catalog = json.loads(outputs[-1])
    row = catalog.get("kimi")
    assert isinstance(row, dict), f"catalog has no 'kimi' row: {sorted(catalog)}"

    # THE BUG: provider resolution for the kimi worker crashes (KeyError)
    # instead of resolving its CLI-owned catalog, so the row carries the
    # crash note and the dead-worker source "none".
    note = str(row.get("note", ""))
    assert "provider resolution failed" not in note, (
        "Bug reproduced: sys_list_models crashed resolving the kimi worker's "
        f"provider ({note!r}) — full row: {row}"
    )


@pytest.mark.timeout(600)
def test_omitted_model_kimi_dispatch_keeps_kimi_default(
    page: Page,
    kimi_orch_session: _KimiOrchSession,
    mock_llm_server_url: str,
) -> None:
    """An omitted-model kimi dispatch must not inherit a Claude parent model.

    Drives the reported journey in the SPA: the parent session runs on
    ``claude-opus-4-8`` (its persisted model selection); the orchestrator
    dispatches a review task to the kimi worker with NO ``args.model``. The
    child session then appears in the Agents rail; opening it shows the
    model it came up on. On un-fixed code
    ``tool_dispatch._inherited_parent_model`` forces the parent's Claude
    model onto the Kimi child (``model_family_mismatch`` has no kimi rule),
    so the child persists ``model_override == "claude-opus-4-8"`` — this
    test fails there and passes once the dispatch keeps the Kimi CLI
    default instead of inheriting a foreign-family model.

    :param page: pytest-playwright page fixture.
    :param kimi_orch_session: The orchestrator session handle.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    chat = kimi_orch_session
    dispatch_args = json.dumps(
        {
            "agent": "kimi",
            "args": {
                "input": (
                    "Review the latest change for correctness and reply with "
                    f"a verdict. Marker: {chat.routing_token}"
                )
            },
        }
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-send-{chat.routing_token}",
                        "name": "sys_session_send",
                        "arguments": dispatch_args,
                    }
                ]
            },
            {"text": f"Dispatched to kimi. Marker: {chat.routing_token}"},
            # Spare turns: the kimi child (stub CLI) fails its turn and wakes
            # the parent; the wake must not starve the queue.
            {"text": "Acknowledged."},
            {"text": "Acknowledged."},
        ],
        key=_BRAIN_MODEL,
        match=chat.routing_token,
    )

    # The user's session model selection: the parent runs on Claude Opus.
    # (The gear modal exposes no picker for SDK-bundle brains, so the
    # selection is persisted the same way the UI's PATCH would.)
    patch_resp = httpx.patch(
        f"{chat.base_url}/v1/sessions/{chat.session_id}",
        json={"model_override": _PARENT_SESSION_MODEL},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    page.goto(f"{chat.base_url}/c/{chat.session_id}")
    _send(
        page,
        "Have the kimi worker review my latest change and report back. "
        f"Routing marker: {chat.routing_token}",
    )

    # The closing text turn proves the dispatch tool call completed.
    expect(
        page.locator(
            _ASSISTANT, has_text=f"Dispatched to kimi. Marker: {chat.routing_token}"
        ).first
    ).to_be_visible(timeout=_TURN_TIMEOUT_MS)

    # The dispatch handle (persisted tool output) names the child session.
    child_session_id: str | None = None
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and child_session_id is None:
        for output in _tool_outputs(chat.base_url, chat.session_id, "sys_session_send"):
            try:
                handle = json.loads(output)
            except ValueError:
                continue
            if isinstance(handle, dict) and handle.get("kind") == "sub_agent":
                child_session_id = str(handle.get("conversation_id") or "")
                break
        if child_session_id is None:
            time.sleep(1.0)
    assert child_session_id, (
        "sys_session_send returned no sub-agent handle — the dispatch to the "
        "kimi worker never created a child session (tool outputs: "
        f"{_tool_outputs(chat.base_url, chat.session_id, 'sys_session_send')})"
    )

    try:
        # Walk the rail the way a user finds the child: Agents tab → the
        # kimi row → click through to the child session page (the video
        # shows the child session and the model it came up on).
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        row = rail.locator(_SUBAGENT_ROW).first
        expect(row).to_be_visible(timeout=30_000)
        row.click()
        page.wait_for_url(re.compile(re.escape(f"/c/{child_session_id}")), timeout=30_000)
        expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
        # Linger so the recorded journey dwells on the child session (the
        # composer's model label names the model it came up on).
        page.wait_for_timeout(1_500)

        child_resp = httpx.get(f"{chat.base_url}/v1/sessions/{child_session_id}", timeout=15.0)
        child_resp.raise_for_status()
        child = child_resp.json()
        child_model = child.get("model_override")

        # THE BUG: the omitted-model dispatch inherited the parent's
        # foreign-family model instead of keeping the Kimi CLI default.
        assert not (isinstance(child_model, str) and "claude" in child_model.lower()), (
            "Bug reproduced: the omitted-model kimi dispatch came up on the "
            f"parent's model {child_model!r} (parent selection "
            f"{_PARENT_SESSION_MODEL!r}) instead of the Kimi CLI default — "
            "cross-vendor review independence is broken."
        )
    finally:
        httpx.delete(f"{chat.base_url}/v1/sessions/{child_session_id}", timeout=10.0)
