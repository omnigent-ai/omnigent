"""E2E: a ``harness: pi`` agent bundle's project-local ``.pi/extensions``,
``.pi/skills`` and bundle-root ``AGENTS.md`` must reach the session's pi process.

Pi auto-discovers project-local extensions (``.pi/extensions/<name>/``), skills
(``.pi/skills/<name>/``) and context files (``AGENTS.md``) from its **process
cwd**. The runner spawns pi with ``cwd`` set to the session workspace, which is
unrelated to the agent bundle's on-disk location, so the executor has to hand
those bundled resources to pi explicitly; otherwise pi silently falls back to its
stock persona with no error surfaced.

The test drives the reported journey end to end: register a bundle with
``omnigent server --agent <bundle>``, create a session against it, and send a
message. A marker extension modifies the system prompt via ``before_agent_start``,
the bundle's ``AGENTS.md`` carries a second marker and a bundled skill a third;
all three must appear in the LLM request the mock server captures, alongside the
agent's own prompt.

**Serial execution:** uses the session-scoped mock LLM server like the other
``tests/e2e/omnigent/`` pi rows — do not run under xdist against a shared mock.
"""

from __future__ import annotations

import contextlib
import io
import os
import secrets
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.live_server import find_free_port, terminate_process
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_EXT_MARKER = "OMNI_PI_BUNDLE_EXT_ACTIVE"
_AGENTSMD_MARKER = "OMNI_PI_BUNDLE_AGENTSMD_MARKER"
_SKILL_MARKER = "OMNI_PI_BUNDLE_SKILL_MARKER"
_AGENT_PROMPT = "You are the bundle test agent."
_BOOT_TIMEOUT = 60.0
_TURN_TIMEOUT = 180.0

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=(
        "pi bundle-extensions e2e requires a runnable 'pi' CLI; "
        f"{_pytest_pi_unavailable}. Install/fix Pi to run this test."
    ),
)


