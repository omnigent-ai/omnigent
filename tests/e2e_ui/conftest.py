"""Fixtures for browser-driven e2e tests of the ap-web SPA.

The suite spawns a real ``omnigent server --agent`` subprocess against
``examples/hello_world.yaml`` and drives the rendered SPA with
Playwright. The agent calls a real LLM, so the suite is excluded from
the default ``pytest`` run via ``--ignore=tests/e2e_ui`` in
``pyproject.toml`` and gated to ``workflow_dispatch`` in CI for now.

Local usage::

    # one-time setup
    uv sync --extra e2e-ui
    uv run playwright install --with-deps chromium

    # run against a freshly built SPA + spawned server
    uv run pytest tests/e2e_ui -v

    # iterate against an already-running dev server
    cd ap-web && npm run dev &
    omnigent server --agent examples/hello_world.yaml &
    uv run pytest tests/e2e_ui --ui-base-url http://127.0.0.1:5173

``omnigent server`` is documented at ``omnigent/cli.py:server``:
it spins up uvicorn with the Omnigent app and spawns an out-of-process
runner that reconnects over the WebSocket tunnel. The fixture passes
``--database-uri`` and ``--artifact-location`` pointing at the
pytest tmp dir so the test never touches the user's default
``sqlite:///omnigent.db`` / ``./artifacts``.
"""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import filelock
import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]


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
# executor.type is 'omnigent'``. The legacy ``serve --omnigent`` path
# filled ``model: databricks-gpt-5-4`` in via ``_apply_overrides_to_yaml``
# and let harness auto-pick resolve it to ``openai-agents``; we mirror
# the same effective config here so the test agent is byte-identical
# to what the previous fixture spawned.
_TEST_AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: databricks-gpt-5-4
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
  model: databricks-gpt-5-4
  config:
    harness: openai-agents
"""
_FILES_PROBE_ENV_AGENT_YAML = f"""\
name: {_FILES_PROBE_ENV_AGENT_NAME}
prompt: You are a terse assistant with a filesystem.

executor:
  model: databricks-gpt-5-4
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
            "at this URL instead. Useful when iterating with `npm run dev` + "
            "a long-lived `omnigent server` process."
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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

    The agent calls a real LLM. The hello_world spec defaults to
    Databricks-hosted Claude via the FM API, so locally the user's
    ``~/.databrickscfg`` must have a working profile; in CI the
    workflow exchanges OAuth credentials before pytest runs.

    :param built_spa: Required to ensure the static SPA bundle is on
        disk before the server boots and tries to mount it.
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
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OMNIGENT_BUILTIN_AGENT_DIRS": os.pathsep.join(builtin_dirs),
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for Popen lifetime; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
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
  model: databricks-gpt-5-4
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
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    import gzip
    import io
    import json as _json
    import tarfile

    live_server = terminal_agent
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
  model: databricks-gpt-5-4
  harness: openai-agents

tools:
  deep_thought:
    type: agent
    description: >-
      Deep Thought, the supercomputer built to compute the Answer to the
      Ultimate Question of Life, the Universe, and Everything.
    executor:
      model: databricks-gpt-5-4
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
# Native CLI built-ins (``claude-native-ui`` / ``codex-native-ui``) are not
# wired up here yet: driving the real vendor TUI in a PTY needs CI-side setup
# (gateway auth + Claude Code first-run state) owned by the native-harness CI
# enablement work. Add those fixtures back alongside that effort.
# ---------------------------------------------------------------------------

# A precise-echo agent on the openai-agents harness (same provider family as
# hello_world, so it authenticates against the same gateway in CI). spec_version
# 1 + executor.config.harness routes through the strict parser; arcname
# config.yaml keeps it on that path.
_CUSTOM_AGENT_NAME = "echo_probe"
_CUSTOM_AGENT_YAML = f"""\
spec_version: 1
name: {_CUSTOM_AGENT_NAME}
prompt: |
  You are a precise echo assistant. The user sends a turn that ends with an
  instruction to reply with one exact token. Reply with that token verbatim
  and nothing else — no preamble, no quotes, no trailing punctuation.

executor:
  model: databricks-gpt-5-4
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
