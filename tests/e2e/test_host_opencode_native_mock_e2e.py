"""Deterministic opencode-native journeys: a real ``opencode serve`` on a scripted provider.

The live suite (``test_host_opencode_native_e2e.py``) needs a real model and
asserts loosely on its replies. This suite drives the same product path (host
daemon -> runner -> ``opencode serve`` + forwarder + TUI terminal) against the
e2e mock model server instead. The host's config binds ``opencode-native`` to a
chat-wire mock provider, so every reply, tool call and held response is
scripted, and every assertion checks the request the mock actually received.

Needs ``@opencode/cli`` in the supported range and ``tmux`` on ``PATH``; runs in
mock mode only (no ``--llm-api-key``)::

    npm install -g @opencode/cli@2.0.32
    uv run pytest tests/e2e/test_host_opencode_native_mock_e2e.py --reruns 0 -v

Set ``OMNIGENT_E2E_OPENCODE_REQUIRED=1`` to fail instead of skip when the CLI
is missing or unsupported, as CI's ``E2E Harness (opencode)`` job does.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.opencode_native import bridge as opencode_bridge
from tests._helpers.host import Host, isolated_host
from tests._helpers.polling import poll_until
from tests.e2e import _opencode_native_helpers as oc
from tests.e2e._native_resume_helpers import poll_for_pending_elicitation, resolve_elicitation
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    release_mock_gate,
    reset_mock_llm,
)

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux for the OpenCode TUI"),
]

_MODEL = "opencode-mock"
_ALT_MODEL = "opencode-mock-alt"
_PROVIDER = "e2e-opencode-mock"
_TURN_TIMEOUT_S = 120.0
# Scripted queues answer only requests that offer OpenCode's shell tool, so
# OpenCode's tool-less background requests (titles, summaries) never consume them.
_TURN_TOOLS = ["shell"]


def _host_config(mock_url: str) -> dict[str, Any]:
    """Bind ``opencode-native`` to a chat-wire provider served by the mock."""
    return {
        "providers": {
            _PROVIDER: {
                "kind": "key",
                "openai": {
                    "base_url": f"{mock_url}/v1",
                    "api_key": "mock-key",
                    "wire_api": "chat",
                    "models": {"default": _MODEL},
                },
            }
        },
        "inference": {
            "harnesses": {
                "opencode-native": {
                    "provider": _PROVIDER,
                    "default_model": _MODEL,
                    "model_allowlist": [_MODEL, _ALT_MODEL],
                }
            }
        },
    }


@pytest.fixture(scope="module")
def mock_host(
    live_server: str,
    http_client: httpx.Client,
    using_mock_llm: bool,
    mock_llm_server_url: str | None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Host, str]]:
    """One isolated host whose runner binds OpenCode to the mock; stopped at module end."""
    if not using_mock_llm or mock_llm_server_url is None:
        pytest.skip("scripted OpenCode journeys run in mock mode only")
    oc.require_opencode()
    root = tmp_path_factory.mktemp("opencode-mock-host")
    with isolated_host(root, live_server, config=_host_config(mock_llm_server_url)) as host:
        host.wait_online(http_client, timeout=60, poll_interval=0.2)
        yield host, mock_llm_server_url


@pytest.fixture
def opencode_host(
    mock_host: tuple[Host, str],
    http_client: httpx.Client,
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[oc.OpenCodeHost]:
    """Per-test handle on the isolated host; resets the mock and deletes created sessions."""
    host, mock_url = mock_host
    reset_mock_llm(mock_url)
    # Bridge state lives under the isolated host's data directory.
    monkeypatch.setattr(
        opencode_bridge, "_BRIDGE_ROOT", host.home / ".omnigent" / "opencode-native"
    )
    handle = oc.OpenCodeHost(
        client=http_client,
        live_server=live_server,
        host_id=host.host_id,
        agent_id=oc.opencode_agent_id(http_client),
        root=tmp_path,
        model=_MODEL,
    )
    try:
        yield handle
    finally:
        handle.cleanup()


@pytest.fixture
def mock_url(mock_host: tuple[Host, str]) -> str:
    return mock_host[1]


def _marker(label: str) -> str:
    """A prompt token unique to one turn, used to claim its scripted queue.

    The mock prefers the longest matching token, so every marker has the same
    length: a request that replays earlier turns then routes by its newest one.
    """
    return f"{label[:12]:-<12}-{uuid.uuid4().hex[:10]}"


def _script(mock_url: str, marker: str, responses: list[dict[str, Any]]) -> None:
    """Queue *responses* for the turn whose prompt carries *marker*."""
    configure_mock_llm(mock_url, responses, match=marker, required_tools=_TURN_TOOLS)


def _requests_with(mock_url: str, needle: str) -> list[dict[str, Any]]:
    """Captured model requests whose body mentions *needle*."""
    return [r for r in get_mock_requests(mock_url) if needle in json.dumps(r)]


def _shell_call(command: str) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "call_id": "call_shell",
                "name": "shell",
                "arguments": json.dumps({"command": command}),
            }
        ]
    }


def _gate_pending(mock_url: str) -> bool:
    response = httpx.get(f"{mock_url}/gate/pending", timeout=5.0)
    response.raise_for_status()
    return bool(response.json().get("pending"))


def test_reply_reaches_the_bound_mock_and_renders_once(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`launch-reply`: the user's prompt reaches the bound model and its reply appears once."""
    marker = _marker("HELLO")
    reply = f"REPLY-{marker}"
    _script(mock_url, marker, [{"text": reply}])
    session_id = opencode_host.create_session(opencode_host.workspace("reply"))

    items = oc.turn(opencode_host.client, session_id, f"Say hello. {marker}")

    assert oc.assistant_texts(items).count(reply) == 1, oc.transcript(items)
    turn_requests = _requests_with(mock_url, marker)
    assert turn_requests, "the prompt never reached the mock model"
    assert {r.get("model") for r in turn_requests} == {_MODEL}, turn_requests