def _build_bundle(bundle: Path, model: str) -> None:
    """Write a ``harness: pi`` bundle with a project-local extension, a
    ``.pi/skills`` skill and an ``AGENTS.md``.

    The extension appends :data:`_EXT_MARKER` to the system prompt from
    ``before_agent_start``; ``AGENTS.md`` carries :data:`_AGENTSMD_MARKER`; the
    skill's description carries :data:`_SKILL_MARKER`, which pi lists in its
    skill index. All three live where only pi's cwd-based project loaders would
    find them, so they reach the model only when the executor passes them on.
    """
    ext_dir = bundle / ".pi" / "extensions" / "omni-marker"
    ext_dir.mkdir(parents=True)
    (ext_dir / "index.js").write_text(
        "module.exports = function (pi) {\n"
        '  pi.on("before_agent_start", async (event) => {\n'
        f"    return {{ systemPrompt: `${{event.systemPrompt}}\\n\\n{_EXT_MARKER}` }};\n"
        "  });\n"
        "};\n",
        encoding="utf-8",
    )
    (bundle / "AGENTS.md").write_text(
        f"# Bundle guidance\n\n{_AGENTSMD_MARKER}: always answer like a pirate.\n",
        encoding="utf-8",
    )
    skill_dir = bundle / ".pi" / "skills" / "grilling"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: grilling\ndescription: {_SKILL_MARKER} grilling tips\n---\n# Grilling\n",
        encoding="utf-8",
    )
    (bundle / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "pi_ext_bundle_agent",
                "prompt": _AGENT_PROMPT,
                "executor": {"model": model, "config": {"harness": "pi"}},
                "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@contextmanager
def _server_and_runner(
    *,
    python: Path,
    bundle: Path,
    port: int,
    env: dict[str, str],
    cwd: Path,
    db_path: Path,
    runner_id: str,
    binding_token: str,
    log_dir: Path,
) -> Iterator[tuple[subprocess.Popen[str], subprocess.Popen[str]]]:
    """Spawn ``omnigent server --agent <bundle>`` and a sibling runner.

    Output goes to files under *log_dir* so a chatty process cannot fill a
    pipe buffer and stall mid-test; the server log is read back on failure.
    """
    base_url = f"http://127.0.0.1:{port}"
    log_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        server_log = stack.enter_context((log_dir / "server.log").open("w", encoding="utf-8"))
        runner_log = stack.enter_context((log_dir / "runner.log").open("w", encoding="utf-8"))
        server = subprocess.Popen(
            [
                str(python),
                "-m",
                "omnigent",
                "server",
                "--agent",
                str(bundle),
                "-p",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
            ],
            env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
            cwd=str(cwd),
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        stack.callback(terminate_process, server)
        runner = subprocess.Popen(
            [str(python), "-m", "omnigent.runner._entry"],
            env={
                **env,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": base_url,
            },
            cwd=str(cwd),
            stdout=runner_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        stack.callback(terminate_process, runner)
        yield server, runner


def _wait_for_online_runner(
    port: int,
    *,
    runner_id: str,
    procs: dict[str, subprocess.Popen[str]],
    timeout: float,
    log_dir: Path,
) -> None:
    def _logs() -> str:
        return "\n".join(
            f"--- {name}.log ---\n{(log_dir / f'{name}.log').read_text(encoding='utf-8')}"
            for name in procs
            if (log_dir / f"{name}.log").exists()
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, proc in procs.items():
            if proc.poll() is not None:
                pytest.fail(f"{name} exited with code {proc.returncode}:\n{_logs()}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0).status_code == 200:
                status = httpx.get(
                    f"http://127.0.0.1:{port}/v1/runners/{runner_id}/status",
                    timeout=2.0,
                )
                if status.status_code == 200 and status.json().get("online") is True:
                    return
        except (httpx.HTTPError, ValueError):
            # The server is still booting (or answered a partial body); keep polling.
            pass
        time.sleep(0.5)
    pytest.fail(f"server + runner not ready after {timeout}s:\n{_logs()}")


def _bundle_dir_tarball(bundle: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(str(bundle), arcname=".")
    return buffer.getvalue()


def _create_session(client: httpx.Client, bundle: Path, runner_id: str) -> str:
    resp = post_session_bundle(client.post, "/v1/sessions", _bundle_dir_tarball(bundle))
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]
    bind_session_runner(client.patch, "", session_id, runner_id)
    return session_id


def _drive_turn_to_terminal(client: httpx.Client, session_id: str, prompt: str) -> dict[str, Any]:
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    }
    client.post(f"/v1/sessions/{session_id}/events", json=body).raise_for_status()
    deadline = time.monotonic() + _TURN_TIMEOUT
    seen_running = False
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get(f"/v1/sessions/{session_id}").json()
        status = last.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        turn_items = [
            item
            for item in last.get("items", [])
            if item.get("type") not in ("resource_event",)
            and not (item.get("type") == "message" and item.get("data", {}).get("role") == "user")
        ]
        if status == "failed" or (status == "idle" and (seen_running or turn_items)):
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"session {session_id} did not become terminal in {_TURN_TIMEOUT}s; last={last}"
    )


def _captured_system_prompts(mock_url: str, model: str) -> list[str]:
    prompts: list[str] = []
    for req in get_mock_requests(mock_url, key=model):
        if not isinstance(req, dict):
            continue
        for msg in req.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    prompts.append(content)
        instructions = req.get("system") or req.get("instructions")
        if isinstance(instructions, str):
            prompts.append(instructions)
    return prompts


def test_pi_bundle_extensions_reach_session_cwd(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A pi bundle's ``.pi/extensions``, ``.pi/skills`` and ``AGENTS.md``
    reach the live session.

    Drives the reported journey (register bundle → create session → send a
    message) against a real server + runner + real pi CLI, with the mock LLM
    capturing the outgoing request. The marker extension, ``AGENTS.md`` and the
    skill index contribute to the system prompt only when pi receives the
    bundled resources, so their presence in the captured request proves they
    reached pi; the agent's own prompt must survive alongside them.
    """
    from omnigent.runner.identity import token_bound_runner_id

    model = f"mock-pi-bundle-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello from the mock model."}] * 4,
        key=model,
    )

    bundle = tmp_path / "myagent"
    bundle.mkdir()
    _build_bundle(bundle, model)

    config_home = tmp_path / "omnigent-config"
    write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)

    env = dict(mock_credentials_env)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)

    port = find_free_port()
    db_path = tmp_path / "server.db"
    log_dir = tmp_path / "logs"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    with _server_and_runner(
        python=omnigent_python,
        bundle=bundle,
        port=port,
        env=env,
        cwd=omnigent_repo_root,
        db_path=db_path,
        runner_id=runner_id,
        binding_token=binding_token,
        log_dir=log_dir,
    ) as (server, runner):
        _wait_for_online_runner(
            port,
            runner_id=runner_id,
            procs={"server": server, "runner": runner},
            timeout=_BOOT_TIMEOUT,
            log_dir=log_dir,
        )

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            session_id = _create_session(client, bundle, runner_id)
            snapshot = _drive_turn_to_terminal(
                client, session_id, "Introduce yourself in one sentence."
            )

    assert snapshot.get("status") != "failed", (
        f"pi turn failed instead of exercising the extension path: "
        f"{snapshot.get('last_task_error') or snapshot.get('error')}"
    )

    system_prompts = _captured_system_prompts(mock_llm_server_url, model)
    assert system_prompts, (
        "mock LLM captured no system prompt — the pi turn never reached the "
        "model, so the extension-loading path was not exercised"
    )
    joined = "\n".join(system_prompts)

    assert _EXT_MARKER in joined, (
        "bundle's project-local .pi/extensions extension did not load — its "
        f"before_agent_start marker {_EXT_MARKER!r} is absent from the system "
        "prompt pi sent to the model (pi ran in the session workspace, not the "
        "bundle directory)."
    )
    assert _AGENTSMD_MARKER in joined, (
        "bundle-root AGENTS.md did not reach pi — its marker "
        f"{_AGENTSMD_MARKER!r} is absent from the system prompt (pi's cwd-based "
        "context loader never saw the bundle directory)."
    )
    assert _SKILL_MARKER in joined, (
        "bundle's .pi/skills skill did not reach pi — its description marker "
        f"{_SKILL_MARKER!r} is absent from the skill index in the system prompt."
    )
    assert _AGENT_PROMPT in joined, (
        "the agent's own prompt was lost while appending the bundle resources"
    )
