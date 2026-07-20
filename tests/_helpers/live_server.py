"""
Shared fixtures for tests that need a live ``omnigent.cli server`` subprocess.

Lifted out of ``tests/e2e/conftest.py`` so the inner test suite
(``tests/inner/test_integration.py`` running with Omnigent mode) can
reuse the same machinery without duplication. The e2e conftest
re-exports from here.

Provides three primitives:

- :func:`find_free_port` — pick a free TCP port for the server.
- :func:`make_live_server_fixture` — factory that builds a
  session-scoped pytest fixture starting a real
  ``omnigent.cli server`` subprocess. The subprocess inherits
  per-harness env vars per the harness/profile selection.
- :func:`upload_agent` — tar+gzip an agent directory and upload
  via multipart ``POST /v1/sessions``.

Per-harness env-var routing
---------------------------

The server inherits credentials by harness:

- ``--harness=open-responses`` / ``--harness=openai-agents`` →
  ``OPENAI_API_KEY=<--llm-api-key>``.
- ``--harness=claude-sdk`` → ``ANTHROPIC_API_KEY=<--llm-api-key>``,
  OR Databricks-routed via ``HARNESS_CLAUDE_SDK_GATEWAY=true`` +
  ``HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE=<--profile>`` when
  ``--profile`` is set.
- ``--harness=codex`` → ``CODEX_API_KEY=<--llm-api-key>`` (or its
  Databricks variant when ``--profile`` is set).
- ``--harness=databricks`` → server reads from the Databricks CLI
  profile; no extra env var needed beyond the user's
  ``DATABRICKS_CONFIG_PROFILE`` / standard CLI auth.

Adding a new harness is one entry in :func:`_compute_harness_env`.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable

# Project root — this file lives at tests/_helpers/live_server.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def find_free_port() -> int:
    """
    Find a free TCP port by binding to port 0.

    :returns: An available port number, e.g. ``58234``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass(frozen=True)
class HarnessCredentials:
    """
    Per-harness credentials and config the live server needs.

    :param harness: Harness identifier, e.g. ``"databricks"``,
        ``"claude-sdk"``, ``"open-responses"``, ``"openai-agents"``,
        ``"codex"``.
    :param profile: Databricks CLI profile name, e.g. ``"test-profile"``.
        ``None`` when not using a Databricks-backed harness.
    :param llm_api_key: Direct API key for the harness's provider,
        e.g. an OpenAI or Anthropic key. ``None`` when the harness
        is Databricks-backed and credentials come from the profile.
    """

    harness: str
    profile: str | None
    llm_api_key: str | None


def _compute_harness_env(creds: HarnessCredentials) -> dict[str, str]:
    """
    Build the env-var overrides the server subprocess needs for
    *creds*.

    :param creds: Harness identity + auth source.
    :returns: A dict of env vars to merge into the subprocess's
        environment, e.g. ``{"OPENAI_API_KEY": "sk-..."}``. Empty
        dict when the harness reads credentials elsewhere
        (e.g. ``--harness=databricks`` reads the CLI profile from
        ``~/.databrickscfg`` directly).
    """
    env: dict[str, str] = {}
    h = creds.harness
    if h in ("open-responses", "openai-agents"):
        if creds.llm_api_key is not None:
            env["OPENAI_API_KEY"] = creds.llm_api_key
    elif h == "claude-sdk":
        if creds.profile:
            # Claude SDK harness wrap reads these to route through
            # the Databricks gateway instead of Anthropic direct.
            env["HARNESS_CLAUDE_SDK_GATEWAY"] = "true"
            env["HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"] = creds.profile
        elif creds.llm_api_key is not None:
            env["ANTHROPIC_API_KEY"] = creds.llm_api_key
    elif h == "codex":
        if creds.profile:
            env["HARNESS_CODEX_GATEWAY"] = "true"
            env["HARNESS_CODEX_DATABRICKS_PROFILE"] = creds.profile
        elif creds.llm_api_key is not None:
            env["CODEX_API_KEY"] = creds.llm_api_key
    elif h == "databricks":
        # Databricks executor reads the CLI profile from
        # ``~/.databrickscfg`` directly — no extra env var.
        # The profile is selected via the agent YAML's
        # ``executor.config.profile`` (see translator).
        pass
    # else: unknown harness — let the server fail loud rather
    # than silently injecting nothing.
    return env


