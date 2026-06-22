"""Fixtures for end-to-end tests with real LLM and real server.

Usage::

    pytest tests/e2e/ --llm-api-key $LLM_API_KEY -v

Parallel runs::

    pytest tests/e2e/ --llm-api-key $LLM_API_KEY -n 8 --dist=loadscope

Empirically ``-n 8`` is the sweet spot on a 12-core laptop —
fastest wall time, same flake count as ``-n auto`` (12). ``-n 4``
is more stable (matches main's failure set exactly with no
ordering flakes) but slower; bump up if your host has more
cores.

Pass ``--profile <name>`` to route through a Databricks workspace
instead of api.openai.com: ``OPENAI_BASE_URL`` is set to
``<host>/serving-endpoints`` for the spawned server, agent bundle
``llm.model`` values are rewritten via :data:`_DATABRICKS_MODEL_MAP`,
and ``--llm-api-key`` is treated as a Databricks bearer.

These tests are excluded from the default ``pytest`` run via
``--ignore=tests/e2e`` in ``pyproject.toml``.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._model_pools import current_attempt, resolve_model
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S, lookup_databricks_host


@pytest.fixture(autouse=True)
def _skip_when_harness_cli_missing(request: pytest.FixtureRequest) -> None:
    """Skip parametrized rows whose harness CLI isn't on PATH.

    Applies to every test that takes a ``harness`` parametrize
    argument (the ``HARNESS_HARNESS_MODELS`` matrix). Lets CI run
    a subset of harnesses without each test file repeating the
    skip helper. Matches the existing local-dev pattern where a
    machine has only some harnesses installed.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return
    harness = callspec.params.get("harness")
    if harness:
        skip_if_harness_cli_missing(harness)


# Agent bundle directories relative to repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CODER_DIR = _REPO_ROOT / "tests" / "resources" / "examples" / "coder"
_ARCHER_DIR = _REPO_ROOT / "tests" / "resources" / "examples" / "archer"

# OpenAI model name -> nearest-equivalent Databricks foundation-model
# name. Intentionally lossy (e.g. ``gpt-4o`` and ``openai/gpt-4o`` both
# map to ``databricks-gpt-5-4``) — the e2e harness only needs the
# routing to resolve, not exact-model parity.
_DATABRICKS_MODEL_MAP: dict[str, str] = {
    "gpt-5.4": "databricks-gpt-5-4",
    "gpt-5.4-mini": "databricks-gpt-5-4-mini",
    "gpt-4o": "databricks-gpt-5-4",
    "gpt-4o-mini": "databricks-gpt-5-4-mini",
    # openai-coder's reviewer sub-agent ships with gpt-4.1-mini;
    # without this entry the bundle uploads unrewritten and the
    # Databricks serving endpoint 404s on the model name.
    "gpt-4.1-mini": "databricks-gpt-5-4-mini",
    "claude-sonnet-4-20250514": "databricks-claude-sonnet-4-6",
    "openai/gpt-4o": "databricks-gpt-5-4",
}

# Test-only fixtures live under tests/resources/agents/ to keep
# examples/ a curated, user-facing set. These agents exist
# solely to exercise specific code paths in e2e tests (sub-agent
# spawning, terminal hierarchies, etc.) and have no docs pointing
# at them from elsewhere in the repo.
_CLAUDE_CODER_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "claude-coder"
_OPENAI_CODER_DIR = _REPO_ROOT / "tests" / "resources" / "examples" / "openai-coder"
_SANDBOX_DEPS_OS_ENV_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "sandbox-deps-os-env"
_SYS_TERMINAL_TEST_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "sys-terminal-test"
# A plain claude-sdk chat agent seeded as a BUILT-IN (via the server's
# OMNIGENT_BUILTIN_AGENT_DIRS hook) so fork-switch e2e tests have a
# deterministic SDK target to switch INTO — built-in because the fork route
# only binds built-in agents, and plain (not the polly supervisor) so a
# recall assertion isn't flaky.
_SDK_CHAT_BUILTIN_SPEC = _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"


def find_free_port() -> int:
    """
    Find a free TCP port by binding to port 0.

    :returns: An available port number.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(base_url: str, timeout: float = 20.0) -> None:
    """
    Poll until the server responds on its health endpoint.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8000"``.
    :param timeout: Max seconds to wait.
    :raises RuntimeError: If the server doesn't respond.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise RuntimeError(f"Server did not respond within {timeout}s")


@pytest.fixture(scope="session")
def databricks_workspace_host(
    request: pytest.FixtureRequest,
) -> str | None:
    """
    Resolve the Databricks workspace host from ``--profile``, or
    ``None`` when ``--profile`` is empty (api.openai.com path).

    :param request: pytest request — reads ``--profile``.
    :returns: Host URL with trailing ``/`` stripped, or ``None``.
    :raises pytest.UsageError: When ``--profile`` names a missing
        section or one without a ``host`` key.
    """
    # The flag is registered at tests/conftest.py with default="",
    # so getoption("--profile") always returns a string.
    profile: str = request.config.getoption("--profile")
    if not profile:
        return None
    host = lookup_databricks_host(profile)
    if host is None:
        raise pytest.UsageError(
            f"Databricks profile {profile!r} is missing from "
            f"~/.databrickscfg or has no ``host`` entry. "
            f"Either pass ``--profile`` for a profile that exists "
            f"or add the section to your databrickscfg."
        )
    return host


@pytest.fixture(scope="session")
def llm_api_key(request: pytest.FixtureRequest) -> str:
    """
    The LLM API key from ``--llm-api-key``.

    The option itself is declared at the top-level ``tests/conftest.py``
    so both ``tests/e2e/`` and ``tests/frontends/`` share one declaration.
    When ``--llm-api-key`` is omitted, falls back to ``"mock-key"`` so
    tests run against the mock LLM server without real credentials.

    :param request: Pytest request object.
    :returns: The API key string, or ``"mock-key"`` when unset.
    """
    key: str | None = request.config.getoption("--llm-api-key")
    if key is None:
        return "mock-key"
    return key


@pytest.fixture(scope="session")
def using_mock_llm(request: pytest.FixtureRequest) -> bool:
    """True when no real ``--llm-api-key`` was provided.

    Tests can use this to skip assertions that only make sense with
    a real LLM, or to pre-configure the mock server's response queue.

    :param request: Pytest fixture request.
    :returns: Whether the mock LLM server is in use.
    """
    return request.config.getoption("--llm-api-key") is None


