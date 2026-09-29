"""
A live runner must not re-parse a shared agent bundle for every sub-agent
session, and its spec parser must use the libyaml loader when available.

Journey: register a directory bundle whose parent fans out to three
sub-agents, bind it to a real runner, run one warm-up turn so the parent
session has already resolved the bundle, then send the turn that dispatches
all three sub-agents via ``sys_session_send``. Every sub-agent session shares
the parent's ``(agent_id, version)`` bundle, so a runner that memoizes the
parsed spec per bundle parses nothing new during the fan-out.

The runner under test is a real ``omnigent.runner._entry`` subprocess. A
test-only ``sitecustomize`` shim on its ``PYTHONPATH`` records each
``omnigent.spec.parser.parse`` call (the bundle directory it parsed, and the
runner functions on the stack) and the YAML loader class the parser uses;
product code is not modified.

Run::

    .venv/bin/python -m pytest tests/e2e/test_runner_spec_reparse_fanout.py -v
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import subprocess
import tarfile
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    pinned_runner_version,
    runner_executable,
    server_executable,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    find_free_port,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

pytestmark = pytest.mark.timeout(600, method="signal")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SUB_AGENTS = ("alpha", "beta", "gamma")

_WARMUP_DONE = "REPARSE_WARMUP_DONE"
_PARENT_DONE = "REPARSE_PARENT_DONE"
_SUB_AGENT_DONE = "REPARSE_SUB_AGENT_DONE"

_PARSE_LOG_ENV = "OMNIGENT_SPEC_PARSE_LOG"

# Runs inside the runner subprocess at interpreter start-up. ``omnigent.spec.load``
# calls the ``parse`` name bound in the ``omnigent.spec`` namespace, while the
# sub-agent recursion calls it through ``omnigent.spec.parser``, so both are patched.
_SITECUSTOMIZE_SRC = r"""
import json
import os
import sys
import time
import traceback

_LOG = os.environ.get("OMNIGENT_SPEC_PARSE_LOG")


def _record(kind, **fields):
    with open(_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, "pid": os.getpid(), "t": time.time(), **fields}) + "\n")


if _LOG:
    try:
        import yaml
        import omnigent.spec as _spec
        import omnigent.spec.parser as _parser

        _orig_parse = _parser.parse

        def _recording_parse(root, *args, **kwargs):
            runner_frames = [
                f.name
                for f in traceback.extract_stack()
                if f.filename.endswith(("omnigent/runner/app.py", "omnigent/runner/_entry.py"))
            ]
            _record("parse", root=str(root), runner_callers=runner_frames)
            return _orig_parse(root, *args, **kwargs)

        _parser.parse = _recording_parse
        _spec.parse = _recording_parse

        _loader = _parser._ConfigYamlLoader
        _csafe = getattr(yaml, "CSafeLoader", None)
        _record(
            "loader",
            argv=sys.argv,
            with_libyaml=bool(getattr(yaml, "__with_libyaml__", False)),
            mro=[f"{c.__module__}.{c.__name__}" for c in _loader.__mro__],
            is_csafeloader=_csafe is not None and issubclass(_loader, _csafe),
        )
    except Exception as exc:  # noqa: BLE001 - instrumentation must never break the runner
        _record("error", error=repr(exc))
