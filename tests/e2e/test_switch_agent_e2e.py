"""E2E tests for in-place agent switch — ``POST /v1/sessions/{id}/switch-agent``.

Real server + runner + LLM (or mock LLM). Unlike fork (which branches into a
NEW session), switch rebinds the SAME session to a different agent and continues
there. The core guarantee is that the new agent picks up the prior conversation:
an SDK target replays the Omnigent transcript as context, so a code word planted
before the switch must be recalled after it — on the same session id.

In mock mode the source and target are both inline ``openai-agents`` agents
pointed at the mock LLM server; each gets its own keyed response queue so the
test controls exactly what each agent says.

Usage::

    pytest tests/e2e/test_switch_agent_e2e.py -v --timeout=60
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid

import httpx
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import final_assistant_text


def _builtin_agent_id(client: httpx.Client, name: str) -> str:
    """Return the id of a built-in agent by name from ``GET /v1/agents``.

    :param client: HTTP client pointed at the test server.
    :param name: Built-in agent name, e.g. ``"sdk-chat-builtin"``.
    :returns: The built-in agent's id.
    :raises AssertionError: If no built-in with that name is registered.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == name:
            return str(agent["id"])
    raise AssertionError(f"built-in agent {name!r} not registered on the server")


def _bound_agent(client: httpx.Client, session_id: str) -> dict[str, str]:
    """Return the session's currently-bound agent object.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :returns: The agent object (``{id, name, harness, ...}``).
    """
    resp = client.get(f"/v1/sessions/{session_id}/agent")
    resp.raise_for_status()
    return resp.json()