@pytest.fixture(scope="session")
def mock_llm_server_url(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """
    Start a mock LLM server for the test session.

    Always started regardless of ``--llm-api-key`` so mock-only
    e2e tests run alongside real-LLM tests in the same session.
    The mock server is a lightweight FastAPI/uvicorn subprocess.

    :param tmp_path_factory: Pytest temp path factory for logs.
    :returns: The mock server base URL.
    """
    mock_port = find_free_port()
    mock_log = tmp_path_factory.mktemp("mock_llm_logs") / "mock_llm.log"
    log_handle = open(mock_log, "w")  # noqa: SIM115

    proc = subprocess.Popen(
        [
            sys.executable,
            str(_REPO_ROOT / "tests" / "unit" / "server" / "integration" / "mock_llm_server.py"),
            str(mock_port),
        ],
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{mock_port}"

    # Wait for the mock server to be ready
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/stats", timeout=1.0)
            if resp.status_code == 200:
                break
        except httpx.ConnectError:
            # Expected while the mock server is still booting.
            continue
        time.sleep(0.1)
    else:
        proc.kill()
        log_handle.close()
        log_contents = mock_log.read_text() if mock_log.exists() else ""
        raise RuntimeError(
            f"Mock LLM server didn't start within 10s.\nLog at {mock_log}:\n{log_contents[-2000:]}"
        )

    try:
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


def configure_mock_llm(
    mock_llm_server_url: str | None,
    responses: list[dict[str, Any]],
    *,
    key: str = "default",
    match: str | None = None,
) -> None:
    """
    Configure a keyed response queue on the mock LLM server.

    No-op when running against a real LLM. Each dict in *responses*
    maps to a ``QueuedResponse`` on the mock server. The *key*
    determines which queue the responses are stored in — the mock
    server routes each ``POST /v1/responses`` request to the queue
    whose key matches the request's ``model`` field.

    Use different keys to give each agent its own response stream::

        # Parent agent uses "mock-parent" model
        configure_mock_llm(url, [{"text": "spawning reviewer..."}],
                           key="mock-parent")
        # Sub-agent uses "mock-reviewer" model
        configure_mock_llm(url, [{"text": "LGTM"}],
                           key="mock-reviewer")

    Pass *match* to route by request CONTENT instead of model: the queue
    serves any request whose ``role="user"`` input contains the token.
    This lets a test claim its own queue by the unique message it sends,
    so a stray/late request from another test can't draw from it — the
    fix for the #523 cross-test contamination flake without per-test
    mock servers::

        configure_mock_llm(url, [...], match="mangosteen-tr")
        # and the test does child.send("mangosteen-tr ...")

    :param mock_llm_server_url: Mock server URL or ``None``.
    :param responses: List of response configs. Keys:
        ``text``, ``tool_calls``, ``block``, ``stream``,
        ``error``, ``status_code``.
    :param key: Queue key — typically the model name baked into the
        agent spec. Defaults to ``"default"`` (matches any model
        not assigned to a more specific queue).
    :param match: Optional content-routing token. When set, the queue is
        selected if this token appears in the request's user input,
        regardless of ``model``. Use a deliberately-unique token (the
        message the test sends). Also used as the queue *key* when *key*
        is left at its default, so each match-routed queue is distinct.
    """
    if mock_llm_server_url is None:
        return
    payload: dict[str, Any] = {
        "key": match if (match is not None and key == "default") else key,
        "responses": responses,
    }
    if match is not None:
        payload["match"] = match
    resp = httpx.post(
        f"{mock_llm_server_url}/mock/configure",
        json=payload,
        timeout=5.0,
    )
    resp.raise_for_status()


def reset_mock_llm(mock_llm_server_url: str | None) -> None:
    """
    Clear all regular keyed queues, captured requests, and gates.

    Fallbacks set via :func:`set_fallback_mock_llm` are preserved.

    :param mock_llm_server_url: Mock server URL or ``None``.
    """
    if mock_llm_server_url is None:
        return
    resp = httpx.post(f"{mock_llm_server_url}/mock/reset", timeout=5.0)
    resp.raise_for_status()


def set_fallback_mock_llm(
    mock_llm_server_url: str | None,
    key: str,
    text: str,
) -> None:
    """
    Set a non-resettable fallback response for a queue key.

    The fallback is returned when the regular queue for *key* is
    exhausted.  Unlike regular entries, the fallback survives
    :func:`reset_mock_llm` — use it for session-level queues that
    must return a valid response even when per-test resets clear the
    regular queue (e.g. the server-level policy-classifier LLM queue).

    :param mock_llm_server_url: Mock server URL or ``None``.
    :param key: Queue key (typically the server's ``llm.model``).
    :param text: Fallback response text.
    """
    if mock_llm_server_url is None:
        return
    resp = httpx.post(
        f"{mock_llm_server_url}/mock/set_fallback",
        json={"key": key, "text": text},
        timeout=5.0,
    )
    resp.raise_for_status()


def release_mock_gate(mock_llm_server_url: str | None) -> None:
    """
    Release the oldest pending gate on the mock LLM server.

    :param mock_llm_server_url: Mock server URL or ``None``.
    """
    if mock_llm_server_url is None:
        return
    resp = httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)
    resp.raise_for_status()


def get_mock_requests(
    mock_llm_server_url: str | None,
    *,
    key: str | None = None,
) -> list[dict]:
    """
    Retrieve captured request bodies from the mock LLM server.

    :param mock_llm_server_url: Mock server URL or ``None``.
    :param key: When set, only return requests whose ``model``
        field matches this key.
    :returns: List of request body dicts, or empty list in real mode.
    """
    if mock_llm_server_url is None:
        return []
    params = {"key": key} if key is not None else {}
    resp = httpx.get(
        f"{mock_llm_server_url}/mock/requests",
        params=params,
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()["requests"]


@pytest.fixture(scope="session")
def openai_judge_api_key(
    llm_api_key: str,
    databricks_workspace_host: str | None,
) -> str:
    """
    OpenAI key for tests that hit ``api.openai.com`` directly.

    Some tests use ``mlflow.genai.judges.make_judge`` (or build an
    OpenAI client without ``OPENAI_BASE_URL`` overrides) to grade
    the agent's output. Those calls go straight to
    ``api.openai.com``, which under ``--profile`` rejects the
    Databricks bearer with HTTP 401 ``invalid_issuer``. We
    deliberately do NOT translate these tests' specs to Databricks
    models — Databricks is preferred for testing because it's free,
    so we'd rather skip the judge tests under ``--profile`` than
    re-route them and pay for OpenAI tokens on every run.

    Resolution order:
    1. No ``--profile`` → ``--llm-api-key`` is the OpenAI key, use it.
    2. ``--profile`` set + ``OPENAI_API_KEY`` in env (real ``sk-...``)
       → use the explicit key. Caller invoked pytest without
       ``env -u OPENAI_API_KEY``, signaling intent to pay for the
       judge calls.
    3. ``--profile`` set + no ``OPENAI_API_KEY`` → skip the test.

    :param llm_api_key: The ``--llm-api-key`` value (Databricks
        token under ``--profile``, OpenAI key otherwise).
    :param databricks_workspace_host: Workspace host URL, or
        ``None`` when ``--profile`` is empty.
    :returns: An OpenAI key safe to use against ``api.openai.com``.
    """
    if databricks_workspace_host is None:
        return llm_api_key
    explicit = os.environ.get("OPENAI_API_KEY")
    if explicit and explicit.startswith("sk-"):
        return explicit
    pytest.skip(
        "test uses an LLM judge that hits api.openai.com directly. "
        "Under --profile, --llm-api-key is a Databricks token which "
        "OpenAI rejects (401 invalid_issuer). Pass OPENAI_API_KEY in "
        "env (don't strip it via env -u) to opt in and pay for the "
        "judge calls."
    )


_live_runner_state: dict[str, str] = {}


@pytest.fixture(scope="session")
def live_runner_id() -> str:
    """
    Return the runner id used by the live E2E server fixture.

    The runner id is derived from a binding token shared with the
    server so the server's tunnel allowlist accepts exactly this
    runner's WebSocket upgrade.

    :returns: Runner id, e.g. ``"runner_token_abc123..."``.
    """
    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    if "runner_id" not in _live_runner_state:
        token = _secrets.token_urlsafe(32)
        _live_runner_state["binding_token"] = token
        _live_runner_state["runner_id"] = token_bound_runner_id(token)
    return _live_runner_state["runner_id"]


@pytest.fixture(scope="session")
def live_server(
    request: pytest.FixtureRequest,
    llm_api_key: str,
    using_mock_llm: bool,
    databricks_workspace_host: str | None,
    tmp_path_factory: pytest.TempPathFactory,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> Iterator[str]:
    """
    Start a real ``omnigent server`` subprocess and yield its base URL.

    The server runs on a random high port. The fixture waits
    for the health endpoint before yielding, and kills the
    process on teardown. When ``--profile`` is set, the spawned
    server's ``OPENAI_BASE_URL`` is pointed at the workspace's
    serving-endpoints; bundles' ``llm.model`` get rewritten by
    :func:`upload_agent` (see :data:`_DATABRICKS_MODEL_MAP`).
    When running in mock mode (no ``--llm-api-key``), the server's
    ``OPENAI_BASE_URL`` is pointed at the mock LLM server.

    :param llm_api_key: The API key for the LLM (a Databricks
        bearer under ``--profile``, otherwise an OpenAI key, or
        ``"mock-key"`` in mock mode).
    :param databricks_workspace_host: Workspace host URL or ``None``.
    :param tmp_path_factory: Pytest temp path factory for the DB.
    :param live_runner_id: Runner id the server subprocess should
        advertise and tests should bind sessions to.
    :param mock_llm_server_url: Mock LLM server URL, or ``None``
        when using a real LLM.
    :returns: The server's base URL, e.g. ``"http://localhost:18501"``.
    """
    # Dynamic free port so back-to-back test sessions don't race
    # on a hard-coded port (which produced
    # "address already in use" → server death → ConnectError in
    # every subsequent test when a prior run hadn't fully torn
    # down).
    port = find_free_port()
    db_path = tmp_path_factory.mktemp("e2e") / "e2e.db"
    artifact_dir = tmp_path_factory.mktemp("e2e_artifacts")
    server_log = tmp_path_factory.mktemp("e2e_logs") / "server.log"
    # PYTHONPATH forces the server to import from the worktree
    # checkout rather than whatever version is installed in the
    # venv — otherwise a branch with migration or model changes
    # would run against the stale installed copy and fail with
    # "no such column" or similar schema mismatches. _REPO_ROOT is
    # the worktree root (tests/e2e/conftest.py → parents[2]).
    # Seed the plain claude-sdk chat agent as a built-in so fork/switch
    # e2e tests can rebind a session INTO it (the route only binds
    # built-ins). Materialize a profile-aware copy: under ``--profile`` the
    # built-in is gateway-wired (model mapped + profile stamped) like the
    # source agent, so the post-switch turn authenticates through the
    # Databricks gateway. CI has no Claude OAuth, so seeding the on-disk
    # OAuth spec verbatim would 401 the switched-to agent.
    builtin_sdk_chat_spec = _materialize_builtin_sdk_chat_spec(
        tmp_path_factory.mktemp("e2e_builtin_agents"),
        databricks_workspace_host=databricks_workspace_host,
        profile=request.config.getoption("--profile") or None,
        mock_llm_server_url=mock_llm_server_url if using_mock_llm else None,
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": llm_api_key,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(builtin_sdk_chat_spec),
    }
    if using_mock_llm and mock_llm_server_url is not None:
        # Mock mode: point all LLM calls at the mock server.
        # The OpenAI SDK appends /responses to the base URL, so
        # include /v1 in the base so the SDK hits /v1/responses.
        env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    elif databricks_workspace_host is not None:
        env["OPENAI_BASE_URL"] = f"{databricks_workspace_host}/serving-endpoints"
        # Thread --profile so claude-sdk and other harnesses that read
        # ~/.databrickscfg directly pick the right profile. Without it
        # ClaudeSDKExecutor falls through and 403s.
        env["DATABRICKS_CONFIG_PROFILE"] = request.config.getoption("--profile")
    # The CLI exposes ``--database-uri`` but not an env var, so the
    # DB path must be on the command line. Absolute path prevents
    # the server from writing into the CWD (which was previously
    # happening silently — each e2e run polluted ``omnigent.db``
    # in whatever dir pytest was invoked from).
    # Route server output to a file so DBOS/agent logs don't fill
    # a PIPE buffer (which would block the server after ~64KB —
    # previously every session failed mid-way through the second
    # test with "ConnectError: Connection refused" as the server
    # deadlocked on a full stdout pipe). Keeping the log as a file
    # also lets tests inspect it on failure.
    # The server is a pure state server — it does not spawn a runner.
    # We spawn the runner as a sibling subprocess with a shared tunnel
    # token so the server's allowlist accepts exactly this runner.
    # The binding token and runner_id are generated by live_runner_id
    # and shared via _live_runner_state so tests that inject
    # live_runner_id get the same id the runner advertises.
    binding_token = _live_runner_state["binding_token"]
    runner_id = live_runner_id

    # ── Server-level ``llm:`` config (policy classifier) ─────
    # Prompt-policy classifiers run server-side through
    # ``RuntimeCaps.llm``. Without a server ``llm:`` block the
    # classifier's OpenAI client defaults to api.openai.com and
    # 401s under ``--profile``. Point it at the same gateway the
    # agent executors use so prompt-policy e2e tests can classify.
    server_args = [
        sys.executable,
        "-m",
        "omnigent.cli",
        "server",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
    ]
    if using_mock_llm and mock_llm_server_url is not None:
        server_cfg = tmp_path_factory.mktemp("e2e_server_cfg") / "server.yaml"
        server_cfg.write_text(
            yaml.safe_dump(
                {
                    "llm": {
                        "model": "_policy_llm_",
                        "connection": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                        },
                    }
                }
            )
        )
        server_args.extend(["--config", str(server_cfg)])
    elif databricks_workspace_host is not None:
        server_cfg = tmp_path_factory.mktemp("e2e_server_cfg") / "server.yaml"
        server_cfg.write_text(
            yaml.safe_dump(
                {
                    "llm": {
                        # Stable key: session-scoped server must not
                        # depend on which test boots it first.
                        "model": resolve_model("databricks-gpt-5-4-mini", key="live-server"),
                        "connection": {
                            "base_url": f"{databricks_workspace_host}/serving-endpoints",
                            "api_key": llm_api_key,
                        },
                    }
                }
            )
        )
        server_args.extend(["--config", str(server_cfg)])

    # ── Spawn server subprocess ──────────────────────────
    log_handle = open(server_log, "w")  # noqa: SIM115 — handle lives for Popen's lifetime; closed in the cleanup block below
    proc = subprocess.Popen(
        server_args,
        env={
            **env,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        },
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"

    # ── Spawn runner as sibling subprocess ───────────────
    runner_log = tmp_path_factory.mktemp("e2e_logs") / "runner.log"
    runner_log_handle = open(runner_log, "w")  # noqa: SIM115
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env={
            **env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
        },
        stdout=runner_log_handle,
        stderr=subprocess.STDOUT,
    )

    health_iters = int(HEALTH_TIMEOUT_S / POLL_INTERVAL_S)
    for _ in range(health_iters):
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            status_resp = httpx.get(
                f"{base_url}/v1/runners/{runner_id}/status",
                timeout=2,
            )
            if (
                resp.status_code == 200
                and status_resp.status_code == 200
                and status_resp.json()["online"] is True
            ):
                break
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    else:
        if runner_proc.poll() is None:
            runner_proc.kill()
            runner_proc.wait(timeout=5)
        runner_log_handle.close()
        proc.kill()
        log_handle.close()
        log_contents = server_log.read_text() if server_log.exists() else ""
        runner_log_contents = runner_log.read_text() if runner_log.exists() else ""
        raise RuntimeError(
            f"Server didn't start within {HEALTH_TIMEOUT_S}s.\n"
            f"Server log at {server_log}:\n{log_contents[-3000:]}\n"
            f"Runner log at {runner_log}:\n{runner_log_contents[-3000:]}"
        )

    # Set a non-resettable ALLOW fallback on the server's policy-classifier
    # LLM queue ("_policy_llm_") so the classifier always returns ALLOW even
    # when a parallel xdist worker's reset_mock_llm clears the regular queue
    # between configure and the actual classifier call.
    if using_mock_llm and mock_llm_server_url is not None:
        set_fallback_mock_llm(
            mock_llm_server_url,
            "_policy_llm_",
            '{"action": "allow", "reason": ""}',
        )

    try:
        yield base_url
    finally:
        if runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        runner_log_handle.close()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


@pytest.fixture(scope="session")
def http_client(live_server: str) -> Iterator[httpx.Client]:
    """
    HTTP client pointed at the live server.

    :param live_server: The server base URL.
    :returns: An ``httpx.Client`` with long timeout.
    """
    # Announce this as a first-party non-browser client via the sentinel
    # Origin, exactly like the real SDK / runner. The multipart session
    # routes are behind require_trusted_origin; sending the sentinel keeps
    # these tests passing on their own merit rather than leaning on the
    # guard's (temporary) fail-open-on-absent-Origin behavior.
    with httpx.Client(
        base_url=live_server,
        timeout=300,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    ) as client:
        yield client


def upload_agent(
    client: httpx.Client,
    agent_dir: Path,
    *,
    rewrite_model_for_databricks: bool = False,
    databricks_profile: str | None = None,
) -> str:
    """
    Upload an agent bundle via multipart ``POST /v1/sessions``.

    Creates a session with the bundled agent and returns the agent
    name. The session is a side effect but harmless for e2e tests
    that only need the agent registered on the server.

    :param client: HTTP client pointed at the server.
    :param agent_dir: Path to the agent directory.
    :param rewrite_model_for_databricks: When True, rewrite any
        ``model:`` key (at any YAML depth) via
        :data:`_DATABRICKS_MODEL_MAP` before tarballing. Covers
        ``llm.model`` in ``config.yaml`` bundles and
        ``executor.model`` (including nested ``tools.<name>.executor.model``)
        in single-file omnigent YAMLs.
    :param databricks_profile: When set, stamp this profile onto
        every ``executor`` block that lacks one during the rewrite.
        Native (no-harness) agents otherwise reach the gateway with
        no profile and 401; harness agents that already carry a
        profile are left untouched. Only applied when
        ``rewrite_model_for_databricks`` is True.
    :returns: The agent name.
    """
    bundle = build_agent_bundle(
        agent_dir,
        rewrite_model_for_databricks=rewrite_model_for_databricks,
        databricks_profile=databricks_profile,
    )
    import json as _json

    resp = client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                bundle,
                "application/gzip",
            ),
        },
        # First-party sentinel Origin so the multipart create passes the
        # require_trusted_origin guard regardless of which client is passed.
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code == 409:
        return agent_dir.name
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    agent_resp = client.get(f"/v1/sessions/{session_id}/agent")
    agent_resp.raise_for_status()
    return agent_resp.json()["name"]