"""


@dataclass
class _Stack:
    base_url: str
    client: httpx.Client
    runner_id: str
    runner_pid: int
    parse_log: Path
    runner_log: Path
    server_log: Path


def _read_records(parse_log: Path, *, pid: int) -> list[dict[str, Any]]:
    if not parse_log.exists():
        return []
    records = [json.loads(line) for line in parse_log.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("pid") == pid]


def _bundle_root_parses(records: list[dict[str, Any]]) -> Counter[str]:
    """Count parses per bundle root, excluding the ``agents/<name>`` sub-agent dirs."""
    return Counter(
        r["root"]
        for r in records
        if r["kind"] == "parse" and Path(r["root"]).parent.name != "agents"
    )


def _wait_online(base_url: str, runner_id: str, server_proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if server_proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {server_proc.returncode}")
        try:
            health = httpx.get(f"{base_url}/health", timeout=2)
            status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            if (
                health.status_code == 200
                and status.status_code == 200
                and status.json().get("online") is True
            ):
                return
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise RuntimeError(f"server + runner did not come online within {HEALTH_TIMEOUT_S}s")


def _terminate(proc: subprocess.Popen[bytes], *, timeout: float) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def instrumented_stack(
    tmp_path_factory: pytest.TempPathFactory,
    mock_llm_server_url: str,
) -> Iterator[_Stack]:
    """A real server plus a real runner whose spec parses are recorded."""
    if pinned_runner_version() is not None or os.environ.get("OMNIGENT_COMPAT_SERVER_VERSION"):
        pytest.skip(
            "compat mode pins an older build; this stack runs the worktree's server + runner"
        )

    tmp_path = tmp_path_factory.mktemp("spec_reparse")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE_SRC)
    parse_log = tmp_path / "spec_parses.jsonl"

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "server.log"
    runner_log = tmp_path / "runner.log"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
    }
    apply_server_env(env, _REPO_ROOT)

    server_cfg = tmp_path / "server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {"base_url": f"{mock_llm_server_url}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    server_handle = open(server_log, "w")  # noqa: SIM115
    server_proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'e2e.db'}",
            "--artifact-location",
            str(artifact_dir),
            "--config",
            str(server_cfg),
        ],
        env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
        cwd=compat_server_cwd(),
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )

    runner_env = apply_runner_env(
        {
            **env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            _PARSE_LOG_ENV: str(parse_log),
        }
    )
    runner_env["PYTHONPATH"] = os.pathsep.join(
        [str(shim_dir), *filter(None, [runner_env.get("PYTHONPATH")])]
    )
    runner_handle = open(runner_log, "w")  # noqa: SIM115
    runner_proc = subprocess.Popen(
        [runner_executable(), "-m", "omnigent.runner._entry"],
        env=runner_env,
        cwd=compat_runner_cwd(),
        stdout=runner_handle,
        stderr=subprocess.STDOUT,
    )

    client = httpx.Client(base_url=base_url, timeout=30.0)
    try:
        try:
            _wait_online(base_url, runner_id, server_proc)
        except RuntimeError as exc:
            raise RuntimeError(
                f"{exc}\nserver log:\n{server_log.read_text()[-3000:]}\n"
                f"runner log:\n{runner_log.read_text()[-3000:]}"
            ) from exc
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        yield _Stack(
            base_url=base_url,
            client=client,
            runner_id=runner_id,
            runner_pid=runner_proc.pid,
            parse_log=parse_log,
            runner_log=runner_log,
            server_log=server_log,
        )
    finally:
        client.close()
        _terminate(runner_proc, timeout=5)
        runner_handle.close()
        _terminate(server_proc, timeout=10)
        server_handle.close()


def _agent_config(
    *,
    name: str,
    prompt: str,
    model: str,
    mock_llm_base_url: str,
    sub_agents: tuple[str, ...] = (),
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "description": f"Fan-out fixture agent {name}.",
        "executor": {
            "type": "omnigent",
            "model": model,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url},
            "config": {"harness": "openai-agents"},
        },
        "prompt": prompt,
        "os_env": {"type": "caller_process", "cwd": "."},
    }
    if sub_agents:
        config["tools"] = {"agents": list(sub_agents)}
    return config


def _register_fanout_bundle(
    client: httpx.Client,
    *,
    name: str,
    parent_model: str,
    sub_model: str,
    mock_llm_base_url: str,
) -> str:
    """Upload a directory bundle: a parent whose ``tools.agents`` fan out to three sub-agents."""
    parent_cfg = _agent_config(
        name=name,
        prompt=(
            "You are an orchestrator. When asked to run, dispatch each of your "
            "sub-agents exactly once via sys_session_send and then finish."
        ),
        model=parent_model,
        mock_llm_base_url=mock_llm_base_url,
        sub_agents=_SUB_AGENTS,
    )
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:

            def _add_yaml(arcname: str, config: dict[str, Any]) -> None:
                data = yaml.safe_dump(config, sort_keys=False).encode()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add_yaml("config.yaml", parent_cfg)
            for sub in _SUB_AGENTS:
                _add_yaml(
                    f"agents/{sub}/config.yaml",
                    _agent_config(
                        name=sub,
                        prompt="You are a worker. Acknowledge the task and finish.",
                        model=sub_model,
                        mock_llm_base_url=mock_llm_base_url,
                    ),
                )
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"bundle register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _child_sessions(client: httpx.Client, *, parent_session_id: str) -> list[dict[str, Any]]:
    resp = client.get(
        "/v1/sessions", params={"visibility": "all", "kind": "sub_agent", "limit": 1000}
    )
    resp.raise_for_status()
    children = []
    for item in resp.json().get("data", []):
        snap = client.get(f"/v1/sessions/{item['id']}")
        snap.raise_for_status()
        if snap.json().get("parent_session_id") == parent_session_id:
            children.append(snap.json())
    return children


def _wait_for_finished_children(
    client: httpx.Client, *, parent_session_id: str, expected: int, timeout: float = 240.0
) -> list[dict[str, Any]]:
    """Wait until *expected* sub-agent sessions exist and each has replied."""
    deadline = time.monotonic() + timeout
    children: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        children = _child_sessions(client, parent_session_id=parent_session_id)
        finished = [c for c in children if _SUB_AGENT_DONE in json.dumps(c.get("items", []))]
        if len(children) >= expected and len(finished) >= expected:
            return children
        time.sleep(0.5)
    raise AssertionError(
        f"expected {expected} finished sub-agent sessions under {parent_session_id}; "
        f"saw {len(children)}: {[(c['id'], c['status']) for c in children]}"
    )


def _wait_for_idle(client: httpx.Client, *, session_id: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/v1/sessions/{session_id}")
        snap.raise_for_status()
        if snap.json().get("status") == "idle":
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"session {session_id} did not return to idle within {timeout}s")


def test_subagent_fanout_does_not_reparse_shared_bundle(
    instrumented_stack: _Stack, mock_llm_server_url: str
) -> None:
    """
    Sub-agent sessions created from an already-resolved bundle must not make
    the runner parse that bundle's YAML again.
    """
    stack = instrumented_stack
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-reparse-parent-{uid}"
    sub_model = f"mock-reparse-sub-{uid}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_fanout_bundle(
        stack.client,
        name=f"reparse-fanout-{uid}",
        parent_model=parent_model,
        sub_model=sub_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, _PARENT_DONE)
    set_fallback_mock_llm(mock_llm_server_url, sub_model, _SUB_AGENT_DONE)

    session_id = create_runner_bound_session(
        stack.client, agent_name=agent_name, runner_id=stack.runner_id
    )

    configure_mock_llm(mock_llm_server_url, [{"text": _WARMUP_DONE}], key=parent_model)
    warmup = poll_session_until_terminal(
        stack.client,
        session_id=session_id,
        response_id=send_user_message_to_session(
            stack.client, session_id=session_id, content="Say hello and stop."
        ),
        timeout=240,
    )
    assert warmup["status"] == "completed", f"warm-up turn failed: {warmup.get('error')!r}"

    records_before = _read_records(stack.parse_log, pid=stack.runner_pid)
    assert any(r["kind"] == "loader" for r in records_before), (
        f"parse recorder did not load in the runner; records={records_before!r}\n"
        f"runner log:\n{stack.runner_log.read_text()[-3000:]}"
    )
    roots_before = _bundle_root_parses(records_before)
    assert len(roots_before) == 1, (
        f"expected the runner to have resolved exactly one bundle root, got {roots_before!r}"
    )
    (bundle_root, parses_before) = next(iter(roots_before.items()))

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_dispatch_{sub}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": sub,
                                "title": f"fanout-{sub}",
                                "args": "Acknowledge and finish.",
                            }
                        ),
                    }
                    for sub in _SUB_AGENTS
                ]
            }
        ],
        key=parent_model,
    )
    fanout = poll_session_until_terminal(
        stack.client,
        session_id=session_id,
        response_id=send_user_message_to_session(
            stack.client, session_id=session_id, content="RUN: dispatch every sub-agent once."
        ),
        timeout=240,
    )
    assert fanout["status"] == "completed", f"fan-out turn failed: {fanout.get('error')!r}"

    children = _wait_for_finished_children(
        stack.client, parent_session_id=session_id, expected=len(_SUB_AGENTS)
    )
    _wait_for_idle(stack.client, session_id=session_id)

    records_after = _read_records(stack.parse_log, pid=stack.runner_pid)
    parses_after = _bundle_root_parses(records_after)[bundle_root]
    reparses = parses_after - parses_before
    new_parses = [r for r in records_after[len(records_before) :] if r["kind"] == "parse"]
    call_paths = sorted({" -> ".join(r["runner_callers"]) for r in new_parses})
    assert reparses == 0, (
        f"creating {len(children)} sub-agent sessions that share the parent's already-resolved "
        f"bundle {bundle_root} made the runner parse that bundle {reparses} more time(s) "
        f"(root parses before fan-out: {parses_before}, after: {parses_after}; "
        f"parse() calls during the fan-out: {len(new_parses)}; runner call paths: {call_paths})"
    )


def test_runner_parses_spec_yaml_with_libyaml_loader(instrumented_stack: _Stack) -> None:
    """The runner's spec YAML loader must be libyaml-backed when libyaml is available."""
    stack = instrumented_stack
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    loader: dict[str, Any] | None = None
    while loader is None and time.monotonic() < deadline:
        loader = next(
            (
                r
                for r in _read_records(stack.parse_log, pid=stack.runner_pid)
                if r["kind"] == "loader"
            ),
            None,
        )
        time.sleep(POLL_INTERVAL_S)
    assert loader is not None, (
        f"parse recorder did not report the runner's loader\nrunner log:\n"
        f"{stack.runner_log.read_text()[-3000:]}"
    )
    if not loader["with_libyaml"]:
        pytest.skip("libyaml is not available in the runner's runtime")
    assert loader["is_csafeloader"], (
        f"the runner parses agent-bundle YAML with the pure-Python SafeLoader although libyaml "
        f"is available; _ConfigYamlLoader MRO: {loader['mro']}"
    )