def test_switch_agent_in_place_carries_history(
    http_client: httpx.Client,
    claude_coder_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str,
) -> None:
    """A switched agent recalls a code word planted before the switch.

    Plants a marker on a source session, switches the session IN PLACE to
    the ``sdk-chat-builtin`` agent, then asks the new agent to recall it. The
    new agent can only answer from the replayed Omnigent transcript — a
    regression (history not carried, or the runner kept serving the old
    agent) breaks the recall. Also asserts the session id is unchanged and the
    bound agent actually changed, proving this is an in-place switch and not a
    fork.

    In mock mode the source agent is an inline ``openai-agents`` agent
    pointed at the mock LLM server, and the ``sdk-chat-builtin`` built-in
    is wired to the mock server via ``executor.auth.base_url`` (seeded in
    ``conftest._materialize_builtin_sdk_chat_spec``). The source agent's
    queue is keyed by its unique model; the recall queue is claimed by a
    token unique to the recall turn (content-routing ``match``) so a
    stray request under the shared target model cannot drain it.

    :param http_client: HTTP client pointed at the live server.
    :param claude_coder_agent: The uploaded claude-sdk source agent name
        (used only in real-LLM mode).
    :param live_runner_id: The server fixture's runner id.
    :param using_mock_llm: Whether mock LLM is active.
    :param mock_llm_server_url: Mock LLM server URL.
    :returns: None.
    """
    marker = f"SWITCHWORD_{uuid.uuid4().hex[:6].upper()}"

    if using_mock_llm:
        # Source: inline openai-agents agent with its own mock queue.
        uid = uuid.uuid4().hex[:6]
        source_model = f"mock-switch-src-{uid}"
        source_agent = register_inline_agent(
            http_client,
            name=f"switch-src-{uid}",
            harness="openai-agents",
            model=source_model,
            profile="",
            prompt="You are a terse assistant.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        # The sdk-chat-builtin built-in uses the SHARED model name
        # "claude-sonnet-4-20250514", and the recall turn makes MORE than
        # one call to it: newer claude-code follows the recall with a
        # skills/system-reminder call. Two fragilities follow, both fixed
        # here:
        #   1. Keying the recall queue on the shared model name lets a
        #      stray request under the same model drain it. Instead claim
        #      the queue by a token unique to the recall turn (the #523
        #      content-routing "match"), carried only in the recall
        #      message, so only this turn's calls can draw it.
        #   2. A single queued entry is consumed by the first recall call;
        #      the follow-up call then falls through to the mock's default
        #      "Mock LLM response", which becomes the final assistant text
        #      and fails the assertion. A fallback on the same key answers
        #      every recall-turn call with the marker, robust to the count.
        recall_token = f"recall-{uid}"
        recall_key = f"switch-tgt-{uid}"
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "ACK"}],
            key=source_model,
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": marker}],
            key=recall_key,
            match=recall_token,
        )
        set_fallback_mock_llm(mock_llm_server_url, key=recall_key, text=marker)
    else:
        source_agent = claude_coder_agent

    # 1. Source session on the server's runner; plant a word.
    session_id = create_runner_bound_session(
        http_client, agent_name=source_agent, runner_id=live_runner_id
    )
    original_agent_id = _bound_agent(http_client, session_id)["id"]
    rid_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(f"Remember this code word for later: {marker}. Reply with exactly one word: ACK"),
    )
    body_1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid_1, timeout=180
    )
    assert body_1["status"] == "completed", f"plant turn failed: {body_1.get('error')}"

    # 2. Switch the SAME session in place to the built-in claude-sdk agent.
    target_id = _builtin_agent_id(http_client, "sdk-chat-builtin")
    resp = http_client.post(
        f"/v1/sessions/{session_id}/switch-agent", json={"agent_id": target_id}
    )
    assert resp.status_code == 200, f"switch failed: {resp.status_code} {resp.text}"
    switched = resp.json()
    # Same session — switch must not branch into a new conversation.
    assert switched["id"] == session_id, "switch must keep the same session id"
    # The bound agent changed (a fresh session-scoped clone of the target),
    # proving the rebind happened rather than a no-op.
    new_agent_id = switched["agent_id"]
    assert new_agent_id != original_agent_id, "switch must rebind to a new agent"
    # The bound agent now derives from the target built-in.
    assert "sdk-chat-builtin" in _bound_agent(http_client, session_id)["name"]

    # 3. Recall on the NEW agent, SAME session. Only possible if the prior
    # transcript was replayed as context to the switched-in agent.
    recall_prompt = (
        "Earlier in this conversation I gave you a code word to remember. "
        "Reply with exactly that code word and nothing else."
    )
    if using_mock_llm:
        # Carry the recall-only token so this request claims its own mock
        # queue regardless of the shared target model name.
        recall_prompt = f"{recall_prompt} ({recall_token})"
    rid_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=recall_prompt,
    )
    body_2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid_2, timeout=180
    )
    assert body_2["status"] == "completed", f"recall turn failed: {body_2.get('error')}"
    text = final_assistant_text(body_2).upper()
    assert marker in text, (
        f"switched agent did not recall {marker!r} (got {text!r}) — the prior "
        "transcript was not carried into the new agent on switch"
    )