def register_inline_agent(
    client: httpx.Client,
    *,
    name: str,
    harness: str,
    model: str,
    profile: str,
    prompt: str,
    mock_llm_base_url: str | None = None,
    builtin_tools: list[str] | None = None,
    extra_config: dict[str, Any] | None = None,
) -> str:
    """
    Register a single-file omnigent agent built in-memory.

    Tarballs a minimal ``<name>.yaml`` (no directory on disk) and
    uploads it via multipart ``POST /v1/sessions``. Idempotent: a
    409 (already registered from a prior parametrize row against the
    same session-scoped server) is treated as success. The model is
    load-balanced via :func:`tests._model_pools.resolve_model`.

    :param client: HTTP client pointed at the server.
    :param name: Agent name (also the model field on later turns).
    :param harness: Executor harness identifier, e.g. ``"claude-sdk"``.
    :param model: Model identifier the executor receives.
    :param profile: Databricks profile name baked into the executor.
    :param prompt: System prompt for the agent.
    :param mock_llm_base_url: When set, bake an ``auth.type: api_key``
        block into the executor so the harness hits the mock server
        instead of ``api.openai.com``.
    :param builtin_tools: When set, add a ``tools.builtins`` list to
        the agent spec, e.g. ``["list_files", "upload_file"]``.
    :param extra_config: When set, top-level keys shallow-merged into
        the agent YAML before upload (e.g. ``tools`` and ``policies``).
        ``name``/``prompt``/``executor`` stay helper-controlled.
    :returns: The agent name (use the return value, not the *name*
        argument, they differ on rerun attempts).
    """
    import json as _json

    attempt = current_attempt()
    if attempt > 0:
        # Fresh name per rerun: a 409 re-register would keep the first
        # attempt's model and defeat llm_flaky rotation.
        name = f"{name}-r{attempt}"
    executor: dict[str, object] = {
        "harness": harness,
        "model": resolve_model(model),
        "profile": profile,
    }
    if mock_llm_base_url is not None:
        executor["auth"] = {
            "type": "api_key",
            "api_key": "mock-key",
            "base_url": mock_llm_base_url,
        }
    config: dict[str, object] = {
        "name": name,
        "prompt": prompt,
        "executor": executor,
    }
    if builtin_tools:
        config["tools"] = {"builtins": builtin_tools}
    if extra_config:
        for key, value in extra_config.items():
            config[key] = value
        # Reapply identity keys so a stray override can't desync the
        # agent name the caller gets back from later turns.
        config["name"] = name
        config["prompt"] = prompt
        config["executor"] = executor
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        # First-party sentinel Origin so the multipart create passes the
        # require_trusted_origin guard regardless of which client is passed.
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    # 409 = already registered by a prior parametrize row against the
    # same session-scoped server; treat as success. Explicit raise (not
    # assert) so the check survives ``python -O``.
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(
            f"[{harness}] agent register failed: {resp.status_code} {resp.text[:500]}"
        )
    return name


