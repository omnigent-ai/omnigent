"""E2E coverage for fresh registered-agent sessions on a remote-URL target.

``omnigent chat <remote-url>`` connects to a server that already has its
agents registered, so the CLI has no local bundle to upload.  Session
creation must therefore bind by ``agent_id`` through the JSON
``POST /v1/sessions`` route rather than the multipart bundle route.

The following paths are covered:

1. **Interactive fresh session.** ``_SessionsChatReplAdapter`` with
   ``session_bundle=None`` resolves the registered ``agent_id`` and creates
   the session, instead of refusing for lack of a bundle.
2. **Server JSON route.** ``POST /v1/sessions`` with ``{"agent_id": ...}``
   creates a session that completes a turn end-to-end.
3. **Headless ``-p``.** ``_query_sessions_once`` with ``session_bundle=None``
   goes through the sessions API rather than the removed legacy
   ``/v1/responses`` endpoint.
4. **Fresh server.** Agent discovery and the ``omnigent run --server`` one-shot
   work against a server whose agents are registered but which has no
   sessions yet, and a server without an online runner fails cleanly.
5. **Connected host.** Both the one-shot CLI and the interactive terminal
   launch a runner on this machine's host when no matching runner is online.

Run locally (mock LLM, no real API key needed)::

    pytest tests/e2e/test_remote_url_chat_fresh_session_2437.py -v
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml as _yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.host import isolated_host
from tests._helpers.live_server import (
    isolated_local_server,
    isolated_process_env,
)
from tests._helpers.polling import poll_until
from tests._helpers.session import post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_agent_yaml(
    tmp_path: Path,
    *,
    agent_name: str,
    model: str,
    mock_llm_server_url: str | None,
) -> Path:
    """Write a minimal openai-agents YAML wired to the mock LLM server."""
    yaml_path = tmp_path / f"{agent_name}.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "name": agent_name,
                "prompt": "You are a terse test assistant.",
                "executor": {
                    "harness": "openai-agents",
                    "model": model,
                    "profile": "",
                    "auth": {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_llm_server_url}/v1",
                    },
                },
            }
        )
    )
    return yaml_path


class _RegisteredAgentServer:
    """A booted local server plus the registered agent's identifiers."""

    def __init__(self, base_url: str, agent_name: str, runner_id: str, marker: str) -> None:
        self.base_url = base_url
        self.agent_name = agent_name
        self.runner_id = runner_id
        self.marker = marker

    def client(self) -> httpx.Client:
        """Return an HTTP client bound to this server."""
        return httpx.Client(base_url=self.base_url, timeout=30.0)

    def agent_id(self) -> str:
        """Resolve the registered agent id through ``GET /v1/agents``."""
        with self.client() as client:
            resp = client.get("/v1/agents", params={"limit": 100})
            resp.raise_for_status()
            data = resp.json().get("data", [])
        for item in data:
            if item.get("name") == self.agent_name:
                return str(item["id"])
        raise AssertionError(
            f"agent {self.agent_name!r} not registered. Available: {[d.get('name') for d in data]}"
        )


