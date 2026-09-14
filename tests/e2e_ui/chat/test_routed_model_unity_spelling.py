"""E2E (web): Smart Routing must not apply a retired ``databricks-*`` endpoint.

Regression guard for Smart Routing applying a retired legacy Databricks
endpoint id instead of the Unity Catalog model-service spelling.

**The bug.** With model-level Smart Routing on, a coding task routes to the
GPT 5.6 Sol arm. The external ``task_v1`` router returns the bare vocabulary
pick ``gpt-5-6-sol``; Smart Routing resolves that to a *servable* catalog id
before running it. On this build the resolution keeps the legacy
``databricks-gpt-5-6-sol`` spelling (``apply_servable_alias`` /
``_SERVABLE_ALIASES`` in ``omnigent/server/smart_routing.py`` only rewrites
``glm-5-2`` -> ``system.ai.glm-5-2``, and the static fallback ladders in
``omnigent/models/model_fallbacks.py`` are all spelled ``databricks-*``). On a
Unity-enforced Databricks AI Gateway workspace ("Enforce Unity Gateway") the
retired ``databricks-*`` serving endpoints answer HTTP 501 and demand the
Unity Catalog spelling ``system.ai.gpt-5-6-sol`` (v3), so the routed turn dies
before it runs.

**The user journey this drives** (all user-observable):

1. A workspace whose gateway only serves Unity Catalog (``system.ai.*``) model
   services -- the retired ``databricks-*`` serving endpoints answer 501.
2. Enable Smart Routing for the session (``cost_control_mode_override="on"``,
   no model/effort pin -- a pin disables routing).
3. Submit a coding task that routes to GPT 5.6 Sol.
4. Observe: the Smart routing card shows the pick applied, and the turn fails
   with an HTTP 501 "no longer available. Use Unity Catalog model services
   (v3)." error pill instead of producing a reply.

**Environment fidelity (stand-in).** CI cannot be a real Databricks-network
workspace with Enforce Unity Gateway, so two stand-ins reproduce the reported
mechanism: the deterministic ``task_v1`` ``routes:select`` mock
(``tests/e2e/routing/_mock_router.py``) offers the real static arm menu and
picks ``gpt-5-6-sol`` exactly as staging does, and the OpenAI-compatible mock
LLM stands in for the AI Gateway -- it answers HTTP 501 with the verbatim
Unity retirement message for the retired ``databricks-*`` id and serves the
Unity ``system.ai.*`` spelling normally. That is what makes this a fail->pass
target: while the retired spelling is applied the turn 501s (test fails); once
the resolution canonicalizes the pick to a Unity ``system.ai.*`` (or any
non-retired) spelling the gateway serves it and the turn completes (test
passes).

The test asserts the CORRECT (post-fix) behaviour so it reds on this bug:
the applied routed model must not be a retired ``databricks-*`` endpoint id,
and the routed coding turn must complete rather than surface the 501 pill.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, server_executable
from tests.e2e.routing._mock_router import MockRouter, serve_mock_router
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _REPO_ROOT,
    configure_mock_llm,
    set_fallback_mock_llm,
)

# ── SPA selectors (see chat/test_multi_turn_chat.py, test_failure_error_card.py) ──
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_ROUTING_CARD = "routing-decision-card"
_ERROR_PILL = "error-pill"

# ── The routed model spellings the mock gateway is taught about ──────────────
# The router's vocabulary pick for a default codex task is the bare arm
# ``gpt-5-6-sol`` (see _mock_router.decide). Smart Routing resolves it to a
# servable catalog id before running it. The retired legacy spelling is what
# a Unity-enforced gateway rejects; the Unity Catalog spelling is what it
# serves.
_RETIRED_MODEL = "databricks-gpt-5-6-sol"
_UNITY_MODEL = "system.ai.gpt-5-6-sol"
_SOL_ARM = "gpt-5-6-sol"

# The verbatim message a Unity-enforced Databricks AI Gateway returns for a
# retired ``databricks-*`` serving endpoint (HTTP 501).
_UNITY_501_MESSAGE = (
    f"'{_RETIRED_MODEL}' is no longer available. Use Unity Catalog model services (v3)."
)
# Signature fragments that mark the Unity-retirement failure in a persisted
# error item, regardless of how the harness wraps the upstream error.
_UNITY_ERROR_SIGNATURES = ("no longer available", "501")

# A coding task that the deterministic router scores as a plain (non-trivial,
# non-delegate, non-crosscutting) codex request -> default arm ``gpt-5-6-sol``.
# Held verbatim: length (>300, non-trivial) and the absence of code/error
# markers and crosscutting markers ARE the routing signal (see _mock_router).
_ROUTED_PROMPT = (
    "Please add a new preferences panel to the settings page of our dashboard "
    "so users can pick their preferred landing view, remember the last "
    "workspace they opened, and choose between compact and comfortable row "
    "density. Persist the choices per user account, apply them when they sign "
    "in, and keep the defaults matching current behavior so existing users "
    "notice no change until they opt in."
)

# ``omnigent server --agent`` runs the strict validator, so the spec needs an
# explicit executor block. The pinned model (gpt-4o-mini, a plain non-
# databricks name) resolves to OPENAI_BASE_URL (the mock) for a normal turn;
# with Smart Routing on, the router's pick overrides it per turn.
_ROUTED_AGENT_YAML = """\
name: routed_coder
prompt: You are a coding assistant. Implement what the user asks.