def test_approved_tool_result_reaches_the_next_request(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`approvals` allow + `tool-history`: the approved shell output is sent back to the model."""
    marker = _marker("LIST")
    _script(mock_url, marker, [_shell_call("ls"), {"text": f"LISTED-{marker}"}])
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("approve"))
    oc.attach_ask_on_os_tools(client, session_id)

    oc.send(client, session_id, f"List the files. {marker}")
    card = poll_for_pending_elicitation(
        client, conversation_id=session_id, timeout=_TURN_TIMEOUT_S
    )
    resolve_elicitation(
        client, conversation_id=session_id, elicitation_id=str(card["elicitation_id"])
    )
    items = oc.wait_for_idle_after(client, session_id, min_assistant=1, timeout=_TURN_TIMEOUT_S)

    assert any("omnigent_marker.txt" in out for out in oc.outputs(items)), oc.transcript(items)
    assert oc.assistant_texts(items)[-1] == f"LISTED-{marker}", oc.transcript(items)
    follow_up = [
        r for r in _requests_with(mock_url, marker) if "omnigent_marker.txt" in json.dumps(r)
    ]
    assert follow_up, "the tool output never reached the model's next request"


def test_denied_tool_does_not_run_and_the_session_continues(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`approvals` deny: the shell command never runs and the next turn still completes."""
    marker = _marker("DENY")
    _script(mock_url, marker, [_shell_call("touch denied.txt")])
    client = opencode_host.client
    workspace = opencode_host.workspace("deny")
    session_id = opencode_host.create_session(workspace)
    oc.attach_ask_on_os_tools(client, session_id)

    oc.send(client, session_id, f"Create a file. {marker}")
    card = poll_for_pending_elicitation(
        client, conversation_id=session_id, timeout=_TURN_TIMEOUT_S
    )
    resolve_elicitation(
        client,
        conversation_id=session_id,
        elicitation_id=str(card["elicitation_id"]),
        action="decline",
    )
    poll_until(
        lambda: oc.session(client, session_id).get("status") == "idle",
        timeout_s=_TURN_TIMEOUT_S,
    )

    assert not (workspace / "denied.txt").exists(), "the denied command ran"
    outputs = oc.outputs(oc.session_items(client, session_id))
    assert outputs and all(out.startswith("[error]") for out in outputs), outputs

    next_marker = _marker("AFTER-DENY")
    _script(mock_url, next_marker, [{"text": f"PONG-{next_marker}"}])
    items = oc.turn(client, session_id, f"Reply PONG. {next_marker}")
    assert oc.assistant_texts(items)[-1] == f"PONG-{next_marker}", oc.transcript(items)


def test_interrupt_settles_a_held_reply_and_the_next_turn_works(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`interrupt-continue`: Stop while the model holds its reply; the late reply never lands."""
    marker = _marker("HOLD")
    late = f"LATE-{marker}"
    _script(mock_url, marker, [{"text": late, "block": True}])
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("interrupt"))

    oc.send(client, session_id, f"Take your time. {marker}")
    poll_until(lambda: _gate_pending(mock_url), timeout_s=_TURN_TIMEOUT_S)
    client.post(
        f"/v1/sessions/{session_id}/events", json={"type": "interrupt"}, timeout=30.0
    ).raise_for_status()
    poll_until(
        lambda: oc.session(client, session_id).get("status") == "idle",
        timeout_s=60.0,
    )
    release_mock_gate(mock_url)

    # The follow-up runs to a settled idle, which is the window a released
    # reply would have to land in.
    next_marker = _marker("AFTER-STOP")
    _script(mock_url, next_marker, [{"text": f"PONG-{next_marker}"}])
    items = oc.turn(client, session_id, f"Reply PONG. {next_marker}")
    assert oc.assistant_texts(items)[-1] == f"PONG-{next_marker}", oc.transcript(items)
    assert late not in oc.assistant_texts(items), oc.transcript(items)
    assert oc.session(client, session_id).get("status") == "idle"


def test_model_switch_reaches_the_next_request(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`model-effort`: a web model switch changes the model of the next turn's request."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("model"))
    resp = client.patch(f"/v1/sessions/{session_id}", json={"model_override": _ALT_MODEL})
    assert resp.status_code == 200, resp.text[:300]

    marker = _marker("SWITCH")
    _script(mock_url, marker, [{"text": f"OK-{marker}"}])
    oc.turn(client, session_id, f"Reply OK. {marker}")

    models = {r.get("model") for r in _requests_with(mock_url, marker)}
    assert models == {_ALT_MODEL}, models


def test_fork_first_turn_carries_the_parent_history(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`fork`: the clone's first request includes the parent's turn; the parent stays separate."""
    client = opencode_host.client
    workspace = opencode_host.workspace("fork")
    source_id = opencode_host.create_session(workspace)
    parent_marker = _marker("PARENT")
    _script(mock_url, parent_marker, [{"text": f"NOTED-{parent_marker}"}])
    oc.turn(client, source_id, f"Remember the mangosteen. {parent_marker}")

    fork = client.post(f"/v1/sessions/{source_id}/fork", json={}, timeout=60.0)
    assert fork.status_code == 201, fork.text[:300]
    clone_id = str(fork.json()["id"])
    opencode_host.created.append(clone_id)
    launch = client.post(
        f"/v1/hosts/{opencode_host.host_id}/runners",
        json={"session_id": clone_id, "workspace": str(workspace)},
        timeout=60.0,
    )
    assert launch.status_code < 300, launch.text[:300]
    oc.poll_for_terminal(
        client,
        session_id=clone_id,
        resource_id=terminal_resource_id("opencode", "main"),
        timeout=oc.TERMINAL_TIMEOUT_S,
    )
    assert oc.opencode_session_id(client, clone_id) != oc.opencode_session_id(client, source_id)

    clone_marker = _marker("CLONE")
    _script(mock_url, clone_marker, [{"text": f"RECALLED-{clone_marker}"}])
    before = len(oc.assistant_texts(oc.session_items(client, clone_id)))
    oc.send(client, clone_id, f"What fruit did I mention? {clone_marker}")
    oc.wait_for_idle_after(client, clone_id, min_assistant=before + 1, timeout=_TURN_TIMEOUT_S)

    clone_requests = _requests_with(mock_url, clone_marker)
    assert clone_requests, "the clone's prompt never reached the model"
    assert parent_marker in json.dumps(clone_requests[0]), "the fork lost the parent's history"
    parent_texts = oc.assistant_texts(oc.session_items(client, source_id))
    assert f"RECALLED-{clone_marker}" not in parent_texts, (
        "the clone's turn leaked into the parent"
    )


def _compaction_summary(fact: str) -> str:
    """A summary in OpenCode's compaction template, which it rejects otherwise."""
    return "\n".join(
        [
            "## Objective",
            "- Keep the conversation going.",
            "## Requirements",
            "- (none)",
            "## Decisions",
            "- (none)",
            "## Work State",
            "### Completed",
            f"- {fact}",
            "### Active",
            "- (none)",
            "### Blocked",
            "- (none)",
            "## Next Move",
            "1. (none)",
            "## Relevant Files",
            "(none)",
            "## Important Context",
            f"- {fact}",
        ]
    )


def test_compact_finishes_and_the_next_turn_works(
    opencode_host: oc.OpenCodeHost, mock_url: str
) -> None:
    """`compaction`: ``/compact`` completes and the next request carries its summary."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("compact"))
    first = _marker("BEFORE-COMPACT")
    fact = f"SUMMARY-{first}"
    # The compaction request resends the conversation, so the turn's marker
    # routes it to the same queue: the second reply is the summary.
    _script(mock_url, first, [{"text": f"OK-{first}"}, {"text": _compaction_summary(fact)}])
    oc.turn(client, session_id, f"Reply OK. {first}")

    with oc.SseRecorder(opencode_host.live_server, session_id) as sse:
        resp = client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "compact"}, timeout=90.0
        )
        assert resp.status_code in (200, 202), resp.text[:300]
        sse.wait_for("response.compaction.completed", timeout=_TURN_TIMEOUT_S)
        assert not sse.of_type("response.compaction.failed")

    after = _marker("AFTER-COMPACT")
    _script(mock_url, after, [{"text": f"OK-{after}"}])
    items = oc.turn(client, session_id, f"Reply OK. {after}")
    assert oc.assistant_texts(items)[-1] == f"OK-{after}", oc.transcript(items)
    next_request = json.dumps(_requests_with(mock_url, after)[0])
    assert fact in next_request, "the post-compaction request lost the summary"
