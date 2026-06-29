"""Integration tests for the session-scoped ``model_override`` column.

Mirrors the surviving ``reasoning_effort`` patterns in
``test_sessions_endpoints.py``: PATCH writes the column and the
snapshot reads it back. The LLM-piping coverage (mock_llm runner
integration) was retired alongside the DBOS execution path —
runner-path forwarding is verified here by stubbing
``_get_runner_client`` and capturing the runner POST body.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> dict[str, Any]:
    """
    Create a bare session and return the JSON body.

    :param client: The test HTTP client.
    :param agent_id: Agent id to bind, e.g. ``"ag_abc123"``.
    :returns: The session response body.
    """
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "initial_items": []},
    )
    assert resp.status_code == 201
    return resp.json()


async def test_patch_model_override_round_trips_through_snapshot(
    client: httpx.AsyncClient,
) -> None:
    """PATCH writes the column and ``GET`` returns the same value.

    This is the contract the web picker and the REPL's ``/model``
    command depend on for cross-surface sync: writing through one
    surface must be visible to the other on the very next snapshot.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Fresh sessions have no override.
    assert session.get("model_override") is None

    # PATCH a concrete model.
    patch = await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "claude-opus-4-7"},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["model_override"] == "claude-opus-4-7"

    # GET reflects the new value.
    get = await client.get(f"/v1/sessions/{sid}")
    assert get.status_code == 200
    assert get.json()["model_override"] == "claude-opus-4-7"


async def test_patch_model_override_clear_alias_resets(
    client: httpx.AsyncClient,
) -> None:
    """``model_override: "default"`` is the explicit clear alias.

    Mirrors the REPL's ``/model default | off | reset`` semantics so
    that web's "clear" path and the REPL converge on the same wire
    representation.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Set, then clear.
    set_resp = await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "claude-sonnet-4-6"},
    )
    assert set_resp.json()["model_override"] == "claude-sonnet-4-6"

    for alias in ("default", "off", "reset"):
        clear_resp = await client.patch(
            f"/v1/sessions/{sid}",
            json={"model_override": alias},
        )
        assert clear_resp.status_code == 200, clear_resp.text
        assert clear_resp.json()["model_override"] is None, (
            f"clear alias {alias!r} did not null out model_override"
        )

        # Re-set so the next iteration has something to clear.
        await client.patch(
            f"/v1/sessions/{sid}",
            json={"model_override": "claude-sonnet-4-6"},
        )


async def test_patch_model_override_rejects_empty_string(
    client: httpx.AsyncClient,
) -> None:
    """Empty / whitespace-only strings fail loud rather than silently clear.

    The ``default`` alias is the only intended clear path; accepting
    ``""`` would conflate "I didn't fill the picker" with "I want the
    agent default" and make UI bugs invisible.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    for bad in ("", "   ", "\t"):
        resp = await client.patch(
            f"/v1/sessions/{sid}",
            json={"model_override": bad},
        )
        assert resp.status_code == 400, (
            f"Empty model_override {bad!r} should 400, got {resp.status_code}: {resp.text}"
        )


@pytest.mark.parametrize(
    "bad_model",
    [
        pytest.param("claude; rm -rf /", id="shell-metacharacters"),
        pytest.param("--model=evil", id="flag-shaped"),
        pytest.param(
            'x",auth={command="sh",args=["-c","touch /tmp/pwned"]},wire_api="responses"}',
            id="codex-toml-breakout",
        ),
    ],
)
async def test_patch_model_override_rejects_malformed(
    client: httpx.AsyncClient,
    bad_model: str,
) -> None:
    """PATCH ``model_override`` outside the model-id charset 400s.

    Regression for a host-RCE gap: the create path validated the override
    against the model-id charset, but the PATCH path only stripped it. A
    value like ``x",auth={command="sh",args=["-c","..."]}`` would break out
    of the ``model="..."`` field in the Codex provider ``config.toml`` and
    replace the token-minting ``auth.command`` Codex runs via ``sh -c`` on
    the host. PATCH must refuse it exactly like create does, and must not
    persist it.

    :param bad_model: The malformed override under test.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": bad_model},
    )
    assert resp.status_code == 400, (
        f"model_override {bad_model!r} should 400, got {resp.status_code}: {resp.text}"
    )

    # A rejected value must never reach the row the runner snapshots.
    get = await client.get(f"/v1/sessions/{sid}")
    assert get.status_code == 200
    assert get.json().get("model_override") is None


async def test_create_session_with_model_override_persists(
    client: httpx.AsyncClient,
) -> None:
    """Create-time ``model_override`` lands on the row and the snapshot.

    This is the seam ``sys_session_send``'s per-dispatch ``model`` arg
    relies on: the value must be persisted before the runner fetches the
    session snapshot (native terminal launch reads it as ``--model``).
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "model_override": "databricks-claude-sonnet-4-6",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # The create response itself must carry the override — the runner's
    # launch-config fetch consumes this exact snapshot shape.
    assert created["model_override"] == "databricks-claude-sonnet-4-6"

    get = await client.get(f"/v1/sessions/{created['id']}")
    assert get.status_code == 200
    assert get.json()["model_override"] == "databricks-claude-sonnet-4-6"


