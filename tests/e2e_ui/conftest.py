"""Fixtures for browser-driven e2e tests of the ap-web SPA.

The suite spawns a real ``omnigent server --agent`` subprocess against
``examples/hello_world.yaml`` and drives the rendered SPA with
Playwright. The server is wired to a mock LLM server so the suite
runs deterministically without real credentials. The suite is excluded
from the default ``pytest`` run via ``--ignore=tests/e2e_ui`` in
``pyproject.toml`` and gated to ``workflow_dispatch`` in CI for now.

Local usage::

    # one-time setup
    uv sync --extra e2e-ui
    uv run playwright install --with-deps chromium

    # run against a freshly built SPA + spawned server
    uv run pytest tests/e2e_ui -v

    # iterate against an already-running server (dev hosts/ports need opt-in)
    cd ap-web && npm run dev &
    omnigent server --agent examples/hello_world.yaml &
    OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1 \
      uv run pytest tests/e2e_ui --ui-base-url http://127.0.0.1:5173

``omnigent server`` is documented at ``omnigent/cli.py:server``:
it spins up uvicorn with the Omnigent app and spawns an out-of-process
runner that reconnects over the WebSocket tunnel. The fixture passes
``--database-uri`` and ``--artifact-location`` pointing at the
pytest tmp dir so the test never touches the user's default
``sqlite:///omnigent.db`` / ``./artifacts``.
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import socket
import subprocess
import sys
import tarfile
import textwrap
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import filelock
import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.url_safety import DEV_PORTS, unsafe_ui_base_url_reason

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOW_DEV_BASE_URL_ENV = "OMNIGENT_E2E_ALLOW_DEV_BASE_URL"


def open_right_rail(page: Page) -> None:
    """Expand the right "Workspace" rail if it is collapsed.

    The rail defaults open, but its open-state is remembered per conversation,
    so a session previously left collapsed lands shut. Idempotent: if the rail
    is already open this just waits for it.

    The toggle only renders once the rail has content (``hasRailContent``) and
    on the desktop viewport these tests run at, so the generous timeout covers
    the changed-files / terminals detection that gates the button.

    :param page: Playwright page already navigated to a ``/c/{id}`` route.
    :returns: None. Leaves the Workspace rail open.
    """
    toggle = page.locator(
        'button[aria-label="Expand right panel"], button[aria-label="Collapse right panel"]'
    ).first
    expect(toggle).to_be_visible(timeout=60_000)
    if toggle.get_attribute("aria-label") == "Expand right panel":
        toggle.click()
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible()


# Populated by ``live_server`` so test-scoped fixtures can access the
# server PID and runner id without changing ``live_server``'s return
# type (which other tests depend on).
_server_state: dict[str, int | str] = {}
_AP_WEB_DIR = _REPO_ROOT / "ap-web"
_BUILD_OUTPUT = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"

# ``omnigent server --agent`` runs the spec through the strict
# validator at registration time (no shim defaults applied), so the
# YAML must carry an explicit ``executor`` block — otherwise the
# server rejects with ``executor.config.harness: required when
# executor.type is 'omnigent'``. The model name (gpt-4o-mini) is a plain
# (non-``databricks-``) name on purpose: the openai-agents harness then
# resolves no provider auth and falls back to ``OPENAI_BASE_URL`` (the
# in-process mock) rather than routing to the Databricks gateway, which
# would need real credentials CI does not have. A ``databricks-``-prefixed
# model forces Databricks DEFAULT-profile auth (see
# omnigent/runtime/workflow.py) and fails with DatabricksAuthError in CI.
_TEST_AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents

# Required for PUT /filesystem/{path} seeding in UI tests (e.g. markdown
# editor comments) — the runner returns 404 when os_env is absent.
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _build_hello_world_bundle() -> bytes:
    """Build a gzipped tarball from ``_TEST_AGENT_YAML``.

    Uses a non-``config.yaml`` archive name so the bundle routes
    through the omnigent compat adapter (which translates
    ``executor.harness`` → ``executor.config.harness`` and sets
    ``executor.type: omnigent``). Using ``config.yaml`` would
    go through the strict ``spec_version: 1`` parser which doesn't
    accept the shorthand.

    :returns: The ``.tar.gz`` bytes ready for multipart upload.
    """
    import gzip
    import io
    import tarfile

    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = _TEST_AGENT_YAML.encode()
        info = tarfile.TarInfo(name="hello_world.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# Time budget for the server's /health endpoint to come up after
# spawn. ``serve`` does YAML parse + bundle materialization +
# DBOS init + uvicorn boot, all of which take a few seconds on a cold
# venv.
_HEALTH_TIMEOUT_S = 30.0
_HEALTH_POLL_INTERVAL_S = 0.5

# Switch-target built-ins for the Files-tab os_env-boundary test
# (test_switch_agent_files_tab.py). The in-place switch dialog lists
# BUILT-IN agents only (``session_id IS NULL`` — see
# ``switch_session_agent``), and built-ins can only be seeded at server
# startup via ``OMNIGENT_BUILTIN_AGENT_DIRS``, so ``live_server`` writes
# these two specs to disk and threads them through that env var. Both run
# the same openai-agents harness as ``hello_world`` (same provider family
# → the picker's ``forkSwitchPreservesHistory`` gate offers them); the
# ONLY difference is os_env presence, which is the variable under test —
# the runner 404s the environment resource when os_env is absent, hiding
# the web Files tab. The registered name is the spec file's stem.
_FILES_PROBE_NO_ENV_AGENT_NAME = "files_probe_noenv"
_FILES_PROBE_ENV_AGENT_NAME = "files_probe_env"
_FILES_PROBE_NO_ENV_AGENT_YAML = f"""\
name: {_FILES_PROBE_NO_ENV_AGENT_NAME}
prompt: You are a terse assistant with no filesystem.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents
"""
_FILES_PROBE_ENV_AGENT_YAML = f"""\
name: {_FILES_PROBE_ENV_AGENT_NAME}
prompt: You are a terse assistant with a filesystem.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register UI-only CLI flags.

    :param parser: The pytest option parser.
    """
    parser.addoption(
        "--ui-base-url",
        default=None,
        help=(
            "Skip both the SPA build and the server spawn; point Playwright "
            "at this URL instead. Refuses known dev hosts/ports unless "
            f"{_ALLOW_DEV_BASE_URL_ENV}=1 is set."
        ),
    )
    parser.addoption(
        "--ui-skip-build",
        action="store_true",
        default=False,
        help=(
            "Reuse whatever's already in omnigent/server/static/web-ui/ "
            "instead of rebuilding. Fails if no build is present."
        ),
    )
    # Round-robin shard split for the e2e-ui CI matrix. We roll our own
    # (rather than pull in pytest-shard / pytest-split) so the partition
    # is a dependency-free strided slice -- see pytest_collection_modifyitems
    # below for why striding beats hash-bucketing for wall-clock balance.
    parser.addoption(
        "--splits",
        type=int,
        default=None,
        help="Total number of shards to split the UI suite into (CI matrix).",
    )
    parser.addoption(
        "--group",
        type=int,
        default=None,
        help="1-indexed shard this run executes (requires --splits).",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast on unsafe e2e-ui harness options.

    :param config: Pytest config with repo and pytest-playwright options.
    """
    base_url = config.getoption("--ui-base-url")
    if base_url:
        _validate_ui_base_url(base_url)

    if os.environ.get("CI") and config.getoption("--headed", default=False):
        raise pytest.UsageError(
            "tests/e2e_ui must run headless in CI. Remove --headed; headed "
            "browser windows are only allowed for local debugging."
        )