def build_agent_bundle(
    agent_dir: Path,
    *,
    rewrite_model_for_databricks: bool = False,
    databricks_profile: str | None = None,
) -> bytes:
    """
    Package an agent directory as a gzipped tarball.

    :param agent_dir: Agent directory to archive.
    :param rewrite_model_for_databricks: When True, rewrite model
        values through :data:`_DATABRICKS_MODEL_MAP` while archiving.
    :param databricks_profile: When set, stamp this profile onto
        every ``executor`` block that lacks one during the rewrite.
    :returns: Gzipped tar archive bytes accepted by agent/session
        upload endpoints.
    """
    with io.BytesIO() as buffer:
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            if rewrite_model_for_databricks:
                _add_dir_with_model_rewrite(tar, agent_dir, profile=databricks_profile)
            else:
                tar.add(str(agent_dir), arcname=".")
        return buffer.getvalue()


def _rewrite_yaml_models(
    node: Any, profile: str | None = None, spread_key: str | None = None
) -> bool:
    """
    Walk *node* recursively and rewrite ``model:`` string values
    via :data:`_DATABRICKS_MODEL_MAP`, then load-balance the result
    through :func:`tests._model_pools.resolve_model`. Values outside
    both the map and the balance pools pass through untouched.

    When *profile* is set, also stamp ``profile:`` onto every
    ``executor`` block that lacks one — native (no-harness) agents
    otherwise reach the Databricks gateway with no profile and 401.

    :param node: A parsed YAML node (dict, list, or scalar).
    :param profile: Databricks profile to inject into executor
        blocks, or ``None`` to skip profile injection.
    :param spread_key: Stable load-balancing key, e.g. the bundle dir
        name; combined with each model value so bundle siblings spread.
        ``None`` falls back to the running test's nodeid.
    :returns: ``True`` if at least one rewrite occurred.
    """
    changed = False
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "model" and isinstance(v, str):
                mapped = _DATABRICKS_MODEL_MAP.get(v, v)
                key = f"{spread_key}:{v}" if spread_key is not None else None
                resolved = resolve_model(mapped, key=key)
                if resolved != v:
                    node[k] = resolved
                    changed = True
            if k == "executor" and isinstance(v, dict) and profile and "profile" not in v:
                v["profile"] = profile
                changed = True
            if _rewrite_yaml_models(v, profile, spread_key):
                changed = True
    elif isinstance(node, list):
        for item in node:
            if _rewrite_yaml_models(item, profile, spread_key):
                changed = True
    return changed