@pytest.fixture
def registered_agent_server(
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> Iterator[_RegisteredAgentServer]:
    """
    Boot a local server whose agent is registered but has no client bundle.

    Mirrors the remote-URL target: the caller knows only the agent's name,
    and must resolve its id from the server to create a session.
    """
    from omnigent.chat import (
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    suffix = uuid.uuid4().hex[:6]
    agent_name = f"remote-fresh-{suffix}"
    model = f"mock-{suffix}"
    marker = f"REMOTE_FRESH_OK_{suffix}"

    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"

    yaml_path = _write_agent_yaml(
        tmp_path,
        agent_name=agent_name,
        model=model,
        mock_llm_server_url=mock_llm_server_url,
    )
    reset_mock_llm(mock_llm_server_url)
    # Every test in this module drives at most a couple of turns; repeat the
    # marker so a retried or multi-turn path still gets a scripted reply.
    configure_mock_llm(mock_llm_server_url, [{"text": marker}] * 4, key=model)

    port = find_free_port()
    server = _start_local_server(yaml_path, port, ephemeral=True)
    try:
        _wait_for_server(port, server)
        yield _RegisteredAgentServer(
            base_url=f"http://127.0.0.1:{port}",
            agent_name=agent_name,
            runner_id=server.runner_id or "",
            marker=marker,
        )
    finally:
        _stop_local_server(server)


def _assistant_text(base_url: str, session_id: str) -> str:
    """Concatenate persisted assistant text for a session."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        body = resp.json()
    parts: list[str] = []
    for item in body.get("items", []):
        data = item.get("data", {})
        if data.get("role") != "assistant":
            continue
        for chunk in data.get("content", []):
            text = chunk.get("text")
            if text:
                parts.append(text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Interactive fresh session (no local bundle)
# ---------------------------------------------------------------------------


def test_repl_adapter_creates_session_without_bundle(
    registered_agent_server: _RegisteredAgentServer,
    tmp_path: Path,
    mock_llm_server_url: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REPL adapter creates a session by agent_id when it has no bundle.

    ``omnigent chat <remote-url>`` reaches ``_SessionsChatReplAdapter`` with
    ``session_bundle=None`` and ``session_id=None``.  The adapter must resolve
    the registered agent and create the session rather than refusing for lack
    of a bundle.

    Passes ``runner_id=None`` because that is what ``run_chat`` supplies for a
    URL target: a remote client owns no runner, so the adapter has to adopt one
    the server already has online or the first turn fails to dispatch.
    """
    from omnigent_client import OmnigentClient

    from omnigent.chat import _bundle_agent, _pick_agent
    from omnigent.repl._repl import _SessionsChatReplAdapter

    server = registered_agent_server
    registered_id = server.agent_id()
    local_spec = _write_agent_yaml(
        tmp_path,
        agent_name=server.agent_name,
        model="mock-unused-upload",
        mock_llm_server_url=mock_llm_server_url,
    )
    with server.client() as http:
        uploaded = post_session_bundle(
            http.post,
            "/v1/sessions",
            _bundle_agent(local_spec),
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["agent_id"] != registered_id
    monkeypatch.setattr("click.prompt", lambda *_args, **_kwargs: server.agent_name)
    selected = _pick_agent(server.base_url)
    assert selected.id == registered_id

    async def _unexpected_lookup(_name: str) -> object:
        raise AssertionError("the selected catalog entry must not be resolved again")

    async def _drive() -> str:
        async with OmnigentClient(base_url=server.base_url) as client:
            monkeypatch.setattr(client.sessions, "resolve_agent", _unexpected_lookup)
            adapter = _SessionsChatReplAdapter(
                client=client,
                agent_name=server.agent_name,
                registered_agent=selected,
                session_bundle=None,
                session_id=None,
                runner_id=None,
            )
            async for _ in adapter.send("Say hello briefly."):
                pass
            session_id = adapter._session_id
            assert session_id is not None, "adapter did not record a session id"
            return session_id

    session_id = asyncio.run(_drive())
    text = _assistant_text(server.base_url, session_id)
    assert server.marker in text, f"Expected {server.marker!r} in transcript. Got: {text!r}"


def test_repl_adapter_unknown_agent_name_is_reported(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """An unregistered agent name fails with a message naming the agent.

    Guards the resolve step: a typo'd or missing agent must surface as a clear
    lookup failure rather than a confusing session-create error.
    """
    from omnigent_client import OmnigentClient

    from omnigent.repl._repl import _SessionsChatReplAdapter

    server = registered_agent_server

    async def _drive() -> None:
        async with OmnigentClient(base_url=server.base_url) as client:
            adapter = _SessionsChatReplAdapter(
                client=client,
                agent_name="no-such-agent",
                session_bundle=None,
                session_id=None,
                runner_id=server.runner_id,
            )
            async for _ in adapter.send("hello"):
                pass

    with pytest.raises(LookupError, match="no-such-agent"):
        asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Server JSON agent_id route
# ---------------------------------------------------------------------------


def test_server_creates_fresh_session_via_json_agent_id(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """``POST /v1/sessions`` with a JSON agent_id creates a working session.

    This is the route the fixed client paths depend on, so it is worth
    pinning independently of them.
    """
    server = registered_agent_server
    agent_id = server.agent_id()
    assert agent_id, f"expected non-empty agent_id for {server.agent_name!r}"

    with server.client() as client:
        resp = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        resp.raise_for_status()
        session_id = str(resp.json()["id"])
        assert session_id, "expected a session id from the JSON agent_id POST"

        resp = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": server.runner_id})
        resp.raise_for_status()

        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content="Say hello briefly.",
        )
        body = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )

    assert body["status"] == "completed", (
        f"Status: {body['status']!r}. Output: {body.get('output', [])}"
    )
    text = final_assistant_text(body)
    assert server.marker in text, f"Expected {server.marker!r} in output. Got: {text!r}"


# ---------------------------------------------------------------------------
# Headless -p without a bundle
# ---------------------------------------------------------------------------


def test_headless_prompt_without_bundle_uses_sessions_api(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """Headless ``-p`` against a remote target answers via the sessions API.

    The no-bundle path previously fell through to the legacy
    ``/v1/responses`` endpoint, which the server no longer exposes.  It must
    now resolve the registered agent and use the sessions API instead.
    """
    from omnigent_client import OmnigentClient

    from omnigent.chat import _query_sessions_once

    server = registered_agent_server

    async def _one_shot() -> str | None:
        async with OmnigentClient(base_url=server.base_url) as client:
            return await _query_sessions_once(
                client=client,
                agent_name=server.agent_name,
                tool_handler=None,
                prompt="Say hello briefly.",
                session_bundle=None,
                session_bundle_filename="agent.tar.gz",
                runner_id=None,
            )

    result = asyncio.run(_one_shot())
    assert result is not None, "headless one-shot returned no text"
    assert server.marker in result, f"Expected {server.marker!r} in output. Got: {result!r}"


def test_run_one_shot_without_bundle_answers(
    registered_agent_server: _RegisteredAgentServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run --server <url> -p`` answers without a local bundle.

    ``_chat_with_server`` routes ``-p`` through ``_run_one_shot``, a
    separate entry point from ``_run_headless_prompt``.  It also used to
    require a bundle and fall back to the legacy client query, so a
    remote-URL one-shot failed with ``Not Found``.
    """
    from omnigent.chat import _run_one_shot

    server = registered_agent_server

    _run_one_shot(
        base_url=server.base_url,
        agent_name=server.agent_name,
        tool_handler=None,
        prompt="Say hello briefly.",
        # What _chat_with_server passes for a URL target.
        runner_id=None,
        session_bundle=None,
    )

    out = capsys.readouterr().out
    assert server.marker in out, f"Expected {server.marker!r} on stdout. Got: {out!r}"


# ---------------------------------------------------------------------------
# Contracts the remote-URL path depends on
# ---------------------------------------------------------------------------


def test_json_create_returns_full_session_snapshot(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """The JSON create route returns a full snapshot, not just an id.

    ``create_from_agent_id`` parses the POST response directly with
    ``Session.from_dict`` and skips the follow-up ``GET`` that the multipart
    ``create`` needs.  If the server ever narrowed this route to
    ``{"session_id": ...}`` that parse would break, so pin the shape.
    """
    server = registered_agent_server

    with server.client() as client:
        resp = client.post(
            "/v1/sessions",
            json={"agent_id": server.agent_id()},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        resp.raise_for_status()
        body = resp.json()

    for field_name in ("id", "agent_id", "status", "created_at"):
        assert field_name in body, (
            f"JSON create response is missing {field_name!r}; "
            f"Session.from_dict would fail. Got keys: {sorted(body)}"
        )


def test_no_online_runner_reports_actionable_error(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """A server with no online runner fails with a message naming the fix.

    A remote client adopts one of the server's online runners.  When the
    server has none, the turn cannot dispatch, so the error should point at
    starting a host rather than at the ``--server`` flag the user already used.
    """
    from omnigent_client import OmnigentClient

    from omnigent.repl._repl import _SessionsChatReplAdapter

    server = registered_agent_server

    async def _drive() -> None:
        async with OmnigentClient(base_url=server.base_url) as client:
            adapter = _SessionsChatReplAdapter(
                client=client,
                agent_name=server.agent_name,
                session_bundle=None,
                session_id=None,
                runner_id=None,
            )
            # Simulate "server has no online runner" without tearing the real
            # one down: discovery finds nothing, so binding must fail loudly.
            adapter._client.sessions.resolve_online_runner = _no_runner  # type: ignore[method-assign]
            async for _ in adapter.send("hello"):
                pass

    with pytest.raises(RuntimeError, match="no online runner"):
        asyncio.run(_drive())


async def _no_runner(
    *, harness: str | None = None, canonicalize: object | None = None
) -> str | None:
    """Stand in for runner discovery on a server with no online runner."""
    return None


@pytest.mark.parametrize(
    ("agent_harness", "advertised"),
    [
        ("claude", "claude-sdk"),  # agent reports an alias
        ("claude-sdk", "claude"),  # runner advertises an alias
    ],
)
def test_runner_adoption_matches_harness_aliases(agent_harness: str, advertised: str) -> None:
    """Runner adoption accepts either spelling of a harness name.

    The server canonicalizes harness names before matching a runner
    (``_runner_supports_harness``), so the client must too: an agent whose spec
    says ``claude`` has to match a runner advertising ``claude-sdk``, or a
    compatible runner looks absent and the turn fails with "no online runner".
    """
    import json

    from omnigent_client._sessions import SessionsNamespace

    from omnigent.harness_aliases import canonicalize_harness

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, harnesses: list[str]) -> None:
            self.text = json.dumps(
                {"data": [{"runner_id": "r1", "online": True, "harnesses": harnesses}]}
            )

        def json(self) -> object:
            return json.loads(self.text)

    class _Http:
        def __init__(self, harnesses: list[str]) -> None:
            self._harnesses = harnesses

        async def get(self, *_args: object, **_kwargs: object) -> _Resp:
            return _Resp(self._harnesses)

    namespace = SessionsNamespace(_Http([advertised]), "http://example")  # type: ignore[arg-type]

    async def _resolve(canonicalize: object) -> str | None:
        return await namespace.resolve_online_runner(
            harness=agent_harness,
            canonicalize=canonicalize,  # type: ignore[arg-type]
        )

    assert asyncio.run(_resolve(lambda name: canonicalize_harness(name) or name)) == "r1"
    # Without canonicalization the alias would not match at all — the bug this guards.
    assert asyncio.run(_resolve(None)) is None


# ---------------------------------------------------------------------------
# Fresh server: registered agents, no sessions yet
# ---------------------------------------------------------------------------

_NO_SESSIONS_ERROR = "No sessions found on the server"
_STALE_HOST_HINT = "stale host processes may be the cause"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(home: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent <args>`` headlessly with CLI state isolated under *home*.

    Enters through ``main()`` like the ``omnigent`` console script, so the
    top-level error handling (hints, crash screen) is part of what is observed.
    """
    home.mkdir(parents=True, exist_ok=True)
    # Strip ambient OMNIGENT_* settings so an outside config cannot steer the run.
    env = isolated_process_env(home, {"TERM": "dumb"}, None)
    return subprocess.run(
        [sys.executable, "-c", "from omnigent.cli import main\n\nmain()\n", *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _session_count(base_url: str) -> int:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        resp = client.get("/v1/sessions", params={"visibility": "all", "limit": 100})
        resp.raise_for_status()
        return len(resp.json()["data"])


def test_run_one_shot_on_fresh_server_asks_for_agent_then_answers_with_it(
    registered_agent_server: _RegisteredAgentServer,
    tmp_path: Path,
) -> None:
    """Headless ambiguity fails without creating a session; an explicit agent replies.

    Used and never-used registrations remain discoverable afterward.
    """
    server = registered_agent_server
    with server.client() as client:
        agents = client.get("/v1/agents", params={"limit": 1000}).json()["data"]
    unused_name = next(a["name"] for a in agents if a["name"] != server.agent_name)
    assert _session_count(server.base_url) == 0

    proc = _run_cli(tmp_path / "cli-home", "run", "--server", server.base_url, "-p", "hi")
    output = proc.stdout + proc.stderr

    assert _NO_SESSIONS_ERROR not in output, output
    assert _STALE_HOST_HINT not in output, output
    assert "Aborted!" not in output, output
    assert "Traceback (most recent call last)" not in output, output
    assert proc.returncode != 0, output
    assert "--agent <name>" in output, output
    assert server.agent_name in output, output
    assert _session_count(server.base_url) == 0

    proc = _run_cli(
        tmp_path / "cli-home",
        "run",
        "--server",
        server.base_url,
        "--agent",
        server.agent_name,
        "-p",
        "Say hello briefly.",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert server.marker in proc.stdout, proc.stdout + proc.stderr
    with server.client() as client:
        runners = client.get("/v1/runners").json()["data"]
    assert any(r["runner_id"] == server.runner_id and r["online"] for r in runners)
    assert _session_count(server.base_url) == 1

    proc = _run_cli(tmp_path / "cli-home", "run", "--server", server.base_url, "-p", "hi")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "--agent <name>" in output and server.agent_name in output, output
    assert unused_name in proc.stderr, output
    assert _session_count(server.base_url) == 1


def test_run_one_shot_without_online_runner_fails_cleanly(tmp_path: Path) -> None:
    """A server with no online runner yields a CLI error, not a crash screen."""
    with isolated_local_server(
        tmp_path / "server",
        extra_args=["--agent", str(_REPO_ROOT / "examples" / "scribe")],
    ) as base_url:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            assert client.get("/v1/runners").json()["data"] == []

        proc = _run_cli(
            tmp_path / "cli-home", "run", "--server", base_url, "--agent", "scribe", "-p", "hi"
        )

    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "no online runner" in output, output
    assert "Traceback (most recent call last)" not in output, output
    assert "crash report" not in output, output


# ---------------------------------------------------------------------------
# Host launch: a connected host but no online runner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interactive", [False, True], ids=["one-shot", "interactive"])
def test_run_launches_a_runner_on_this_machines_host(
    mock_llm_server_url: str | None,
    tmp_path: Path,
    interactive: bool,
) -> None:
    """With only a connected host (no runner), the CLI has the server launch one there.

    The CLI and ``omnigent host`` share a home, so the CLI is "this machine's"
    host. Both entry points must answer through the launched runner. Only the
    one-shot stops it on exit; interactive runners keep the host-owned lifecycle.
    """
    suffix = uuid.uuid4().hex[:6]
    agent_name = f"host-launch-{suffix}"
    model = f"mock-{suffix}"
    marker = f"HOST_LAUNCH_OK_{suffix}"
    yaml_path = _write_agent_yaml(
        tmp_path, agent_name=agent_name, model=model, mock_llm_server_url=mock_llm_server_url
    )
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": marker}] * 4, key=model)
    with isolated_local_server(tmp_path / "server", extra_args=["--agent", str(yaml_path)]) as url:
        with isolated_host(
            tmp_path / "host",
            url,
            command=[
                sys.executable,
                "-c",
                "from omnigent.cli import main\n\nmain()\n",
                "host",
                "--server",
                url,
                "--no-open",
                "--non-interactive",
            ],
            env={"TERM": "dumb", "OPENAI_API_KEY": "mock-key"},
        ) as host:
            with httpx.Client(base_url=url, timeout=10.0) as client:
                host.wait_online(client, timeout=90.0)
                assert client.get("/v1/runners").json()["data"] == []
                registered_id = next(
                    agent["id"]
                    for agent in client.get("/v1/agents").json()["data"]
                    if agent["name"] == agent_name
                )

                if interactive:
                    pexpect = pytest.importorskip("pexpect")
                    pyte = pytest.importorskip("pyte")
                    config = host.home / ".omnigent" / "config.yaml"
                    config.write_text(
                        config.read_text()
                        + "\nauto_open_conversation: false\ntui:\n  theme: dark\n"
                    )
                    env = isolated_process_env(
                        host.home,
                        {"TERM": "xterm-256color", "OMNIGENT_NO_UPDATE_CHECK": "1"},
                        None,
                    )
                    with (tmp_path / "repl.log").open("w") as transcript:
                        child = pexpect.spawn(
                            sys.executable,
                            [
                                "-c",
                                "from omnigent.cli import main\n\nmain()\n",
                                "run",
                                "--server",
                                url,
                                "--agent",
                                agent_name,
                            ],
                            env=env,
                            encoding="utf-8",
                            dimensions=(40, 120),
                            timeout=120,
                        )
                        child.logfile_read = transcript
                        try:
                            child.expect(r"Ctrl.C exit")
                            child.send("hi\r")
                            child.expect_exact("Waiting for this machine to start your agent...")
                            child.expect(marker)
                            poll_until(
                                lambda: any(
                                    session.get("status") == "idle"
                                    for session in client.get(
                                        "/v1/sessions", params={"visibility": "all"}
                                    ).json()["data"]
                                ),
                                timeout_s=30.0,
                            )
                            child.sendcontrol("d")
                            child.expect(pexpect.EOF)
                            child.close()
                            assert child.exitstatus == 0
                        finally:
                            child.close(force=True)
                    screen = pyte.Screen(120, 40)
                    pyte.Stream(screen).feed((tmp_path / "repl.log").read_bytes().decode("utf-8"))
                    visible = "\n".join(screen.display)
                    assert marker in visible
                    assert "Waiting for this machine" not in visible
                else:
                    proc = _run_cli(
                        host.home,
                        "run",
                        "--server",
                        url,
                        "--agent",
                        agent_name,
                        "-p",
                        "hi",
                        timeout=180.0,
                    )
                    output = proc.stdout + proc.stderr
                    assert proc.returncode == 0, output + host.log_path.read_text()
                    assert marker in proc.stdout, output
                    assert "Waiting for this machine to start your agent..." in proc.stderr
                    assert "Waiting for this machine" not in proc.stdout

                sessions = client.get(
                    "/v1/sessions", params={"visibility": "all", "limit": 10}
                ).json()["data"]
                assert len(sessions) == 1, sessions
                assert sessions[0].get("host_id") == host.host_id, sessions
                assert sessions[0]["agent_id"] == registered_id, sessions
                assert sessions[0]["workspace"] == os.getcwd(), sessions
                if interactive:
                    assert marker in _assistant_text(url, sessions[0]["id"])
                    assert any(
                        runner.get("online") for runner in client.get("/v1/runners").json()["data"]
                    )
                    response = client.post(
                        f"/v1/sessions/{sessions[0]['id']}/events",
                        json={"type": "stop_session", "data": {}},
                        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
                    )
                    response.raise_for_status()
                # The one-shot or the test stops only its own launched runner.
                poll_until(
                    lambda: (
                        not any(r.get("online") for r in client.get("/v1/runners").json()["data"])
                    ),
                    timeout_s=60.0,
                )