def test_switch_agent_unknown_target_is_rejected(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Switching to a non-existent agent is rejected and leaves the session.

    Guards the validation path against the real server: an unknown target
    returns 404 and the session's bound agent is unchanged (no half-switch).

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: The server fixture's runner id.
    :param mock_llm_server_url: Mock LLM server URL.
    :returns: None.
    """
    uid = uuid.uuid4().hex[:6]
    model = f"mock-switch-unk-{uid}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"switch-unk-{uid}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Placeholder agent for unknown-target switch test.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    original_agent_id = _bound_agent(http_client, session_id)["id"]

    resp = http_client.post(
        f"/v1/sessions/{session_id}/switch-agent",
        json={"agent_id": "ag_does_not_exist"},
    )
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    # The rejected switch must not have mutated the binding.
    assert _bound_agent(http_client, session_id)["id"] == original_agent_id


def _upload_single_file_agent_with_os_env(
    client: httpx.Client,
    *,
    name: str,
    harness: str,
    model: str,
    os_env: dict[str, object],
) -> str:
    """Upload a single-file omnigent agent that declares an ``os_env``.

    Tarballs a minimal ``<name>.yaml`` carrying the given ``os_env`` block and
    uploads it via multipart ``POST /v1/sessions`` (same path the inline-agent
    helper uses, which the shared helper can't cover because it omits
    ``os_env``). Idempotent: a 409 from a prior parametrize row is treated as
    success.

    :param client: HTTP client pointed at the test server.
    :param name: Agent name, also used as the bundle's yaml filename.
    :param harness: Executor harness, e.g. ``"claude-sdk"``.
    :param model: Model identifier baked into the executor (never reached —
        this test runs no LLM turn, so the value only needs to parse).
    :param os_env: The ``os_env`` mapping, e.g.
        ``{"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}``.
    :returns: The registered agent name.
    """
    config: dict[str, object] = {
        "name": name,
        "prompt": "Filesystem-availability switch-test agent.",
        "executor": {"harness": harness, "model": model},
        "os_env": os_env,
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        bundle = buf.getvalue()
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    if resp.status_code == 409:
        return name
    resp.raise_for_status()
    side_session = resp.json()["session_id"]
    agent_resp = client.get(f"/v1/sessions/{side_session}/agent")
    agent_resp.raise_for_status()
    return str(agent_resp.json()["name"])


def test_switch_resets_os_env_filesystem_availability(
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """Switching flips the web-UI filesystem endpoint to the new agent's os_env.

    Exercises the exact surface the in-place-switch sandbox fix targets: the
    web-UI file/shell REST endpoints materialize the primary OSEnv from the
    runner's cached, spec-derived view of the session. This is NOT covered by
    the agent's own tools (re-derived per call) or by terminals.

    The source agent declares an ``os_env`` (filesystem available), so
    ``GET .../environments/default/filesystem`` returns 200. The session is
    then switched IN PLACE to ``sdk-chat-builtin``, which declares NO
    ``os_env`` — so once the runner re-resolves the spec, ``_require_os_env``
    makes the same endpoint return 404. The runner only re-resolves because
    the switch's ``POST /reset-state`` reset dropped the cached
    spec/snapshot; without that reset it keeps serving the source's spec and
    the endpoint stays 200. The AP server proxies this endpoint straight to
    the runner (no AP-side filesystem gate), so the 200 -> 404 flip is
    attributable to the runner-side reset, not to the AP server's own agent
    view. The reset runs as a post-response background task, hence the poll.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: The server fixture's runner id.
    :returns: None.
    """
    src_name = f"fs-switch-src-{uuid.uuid4().hex[:6]}"
    _upload_single_file_agent_with_os_env(
        http_client,
        name=src_name,
        harness="claude-sdk",
        model="claude-sonnet-4-20250514",
        os_env={"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=src_name, runner_id=live_runner_id
    )
    fs_url = f"/v1/sessions/{session_id}/resources/environments/default/filesystem"

    # Source declares os_env -> filesystem endpoint is available. This also
    # materializes the primary env and caches the source's spec/snapshot.
    before = http_client.get(fs_url)
    assert before.status_code == 200, (
        f"source agent declares os_env, so the filesystem endpoint should be "
        f"available before the switch, got {before.status_code}: {before.text}"
    )

    # Switch IN PLACE to the built-in sdk-chat-builtin, which has NO os_env.
    target_id = _builtin_agent_id(http_client, "sdk-chat-builtin")
    sw = http_client.post(f"/v1/sessions/{session_id}/switch-agent", json={"agent_id": target_id})
    assert sw.status_code == 200, f"switch failed: {sw.status_code} {sw.text}"
    assert sw.json()["id"] == session_id, "switch must keep the same session id"

    # The runner-side reset is a post-response background task, so poll until
    # the endpoint reflects the switched-to agent. 404 = the runner re-resolved
    # to sdk-chat-builtin (no os_env). If it stays 200 past the deadline, the
    # runner kept serving the source's cached spec/env — the bug this guards.
    deadline = time.time() + 30.0
    last_status = before.status_code
    while time.time() < deadline:
        after = http_client.get(fs_url)
        last_status = after.status_code
        if after.status_code == 404:
            break
        time.sleep(0.5)
    assert last_status == 404, (
        f"after switching to a no-os_env agent the filesystem endpoint must "
        f"become 404 (runner re-resolved the new spec); it stayed "
        f"{last_status} — the reset-state call did not drop the cached "
        f"spec/snapshot and the stale source env kept serving"
    )