def _add_dir_with_model_rewrite(
    tar: tarfile.TarFile, agent_dir: Path, *, profile: str | None = None
) -> None:
    """
    Add *agent_dir* to *tar*, rewriting any YAML file's ``model:``
    values via :data:`_DATABRICKS_MODEL_MAP`. Files without a
    recognized model value are tarred verbatim (preserves comments
    and formatting). Symlinks and non-regular files are skipped.

    :param tar: The open tarfile being built.
    :param agent_dir: Path to the agent directory.
    :param profile: Databricks profile to stamp onto executor blocks
        that lack one, or ``None`` to skip profile injection.
    :returns: ``None``. The tar is mutated in place.
    """
    for entry in sorted(agent_dir.rglob("*")):
        if not entry.is_file():
            continue
        rel = entry.relative_to(agent_dir).as_posix()
        if entry.suffix.lower() not in {".yaml", ".yml"}:
            tar.add(str(entry), arcname=rel)
            continue
        raw = entry.read_text()
        try:
            config = yaml.safe_load(raw)
        except yaml.YAMLError:
            config = None
        if not isinstance(config, dict) or not _rewrite_yaml_models(
            config, profile, spread_key=agent_dir.name
        ):
            tar.add(str(entry), arcname=rel)
            continue
        modified = yaml.safe_dump(config, sort_keys=False).encode()
        info = tarfile.TarInfo(name=rel)
        info.size = len(modified)
        info.mtime = int(entry.stat().st_mtime)
        tar.addfile(info, io.BytesIO(modified))