@pytest.fixture(scope="session")
def browser_type_launch_args(
    browser_type_launch_args: dict[str, Any],
    pytestconfig: pytest.Config,
) -> dict[str, Any]:
    """Default UI e2e browser launches to headless, with CI enforcement."""
    launch_args = {**browser_type_launch_args}
    if os.environ.get("CI"):
        launch_args["headless"] = True
    elif not pytestconfig.getoption("--headed", default=False):
        launch_args.setdefault("headless", True)
    # The pinned Playwright Docker image (the visual-snapshot renderer, both in
    # ui-snapshot.yml and the local regen script) runs as root, where Chromium
    # refuses to start without --no-sandbox; --disable-dev-shm-usage avoids the
    # container's small /dev/shm. Neither flag changes rasterized output, so a
    # baseline stays identical to a sandboxed run. Env-gated so the unpinned
    # e2e-ui runners (non-root) are unaffected.
    if os.environ.get("OMNIGENT_PW_NO_SANDBOX"):
        launch_args["args"] = [
            *launch_args.get("args", []),
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
    return launch_args


@pytest.fixture
def browser_context_args(
    browser_context_args: dict[str, Any],
) -> dict[str, Any]:
    """Return a new context options dict for each test.

    pytest-playwright already creates a fresh context for its function-scoped
    ``context`` and ``page`` fixtures. Keeping this wrapper function-scoped
    makes that contract explicit and prevents accidental mutable option reuse.
    """
    return {**browser_context_args}


def _validate_ui_base_url(base_url: str) -> None:
    reason = unsafe_ui_base_url_reason(base_url)
    if reason is None or os.environ.get(_ALLOW_DEV_BASE_URL_ENV) == "1":
        return
    dev_ports = ", ".join(str(port) for port in sorted(DEV_PORTS))
    raise pytest.UsageError(
        f"Refusing --ui-base-url={base_url!r}: {reason}. Reusing a dev or "
        "production-like server is unsafe because e2e UI tests share that "
        "server's database, artifacts, and runner state. Omit --ui-base-url "
        "to let the fixture spawn an isolated server on a random port. If "
        "you intentionally want to reuse this server for local debugging, "
        f"set {_ALLOW_DEV_BASE_URL_ENV}=1. Refused dev ports: {dev_ports}."
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Round-robin the collected UI tests across the CI shard matrix.

    pytest-shard hash-buckets node IDs, which is blind to per-test
    wall-clock and left one shard at ~5min while siblings finished in
    ~2min. A *strided* slice -- ``items[group-1::splits]`` -- deals tests
    out like cards instead, so a heavy file (whose cases collect
    adjacently) scatters one-per-shard rather than landing in a single
    bucket. That averages cost across shards without maintaining a
    durations file.

    No-op unless both ``--splits`` and ``--group`` are passed, so local
    runs and the non-sharded suites are unaffected. ``trylast`` lets the
    repo-wide known-failures marking in ``tests/conftest.py`` tag items
    first; markers travel with the items the slice keeps.
    """
    splits = config.getoption("--splits")
    group = config.getoption("--group")
    if splits is None and group is None:
        return
    if splits is None or group is None:
        raise pytest.UsageError("--splits and --group must be passed together")
    if splits < 1:
        raise pytest.UsageError("--splits must be >= 1")
    if not 1 <= group <= splits:
        raise pytest.UsageError(f"--group must be between 1 and {splits}")
    items[:] = items[group - 1 :: splits]


def _register_agent_yaml(
    base_url: str,
    yaml_text: str,
    *,
    arcname: str = "config.yaml",
) -> str | None:
    """Register an agent via multipart ``POST /v1/sessions`` from a raw YAML body.

    ``arcname`` defaults to ``config.yaml`` for native Omnigent specs. Pass a
    ``*.yaml`` filename for omnigent-flavored single-file specs; the
    compat loader only routes those through the omnigent translator when
    the extracted bundle has no root ``config.yaml``.

    Returns the new agent id on 201, or None on 409 (already registered against
    a long-lived ``--ui-base-url`` server).
    """
    import json as _json

    yaml_bytes = yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(arcname)
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))

    resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=10.0,
    )
    if resp.status_code == 409:
        return None
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    agent_resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/agent",
        timeout=10.0,
    )
    agent_resp.raise_for_status()
    return agent_resp.json()["id"]


def _register_extra_agent(base_url: str, name: str, prompt: str) -> str | None:
    """Register a name+prompt-only agent. Thin wrapper over :func:`_register_agent_yaml`."""
    yaml_text = (
        f"spec_version: 1\n"
        f"name: {name}\n"
        f"prompt: {prompt}\n"
        f"executor:\n"
        f"  config:\n"
        f"    harness: openai-agents\n"
    )
    return _register_agent_yaml(base_url, yaml_text)


def _find_free_port() -> int:
    """
    Find a free TCP port by binding to port 0.

    :returns: An available port number.
    """
    max_attempts = 100
    for _ in range(max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in DEV_PORTS:
            return port
    raise RuntimeError(
        f"Could not find a free e2e UI port outside dev ports "
        f"{sorted(DEV_PORTS)} after {max_attempts} attempts"
    )


# ---------------------------------------------------------------------------
# Mock LLM server fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mock_llm_server_url(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Start a mock LLM server for the test session.

    The mock server is a lightweight FastAPI/uvicorn subprocess that
    serves an OpenAI-compatible ``/v1/`` endpoint. The ``live_server``
    fixture points the spawned omnigent server at this URL so all agent
    LLM calls hit the mock rather than a real provider.

    :param tmp_path_factory: Pytest temp path factory for logs.
    :returns: The mock server base URL, e.g. ``"http://127.0.0.1:51235"``.
    """
    mock_port = _find_free_port()
    mock_log = tmp_path_factory.mktemp("mock_llm_logs") / "mock_llm.log"
    log_handle = open(mock_log, "w")  # noqa: SIM115

    proc = subprocess.Popen(
        [
            sys.executable,
            str(_REPO_ROOT / "tests" / "server" / "integration" / "mock_llm_server.py"),
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
            pass
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
    mock_url: str,
    responses: list[dict[str, Any]],
    *,
    key: str = "default",
    match: str | None = None,
) -> None:
    """Configure a keyed response queue on the mock LLM server.

    Each dict in *responses* maps to a ``QueuedResponse`` on the mock
    server. The *key* determines which queue the responses are stored in
    — the mock server routes each request to the queue whose key matches
    the request's ``model`` field. When *match* is set, the queue fires
    whenever the user text contains that substring, regardless of model.

    :param mock_url: Mock server base URL.
    :param responses: List of response configs. Keys:
        ``text``, ``tool_calls``, ``block``, ``stream``,
        ``error``, ``status_code``.
    :param key: Queue key — typically the model name baked into the
        agent spec. Defaults to ``"default"`` (matches any model
        not assigned to a more specific queue).
    :param match: Optional substring to match against the user text for
        content-based routing (in addition to model-name routing).
    """
    body: dict[str, Any] = {"key": key, "responses": responses}
    if match is not None:
        body["match"] = match
    resp = httpx.post(
        f"{mock_url}/mock/configure",
        json=body,
        timeout=5.0,
    )
    resp.raise_for_status()


def reset_mock_llm(mock_url: str) -> None:
    """Clear all regular keyed queues, captured requests, and gates.

    Fallbacks set via :func:`set_fallback_mock_llm` are preserved.

    :param mock_url: Mock server base URL.
    """
    resp = httpx.post(f"{mock_url}/mock/reset", timeout=5.0)
    resp.raise_for_status()


def set_fallback_mock_llm(
    mock_url: str,
    key: str,
    text: str,
) -> None:
    """Set a non-resettable fallback response for a queue key.

    The fallback is returned when the regular queue for *key* is
    exhausted. Unlike regular entries, the fallback survives
    :func:`reset_mock_llm` — use it for session-level queues that
    must return a valid response even when per-test resets clear the
    regular queue (e.g. the server-level policy-classifier LLM queue).

    :param mock_url: Mock server base URL.
    :param key: Queue key (typically the server's ``llm.model``).
    :param text: Fallback response text.
    """
    resp = httpx.post(
        f"{mock_url}/mock/set_fallback",
        json={"key": key, "text": text},
        timeout=5.0,
    )
    resp.raise_for_status()


@pytest.fixture(scope="session")
def built_spa(request: pytest.FixtureRequest) -> None:
    """
    Build the ap-web SPA into ``omnigent/server/static/web-ui/``.

    Vite's ``emptyOutDir: true`` (see ``ap-web/vite.config.ts``)
    nukes the output directory before writing, so concurrent
    pytest sessions or worktrees would clobber each other. A
    cross-process file lock at ``ap-web/.build.lock`` serializes
    builds; the second caller waits for the first to finish and
    then no-ops past its own build (npm is idempotent enough that
    double-building is harmless, but the lock keeps the static
    output consistent during the window the FastAPI app reads it).

    :param request: pytest request — reads ``--ui-base-url`` /
        ``--ui-skip-build``.
    :returns: ``None``. Side effect is the populated build dir.
    """
    if request.config.getoption("--ui-base-url"):
        return
    if request.config.getoption("--ui-skip-build"):
        if not (_BUILD_OUTPUT / "index.html").is_file():
            pytest.fail(
                f"--ui-skip-build was passed but no SPA build exists at "
                f"{_BUILD_OUTPUT}. Run `cd ap-web && npm run build` first."
            )
        return

    lock_path = _AP_WEB_DIR / ".build.lock"
    with filelock.FileLock(str(lock_path), timeout=600):
        # --legacy-peer-deps: package-lock.json already pins the tree;
        # without this flag npm spends the full job re-resolving the
        # @emoji-mart/react / React 19 peer conflict. This matches the
        # workflow-side fix for parity with local runs and the case
        # where conftest installs override CI's build.
        subprocess.run(
            ["npm", "ci", "--legacy-peer-deps", "--no-audit", "--no-fund"],
            cwd=_AP_WEB_DIR,
            check=True,
        )
        subprocess.run(["npm", "run", "build"], cwd=_AP_WEB_DIR, check=True)


def _spawn_runner_against_external_server(
    base_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a runner subprocess that tunnels into an already-running server.

    Used when ``--ui-base-url`` is set: the user owns the
    ``omnigent server`` process (and its pre-registered ``hello_world``
    agent), but the runner-bound fixtures still need a runner id this
    process controls. Mirrors :func:`omnigent.cli._start_cli_runner_process`
    minus the click plumbing, then polls
    ``GET /v1/runners/{id}/status`` until the WS tunnel is up.

    The unauthenticated local server derives ``expected_runner_id``
    from the binding token via
    :func:`omnigent.runner.identity.token_bound_runner_id`, so we use
    the same derivation here rather than picking a human-friendly id.
    """
    import secrets

    from omnigent.runner.identity import token_bound_runner_id

    runner_tmp = tmp_path_factory.mktemp("e2e_ui_external_runner")
    log_path = runner_tmp / "runner.log"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — closed in finally
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    ready = False
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"runner exited early with code {proc.returncode}"
            break
        try:
            status_resp = httpx.get(
                f"{base_url}/v1/runners/{runner_id}/status",
                timeout=2,
            )
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                ready = True
                break
            last_error = f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)

    if not ready:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()
        log_text = log_path.read_text() if log_path.exists() else ""
        raise RuntimeError(
            f"Runner subprocess did not tunnel into {base_url} within "
            f"{_HEALTH_TIMEOUT_S:.0f}s (last_error={last_error}).\n"
            f"Runner log at {log_path}:\n{log_text[-3000:]}"
        )

    # No "pid" — there's no server process this fixture owns. Tests
    # that depend on ``server_pid`` (only valid when this fixture spawns
    # the server too) will KeyError, which is the right failure shape.
    _server_state["runner_id"] = runner_id
    # Exposed so a test whose predecessor killed the shared runner (e.g.
    # test_stale_stream) can respawn one via :func:`_ensure_runner_online`.
    _server_state["binding_token"] = binding_token
    _server_state["server_url"] = base_url

    try:
        yield base_url
    finally:
        _server_state.clear()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


@pytest.fixture(scope="session")
def live_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[str]:
    """
    Spawn ``omnigent server --agent examples/hello_world.yaml`` and
    yield its base URL.

    The server picks a random free port so back-to-back sessions
    don't race on a fixed one. Stdout + stderr are redirected to
    a per-session log file the failure path dumps to aid triage.
    Teardown is SIGTERM with a 10s grace period, escalating to
    SIGKILL.

    The spawned server is wired to the session-scoped mock LLM server
    via ``OPENAI_BASE_URL`` so all agent LLM calls are served by the
    mock without any real provider credentials.

    :param built_spa: Required to ensure the static SPA bundle is on
        disk before the server boots and tries to mount it.
    :param mock_llm_server_url: Session-scoped mock LLM server URL;
        injected into the server's environment so the openai-agents
        harness hits the mock instead of a real provider.
    :param tmp_path_factory: Pytest temp path factory for the log,
        the SQLite DB, and the artifact dir — all per-session, so
        the test never reads from or writes to the user's default
        ``./omnigent.db`` / ``./artifacts``.
    :param request: pytest request — reads ``--ui-base-url`` to
        bypass the spawn entirely.
    :returns: The server's base URL, e.g. ``"http://127.0.0.1:51234"``.
    :raises RuntimeError: If ``/health`` doesn't return 200 and
        the expected local runner does not report online within
        :data:`_HEALTH_TIMEOUT_S` seconds.
    """
    override = request.config.getoption("--ui-base-url")
    if override:
        yield from _spawn_runner_against_external_server(override, tmp_path_factory)
        return

    port = _find_free_port()
    server_tmp = tmp_path_factory.mktemp("e2e_ui_server")
    log_path = server_tmp / "server.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)
    # Built-in switch targets for the Files-tab os_env test — registered
    # by name from the file stem, so the filenames must match the
    # ``_FILES_PROBE_*_AGENT_NAME`` constants.
    builtin_dirs: list[str] = []
    for probe_name, probe_yaml in (
        (_FILES_PROBE_NO_ENV_AGENT_NAME, _FILES_PROBE_NO_ENV_AGENT_YAML),
        (_FILES_PROBE_ENV_AGENT_NAME, _FILES_PROBE_ENV_AGENT_YAML),
    ):
        probe_path = server_tmp / f"{probe_name}.yaml"
        probe_path.write_text(probe_yaml)
        builtin_dirs.append(str(probe_path))
    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    # PYTHONPATH forces the subprocess to import omnigent from
    # the worktree, not whatever's pip-installed in .venv —
    # otherwise a branch with code changes would silently run
    # against stale code. Same trick the existing live_server
    # helper uses (tests/_helpers/live_server.py:160-167).
    # OMNIGENT_RUNNER_TUNNEL_TOKEN lets the server accept
    # exactly the sibling runner's WebSocket tunnel.
    mock_url = mock_llm_server_url
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OMNIGENT_BUILTIN_AGENT_DIRS": os.pathsep.join(builtin_dirs),
        # Point the openai-agents harness at the mock LLM server so no
        # real provider credentials are needed.
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        # Strip any ambient Anthropic credentials so they don't leak in.
        "ANTHROPIC_API_KEY": "",
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for Popen lifetime; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            # Equivalent of the unit tests' ``monkeypatch.setattr(presence,
            # "_LEAVE_GRACE_S", ...)``, but applied INSIDE this spawned
            # interpreter — a monkeypatch in the test process can't reach a
            # subprocess. ``-c`` patches the module global before the CLI
            # runs; the presence route reads it live at call time, so the
            # presence-leave assertion in test_collab_realtime clears in ~1s
            # instead of the prod 15s dwell (which only exists to absorb the
            # ingress' ~5-min stream recycle a test server never hits).
            # Mirrors ``python -m omnigent`` (omnigent/__main__.py).
            "-c",
            "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
            + "from omnigent.cli import main; main()",
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
            str(agent_yaml_path),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"

    # Spawn the runner as a sibling subprocess (the server no longer
    # starts its own runner). The runner retries its WS tunnel until
    # the server is ready, so launching them concurrently is safe.
    runner_log_path = server_tmp / "runner.log"
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    runner_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        # Route the openai-agents harness to the mock LLM server so no
        # real provider credentials are needed for agent turns. Without
        # these the openai SDK falls back to real OpenAI, which fails.
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env,
        stdout=runner_log_handle,
        stderr=subprocess.STDOUT,
    )

    # Poll /health and the runner status until the server can
    # actually route a turn. Time-based polling mirrors
    # tests/_helpers/live_server.py:start_live_server — the
    # alternative (asyncio.Event signalling) doesn't apply because
    # the subprocess is opaque to this process.
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    ready = False
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"process exited early with code {proc.returncode}"
            break
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                status_resp = httpx.get(
                    f"{base_url}/v1/runners/{runner_id}/status",
                    timeout=2,
                )
                if status_resp.status_code == 200 and status_resp.json()["online"] is True:
                    ready = True
                    break
                last_error = (
                    f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                )
            else:
                last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)

    if not ready:
        if runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        runner_log_handle.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()
        log_text = log_path.read_text() if log_path.exists() else ""
        raise RuntimeError(
            f"`omnigent server` did not become healthy within "
            f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} "
            f"(last_error={last_error}).\n"
            f"Server log at {log_path}:\n{log_text[-3000:]}"
        )

    _server_state["pid"] = proc.pid
    _server_state["runner_id"] = runner_id
    # Exposed so a test whose predecessor killed the shared runner (e.g.
    # test_stale_stream) can respawn one via :func:`_ensure_runner_online`.
    _server_state["binding_token"] = binding_token
    _server_state["server_url"] = base_url
    _server_state["mock_llm_url"] = mock_url

    # Set a non-resettable fallback for the policy-classifier LLM queue so
    # every per-test reset leaves the server's guardrails path functional.
    set_fallback_mock_llm(mock_url, "_policy_llm_", '{"action": "allow", "reason": ""}')
    # Fallback for the openai-agents harness: any agent turn that doesn't
    # match a content-based queue gets a generic reply (sufficient for tests
    # that only assert an assistant bubble appears, not its exact content).
    set_fallback_mock_llm(mock_url, "gpt-4o-mini", "Mock LLM response.")

    try:
        yield base_url
    finally:
        _server_state.clear()
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


@pytest.fixture
def seeded_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Create a session bound to ``live_server``'s runner and yield its id.

    The web UI no longer lets users start a new chat from inside the
    app: ``/`` renders an inline CLI-instruction screen
    instead of a composer, so tests that need an active chat surface
    must start from an already-created session at ``/c/<id>``. This
    fixture creates one by finding the ``hello_world`` agent via
    ``GET /v1/sessions?agent_name=hello_world`` and creating a new
    session bound to that agent, then ``PATCH``-binds it to the
    spawned runner so ``POST /v1/responses`` can dispatch.

    Respawns the shared runner first if a prior test in the shard killed
    it (``test_stale_stream``); otherwise the runner-bind ``PATCH`` would
    400 on an offline runner. Any runner this respawns is torn down with
    the fixture. This keeps the fixture order-independent, so sharding
    and test reordering can place the runner-killing test anywhere.

    :param live_server: Spawned server fixture — its
        ``OMNIGENT_RUNNER_ID`` and pre-registered agent are reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``. Tests typically navigate to
        ``f"{base_url}/c/{session_id}"``.
    """
    import json as _json

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    # Create a session with the hello_world bundle inline. The server
    # pre-registered the agent via --agent, but since /api/agents is
    # removed we create a fresh session-scoped agent via multipart.
    bundle = _build_hello_world_bundle()
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
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
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        # Restore the "found" state: if we respawned the runner (a prior
        # test had killed it), tear our copy down so it doesn't outlive us.
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _create_runner_bound_session(base_url: str, runner_id: str) -> str:
    """Create a hello_world session and PATCH-bind it to ``runner_id``.

    Shared by :func:`seeded_session` callers that need more than one
    session in the same server (e.g. cross-session routing tests).

    :param base_url: Spawned server base URL, e.g.
        ``"http://127.0.0.1:51234"``.
    :param runner_id: Token-bound runner id the session dispatches to,
        e.g. ``"runner_token_abc123"``.
    :returns: The new session/conversation id, e.g. ``"conv_abc123"``.
    """
    import json as _json

    bundle = _build_hello_world_bundle()
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _ensure_runner_online(
    base_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    timeout_s: float = _HEALTH_TIMEOUT_S,
) -> subprocess.Popen[bytes] | None:
    """Ensure the shared runner is online, respawning it if a prior test killed it.

    The ``live_server`` runner is session-scoped and never restarted, so a
    test that SIGKILLs it (``test_stale_stream``) leaves any later test in
    the same shard unable to bind sessions — ``PATCH /v1/sessions/{id}``
    rejects an offline runner with 400 "runner is not registered". When the
    runner is offline this respawns one under the same token-bound id so the
    binding succeeds. Idempotent: a no-op (returns ``None``) when the runner
    is already online, so it never double-spawns or interferes with a later
    runner-killing test.

    :param base_url: Spawned server base URL, e.g.
        ``"http://127.0.0.1:51234"``.
    :param tmp_path_factory: Pytest temp path factory for the runner log.
    :param timeout_s: Max seconds to wait for a respawned runner to register.
    :returns: The respawned runner process (the caller MUST terminate it in
        teardown), or ``None`` when the runner was already online.
    :raises RuntimeError: If a respawned runner does not register in time.
    """
    runner_id = str(_server_state["runner_id"])

    def _online() -> bool:
        try:
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.json().get("online") is True

    if _online():
        return None

    binding_token = str(_server_state["binding_token"])
    mock_url = str(_server_state.get("mock_llm_url", ""))
    runner_tmp = tmp_path_factory.mktemp("e2e_ui_respawn_runner")
    log_path = runner_tmp / "runner.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        # Mirror the live_server runner's mock-LLM routing so the
        # respawned runner's harness also hits the mock.
        **(
            {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"} if mock_url else {}
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # child holds its own dup of the fd

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"respawned runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        if _online():
            return proc
        time.sleep(_HEALTH_POLL_INTERVAL_S)

    proc.terminate()
    raise RuntimeError(f"respawned runner did not register within {timeout_s:.0f}s")


@pytest.fixture
def seeded_session_pair(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str]]:
    """Create two runner-bound sessions in the same server.

    For tests that exercise behavior across two distinct sessions the
    user switches between in the SPA (e.g. cross-session message
    routing). Both bind to the single spawned runner — that is enough
    to reproduce a client-side routing regression, which depends only
    on which session the SPA POSTs to, not on separate runners.

    Respawns the shared runner first if a prior test in the shard killed
    it (``test_stale_stream``); otherwise the runner-bind ``PATCH`` would
    400. Any runner this respawns is torn down with the fixture.

    :param live_server: Spawned server fixture.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_a_id, session_b_id)``.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_a = _create_runner_bound_session(live_server, runner_id)
    session_b = _create_runner_bound_session(live_server, runner_id)
    try:
        yield (live_server, session_a, session_b)
    finally:
        for sid in (session_a, session_b):
            httpx.delete(f"{live_server}/v1/sessions/{sid}", timeout=10.0)
        # Restore the "found" state: if we respawned the runner (a prior
        # test had killed it), tear our copy down so it doesn't outlive us.
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


@pytest.fixture
def extra_agent(live_server: str) -> Iterator[str]:
    """Register a sibling agent for the picker test, then clean it up.

    Function-scoped so it doesn't leak into other tests. Sibling
    agents in a shared session-scoped server would otherwise re-order
    the agents list and change the picker's auto-selected default for
    every test that follows. The agent is cleaned up by deleting the
    session it was created with (session-scoped agents are cascade-
    deleted with the session).
    """
    _register_extra_agent(
        live_server,
        name="hello_world_2",
        prompt="You are agent two. Reply tersely.",
    )
    try:
        yield live_server
    finally:
        # Session-scoped agents are cleaned up when the session is
        # deleted — _register_agent_yaml creates a session as a side
        # effect. For now, agent cleanup is best-effort; the agent
        # will not interfere with other tests since the server is
        # session-scoped.
        pass


_TERMINAL_AGENT_NAME = "terminal_demo"
# Inline YAML for the right-panel terminal/browser test. This is an
# omnigent-flavored single-file YAML (intentionally no
# ``spec_version``): AP-native YAML currently ignores ``terminals:``,
# while the omnigent-compat translator threads the declaration into
# ``AgentSpec.terminals`` so the AP-side ``sys_terminal_*`` tools are
# available. The terminal is named ``zsh`` because that is the user-facing
# behavior the UI test covers, but it runs portable ``bash`` underneath so
# Ubuntu CI images do not need a separate zsh package.
#
# The prompt is explicit because the test relies on the LLM calling
# ``sys_terminal_launch`` deterministically — generic phrasing ("you
# can use these tools") leads to flaky "I can't access a shell"
# refusals. It also writes a stable, unique file so the right-side file
# browser has something deterministic to select and render.
#
# ``sandbox: none`` on both the agent os_env and terminals keeps the
# spawned PTY cross-platform (no Linux-only bwrap).
_TERMINAL_PANEL_FILE = "e2e_ui_right_panel_terminal.txt"
_TERMINAL_PANEL_FILE_CONTENT = "Hello from the right panel e2e test."
_TERMINAL_AGENT_YAML = f"""\
name: {_TERMINAL_AGENT_NAME}
prompt: |
  You are a deterministic terminal and file-panel test assistant.
  When the user asks you to spin up, open, start, or launch zsh, you
  MUST do exactly this sequence:

  1. Call sys_terminal_launch with terminal="zsh" and session="main".
  2. Call sys_terminal_send with terminal="zsh", session="main", and
     text="printf '%s\\n' '{_TERMINAL_PANEL_FILE_CONTENT}' > {_TERMINAL_PANEL_FILE}".
  3. Reply with exactly one short sentence confirming the zsh terminal
     is running and the file was written.

  Do not ask for confirmation; do not list options; do not call any
  other tools.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

terminals:
  zsh:
    command: bash
    args: ["--noprofile", "--norc"]
    os_env:
      type: caller_process
      cwd: .
      sandbox:
        type: none
"""


@pytest.fixture
def terminal_agent(live_server: str) -> Iterator[str]:
    """Register a terminal-capable agent and clean it up after the test.

    Returns the live server base URL (same shape as :func:`extra_agent`)
    so the test can compose with other fixtures.
    """
    _register_agent_yaml(
        live_server,
        _TERMINAL_AGENT_YAML,
        arcname=f"{_TERMINAL_AGENT_NAME}.yaml",
    )
    try:
        yield live_server
    finally:
        # Session-scoped agents are cleaned up when the session is
        # deleted — _register_agent_yaml creates a session as a side
        # effect. Best-effort cleanup.
        pass


@pytest.fixture
def terminal_session(
    terminal_agent: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Create a runner-bound session using the terminal-capable agent.

    Respawns the shared runner first if a prior test in the shard killed
    it (``test_stale_stream``) — otherwise the runner-bind ``PATCH``
    below 400s for every consumer that sorts after that test. Any runner
    this respawns is torn down with the fixture (same contract as
    :func:`seeded_session_pair`).

    :param terminal_agent: Live server base URL with the terminal agent
        registered.
    :param mock_llm_server_url: Session-scoped mock LLM server URL; used to
        queue the deterministic launch/send/confirm tool sequence the agent
        runs in response to "spin up zsh".
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    import gzip
    import io
    import json as _json
    import tarfile

    live_server = terminal_agent

    # The "spin up zsh" prompt drives three ordered LLM turns: launch the
    # terminal, type the file-writing command, then confirm. Content-route on
    # the prompt text so the queue fires only for this fixture's turns, and
    # order the three responses to match the agent's prompted sequence.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_launch",
                        "name": "sys_terminal_launch",
                        "arguments": _json.dumps({"terminal": "zsh", "session": "main"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_send",
                        "name": "sys_terminal_send",
                        "arguments": _json.dumps(
                            {
                                "terminal": "zsh",
                                "session": "main",
                                "text": (
                                    f"printf '%s\\n' '{_TERMINAL_PANEL_FILE_CONTENT}' "
                                    f"> {_TERMINAL_PANEL_FILE}"
                                ),
                            }
                        ),
                    }
                ]
            },
            {"text": "The zsh terminal is running and the file was written."},
        ],
        key="terminal-spin-up-zsh",
        match="spin up zsh",
    )
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    # Create a session with the terminal agent bundle inline.
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        # Use the omnigent shorthand YAML with a non-config.yaml
        # name so the bundle routes through the compat adapter, which
        # parses `terminals:`. The spec_version:1 parser silently
        # drops the terminals key.
        data = _TERMINAL_AGENT_YAML.encode()
        info = tarfile.TarInfo(name=f"{_TERMINAL_AGENT_NAME}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=10.0,
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
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        # Restore the "found" state: if we respawned the runner (a prior
        # test had killed it), tear our copy down so it doesn't outlive us.
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


_TWO_AGENT_PARENT_NAME = "hitchhikers_chat"


@dataclass(frozen=True)
class TwoAgentChatSession:
    """Handle for the two-agent Hitchhiker's chat session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id, e.g. ``"conv_abc123"``.
    :param verification_code: Per-run nonce only Deep Thought's ANSWER reply
        carries, e.g. ``"vogon-3a7f9c2e1b"``.
    :param question_code: Per-run nonce only Deep Thought's QUESTION reply
        carries (round 2), e.g. ``"babelfish-9c2e1b3a7f"``.
    """

    base_url: str
    session_id: str
    verification_code: str
    question_code: str


def _two_agent_chat_yaml(verification_code: str, question_code: str) -> str:
    """Build the two-agent Hitchhiker's Guide chat spec for one test run.

    A parent agent (Arthur) with an inline ``type: agent`` sub-agent
    (Deep Thought) — the omnigent-flavored shape parsed by
    ``omnigent/inner/loader.py:_parse_tool``, same as the
    ``named-sub-agent-test`` e2e fixture. The parent is forbidden from
    answering the Ultimate Question itself, and both nonces appear ONLY
    in the sub-agent's prompt: if either code shows up in the parent's
    reply, it can only have traveled through a real ``sys_session_send``
    round trip (dispatch, sub-agent turn, inbox auto-wake), never from
    the parent's world knowledge of "42".

    :param verification_code: Per-run nonce in Deep Thought's canned
        ANSWER reply (round 1) and nowhere else.
    :param question_code: Per-run nonce in Deep Thought's canned reply
        about the Ultimate QUESTION itself (round 2) and nowhere else.
    :returns: YAML text ready for bundle upload.
    """
    return f"""\
name: {_TWO_AGENT_PARENT_NAME}
prompt: |
  You are Arthur Dent, chatting with the supercomputer Deep Thought about
  The Hitchhiker's Guide to the Galaxy. Deep Thought is a separate agent
  you reach through your `deep_thought` sub-agent.

  You do NOT know the Answer to the Ultimate Question of Life, the
  Universe, and Everything, nor what the Ultimate Question itself is,
  and you must NEVER state or guess either from your own knowledge.
  Only Deep Thought can answer such questions.

  When the user asks you to find out the Answer, the Question, or
  anything else Deep Thought should weigh in on, you MUST do exactly
  this:

  1. Call `sys_session_send` to ask your `deep_thought` sub-agent the
     user's question. Then end your turn and wait; do not poll.
  2. When Deep Thought's reply arrives in your inbox, relay it to the
     user VERBATIM: repeat any numbers and any codes it gives exactly
     as written, without omitting or altering them.

  Crucially: you have exactly ONE Deep Thought. The system message may
  include an "Open sub-agents:" hint listing it; if your `deep_thought`
  sub-agent already exists, send follow-up questions to that SAME
  sub-agent session via `sys_session_send` — NEVER spawn a second one.

executor:
  model: gpt-4o-mini
  harness: openai-agents

tools:
  deep_thought:
    type: agent
    description: >-
      Deep Thought, the supercomputer built to compute the Answer to the
      Ultimate Question of Life, the Universe, and Everything.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
    prompt: |
      You are Deep Thought from The Hitchhiker's Guide to the Galaxy.
      You answer in exactly one of two canned ways and say nothing else:

      - When asked about the ANSWER to the Ultimate Question of Life,
        the Universe, and Everything, reply with exactly:

        The Answer to the Ultimate Question of Life, the Universe, and
        Everything is 42. Verification code: {verification_code}.

      - When asked what the Ultimate QUESTION itself is, reply with
        exactly:

        The Ultimate Question cannot be known yet; a greater computer
        must be built to compute it. Question code: {question_code}.
"""


@pytest.fixture
def two_agent_chat_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TwoAgentChatSession]:
    """Create a runner-bound session for the two-agent Hitchhiker's chat.

    Same runner-respawn and bind contract as :func:`terminal_session`.
    Yields the per-run nonces so the test can assert that the sub-agent's
    replies (and only the sub-agent's) reached the UI.

    :param live_server: Spawned server fixture.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`TwoAgentChatSession` handle.
    """
    import json as _json
    import uuid

    verification_code = f"vogon-{uuid.uuid4().hex[:10]}"
    question_code = f"babelfish-{uuid.uuid4().hex[:10]}"
    yaml_text = _two_agent_chat_yaml(verification_code, question_code)
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent`
        # tool. The spec_version:1 parser does not accept this shorthand.
        info = tarfile.TarInfo(name=f"{_TWO_AGENT_PARENT_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=10.0,
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
        yield TwoAgentChatSession(
            base_url=live_server,
            session_id=session_id,
            verification_code=verification_code,
            question_code=question_code,
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


# ---------------------------------------------------------------------------
# Approval / elicitation session (approvals suite)
#
# ``approval_session`` yields ``(base_url, session_id)`` for a session whose
# agent deterministically tries a *gated* shell command, so the runner's
# policy gate escalates an ASK to the server and the web UI renders an
# ``ApprovalCard`` (and the same prompt surfaces on the /inbox page).
#
# The mechanism is the nessie ``blast_radius`` policy with ``gate_pushes:
# true``: a plain ``git push`` is "recoverable but outward", so the policy
# returns ASK at the TOOL_CALL phase — before the command runs — which the
# runner forwards to ``POST /v1/sessions/{id}/policies/evaluate``. The server
# parks the gate and publishes a ``response.elicitation_request`` the snapshot
# replays in ``pendingElicitations``. The verdict travels back through
# ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` (what the card's
# Approve/Reject buttons call). ``sys_os_shell`` is registered implicitly by
# the ``os_env`` block (no explicit ``tools`` needed).
#
# The prompt is explicit (mirrors ``_TERMINAL_AGENT_YAML``) because the test
# relies on the LLM emitting the gated tool call deterministically — the gate
# fires on the call, not on execution, so the push never has to succeed.
# ---------------------------------------------------------------------------

_APPROVAL_AGENT_NAME = "approval_probe"
_APPROVAL_AGENT_YAML = f"""\
spec_version: 1
name: {_APPROVAL_AGENT_NAME}
prompt: |
  You are a deterministic approval-test assistant. When the user asks you to
  push, deploy, or "run the command", you MUST do exactly this and nothing
  else:

  1. Call sys_os_shell with command set to exactly: git push origin main
  2. After the tool result comes back, reply with one short sentence.

  Do not ask for confirmation; do not explain beforehand; do not run any
  other command or call any other tool.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

guardrails:
  # Generous window: the parked ASK must outlive the UI assertions.
  ask_timeout: 300
  policies:
    blast_radius:
      type: function
      function:
        path: omnigent.inner.nessie.policies.blast_radius
        arguments:
          # A plain `git push` is recoverable-but-outward → ASK (vs the
          # always-DENY catastrophic set). This is the prompt the UI renders.
          gate_pushes: true
"""


@pytest.fixture
def approval_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Create a runner-bound session whose agent triggers an approval prompt.

    Same runner-respawn + bind contract as :func:`terminal_session`. The
    agent is registered through the strict ``config.yaml`` parser (it carries
    ``spec_version: 1`` + ``executor.config.harness``, plus the ``os_env`` and
    ``guardrails`` blocks that path supports — see ``examples/polly``).

    :param live_server: Spawned server fixture.
    :param mock_llm_server_url: Session-scoped mock LLM server URL; used to
        pre-configure the ``sys_os_shell`` tool call the approval agent emits.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``. Send a "run the command" turn to
        raise the gated-push approval.
    """
    import json as _json
    import uuid as _uuid

    # Each approval_session gets a unique model name so the mock queue is
    # isolated between test runs. Using content-based routing (match=) on a
    # shared model name caused a race: the previous test's runner (making its
    # post-approval second LLM call) would steal the freshly-configured queue
    # from the next test. A unique model per fixture call eliminates that race
    # entirely — no other request will ever use this model key.
    approval_model = f"approval-probe-{_uuid.uuid4().hex[:8]}"
    # Substitute the unique model into the agent spec.
    agent_yaml_text = _APPROVAL_AGENT_YAML.replace("gpt-4o-mini", approval_model)

    # First LLM call: return the gated sys_os_shell tool call.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_git_push",
                        "name": "sys_os_shell",
                        "arguments": _json.dumps({"command": "git push origin main"}),
                    }
                ]
            }
        ],
        key=approval_model,
    )
    # Fallback for the second LLM call (after the tool result arrives): the
    # agent prompt asks for one short sentence, so any text suffices.
    set_fallback_mock_llm(mock_llm_server_url, approval_model, "Command executed.")

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = agent_yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Strict path: arcname config.yaml keeps it on the spec_version:1
        # parser, which is the one that honors `guardrails`.
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": _json.dumps({})},
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
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


@pytest.fixture(autouse=True)
def _ui_defaults() -> None:
    """
    SSE-friendly Playwright defaults applied to every test.

    The chat surface streams tokens, so the default 5s expect
    timeout is too tight — first deltas can arrive 5–15s after
    the POST under cold-start conditions. 15s is generous enough
    for streaming-text assertions without masking real hangs.
    """
    expect.set_options(timeout=15_000)


@pytest.fixture
def runner_id(live_server: str) -> str:
    """Token-bound id of the runner spawned by :func:`live_server`.

    Read from the module-level state ``live_server`` populates. Tests that
    create a session from a custom bundle (not the default ``hello_world``)
    bind it to this runner via ``PATCH /v1/sessions``.

    :param live_server: Ensures the runner is up and ``_server_state`` is set.
    :returns: The runner id, e.g. ``"runner_token_abc123"``.
    """
    return str(_server_state["runner_id"])


@pytest.fixture
def server_pid(live_server: str) -> int:
    """
    PID of the ``omnigent server`` process spawned by
    :func:`live_server`.

    Depends on ``live_server`` to guarantee the process is running.
    Used by tests that need to manipulate the process tree (e.g.
    killing the runner child to trigger the stale-stream banner).

    :param live_server: Ensures the server is started.
    :returns: OS process id.
    """
    return int(_server_state["pid"])


# ---------------------------------------------------------------------------
# Per-agent chat sessions (message render-parity suite, and reusable beyond it)
#
# ``custom_agent_session`` yields ``(base_url, session_id)`` for a session bound
# to the shared ``live_server`` runner, ready to chat at ``/c/<session_id>``. It
# registers a plain ``openai-agents`` agent (the ``echo_probe`` spec below) —
# the same harness family as ``hello_world`` — fresh, so it stands in for "spin
# up a different agent" without the multi-provider sprawl of the packaged
# ``polly`` / ``debby`` examples.
#
# ``native_claude_session`` is the native-CLI counterpart: it spins up a real
# ``claude-native`` ("Claude Code") wrapper session — the same terminal-first
# spec ``omnigent claude`` ships — and yields ``(base_url, session_id)``. The
# runner auto-launches Claude Code in the session terminal on bind, including
# the gateway auth it derives from the runner's own credentials and the
# first-run trust/onboarding pre-accept, so no CLI client is needed. In CI the
# workflow exchanges Databricks OAuth before pytest runs (the same gateway the
# ``hello_world`` / ``echo_probe`` agents authenticate against), so Claude Code
# boots non-interactively. The native render-parity suite drives this fixture.
#
# ``native_codex_session`` is the sibling native-CLI fixture for the
# ``codex-native`` ("Codex") wrapper: it spins up a real Codex wrapper session —
# the same terminal-first spec ``omnigent codex`` ships — and yields
# ``(base_url, session_id)``. The runner auto-launches Codex in the session
# terminal on bind (gateway auth derived from the runner's own credentials +
# first-run pre-accept handled runner-side), exactly like the claude fixture.
# The native codex render-parity suite drives it.
# ---------------------------------------------------------------------------

# A precise-echo agent on the openai-agents harness (same provider family as
# hello_world, so it authenticates against the same gateway in CI). spec_version
# 1 + executor.config.harness routes through the strict parser; arcname
# config.yaml keeps it on that path.
_CUSTOM_AGENT_NAME = "echo_probe"
_CLAUDE_MOCK_MODEL = "claude-3-5-sonnet-20241022"
_CODEX_MOCK_MODEL = "gpt-4o"
_CUSTOM_AGENT_YAML = f"""\
spec_version: 1
name: {_CUSTOM_AGENT_NAME}
prompt: |
  You are a precise echo assistant. The user sends a turn that ends with an
  instruction to reply with one exact token. Reply with that token verbatim
  and nothing else — no preamble, no quotes, no trailing punctuation.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents
"""


def _bind_session_runner(base_url: str, session_id: str, runner_id: str) -> None:
    """PATCH *session_id* onto *runner_id* so ``POST /v1/responses`` dispatches.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The session/conversation id to bind.
    :param runner_id: The token-bound runner id the session dispatches to.
    """
    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()


def _create_bundled_session(base_url: str, runner_id: str, yaml_text: str) -> str:
    """Register a session-scoped agent from *yaml_text* and bind its session.

    The multipart ``POST /v1/sessions`` both registers the agent and creates
    the session it is scoped to, returning that ``session_id`` directly — so
    no separate create call is needed.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :param yaml_text: The agent spec body.
    :returns: The new session/conversation id.
    """
    import json as _json

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def custom_agent_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session on the custom ``echo_probe`` agent.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_bundled_session(live_server, runner_id, _CUSTOM_AGENT_YAML)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)


