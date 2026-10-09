"""End-to-end: a top-level custom codex-native bundle must keep its spec settings.

Reported journey: a user authors a minimal custom
agent —

.. code-block:: yaml

    spec_version: 1
    name: codex-top-level-probe
    executor:
      type: omnigent
      config:
        harness: codex-native
        yolo: true
    llm:
      model: gpt-5.6-terra
      reasoning_effort: xhigh

— and starts a fresh top-level session from it (``omnigent run <dir>``, no
picker-supplied model/effort/launch-arg overrides). The persisted session then
drives the native Codex launch, so whatever is missing from the row is missing
from the real Codex thread:

* ``reasoning_effort`` must persist as ``xhigh`` (was NULL in the report;
  fixed by the spec-effort fallback on the create paths).
* ``executor.config.yolo: true`` must become the Codex full-bypass launch
  state (``--dangerously-bypass-approvals-and-sandbox`` in
  ``terminal_launch_args``) exactly as it already does for the equivalent
  NAMED worker — while the bug is live the top-level row keeps
  ``terminal_launch_args = NULL``, Codex launches at its default approval
  stance, and terminal commands park on approval cards despite ``yolo: true``.

The three tests drive the two create shapes a real user hits — the multipart
bundle upload ``omnigent run`` performs, and the JSON ``agent_id`` +
``sub_agent_name`` create the runner's ``sys_session_send`` dispatch posts —
against a real spawned server, then assert on the persisted session the same
way the reporter inspected the conversation row::

    .venv/bin/python -m pytest tests/e2e/test_top_level_codex_bundle_spec_settings_e2e.py -v

While the bug is live, ``test_top_level_custom_bundle_derives_codex_full_bypass``
fails (launch args stay NULL); the other two pin the already-working behavior
so a fix cannot regress the effort seeding or named-worker parity.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CODEX_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

# The reporter's minimal config, verbatim.
_TOP_LEVEL_YAML = """\
spec_version: 1
name: codex-top-level-probe

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

llm:
  model: gpt-5.6-terra
  reasoning_effort: xhigh
"""

# A coordinator declaring the SAME codex-native worker spec as a named
# sub-agent (bundles declare sub-agents as agents/<name>/config.yaml).
# This is the shape the reporter contrasted against: the named-worker path
# already propagates both settings, the top-level path must reach parity.
_PARENT_YAML = """\
spec_version: 1
name: coordinator-probe

executor:
  type: omnigent
  config:
    harness: claude-sdk

llm:
  model: some-parent-model
"""

_WORKER_YAML = """\
spec_version: 1
name: codex_worker
description: named codex-native worker (same spec as the top-level probe)

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

llm:
  model: gpt-5.6-terra
  reasoning_effort: xhigh
"""

_HEALTH_TIMEOUT_S = 90.0

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False, timeout=30.0)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _bundle(files: dict[str, str]) -> bytes:
    """Gzip a mapping of arcname -> YAML text as a session bundle."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(scope="module")