def _materialize_builtin_sdk_chat_spec(
    dest_dir: Path,
    *,
    databricks_workspace_host: str | None,
    profile: str | None,
    mock_llm_server_url: str | None = None,
) -> Path:
    """
    Write a profile-aware copy of ``sdk-chat-builtin.yaml`` to seed as a built-in.

    The built-in fork/switch TARGET is seeded via
    ``OMNIGENT_BUILTIN_AGENT_DIRS``, which reads the spec verbatim — it
    does NOT pass through :func:`upload_agent`'s model rewrite. The on-disk
    spec (``model: claude-sonnet-4-20250514``, no profile) therefore
    authenticates via the ``claude`` CLI's OAuth session, which hosted CI
    lacks (the post-switch turn would fail ``NOT LOGGED IN``). Under
    ``--profile`` we apply the SAME rewrite ``upload_agent`` does — map the
    model via :data:`_DATABRICKS_MODEL_MAP` and stamp ``executor.profile``
    — so the seeded built-in is gateway-wired like the source agent.
    Verbatim (local OAuth path) otherwise. The filename is kept as
    ``sdk-chat-builtin.yaml`` so the built-in seeds under the name the e2e
    tests look up, and no ``os_env`` is added (the os_env-reset test relies
    on the target declaring none).

    In mock mode (``mock_llm_server_url`` set), injects an ``auth`` block
    so the claude-sdk executor routes ``ANTHROPIC_BASE_URL`` at the mock
    server. The Anthropic SDK appends ``/v1/messages`` to the base URL, so
    the base URL must NOT include ``/v1``.

    :param dest_dir: Directory to write the materialized spec into, e.g. a
        ``tmp_path_factory.mktemp(...)`` dir.
    :param databricks_workspace_host: Workspace host URL, or ``None`` when
        ``--profile`` is empty (the api.openai.com / local OAuth path).
    :param profile: The ``--profile`` value to stamp onto the executor,
        e.g. ``"default"``; ignored when *databricks_workspace_host* is
        ``None``.
    :param mock_llm_server_url: Mock LLM server base URL, e.g.
        ``"http://127.0.0.1:12345"``. When set, the built-in's executor
        gets an ``auth`` block pointing at this URL (without ``/v1``).
    :returns: Path to the written ``sdk-chat-builtin.yaml``.
    """
    config = yaml.safe_load(_SDK_CHAT_BUILTIN_SPEC.read_text())
    if databricks_workspace_host is not None:
        _rewrite_yaml_models(config, profile, spread_key=_SDK_CHAT_BUILTIN_SPEC.stem)
    if mock_llm_server_url is not None:
        # The Anthropic SDK appends /v1/messages to base_url, so do NOT
        # include /v1 here — the mock server serves POST /v1/messages.
        config.setdefault("executor", {})["auth"] = {
            "type": "api_key",
            "api_key": "mock-key",
            "base_url": mock_llm_server_url,
        }
    dest = dest_dir / _SDK_CHAT_BUILTIN_SPEC.name
    dest.write_text(yaml.safe_dump(config, sort_keys=False))
    return dest


@pytest.fixture(scope="session")
def coder_agent(http_client: httpx.Client, databricks_workspace_host: str | None) -> str:
    """
    Upload the coder agent (with reviewer sub-agent) and
    return its name.

    :param http_client: HTTP client pointed at the server.
    :returns: The agent name, e.g. ``"coder"``.
    """
    return upload_agent(
        http_client,
        _CODER_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
    )


@pytest.fixture(scope="session")
def archer_agent(http_client: httpx.Client, databricks_workspace_host: str | None) -> str:
    """
    Upload the archer agent (with fact_checker and summarizer
    sub-agents) and return its name.

    :param http_client: HTTP client pointed at the server.
    :returns: The agent name, e.g. ``"archer"``.
    """
    return upload_agent(
        http_client,
        _ARCHER_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
    )


@pytest.fixture(scope="session")
def claude_coder_agent(http_client: httpx.Client, databricks_workspace_host: str | None) -> str:
    """
    Upload the claude-coder agent and return its name.

    The Claude Agent SDK authenticates via the ``claude`` CLI's
    own session (OAuth), so no explicit API key env var is required.

    :param http_client: HTTP client pointed at the server.
    :returns: The agent name, ``"claude-coder"``.
    """
    return upload_agent(
        http_client,
        _CLAUDE_CODER_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
    )


@pytest.fixture(scope="session")
def sandbox_deps_os_env_agent(
    http_client: httpx.Client, databricks_workspace_host: str | None
) -> str:
    """
    Upload the minimal os_env dependency-install test fixture.

    :param http_client: HTTP client pointed at the server.
    :returns: The agent name, ``"sandbox-deps-os-env"``.
    """
    return upload_agent(
        http_client,
        _SANDBOX_DEPS_OS_ENV_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
    )


@pytest.fixture(scope="session")
def databricks_profile_or_none(request: pytest.FixtureRequest) -> str | None:
    """
    Return the active ``--profile`` value, or ``None`` when unset.

    Non-skipping companion to the per-test ``databricks_profile``
    fixtures: agent-upload fixtures need the profile to stamp onto
    native executor blocks but must still build (api.openai.com path)
    when no profile is set.

    :param request: Pytest fixture request.
    :returns: The profile name, or ``None``.
    """
    return request.config.getoption("--profile") or None


@pytest.fixture(scope="session")
def openai_coder_agent(
    http_client: httpx.Client,
    databricks_workspace_host: str | None,
    databricks_profile_or_none: str | None,
) -> str:
    """
    Upload the openai-coder agent (with reviewer sub-agent
    and skills) and return its name.

    :param http_client: HTTP client pointed at the server.
    :returns: The agent name, ``"openai-coder"``.
    """
    return upload_agent(
        http_client,
        _OPENAI_CODER_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
        databricks_profile=databricks_profile_or_none,
    )


@pytest.fixture(scope="session")
def sys_terminal_test_agent(
    http_client: httpx.Client, databricks_workspace_host: str | None
) -> str:
    """
    Upload the ``sys-terminal-test`` agent (omnigent-flavored
    YAML with a ``terminals:`` block) and return its name.

    Used by ``test_sys_terminal_e2e.py``. The agent declares a
    single ``bash`` terminal so ``sys_terminal_*`` tools register
    on the AP-side ToolManager — verifying the compat translator
    threads ``AgentDef.terminals`` → ``AgentSpec.terminals``.

    :param http_client: HTTP client pointed at the server.
    :returns: The agent name, ``"sys-terminal-test"``.
    """
    return upload_agent(
        http_client,
        _SYS_TERMINAL_TEST_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
    )


