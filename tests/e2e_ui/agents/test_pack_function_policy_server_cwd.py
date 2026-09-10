"""E2E: pack function policies evaluate regardless of server cwd.

An agent pack registered via ``omnigent server --agent <pack-dir>`` that
declares a guardrail function policy by pack-local dotted path
(``agents.mypack.policies.custom_policy.my_factory``) must have that policy
evaluated server-side even when the server process was launched from a
directory other than the pack's repo root — the launchd/systemd
service-manager shape (``cd $HOME && omnigent server --agent
/repo/agents/mypack``). Today the dotted path resolves via importlib against
the server process ``sys.path`` only, so every user message on such a
deployment is fail-closed denied with ``Denied by policy (policy evaluation
error).`` before the agent runs, and the server log shows
``ModuleNotFoundError: No module named 'agents.mypack'``.

This test spawns a dedicated server whose cwd is a neutral "service home"
directory (NOT the pack repo root) and drives the real user journey through
the SPA:

1. send a normal message → the agent must reply; the generic
   policy-evaluation-error deny must NOT appear (fails on the bug);
2. send a message containing the policy's deny keyword → the pack policy's
   OWN deny reason must surface, proving the dotted-path policy resolved and
   ran (fails on the bug: the generic error reason appears instead).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _find_free_port,
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    set_fallback_mock_llm,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_COMPOSER_LABEL = "Message the agent"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

_PACK_AGENT_NAME = "mypack"
# The exact fail-closed reason routes_events.py returns when input-policy
# evaluation crashes — the bug's user-visible signature.
_GENERIC_POLICY_ERROR = "policy evaluation error"
# The pack policy's own deny reason — proves the dotted-path policy loaded.
_PACK_DENY_REASON = "mypack policy: input contains 'forbidden'"

_PACK_CONFIG_YAML = textwrap.dedent(
    """\
    spec_version: 1
    name: mypack
    description: Agent pack with a pack-local function policy.

    executor:
      type: omnigent
      model: gpt-4o-mini
      config:
        harness: openai-agents

    prompt: |
      You are mypack, a friendly test agent. Answer briefly.

    os_env:
      type: caller_process
      cwd: .
      sandbox:
        type: none

    guardrails:
      policies:
        mypack_guard:
          type: function
          function:
            path: agents.mypack.policies.custom_policy.my_factory
            arguments:
              keyword: forbidden
    """
)

_PACK_POLICY_PY = textwrap.dedent(
    '''\
    """Pack-local function policy, referenced by dotted path from config.yaml."""


    def my_factory(keyword: str = "forbidden"):
        """Factory: returns an input policy denying messages containing *keyword*."""

        def policy(event):
            if event.get("type") != "request":
                return {"result": "ALLOW"}
            text = str(event.get("data") or "")
            if keyword in text.lower():
                return {
                    "result": "DENY",
                    "reason": f"mypack policy: input contains '{keyword}'",
                }
            return {"result": "ALLOW"}

        return policy
    '''
)


def _build_pack_repo(root: Path) -> Path:
    """Create ``<root>/packrepo`` mirroring the report's pack layout.

    ``agents/`` is a real package chain (``__init__.py`` all the way down)
    so ``agents.mypack.policies.custom_policy`` imports fine from the pack
    repo root — and from nowhere else.

    :param root: Temp directory to build under.
    :returns: The pack directory to pass to ``--agent``.
    """
    pack_repo = root / "packrepo"
    pack_dir = pack_repo / "agents" / "mypack"
    policies_dir = pack_dir / "policies"
    policies_dir.mkdir(parents=True)
    (pack_repo / "agents" / "__init__.py").write_text("")
    (pack_dir / "__init__.py").write_text("")
    (policies_dir / "__init__.py").write_text("")
    (policies_dir / "custom_policy.py").write_text(_PACK_POLICY_PY)
    (pack_dir / "config.yaml").write_text(_PACK_CONFIG_YAML)
    return pack_dir


def _absolute_pythonpath() -> str:
    """PYTHONPATH for the spawned server/runner, with every entry absolute.

    The ambient test PYTHONPATH may carry worktree-relative entries (e.g.
    ``sdks/python-client``); those break when the server's cwd is moved off
    the repo root — which is the whole point of this test — so resolve them
    against the current (repo-root) cwd first.
    """
    parts = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if entry:
            parts.append(str(Path(entry).resolve()))
    return os.pathsep.join(parts)


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def pack_policy_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str]]:
    """Spawn ``omnigent server --agent <pack>`` from a NEUTRAL cwd + a runner.

    Dedicated server (not the shared ``live_server``) because the bug is a
    property of the server process's launch directory: cwd is a fresh empty
    "service home" dir, standing in for launchd/systemd's ``$HOME``, so the
    pack repo root is NOT on the server's ``sys.path``.

    :returns: ``(base_url, runner_id)``.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("pack policy e2e requires an isolated spawned server")

    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    server_tmp = tmp_path_factory.mktemp("pack_policy_server")
    pack_dir = _build_pack_repo(server_tmp)
    # The service manager's working directory — intentionally not the pack
    # repo root and containing no `agents` package.
    service_home = server_tmp / "service-home"
    service_home.mkdir()
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir()
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    pythonpath = _absolute_pythonpath()
    mock_url = mock_llm_server_url
    server_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"),
    }
    runner_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for Popen lifetime; closed in finally
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from omnigent.cli import main; main()",
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
                str(pack_dir),
            ],
            env=server_env,
            # The reproduction's load-bearing detail: launch from a directory
            # that is NOT the pack repo root (the service-manager shape).
            cwd=str(service_home),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            if runner_proc.poll() is not None:
                last_error = f"runner exited early with code {runner_proc.returncode}"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2, trust_env=False)
                if resp.status_code == 200:
                    status_resp = httpx.get(
                        f"{base_url}/v1/runners/{runner_id}/status",
                        timeout=2,
                        trust_env=False,
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
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"pack-policy server did not become healthy within "
                f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                f"Server log at {log_path}:\n{log_text[-3000:]}"
            )

        # Any agent turn gets a generic reply from the mock LLM; fallback so
        # per-test mock resets elsewhere in the shard can't strand this module.
        set_fallback_mock_llm(mock_url, "gpt-4o-mini", "Mock LLM response.")

        yield (base_url, runner_id)
    finally:
        _terminate(runner_proc)
        _terminate(proc)
        runner_log_handle.close()
        log_handle.close()