executor:
  model: gpt-4o-mini
  harness: openai-agents
"""

_HEALTH_TIMEOUT_S = 90.0
_HEALTH_POLL_S = 0.5


def _ambient_safe_env() -> dict[str, str]:
    """Return ``os.environ`` minus ambient Omnigent/Databricks state.

    A host that itself runs inside an omnigent runner (or carries Databricks
    credentials) leaks into the spawned server/runner otherwise: leaked runner
    vars make the child take the zygote path and hang, and real gateway
    credentials let a Databricks-prefixed routed model bypass the mock gateway,
    so the journey never observes the behavior under test.

    :returns: A copy of the environment safe to base subprocess envs on.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT", "DATABRICKS"))
    }


def _free_port() -> int:
    """Return a free TCP port on loopback.

    :returns: A port nothing is currently listening on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class RoutedServer:
    """Handle on the routing-configured server the test drives."""

    base_url: str
    runner_id: str
    mock_llm_url: str
    router: MockRouter


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Film the journey when ``OMNIGENT_E2E_RECORD_DIR`` is set.

    The conftest's ``_record_video`` autouse fixture only patches the *async*
    Playwright API; this test drives the sync ``page`` fixture, so inject
    ``record_video_dir`` here (the doc's "hard-code its own record_video_dir"
    path). Playwright writes the ``.webm`` when the context closes.

    :param browser_context_args: The base context args from pytest-playwright.
    :returns: The context args, with ``record_video_dir`` added when recording.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        Path(record_dir).mkdir(parents=True, exist_ok=True)
        return {**browser_context_args, "record_video_dir": record_dir}
    return {**browser_context_args}


@pytest.fixture(scope="module")
def _mock_router() -> Iterator[MockRouter]:
    """Run the deterministic ``routes:select`` mock for the module.

    :yields: The router handle; its ``base_url`` becomes the server's
        ``routing.base_url``.
    """
    yield from serve_mock_router()


@pytest.fixture(scope="module")
def routed_server(
    built_spa: None,
    mock_llm_server_url: str,
    _mock_router: MockRouter,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[RoutedServer]:
    """Spawn an ``omnigent server`` + runner whose router is the mock.

    A ``live_server`` variant carrying a ``routing:`` block (the default
    ``live_server`` has none), so real turn-time Smart Routing runs against the
    deterministic ``task_v1`` mock. The openai-agents harness points at the
    session-scoped mock LLM, which stands in for the Databricks AI Gateway.

    :param built_spa: Ensures the SPA bundle is on disk before the server
        mounts it.
    :param mock_llm_server_url: Session-scoped mock LLM base URL.
    :param _mock_router: The routing mock whose base URL is configured.
    :param tmp_path_factory: Pytest temp path factory.
    :yields: A :class:`RoutedServer` handle.
    :raises RuntimeError: If the server/runner do not become ready in time.
    """
    root = tmp_path_factory.mktemp("routed_unity_server")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = root / "test.db"
    artifact_dir = root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    agent_yaml = root / "routed_coder.yaml"
    agent_yaml.write_text(_ROUTED_AGENT_YAML)

    config_yaml = root / "server.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "routing": {
                    "provider": "external",
                    "base_url": _mock_router.base_url,
                    "router_name": "task_v1",
                    # The catalog prefixes this deployment's ids carry; the
                    # router expects bare arms, so the client strips these.
                    "model_prefix": ["databricks-", "system.ai."],
                }
            },
            sort_keys=False,
        )
    )

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    server_env: dict[str, str] = {
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        # Serve the HEAD SPA bundle built by ``built_spa``.
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    server_env = {**_ambient_safe_env(), **server_env}
    apply_server_env(server_env, _REPO_ROOT)

    server_log = root / "server.log"
    server_handle = open(server_log, "w")  # noqa: SIM115 — lives for the subprocess
    server_proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--agent",
            str(agent_yaml),
            "--config",
            str(config_yaml),
        ],
        env=server_env,
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )

    runner_log = root / "runner.log"
    runner_handle = open(runner_log, "w")  # noqa: SIM115 — lives for the subprocess
    runner_env = {
        **_ambient_safe_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env,
        stdout=runner_handle,
        stderr=subprocess.STDOUT,
    )

    def _tail() -> str:
        return server_log.read_text(errors="replace")[-3000:] if server_log.exists() else ""

    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    ready = False
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if server_proc.poll() is not None:
            last_error = f"server exited early with code {server_proc.returncode}"
            break
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online") is True:
                    ready = True
                    break
                last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_S)

    def _terminate(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    if not ready:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_handle.close()
        runner_handle.close()
        raise RuntimeError(
            f"routing server/runner not ready within {_HEALTH_TIMEOUT_S:.0f}s "
            f"(last_error={last_error}).\nserver log tail:\n{_tail()}"
        )

    # Teach the mock gateway the two spellings under test. Keyed by the request
    # ``model`` field: the retired id 501s exactly like a Unity-enforced
    # gateway; the Unity spelling (and any other non-retired spelling, via the
    # default fallback) serves normally so a fixed resolution completes.
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": _UNITY_501_MESSAGE, "status_code": 501}] * 80,
        key=_RETIRED_MODEL,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Preferences panel plan drafted."}] * 10,
        key=_UNITY_MODEL,
    )
    set_fallback_mock_llm(mock_llm_server_url, _UNITY_MODEL, "Preferences panel plan drafted.")
    set_fallback_mock_llm(mock_llm_server_url, "default", "Routed coding task acknowledged.")
    set_fallback_mock_llm(mock_llm_server_url, "gpt-4o-mini", "Routed coding task acknowledged.")
    set_fallback_mock_llm(mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}')

    try:
        yield RoutedServer(
            base_url=base_url,
            runner_id=runner_id,
            mock_llm_url=mock_llm_server_url,
            router=_mock_router,
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_handle.close()
        runner_handle.close()


def _create_routed_session(server: RoutedServer) -> str:
    """Create a Smart-Routing session bound to the runner and return its id.

    Mirrors the web create path: ``cost_control_mode_override="on"`` with no
    model/effort pin turns on model-level Smart Routing per turn.

    :param server: The routing server handle.
    :returns: The new session id.
    """
    agents = httpx.get(f"{server.base_url}/v1/agents", timeout=10.0)
    agents.raise_for_status()
    agent_id = next(a["id"] for a in agents.json()["data"] if a["name"] == "routed_coder")

    created = httpx.post(
        f"{server.base_url}/v1/sessions",
        json={"agent_id": agent_id, "cost_control_mode_override": "on"},
        timeout=30.0,
    )
    assert created.status_code < 400, f"create failed {created.status_code}: {created.text[:500]}"
    session_id = created.json()["id"]

    bound = httpx.patch(
        f"{server.base_url}/v1/sessions/{session_id}",
        json={"runner_id": server.runner_id},
        timeout=10.0,
    )
    bound.raise_for_status()
    return session_id


def _applied_decisions(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Return this session's applied ``routing_decision`` payloads.

    :param base_url: Server base URL.
    :param session_id: Session id.
    :returns: The ``data`` payloads of applied routing decisions, oldest first.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for item in resp.json().get("data", []):
        # /items flattens the RoutingDecisionData fields (model, applied,
        # rationale, router_source) onto the item itself.
        if item.get("type") != "routing_decision":
            continue
        if item.get("applied"):
            out.append(item)
    return out


def _unity_error_items(base_url: str, session_id: str) -> list[str]:
    """Return persisted error messages carrying the Unity-retirement signature.

    :param base_url: Server base URL.
    :param session_id: Session id.
    :returns: The matching error messages.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    resp.raise_for_status()
    hits: list[str] = []
    for item in resp.json().get("data", []):
        # The gateway failure persists as an error item whose message is
        # flattened onto the item.
        message = str(item.get("message") or "")
        if any(sig in message for sig in _UNITY_ERROR_SIGNATURES):
            hits.append(message)
    return hits