@pytest.fixture(scope="session")
def sample_code_dir(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """
    Create a temp directory with sample Python files for the
    reviewer sub-agent to inspect.

    :param tmp_path_factory: Pytest temp path factory.
    :returns: Path to the directory containing sample files.
    """
    d = tmp_path_factory.mktemp("sample_code")

    # A module with a deliberate bug (division by zero risk).
    (d / "calculator.py").write_text(
        "def divide(a: float, b: float) -> float:\n"
        '    """Divide a by b."""\n'
        "    return a / b\n"
        "\n"
        "\n"
        "def average(numbers: list[float]) -> float:\n"
        '    """Return the mean of a list of numbers."""\n'
        "    total = sum(numbers)\n"
        "    return divide(total, len(numbers))\n"
    )

    # A test file with incomplete coverage.
    (d / "test_calculator.py").write_text(
        "from calculator import divide, average\n"
        "\n"
        "\n"
        "def test_divide():\n"
        "    assert divide(10, 2) == 5.0\n"
        "\n"
        "\n"
        "def test_average():\n"
        "    assert average([1, 2, 3]) == 2.0\n"
        "    # Missing: test for empty list (ZeroDivisionError)\n"
    )

    # A utility with a bare except and hardcoded path.
    (d / "utils.py").write_text(
        "import json\n"
        "import os\n"
        "\n"
        "\n"
        "def load_config():\n"
        "    try:\n"
        '        with open("/etc/myapp/config.json") as f:\n'
        "            return json.load(f)\n"
        "    except:\n"
        "        return {}\n"
        "\n"
        "\n"
        "def get_temp_dir():\n"
        '    return os.environ.get("TEMP_DIR", "/tmp/myapp")\n'
    )

    return d


def poll_until_terminal(
    client: httpx.Client,
    response_id: str,
    timeout: float = 300,
) -> dict[str, Any]:
    """
    Poll GET /v1/responses/{id} until terminal state.

    :param client: HTTP client.
    :param response_id: The response ID to poll.
    :param timeout: Max seconds to wait.
    :returns: The terminal response body.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/responses/{response_id}")
        resp.raise_for_status()
        body = resp.json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Response {response_id} didn't complete within {timeout}s")


def lookup_agent_id(client: httpx.Client, agent_name: str) -> str:
    """
    Return the durable ``agent_id`` for an agent registered by name.

    The sessions API binds by durable id, not display name, so tests
    that uploaded through ``POST /v1/sessions`` (multipart, which
    returns the session id) need this lookup to drive subsequent
    ``POST /v1/sessions`` calls with ``agent_id``.

    Derives the id from ``GET /v1/sessions?agent_name=<name>``
    which returns session snapshots containing the ``agent_id``
    field.

    :param client: HTTP client pointed at the live server.
    :param agent_name: Display name returned by :func:`upload_agent`.
    :returns: The matching ``"ag_..."`` durable id.
    :raises AssertionError: If no session with that agent name exists.
    """
    resp = client.get("/v1/sessions", params={"agent_name": agent_name, "limit": 1})
    resp.raise_for_status()
    sessions = resp.json()["data"]
    if sessions:
        return str(sessions[0]["agent_id"])
    raise AssertionError(f"agent {agent_name!r} not registered on the server")


def create_runner_bound_session(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
) -> str:
    """
    Create a session bound to *agent_name* and to *runner_id*.

    Mirrors the alpha-runner-state contract that
    ``POST /v1/sessions/{id}/events`` requires: a session
    cannot dispatch until ``conversations.runner_id`` is set, and that
    column is mutated only through ``PATCH /v1/sessions/{id}``. This
    helper performs both steps so callers can issue events immediately.

    :param client: HTTP client pointed at the live server.
    :param agent_name: Display name of an already-uploaded agent.
    :param runner_id: Registered runner id (e.g. the
        :func:`live_runner_id` fixture).
    :returns: The session/conversation id, e.g. ``"conv_abc"``.
    """
    agent_id = lookup_agent_id(client, agent_name)
    # First-party sentinel Origin so the create passes the
    # require_trusted_origin guard regardless of which client is passed.
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["id"])
    resp = client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
    )
    resp.raise_for_status()
    return session_id


def send_user_message_to_session(
    client: httpx.Client,
    *,
    session_id: str,
    content: str | list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """
    POST a user message to *session_id* and return the input response_id.

    The events endpoint returns ``{"queued": True, "item_id": "..."}``
    but runner-native sessions do not create Omnigent DBOS task rows for
    the turn. For session-dispatch tests, use this id only as the
    turn grouping key in ``conversation_items``; poll the session
    snapshot with :func:`poll_session_until_terminal` instead of
    ``GET /v1/responses/{id}``.

    :param client: HTTP client pointed at the live server.
    :param session_id: Runner-bound session id returned by
        :func:`create_runner_bound_session`.
    :param content: Either a plain user-prompt string (shorthand for
        a single ``input_text`` block) or a list of content blocks
        (e.g. ``input_text`` + ``input_file``).
    :param tools: Optional OpenAI function-tool dicts, e.g.
        ``[{"type": "function", "function": {"name": "Read", ...}}]``.
        Registered when this event creates a new task; ignored on the
        steer-into-running path.
    :returns: The response_id stamped on the posted user item, e.g.
        ``"resp_abc"``.
    """
    blocks: list[dict[str, Any]] = (
        [{"type": "input_text", "text": content}] if isinstance(content, str) else content
    )
    body: dict[str, Any] = {"type": "message", "data": {"role": "user", "content": blocks}}
    if tools is not None:
        body["tools"] = tools
    resp = client.post(f"/v1/sessions/{session_id}/events", json=body)
    resp.raise_for_status()
    payload = resp.json()
    # A queued turn returns ``{"queued": True, "item_id": ...}``. A
    # request-phase policy that resolves synchronously instead returns an
    # inline verdict (e.g. ``{"denied": True, "reason": ...}``) with no
    # ``item_id``. Fail loud with the actual body rather than letting the
    # bare ``["item_id"]`` index raise a cryptic ``KeyError`` — that masked a
    # real prompt-policy classifier flake as an unreadable crash. Callers that
    # expect the synchronous-verdict path should use ``_post_user_message``.
    if "item_id" not in payload:
        raise AssertionError(
            f"events endpoint did not queue a turn for session {session_id!r}: "
            f"expected {{'queued': True, 'item_id': ...}}, got {payload}. A "
            f"synchronous policy verdict here means a request-phase policy "
            f"short-circuited the turn instead of letting it through."
        )
    item_id = payload["item_id"]
    snap = client.get(f"/v1/sessions/{session_id}")
    snap.raise_for_status()
    for item in reversed(snap.json().get("items", [])):
        if item.get("id") == item_id:
            return str(item["response_id"])
    raise AssertionError(
        f"persisted item {item_id!r} not found in session {session_id!r} snapshot"
    )


def _flatten_session_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return the flat Responses-style shape for a session item.

    ``GET /v1/sessions/{id}`` serializes persisted conversation items
    as ``ConversationItem`` objects, so type-specific fields may live
    under ``data``. The E2E response helpers historically consume the
    flat ``GET /v1/responses/{id}`` shape where ``name``, ``arguments``,
    ``call_id``, ``role``, and ``content`` are top-level fields. Flatten
    here so the tests observe the same shape on runner-native sessions.
    """
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    return {
        "id": item.get("id"),
        "response_id": item.get("response_id"),
        "type": item.get("type"),
        "status": item.get("status"),
        **data,
    }


def _session_items_for_response(
    client: httpx.Client,
    *,
    session_id: str,
    response_id: str,
) -> list[dict[str, Any]]:
    """Return flat session items for a runner-native turn.

    The AP-stamped user input ``response_id`` is only a local grouping id.
    Runner-native output items use the harness-allocated response id, so
    do not filter by ``response_id`` here. These E2E helpers create one
    fresh session per turn; all non-user items in the snapshot belong to
    the turn under observation.
    """
    del response_id
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return [
        flattened
        for item in resp.json().get("items", [])
        if not (
            (flattened := _flatten_session_item(item)).get("type") == "message"
            and flattened.get("role") == "user"
        )
    ]


def poll_session_until_terminal(
    client: httpx.Client,
    *,
    session_id: str,
    response_id: str,
    timeout: float = 300,
) -> dict[str, Any]:
    """
    Poll a runner-native session snapshot until the turn is terminal.

    Session dispatch runs on the runner and therefore may not create a
    pollable Omnigent ``Task`` for ``GET /v1/responses/{response_id}``. This
    helper returns a Responses-like dict synthesized from the session
    snapshot: terminal status from ``session.status`` and output from
    non-user ``conversation_items`` sharing the turn ``response_id``.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :param response_id: Turn grouping id returned by
        :func:`send_user_message_to_session`.
    :param timeout: Max seconds to wait.
    :returns: ``{"status": ..., "output": ...}``.
    """
    deadline = time.monotonic() + timeout
    last_body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last_body = resp.json()
        status = last_body.get("status")
        if status in ("idle", "failed"):
            output = [
                flattened
                for item in last_body.get("items", [])
                if not (
                    (flattened := _flatten_session_item(item)).get("type") == "message"
                    and flattened.get("role") == "user"
                )
            ]
            return {
                "id": response_id,
                "status": "completed" if status == "idle" else "failed",
                "output": output,
                "error": last_body.get("last_task_error") or last_body.get("error"),
            }
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Session {session_id} did not become terminal within {timeout}s; "
        f"last snapshot={last_body}"
    )