def start_live_server(
    *,
    creds: HarnessCredentials,
    db_path: Path,
    artifact_dir: Path,
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], str]:
    """
    Spawn an ``omnigent.cli server`` subprocess and wait for
    health.

    :param creds: Harness credentials to thread into the subprocess.
    :param db_path: Filesystem path for the server's SQLite DB,
        e.g. ``Path("/tmp/test/db.sqlite")``. Absolute path
        recommended so the server doesn't pollute the test's CWD.
    :param artifact_dir: Filesystem path for the artifact store,
        e.g. ``Path("/tmp/test/artifacts/")``.
    :param log_path: File to redirect server stdout/stderr into,
        e.g. ``Path("/tmp/test/server.log")``.
    :returns: A tuple ``(proc, base_url)`` — the subprocess handle
        the caller is responsible for killing on teardown, and the
        server's base URL.
    :raises RuntimeError: If the server doesn't respond on
        ``/health`` within 30s.
    """
    port = find_free_port()
    harness_env = _compute_harness_env(creds)
    env = {**os.environ, **harness_env}
    # Force the subprocess to import from the worktree, not whatever's
    # installed in the venv — otherwise a branch with schema/model changes
    # runs against a stale installed copy and fails with cryptic "no such
    # column" errors. In compat mode (OMNIGENT_COMPAT_SERVER_PYTHON set) this
    # prepend is dropped so the pinned older build in the compat venv wins.
    apply_server_env(env, _REPO_ROOT)
    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for Popen lifetime
    proc = subprocess.Popen(
        [
            # The test process's own interpreter normally; in compat mode the
            # pinned old server's venv python (server_executable()). Never bare
            # "python" — that resolves against PATH and on macOS can pick up
            # system Python 2.7 and SyntaxError.
            server_executable(),
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
        # Compat mode: neutral CWD so the worktree's omnigent/ on sys.path[0]
        # doesn't shadow the pinned old install. None (inherit) otherwise.
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                return proc, base_url
        except httpx.ConnectError:
            pass
        time.sleep(0.5)

    proc.kill()
    log_handle.close()
    log_contents = log_path.read_text() if log_path.exists() else ""
    raise RuntimeError(
        f"Server didn't start within 30s. Log at {log_path}:\n{log_contents[-3000:]}"
    )


def make_live_server_fixture(
    creds_factory,  # type: ignore[no-untyped-def]
):
    """
    Build a session-scoped pytest fixture starting an
    ``omnigent.cli server`` subprocess.

    Returned fixture yields the server's base URL. Teardown sends
    SIGTERM, escalates to SIGKILL after 10s.

    The conftest in each consumer (e2e, inner) supplies a
    ``creds_factory`` that derives :class:`HarnessCredentials`
    from the test session's ``--harness`` / ``--profile`` /
    ``--llm-api-key`` options.

    :param creds_factory: Callable taking a ``pytest.FixtureRequest``
        and returning :class:`HarnessCredentials`. Called once per
        session.
    :returns: A pytest fixture function ready to be assigned in a
        ``conftest.py`` module.
    """

    @pytest.fixture(scope="session")
    def live_server(  # type: ignore[no-untyped-def]
        request: pytest.FixtureRequest,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> Iterator[str]:
        creds = creds_factory(request)
        db_path = tmp_path_factory.mktemp("ap_test_db") / "ap.db"
        artifact_dir = tmp_path_factory.mktemp("ap_test_artifacts")
        log_path = tmp_path_factory.mktemp("ap_test_logs") / "server.log"
        proc, base_url = start_live_server(
            creds=creds,
            db_path=db_path,
            artifact_dir=artifact_dir,
            log_path=log_path,
        )
        try:
            yield base_url
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    return live_server


def upload_agent(client: httpx.Client, agent_dir: Path) -> str:
    """
    Tar+gzip an agent directory and upload via multipart
    ``POST /v1/sessions``.

    Creates a session with the bundled agent in one request, then
    fetches the agent name from the session agent endpoint. The
    session is a side effect but harmless for tests that only need
    the agent registered on the server.

    :param client: HTTP client pointed at the live server, e.g.
        ``httpx.Client(base_url="http://localhost:58234")``.
    :param agent_dir: Filesystem path to the agent bundle root
        (the directory containing ``config.yaml``), e.g.
        ``Path("/tmp/agent_bundle")``.
    :returns: The agent's name as the server reports it
        (used as the ``model`` field in subsequent
        ``/v1/responses`` calls). On 409 (agent already
        registered), returns ``agent_dir.name`` as the
        identifier — matches the existing e2e helper's behavior.
    """
    import json as _json

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            tar.add(str(agent_dir), arcname=".")
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            resp = client.post(
                "/v1/sessions",
                data={"metadata": _json.dumps({})},
                files={
                    "bundle": (
                        "agent.tar.gz",
                        f,
                        "application/gzip",
                    ),
                },
            )
        if resp.status_code == 409:
            # Agent already registered (idempotent re-upload of
            # the same bundle). Return the directory name as the
            # identifier — matches the existing e2e helper.
            return agent_dir.name
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        agent_resp = client.get(f"/v1/sessions/{session_id}/agent")
        agent_resp.raise_for_status()
        return agent_resp.json()["name"]
    finally:
        os.unlink(tmp_path)