def _wait_turn_settled(base_url: str, session_id: str, *, timeout: float = 90.0) -> None:
    """Block until the routed turn reaches a terminal state.

    Settled means the transcript carries either an assistant message (the turn
    produced a reply) or an error item (the turn failed) -- both are terminal,
    so this never hangs on the buggy 501 path nor on the fixed reply path.

    :param base_url: Server base URL.
    :param session_id: Session id.
    :param timeout: Max seconds to wait.
    :raises AssertionError: When no terminal item appears in time.
    """
    # The /items endpoint flattens each item's data fields to the top level
    # (see ConversationItem's flatten-for-API shape), so role/model/message/
    # applied are read directly off the item, not a nested "data" object.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            if item.get("type") == "error":
                return
            if item.get("type") == "message" and item.get("role") == "assistant":
                return
        time.sleep(1.0)
    raise AssertionError(f"routed turn on {session_id} did not settle within {timeout:.0f}s")


def test_routed_model_uses_unity_spelling_not_retired_databricks_endpoint(
    page: Page,
    routed_server: RoutedServer,
) -> None:
    """A routed coding task must run on a servable id, not a retired endpoint.

    Drives the real web journey: enable Smart Routing, submit a coding task,
    watch it route to the GPT 5.6 Sol arm, and require that the applied model
    is a Unity-servable spelling and that the turn completes instead of 501-ing.

    :param page: Playwright page fixture.
    :param routed_server: The routing-configured server + runner + mock router.
    :returns: None.
    """
    base_url = routed_server.base_url
    session_id = _create_routed_session(routed_server)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_ROUTED_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    # Smart routing fires and the pick is shown on the web surface (holds on
    # both the buggy and fixed builds — it is the precondition, not the bug).
    routing_card = page.get_by_test_id(_ROUTING_CARD).first
    expect(routing_card).to_be_visible(timeout=90_000)

    # Wait for the routed turn to settle (a reply on the fixed build, a failure
    # on the buggy one) via the transcript API, which is immune to how the SPA
    # lays out an empty assistant bubble alongside a failure pill.
    _wait_turn_settled(base_url, session_id, timeout=90.0)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)

    # ── Authoritative assertions on the persisted decision + turn outcome ──
    applied = _applied_decisions(base_url, session_id)
    assert applied, "Smart Routing recorded no applied routing decision for the turn"

    sol_decisions = [d for d in applied if _SOL_ARM in str(d.get("model", ""))]
    assert sol_decisions, (
        "expected Smart Routing to route the coding task to the "
        f"{_SOL_ARM!r} arm; applied decisions were "
        f"{[d.get('model') for d in applied]}"
    )
    for decision in sol_decisions:
        model = str(decision.get("model", ""))
        assert not model.startswith("databricks-"), (
            f"Smart Routing applied the retired legacy endpoint id {model!r}. "
            "On a Unity-enforced Databricks AI Gateway the retired "
            "'databricks-*' serving endpoints answer HTTP 501; the resolved "
            "pick must be a Unity Catalog ('system.ai.*') servable spelling."
        )

    unity_errors = _unity_error_items(base_url, session_id)
    assert not unity_errors, (
        "the routed coding turn failed with a Unity-retirement gateway error "
        f"instead of running: {unity_errors!r}"
    )

    # ── Web-surface flip: no gateway failure pill, and a reply is shown ──
    expect(page.get_by_test_id(_ERROR_PILL)).to_have_count(0)
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=10_000)