def poll_for_pending_tool_calls(
    client: httpx.Client,
    response_id: str,
    timeout: float = 120,
    *,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Poll until ``action_required`` function_calls appear.

    By default this uses ``GET /v1/responses/{response_id}`` for
    legacy/background response tests. Pass ``session_id`` for
    runner-native session turns, which do not create Omnigent DBOS task rows
    for ``response_id`` and must be observed through the session
    snapshot.

    :param client: HTTP client.
    :param response_id: The response or session-turn ID.
    :param timeout: Max seconds to wait.
    :param session_id: Optional session/conversation id for the
        runner-native sessions path.
    :returns: List of action_required function_call items.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session_id is None:
            resp = client.get(f"/v1/responses/{response_id}")
            body = resp.json()
            pending = [
                item
                for item in body.get("output", [])
                if item.get("type") == "function_call" and item.get("status") == "action_required"
            ]
            if pending:
                return pending
            if body["status"] in ("completed", "failed"):
                return []
        else:
            items = _session_items_for_response(
                client,
                session_id=session_id,
                response_id=response_id,
            )
            pending = [
                item
                for item in items
                if item.get("type") == "function_call" and item.get("status") == "action_required"
            ]
            if pending:
                return pending
            snap = client.get(f"/v1/sessions/{session_id}")
            snap.raise_for_status()
            if snap.json().get("status") in ("idle", "failed"):
                return []
        time.sleep(POLL_INTERVAL_S)
    return []


@pytest.fixture
def resume_test_server(
    llm_api_key: str,
    databricks_workspace_host: str | None,
    tmp_path: Path,
) -> Iterator[str]:
    """
    Spawn a real ``omnigent server`` that accepts the CLI's own runner.

    Used by the native-CLI resume e2e tests
    (``test_claude_native_cli_resume_e2e`` / ``test_codex_native_cli_resume_e2e``),
    which drive the real ``omnigent claude/codex --server`` CLI. It differs
    from :func:`live_server` in two ways, both required for that:

    * **No tunnel-token allow-list.** ``OMNIGENT_RUNNER_TUNNEL_TOKEN``
      installs a binding-token allow-list (see
      ``runner_tunnel.create_runner_tunnel_router``) that rejects the runner
      the CLI spawns with its own per-run token. Omitting it selects the
      deployed-server posture: accept any token-bound runner.
    * **Accounts auth.** The server defaults to accounts mode, which
      provides built-in authentication without an external identity provider.

    No sibling runner is started: in ``--server`` mode the CLI brings its own.

    :param llm_api_key: LLM key from ``--llm-api-key`` (a Databricks bearer
        under ``--profile``). The native harnesses authenticate the model via
        their own login, so this only satisfies the server's startup env.
    :param databricks_workspace_host: Workspace host URL, or ``None``.
    :param tmp_path: Per-test temp dir for the DB, artifacts, and server log.
    :returns: The server base URL, e.g. ``"http://127.0.0.1:54321"``.
    :raises RuntimeError: If the server does not pass health within
        :data:`HEALTH_TIMEOUT_S`.
    """
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "resume_e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "server.log"

    env = {
        **os.environ,
        "OPENAI_API_KEY": llm_api_key,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    if databricks_workspace_host is not None:
        env["OPENAI_BASE_URL"] = f"{databricks_workspace_host}/serving-endpoints"
    # See docstring: an allow-list would reject the CLI's own runner.
    env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)

    log_handle = open(server_log, "w")  # noqa: SIM115 — lives for the Popen lifetime; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            raise RuntimeError(
                f"server didn't pass health within {HEALTH_TIMEOUT_S}s; "
                f"log tail:\n{server_log.read_text()[-3000:]}"
            )
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited early (code {proc.returncode}); "
                f"log tail:\n{server_log.read_text()[-3000:]}"
            )
        yield base_url
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()