def spec_probe_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real spawned server (no runner needed: persistence is server-side).

    :returns: The server base URL.
    """
    work = tmp_path_factory.mktemp("codex_bundle_server")
    artifacts = work / "artifacts"
    config_home = work / "config-home"
    home_dir = work / "home"
    for path in (artifacts, config_home, home_dir):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "HOME": str(home_dir),
        # A binding token so the server boots with the same tunnel posture as
        # production; no runner connects in this suite.
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": secrets.token_urlsafe(32),
    }

    log_path = work / "server.log"
    log_handle = log_path.open("w")
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early: {log_path.read_text()[-3000:]}")
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"server not healthy within {_HEALTH_TIMEOUT_S:.0f}s:\n"
                f"{log_path.read_text()[-3000:]}"
            )
        yield base_url
    finally:
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


def _create_top_level_session(base_url: str) -> dict:
    """Create a fresh top-level session from the reporter's bundle.

    Same multipart POST /v1/sessions body ``omnigent run <dir>`` sends, with
    no model / reasoning_effort / terminal_launch_args in the metadata (the
    report's "without picker-supplied ... overrides").

    :returns: The persisted session detail (GET /v1/sessions/{id}).
    """
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={
            "bundle": (
                "codex-top-level-probe.tar.gz",
                _bundle({"config.yaml": _TOP_LEVEL_YAML}),
                "application/gzip",
            )
        },
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    detail = _client.get(f"{base_url}/v1/sessions/{session_id}")
    detail.raise_for_status()
    return detail.json()


@pytest.mark.timeout(180)
def test_top_level_custom_bundle_persists_spec_reasoning_effort(
    spec_probe_server: str,
) -> None:
    """The bundle's ``llm.reasoning_effort: xhigh`` must persist on the session.

    The persisted ``reasoning_effort`` seeds the native Codex launch
    (``--effort``), so a NULL here silently downgrades the real Codex thread —
    the first half of the report.
    """
    session = _create_top_level_session(spec_probe_server)
    assert session.get("reasoning_effort") == "xhigh", (
        "top-level custom codex-native bundle dropped llm.reasoning_effort: "
        f"persisted {session.get('reasoning_effort')!r} instead of 'xhigh'"
    )


@pytest.mark.timeout(180)
def test_top_level_custom_bundle_derives_codex_full_bypass(
    spec_probe_server: str,
) -> None:
    """``executor.config.yolo: true`` must become the Codex full-bypass launch state.

    The named-worker create derives ``terminal_launch_args`` from the trusted
    spec (codex-native -> ``--dangerously-bypass-approvals-and-sandbox``); the
    top-level create of the SAME spec must not silently drop the explicitly
    configured ``yolo: true``. While the bug is live this stays NULL, Codex
    launches at its default approval stance, and the user's unattended
    orchestrator parks on approval prompts — the second half of the report.
    """
    session = _create_top_level_session(spec_probe_server)
    launch_args = session.get("terminal_launch_args")
    assert launch_args and _CODEX_BYPASS_FLAG in launch_args, (
        "top-level custom codex-native bundle with executor.config.yolo: true "
        f"persisted terminal_launch_args={launch_args!r}; expected the Codex "
        f"full-bypass launch state ({_CODEX_BYPASS_FLAG}) for parity with the "
        "named-worker path"
    )


@pytest.mark.timeout(180)
def test_named_codex_worker_keeps_effort_and_bypass_parity(
    spec_probe_server: str,
) -> None:
    """The named-worker path must keep seeding effort + bypass from the sub-spec.

    Pins the parity baseline the report measured against (and the follow-up
    claim that the dispatch create dropped the sub-spec effort): a child
    created with the runner dispatch body (parent ``agent_id`` +
    ``sub_agent_name``, no client model/effort) persists the sub-spec's
    ``xhigh`` AND the codex-native full-bypass launch args.
    """
    base_url = spec_probe_server
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={
            "bundle": (
                "coordinator-probe.tar.gz",
                _bundle(
                    {
                        "config.yaml": _PARENT_YAML,
                        "agents/codex_worker/config.yaml": _WORKER_YAML,
                    }
                ),
                "application/gzip",
            )
        },
    )
    create.raise_for_status()
    parent_id = str(create.json()["session_id"])
    parent = _client.get(f"{base_url}/v1/sessions/{parent_id}")
    parent.raise_for_status()

    # The exact create body shape the runner's sys_session_send dispatch posts
    # (omnigent/runner/tool_dispatch.py): parent agent_id + sub_agent_name,
    # no reasoning_effort / terminal_launch_args from the caller.
    child_create = _client.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": parent.json()["agent_id"],
            "parent_session_id": parent_id,
            "title": "codex_worker:effort-bypass-parity",
            "sub_agent_name": "codex_worker",
        },
    )
    assert child_create.status_code in (200, 201), (
        f"named-worker create rejected: {child_create.status_code} {child_create.text[:500]}"
    )
    child_json = child_create.json()
    child_id = str(child_json.get("session_id") or child_json.get("id"))
    child = _client.get(f"{base_url}/v1/sessions/{child_id}")
    child.raise_for_status()
    child_detail = child.json()

    assert child_detail.get("reasoning_effort") == "xhigh", (
        "named codex-native worker dropped the sub-spec reasoning_effort: "
        f"persisted {child_detail.get('reasoning_effort')!r} instead of 'xhigh'"
    )
    launch_args = child_detail.get("terminal_launch_args")
    assert launch_args and _CODEX_BYPASS_FLAG in launch_args, (
        "named codex-native worker lost its full-bypass launch state: "
        f"terminal_launch_args={launch_args!r}"
    )