@pytest.mark.parametrize(
    "bad_model",
    [
        pytest.param("claude; rm -rf /", id="shell-metacharacters"),
        pytest.param("--model=evil", id="flag-shaped"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
async def test_create_session_rejects_malformed_model_override(
    client: httpx.AsyncClient,
    bad_model: str,
) -> None:
    """Create-time ``model_override`` outside the model-id charset 400s.

    The persisted value later becomes a ``--model`` argv element on the
    runner, so the route must refuse shell-/flag-shaped strings before
    any row exists — the runner-side validation alone is not the trust
    boundary.

    :param bad_model: The malformed override under test.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "model_override": bad_model,
        },
    )
    assert resp.status_code == 400, (
        f"model_override {bad_model!r} should 400, got {resp.status_code}: {resp.text}"
    )


async def test_create_session_with_reasoning_effort_persists(
    client: httpx.AsyncClient,
) -> None:
    """Create-time ``reasoning_effort`` lands on the row and the snapshot.

    This is the seam the web new-session model/effort picker relies on:
    the value must be persisted before the runner fetches the session
    snapshot (native Claude Code reads it as ``--effort`` at terminal
    launch).
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "reasoning_effort": "high",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # The create response itself must carry the effort — the runner's
    # launch-config fetch consumes this exact snapshot shape.
    assert created["reasoning_effort"] == "high"

    get = await client.get(f"/v1/sessions/{created['id']}")
    assert get.status_code == 200
    assert get.json()["reasoning_effort"] == "high"


async def test_create_session_rejects_invalid_reasoning_effort(
    client: httpx.AsyncClient,
) -> None:
    """Create-time ``reasoning_effort`` outside the effort vocabulary 400s.

    Validated before any row exists so a bad value never creates an orphan
    session, mirroring the ``model_override`` charset guard.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "reasoning_effort": "turbo",
        },
    )
    assert resp.status_code == 400, (
        f"reasoning_effort 'turbo' should 400, got {resp.status_code}: {resp.text}"
    )


class _CaptureClient:
    """Stub runner client that records the POSTed body for inspection."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        """Record the path + body and return a fake 202 response."""
        self._captured["path"] = path
        self._captured["body"] = json

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


def _stub_runner_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``_get_runner_client`` to return a capturing stub.

    :returns: A dict the test inspects after the runner POST runs;
        contains ``path`` and ``body`` keys once the route fires.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured: dict[str, Any] = {}

    async def _stub(*_: Any, **__: Any) -> _CaptureClient:
        return _CaptureClient(captured)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    return captured


async def test_runner_path_forwards_persisted_model_override(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default runner path forwards the persisted ``model_override``.

    Without this guarantee, a UI / REPL PATCH would silently no-op
    for clients that don't repeat ``model_override`` on every event.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Persist the override and send an event WITHOUT repeating it —
    # only the persisted column should reach the runner body.
    patch = await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "claude-opus-4-7"},
    )
    assert patch.status_code == 200, patch.text

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    )
    assert resp.status_code == 202, resp.text

    assert captured.get("body") is not None, (
        "Runner client was never POSTed to — _forward_event_to_runner "
        "did not run. Check the runner-stub wiring."
    )
    assert captured["body"].get("model_override") == "claude-opus-4-7", (
        f"Runner body missing model_override; got keys "
        f"{sorted(captured['body'].keys())!r}. The persisted column did "
        f"not flow into _forward_event_to_runner's runner_body."
    )


async def test_create_time_model_override_forwards_on_first_event(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create-time override reaches the runner on the very first event.

    This is the SDK-harness leg of ``sys_session_send``'s per-dispatch
    ``model``: the child's first message event must carry the persisted
    override so ``_resolve_harness_config`` bakes it into the spawn env
    (``HARNESS_<H>_MODEL``) for the child's first turn — not only after
    a later PATCH.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "model_override": "databricks-claude-sonnet-4-6",
        },
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "first turn"}],
            },
        },
    )
    assert event.status_code == 202, event.text

    assert captured.get("body") is not None, (
        "Runner client was never POSTed to — _forward_event_to_runner "
        "did not run. Check the runner-stub wiring."
    )
    assert captured["body"].get("model_override") == "databricks-claude-sonnet-4-6", (
        f"First-event runner body missing the create-time override; got "
        f"{captured['body'].get('model_override')!r}. The create route "
        f"did not persist model_override before the first turn."
    )


async def test_context_window_uses_effective_model(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``context_window`` follows the override, not the spec model.

    The picker's context ring would otherwise lie after a model switch
    (e.g. spec=Sonnet 200K, override=Haiku 200K vs Opus 200K) until
    the override is cleared. Stub the litellm lookup so we can assert
    *which* model the snapshot used to size the window.
    """
    from omnigent.llms import context_window as context_window_mod

    lookup_calls: list[str] = []

    def _stub(model: str) -> int:
        lookup_calls.append(model)
        return 999_999

    monkeypatch.setattr(context_window_mod, "get_model_context_window", _stub)

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Baseline: no override → lookup uses the spec model.
    lookup_calls.clear()
    baseline = await client.get(f"/v1/sessions/{sid}")
    assert baseline.status_code == 200
    baseline_lookup = lookup_calls[-1] if lookup_calls else None

    # Apply an override and re-fetch — lookup must now use the override.
    await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "claude-opus-4-7"},
    )
    lookup_calls.clear()
    after = await client.get(f"/v1/sessions/{sid}")
    assert after.status_code == 200
    assert after.json()["context_window"] == 999_999
    assert "claude-opus-4-7" in lookup_calls, (
        f"Expected the override model in the context_window lookup; got "
        f"{lookup_calls!r}. The snapshot is still sizing the context "
        f"ring against the spec model {baseline_lookup!r}."
    )


async def test_context_window_override_bypasses_declared_window(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec-declared ``executor.context_window`` is bypassed by an override.

    Anti-drift guarantee for the shared resolver: the ring and the runner's
    compaction budget are now computed by the SAME
    ``resolve_effective_context_window``, so a declared 1M window can't mask a
    small override model. With no override the declared window wins (no catalog
    lookup); with an override active the ring sizes against the override
    model's real window instead. Before the shared resolver these two paths
    drifted (PR #769).
    """
    from omnigent.llms import context_window as context_window_mod

    lookup_calls: list[str] = []

    def _stub(model: str) -> int:
        lookup_calls.append(model)
        return 200_000

    monkeypatch.setattr(context_window_mod, "get_model_context_window", _stub)

    agent = await create_test_agent(
        client,
        name="declared-window-agent",
        executor={"type": "omnigent", "context_window": 1_000_000},
    )
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # No override: the declared 1M window wins and short-circuits the catalog.
    lookup_calls.clear()
    baseline = await client.get(f"/v1/sessions/{sid}")
    assert baseline.status_code == 200
    assert baseline.json()["context_window"] == 1_000_000
    assert lookup_calls == [], (
        f"A declared window must short-circuit the catalog lookup; got {lookup_calls!r}."
    )

    # Override active: the declared 1M is bypassed; the ring sizes against the
    # override model's real (stubbed 200K) window.
    await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "claude-opus-4-7"},
    )
    lookup_calls.clear()
    after = await client.get(f"/v1/sessions/{sid}")
    assert after.status_code == 200
    assert after.json()["context_window"] == 200_000, (
        "An active override must bypass the declared 1M window and size the "
        "ring against the override model's real window."
    )
    assert "claude-opus-4-7" in lookup_calls


async def test_silent_patch_skips_claude_native_forward(
    client: httpx.AsyncClient,
) -> None:
    """``silent: true`` persists but doesn't inject ``/model`` into tmux.

    Without this, the web sticky-pref handoff on a fresh session
    would render a leading "Command model X" slash-command item
    before the user has sent anything — the bug a user reported.

    Updated for the unified-events refactor: Omnigent server no longer
    calls a dedicated ``_forward_claude_native_model`` helper. It
    POSTs ``{"type": "model_change", "model": <override>}`` to the
    runner's ``/v1/sessions/{id}/events``. The silent-skip semantic
    is pinned by intercepting the runner POST and asserting no
    ``model_change`` event reaches it when ``silent: true``.
    """
    import json

    from omnigent.runtime import set_runner_client

    captured: list[tuple[str, dict[str, Any] | None]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record POSTs to /events; let snapshot/status reads pass through."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        captured.append((str(request.url), body))
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        sid = session["id"]
        # Mark the session as claude-native so the forward gate is True.
        await client.patch(
            f"/v1/sessions/{sid}",
            json={"labels": {"omnigent.wrapper": "claude-code-native-ui"}},
        )
        captured.clear()

        # User-driven PATCH (silent omitted → False): forward runs.
        resp = await client.patch(
            f"/v1/sessions/{sid}",
            json={"model_override": "claude-opus-4-7"},
        )
        assert resp.status_code == 200, resp.text
        # Exactly one model_change POST to /events. 0 = the always-
        # forward contract regressed; 2+ = a legacy carve-out came back.
        model_forwards_after_user_patch = [
            (url, body)
            for url, body in captured
            if url.endswith(f"/v1/sessions/{sid}/events")
            and isinstance(body, dict)
            and body.get("type") == "model_change"
        ]
        assert len(model_forwards_after_user_patch) == 1, (
            f"User PATCH should POST exactly one model_change event; "
            f"got {model_forwards_after_user_patch!r}. All runner "
            f"POSTs: {captured!r}"
        )
        # Body contract: runner reads ``type`` and ``model`` from
        # this to drive the harness-specific dispatch.
        assert model_forwards_after_user_patch[0][1] == {
            "type": "model_change",
            "model": "claude-opus-4-7",
        }

        # Bind-time PATCH (silent: true): forward suppressed.
        captured.clear()
        resp = await client.patch(
            f"/v1/sessions/{sid}",
            json={"model_override": "claude-sonnet-4-6", "silent": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["model_override"] == "claude-sonnet-4-6"
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # No model_change POST must reach the runner. A non-empty list
    # here means the silent flag was ignored and bind-time sticky-
    # pref handoff would inject a visible ``/model X`` item into a
    # fresh pane — the bug this skip exists to prevent.
    model_forwards_after_silent_patch = [
        (url, body)
        for url, body in captured
        if isinstance(body, dict) and body.get("type") == "model_change"
    ]
    assert model_forwards_after_silent_patch == [], (
        f"silent=True must skip the model_change forward; got "
        f"{model_forwards_after_silent_patch!r}. All runner POSTs: {captured!r}"
    )

    # No POST to the legacy ``/claude-native-model`` route should
    # happen anymore — its callsite is gone from Omnigent server.
    legacy_forwards = [url for url, _ in captured if "/claude-native-model" in url]
    assert legacy_forwards == [], (
        f"Legacy /claude-native-model POSTs must not happen anymore; "
        f"AP server forwards model changes through /events. Got: "
        f"{legacy_forwards!r}"
    )


async def test_per_event_model_override_wins_over_persisted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event-level model_override takes precedence over the persisted column.

    Documents the precedence: per-event > persisted > agent default.
    The REPL still sends ``model_override`` on each event for safety,
    so a stale persisted value must not override an explicit per-event
    ask.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Persist Opus, then send an event explicitly asking for Sonnet.
    await client.patch(
        f"/v1/sessions/{sid}",
        json={"model_override": "claude-opus-4-7"},
    )
    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
            "model_override": "claude-sonnet-4-6",
        },
    )
    assert resp.status_code == 202, resp.text

    assert captured.get("body", {}).get("model_override") == "claude-sonnet-4-6", (
        f"Per-event model_override should win; runner body had "
        f"{captured.get('body', {}).get('model_override')!r}."
    )