def _create_pack_session(base_url: str, runner_id: str) -> str:
    """Create a session bound to the pre-registered ``mypack`` agent + runner.

    :param base_url: The dedicated server's base URL.
    :param runner_id: The sibling runner's token-bound id.
    :returns: The new session id.
    """
    agents_resp = httpx.get(f"{base_url}/v1/agents", timeout=10, trust_env=False)
    agents_resp.raise_for_status()
    data = agents_resp.json()
    items = data if isinstance(data, list) else data.get("agents", data.get("data", []))
    agent_id = next(a["id"] for a in items if a.get("name") == _PACK_AGENT_NAME)

    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id},
        timeout=30,
        trust_env=False,
    )
    create_resp.raise_for_status()
    payload = create_resp.json()
    session_id = str(payload.get("id") or payload.get("session_id"))

    bind_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10,
        trust_env=False,
    )
    bind_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_pack_function_policy_evaluates_when_server_cwd_is_not_pack_root(
    page: Page,
    pack_policy_server: tuple[str, str],
) -> None:
    """A pack's dotted-path function policy works on a service-managed server.

    On the bug, EVERY message is fail-closed denied with the generic
    ``Denied by policy (policy evaluation error).`` reason because
    ``agents.mypack`` cannot be imported from the server process.
    """
    base_url, runner_id = pack_policy_server
    session_id = _create_pack_session(base_url, runner_id)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)

    # 1. A normal message must reach the agent and get a reply — not the
    #    generic policy-evaluation-error deny.
    _send(page, "hello there")
    assistant = page.locator(_ASSISTANT_BUBBLE)
    generic_error = page.get_by_text(_GENERIC_POLICY_ERROR)
    # Wait for the turn to settle into either outcome, then require the good
    # one: the bug fails fast here (the deny banner appears in place of any
    # agent reply).
    expect(assistant.or_(generic_error).first).to_be_visible(timeout=90_000)
    expect(generic_error).to_have_count(0)
    expect(assistant.first).to_contain_text("Mock LLM response.", timeout=60_000)

    # 2. The deny keyword must trip the pack policy's OWN reason — proof the
    #    pack-local dotted path resolved and the policy actually ran.
    _send(page, "this is forbidden text")
    expect(page.get_by_text(_PACK_DENY_REASON)).to_be_visible(timeout=30_000)
    expect(generic_error).to_have_count(0)