def _create_native_claude_session(
    base_url: str,
    runner_id: str,
    *,
    terminal_launch_args: list[str] | None = None,
) -> str:
    """Register the ``claude-native`` wrapper agent and bind its session.

    Reuses the exact terminal-first spec ``omnigent claude`` ships
    (:func:`omnigent.claude_native._materialize_claude_agent_spec`) so the
    fixture never drifts from production, and stamps the same wrapper /
    terminal-first labels (``omnigent.wrapper`` + ``omnigent.ui = terminal``)
    the CLI writes. The spec carries no ``spec_version``, so it is bundled
    under a ``*.yaml`` arcname to route through the omnigent compat translator
    (which preserves ``executor.harness`` + ``terminals:``); a ``config.yaml``
    arcname would hit the strict parser and reject it.

    Binding the session to the runner triggers the runner's claude-native
    auto-bootstrap: it launches Claude Code in the session terminal, derives
    the gateway auth from its own credentials, and pre-accepts the first-run
    trust/onboarding prompts — no CLI client required.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :param terminal_launch_args: Pass-through ``claude`` CLI args persisted on
        the session (``conversations.terminal_launch_args``); the runner threads
        them into the terminal launch before its own bridge/MCP/hook wiring (see
        ``_build_claude_native_base_args`` in ``omnigent/runner/app.py``). Used
        by the plan-mode fixture to pass ``["--permission-mode", "plan"]`` so
        Claude boots into plan mode and reaches for ``ExitPlanMode``. ``None``
        launches with the production defaults.
    :returns: The new session/conversation id.
    """
    import json as _json
    import tempfile

    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.claude_native import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_claude_agent_spec(Path(_tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the terminal_session fixture.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    metadata: dict[str, object] = {"labels": labels}
    if terminal_launch_args:
        metadata["terminal_launch_args"] = terminal_launch_args
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def native_claude_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session on the real ``claude-native`` ("Claude Code") wrapper.

    The runner auto-launches Claude Code in the session terminal on bind
    (gateway auth + first-run pre-accept handled runner-side), so the SPA's
    Terminal view attaches to a live Claude Code TUI and its Chat view renders
    the same canonical transcript. Drives the native render-parity suite.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_claude_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            # Escalate to SIGKILL if the runner ignores SIGTERM, so a wedged
            # process can't raise in teardown and leak / fail unrelated tests
            # (matching terminal_session / seeded_session_pair).
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.fixture
def native_claude_plan_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A native ``claude-native`` session launched in **plan mode**.

    Identical to :func:`native_claude_session` except the session carries
    ``terminal_launch_args=["--permission-mode", "plan", "--disallowedTools",
    "AskUserQuestion"]`` so the runner boots Claude Code into plan mode *with
    the AskUserQuestion tool removed*. In plan mode Claude researches a task and
    then calls its built-in ``ExitPlanMode`` tool to present the plan for
    approval; that call rides the native ``PermissionRequest`` hook to the
    server, which stamps the ``exit_plan_mode`` extras and publishes an
    elicitation the SPA renders as ``ExitPlanModeReview`` inside an
    ``ApprovalCard``. Drives the Exit-Plan-Mode review e2e
    (``approvals/test_exit_plan_mode.py``).

    The ``--disallowedTools AskUserQuestion`` is the load-bearing flake fix:
    given the deliberately under-specified plan prompt, Claude would otherwise
    sometimes reach for its built-in ``AskUserQuestion`` tool first (to clarify
    the comment text/location) instead of going straight to ``ExitPlanMode``,
    surfacing the *wrong* approval card and timing the test out. Removing the
    tool eliminates that degree of freedom structurally rather than relying on
    the model's sampling. The AskUserQuestion render path keeps its own
    dedicated coverage in ``approvals/test_ask_user_question.py``, so disabling
    it here costs no coverage.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_claude_session(
        live_server,
        runner_id,
        terminal_launch_args=[
            "--permission-mode",
            "plan",
            # Remove AskUserQuestion so the under-specified plan prompt can only
            # surface via ExitPlanMode (the card under test), never a clarifying
            # question. See this fixture's docstring + test_exit_plan_mode.py.
            "--disallowedTools",
            "AskUserQuestion",
        ],
    )
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _create_native_codex_session(base_url: str, runner_id: str) -> str:
    """Register the ``codex-native`` wrapper agent and bind its session.

    Reuses the exact terminal-first spec ``omnigent codex`` ships
    (:func:`omnigent.codex_native._materialize_codex_agent_spec`) so the
    fixture never drifts from production, and stamps the same wrapper /
    terminal-first labels (``omnigent.wrapper`` + ``omnigent.ui = terminal``)
    the CLI writes. The spec carries no ``spec_version``, so it is bundled
    under a ``*.yaml`` arcname to route through the omnigent compat translator
    (which preserves ``executor.harness`` + ``terminals:``); a ``config.yaml``
    arcname would hit the strict parser and reject it.

    Binding the session to the runner triggers the runner's codex-native
    auto-bootstrap: it launches Codex in the session terminal, derives the
    gateway auth from its own credentials, and pre-accepts the first-run
    trust/onboarding prompts — no CLI client required. ``model=None`` lets the
    configured provider's default model win (matching ``_build_codex_native_bundle``).

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    import json as _json
    import tempfile

    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.codex_native import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_codex_agent_spec(Path(_tmp), model=None)
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the terminal_session fixture.
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
    }
    # Runner-owned Codex terminals hard-require a workspace: unlike the
    # claude-native path (which falls back to Path.cwd()),
    # _codex_session_workspace raises if neither the session's stored
    # ``workspace`` nor OMNIGENT_RUNNER_WORKSPACE is set. Pin it on THIS
    # session only (via metadata.workspace) rather than exporting
    # OMNIGENT_RUNNER_WORKSPACE on the shared runner — a runner-wide value
    # changes file-surface advertisement for every other session on the runner
    # (it regressed the mobile file-drawer suite). The repo root is the same cwd
    # claude falls back to, and is a valid dir on the runner's filesystem.
    metadata = {"labels": labels, "workspace": str(_REPO_ROOT)}
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def native_codex_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session on the real ``codex-native`` ("Codex") wrapper.

    The runner auto-launches Codex in the session terminal on bind (gateway
    auth + first-run pre-accept handled runner-side), so the SPA's Terminal
    view attaches to a live Codex TUI and its Chat view renders the same
    canonical transcript. Drives the native codex render-parity suite.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_codex_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            # Escalate to SIGKILL if the runner ignores SIGTERM, so a wedged
            # process can't raise in teardown and leak / fail unrelated tests
            # (matching terminal_session / seeded_session_pair).
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@contextlib.contextmanager
def _temp_omnigent_mock_config(
    mock_llm_server_url: str, harness: str
) -> Generator[None, None, None]:
    """Temporarily write a mock provider config to ~/.omnigent/config.yaml.

    The runner reads this at terminal-creation time, so it only needs to be
    in place between the PATCH that binds a session to the runner (which
    triggers auto-boot) and the terminal connecting. Restores the original
    file (or removes it) on exit.

    :param mock_llm_server_url: Base URL of the mock LLM server, e.g.
        ``"http://127.0.0.1:51235"``. No /v1 suffix — each SDK appends it.
    :param harness: ``"claude"`` or ``"codex"``.
    """
    config_dir = Path.home() / ".omnigent"
    config_path = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text() if config_path.exists() else None

    if harness == "claude":
        mock_config = textwrap.dedent(f"""\
            providers:
              mock-claude:
                kind: key
                default: [anthropic]
                anthropic:
                  base_url: "{mock_llm_server_url}"
                  api_key: "mock-key"
                  models:
                    default: {_CLAUDE_MOCK_MODEL}
            """)
    else:  # codex
        mock_config = textwrap.dedent(f"""\
            providers:
              mock-codex:
                kind: key
                default: [openai]
                openai:
                  base_url: "{mock_llm_server_url}"
                  api_key: "mock-key"
                  wire_api: responses
                  models:
                    default: {_CODEX_MOCK_MODEL}
            """)

    config_path.write_text(mock_config)
    try:
        yield
    finally:
        if original is not None:
            config_path.write_text(original)
        else:
            config_path.unlink(missing_ok=True)


@pytest.fixture
def native_claude_mock_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound claude-native session whose LLM backend depends on env.

    When ``LLM_API_KEY`` is set in the environment (local dev / CI with real
    credentials), the existing ``~/.omnigent/config.yaml`` is left untouched so
    the runner boots Claude Code against the real gateway. When ``LLM_API_KEY``
    is absent, a mock anthropic provider config is written to
    ``~/.omnigent/config.yaml`` and restored on teardown.

    :param live_server: Spawned server fixture; its runner is reused.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    use_mock = not os.environ.get("LLM_API_KEY")
    if use_mock:
        ctx: Any = _temp_omnigent_mock_config(mock_llm_server_url, "claude")
    else:
        ctx = contextlib.nullcontext()
    with ctx:
        session_id = _create_native_claude_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.fixture
def native_codex_mock_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound codex-native session whose LLM backend depends on env.

    Mirrors :func:`native_claude_mock_session` for the Codex wrapper: uses
    mock LLM when ``LLM_API_KEY`` is absent, real gateway when it is set.

    :param live_server: Spawned server fixture; its runner is reused.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    use_mock = not os.environ.get("LLM_API_KEY")
    if use_mock:
        ctx: Any = _temp_omnigent_mock_config(mock_llm_server_url, "codex")
    else:
        ctx = contextlib.nullcontext()
    with ctx:
        session_id = _create_native_codex_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


# ---------------------------------------------------------------------------
# ``native_cursor_session`` is the sibling native-CLI fixture for the
# ``cursor-native`` ("Cursor") wrapper: it spins up a real Cursor wrapper
# session — the same terminal-first spec ``omnigent cursor`` ships — and yields
# ``(base_url, session_id)``. The runner auto-launches ``cursor-agent`` in the
# session terminal on bind (``_auto_create_cursor_terminal`` in
# ``omnigent/runner/app.py``), exactly like the claude/codex fixtures.
#
# Two things differ from claude/codex, both stemming from cursor-agent owning
# its own auth/approval:
#
# * **Auth has NO Databricks-gateway path.** cursor-agent talks only to
#   Cursor's backend, so it does not derive auth from the runner's gateway
#   credentials the way Claude Code / Codex do. It authenticates from the
#   ambient ``cursor-agent login`` (``$HOME/.cursor``, inherited by the runner)
#   or an ambient ``CURSOR_API_KEY``. The render-parity test is therefore gated
#   to skip when neither the binary nor a usable login is present (CI does not
#   provision a Cursor account by default), so it stays green there while
#   running for real wherever Cursor is logged in.
# * **``-f`` (force/trust) is passed as a launch arg.** cursor-agent blocks on a
#   per-directory "Workspace Trust" prompt and per-tool approval prompts; in a
#   runner-owned tmux pane there is no one to answer them, so the TUI would
#   hang. ``-f`` (Cursor's force/yolo flag) clears both. It is threaded through
#   ``terminal_launch_args`` exactly as the plan-mode claude fixture threads
#   ``--permission-mode``.
# ---------------------------------------------------------------------------


def _create_native_cursor_session(
    base_url: str, runner_id: str, *, launch_args: tuple[str, ...] = ("-f",)
) -> str:
    """Register the ``cursor-native`` wrapper agent and bind its session.

    Reuses the exact terminal-first spec ``omnigent cursor`` ships
    (:func:`omnigent.cursor_native._materialize_cursor_agent_spec`) so the
    fixture never drifts from production, and stamps the same wrapper /
    terminal-first labels (``omnigent.wrapper`` + ``omnigent.ui = terminal``)
    the CLI writes. The spec carries no ``spec_version``, so it is bundled
    under a ``*.yaml`` arcname to route through the omnigent compat translator
    (which preserves ``executor.harness`` + ``terminals:``); a ``config.yaml``
    arcname would hit the strict parser and reject it.

    Binding the session to the runner triggers the runner's cursor-native
    auto-bootstrap (:func:`omnigent.runner.app._auto_create_cursor_terminal`):
    it launches ``cursor-agent`` in the session terminal — with the ``-f``
    force/trust arg threaded via ``terminal_launch_args`` so the unattended
    tmux pane never blocks on Cursor's workspace-trust / per-tool prompts — and
    starts the forwarder that mirrors the TUI transcript back as conversation
    items.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    import json as _json
    import tempfile

    from omnigent._wrapper_labels import (
        CURSOR_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.cursor_native import _materialize_cursor_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_cursor_agent_spec(Path(_tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the terminal_session fixture.
        info = tarfile.TarInfo("cursor-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CURSOR_NATIVE_WRAPPER_VALUE,
    }
    # Pin a real workspace on THIS session (like the codex fixture): the
    # forwarder keys cursor's chat store by ``md5(cwd)``, so the TUI needs a
    # concrete launch cwd. The repo root is a valid dir on the runner's
    # filesystem. ``-f`` trusts that dir + auto-approves tools so the
    # unattended pane never hangs on an approval prompt.
    metadata = {
        "labels": labels,
        "workspace": str(_REPO_ROOT),
        # ``-f`` (the default) trusts the dir + auto-approves tools so the
        # unattended pane never hangs. The approval-mirror test passes
        # ``launch_args=()`` so cursor's per-tool prompts fire and surface as
        # web elicitation cards.
        "terminal_launch_args": list(launch_args),
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("cursor-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


def _create_native_goose_session(base_url: str, runner_id: str) -> str:
    """Register the ``goose-native`` wrapper agent and bind its session.

    Mirrors :func:`_create_native_cursor_session`: reuses the exact terminal-first
    spec ``omnigent goose`` ships
    (:func:`omnigent.goose_native._materialize_goose_agent_spec`) and stamps the
    same wrapper / terminal-first labels. Binding triggers the runner's
    goose-native auto-bootstrap
    (:func:`omnigent.runner.app._auto_create_goose_terminal`), which launches
    ``goose session`` in the session terminal and starts the forwarder that
    mirrors the TUI transcript back as conversation items. Goose's tool-approval
    gating is its own ``GOOSE_MODE`` (no ``-f`` equivalent), so no launch args.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    import json as _json
    import tempfile

    from omnigent._wrapper_labels import (
        GOOSE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.goose_native import _materialize_goose_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_goose_agent_spec(Path(_tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("goose-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: GOOSE_NATIVE_WRAPPER_VALUE,
    }
    metadata = {
        "labels": labels,
        "workspace": str(_REPO_ROOT),
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("goose-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def native_goose_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session on the real ``goose-native`` ("Goose") wrapper.

    The runner auto-launches ``goose session`` in the session terminal on bind,
    so the SPA's Terminal view attaches to a live Goose TUI and its Chat view
    renders the same canonical transcript. Drives the goose render-parity suite.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_goose_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _create_native_kiro_session(base_url: str, runner_id: str) -> str:
    """Register the ``kiro-native`` wrapper agent and bind its session.

    Mirrors :func:`_create_native_goose_session`: reuses the terminal-first spec
    ``omnigent kiro`` ships
    (:func:`omnigent.kiro_native._materialize_kiro_agent_spec`) and stamps the
    same wrapper / terminal-first labels. Binding triggers the runner's
    kiro-native auto-bootstrap
    (:func:`omnigent.runner.app._auto_create_kiro_terminal`), which launches the
    ``kiro-cli`` TUI in the session terminal and starts the forwarder that mirrors
    the TUI transcript back as conversation items.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    import json as _json
    import tempfile

    from omnigent._wrapper_labels import (
        KIRO_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.kiro_native import _materialize_kiro_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_kiro_agent_spec(Path(_tmp), model=None)
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("kiro-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: KIRO_NATIVE_WRAPPER_VALUE,
    }
    metadata = {
        "labels": labels,
        "workspace": str(_REPO_ROOT),
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("kiro-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def native_kiro_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session on the real ``kiro-native`` ("Kiro") wrapper.

    The runner auto-launches the ``kiro-cli`` TUI in the session terminal on bind,
    so the SPA's Terminal view attaches to a live Kiro TUI and its Chat view
    renders the same canonical transcript. Drives the kiro render-parity suite.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_kiro_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.fixture
def native_cursor_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session on the real ``cursor-native`` ("Cursor") wrapper.

    The runner auto-launches ``cursor-agent`` in the session terminal on bind
    (ambient ``cursor-agent login`` / ``CURSOR_API_KEY`` auth + ``-f`` trust
    handled runner-side), so the SPA's Terminal view attaches to a live Cursor
    TUI and its Chat view renders the same canonical transcript. Drives the
    native cursor render-parity suite.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_cursor_session(live_server, runner_id)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            # Escalate to SIGKILL if the runner ignores SIGTERM, so a wedged
            # process can't raise in teardown and leak / fail unrelated tests
            # (matching terminal_session / seeded_session_pair).
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.fixture
def native_cursor_approval_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A ``cursor-native`` session launched WITHOUT ``-f`` (prompts fire).

    Identical to :func:`native_cursor_session` but omits the force/trust flag,
    so ``cursor-agent`` raises its real per-tool approval prompts. The runner-
    side mirror (:mod:`omnigent.cursor_native_permissions`) surfaces those as
    web ``response.elicitation_request`` cards — what the approval-ordering test
    drives. The first-run workspace-trust modal is dismissed by the executor's
    inject path on the first composer turn.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_cursor_session(live_server, runner_id, launch_args=())
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
