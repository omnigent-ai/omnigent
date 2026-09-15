"""E2E reproduction: the runner re-parses the agent bundle YAML per session
with the pure-Python ``SafeLoader``.

Two independent defects, each guarded by its own test here.

Facet A -- slow loader (``test_config_loader_uses_libyaml_csafeloader``):
``omnigent/spec/parser.py`` parses ``config.yaml`` with
``yaml.load(..., Loader=_ConfigYamlLoader)`` where
``_ConfigYamlLoader(yaml.SafeLoader)`` is the pure-Python loader (~10-20x
slower than the libyaml ``CSafeLoader``). libyaml *is* available in the
runtime (``yaml.__with_libyaml__`` is True and ``yaml.CSafeLoader`` exists),
so the parse pays the pure-Python tax needlessly. This is a purely internal
(``api``) defect -- no user-visible surface -- so it is asserted directly on
the shipped runtime the runner imports. Fails on the buggy build; passes once
``_ConfigYamlLoader`` is rebased onto ``CSafeLoader``.

Facet B -- re-parse per session, no memoization
(``test_runner_does_not_reparse_shared_bundle_per_session``): the runner's
``_resolve_agent_spec_from_server`` calls ``load(dest, ...)`` -> ``parse(dest)``
unconditionally on every session-create, and the runner's session-spec cache
is keyed by ``session_id``. So a parent session and each of its sub-agent
sessions -- which share the SAME ``(agent_id, version)`` bundle -- each miss
the cache and re-parse the identical bundle YAML (and, recursively, every
sub-agent ``config.yaml`` under ``agents/``). During sub-agent fan-out this
bursts to a meaningful fraction of runner CPU.

This drives the real user journey: register a directory-format bundle (a
parent whose ``tools.agents`` fan out to three sub-agents), bind it to a real
runner, and dispatch a turn in which the parent dispatches all three
sub-agents via ``sys_session_send``. A test-only ``sitecustomize`` shim
injected onto the runner subprocess's ``PYTHONPATH`` records every
``omnigent.spec.parser.parse`` call the runner makes (product code is NOT
modified). Because exactly one bundle is registered, every parsed directory
belongs to it; on the buggy build the parent bundle directory is parsed once
per session (parent create + each sub-agent session), so its parse count is
> 1. A build that memoizes the parsed ``AgentSpec`` by ``(agent_id, version)``
parses it exactly once, so this test then passes.

Run::

    .venv/bin/python -m pytest tests/e2e/test_runner_spec_reparse_fanout.py -v
"""

from __future__ import annotations

import io
import json
import os
import signal
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

from omnigent.runner.identity import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    token_bound_runner_id,
)
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# Turn dispatch + three sub-agent session creates + the parent auto-wake are
# several serial mock-LLM turns, so give the journey generous headroom.
pytestmark = pytest.mark.timeout(600, method="signal")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Number of sub-agents the parent fans out to. Each fan-out creates one
# sub-agent session that shares the parent's (agent_id, version) bundle and
# therefore re-resolves -> re-parses it on the buggy build.
_N_SUBAGENTS = 3

# Sentinel the parent's final scripted turn emits, so the poll can tell the
# fan-out dispatch turn actually ran to completion.
_PARENT_DONE = "REPARSE_PARENT_DONE"

# sitecustomize shim: injected onto the runner subprocess's PYTHONPATH so the
# runner records every omnigent.spec.parser.parse() call (root path) to a
# JSONL file, and dumps the _ConfigYamlLoader identity once. Gated on the
# OMNIGENT_SPEC_PARSE_LOG env var so it is inert in any other process that
# happens to see this directory on its path. Patches BOTH omnigent.spec.parser
# and the re-exported name in omnigent.spec, because omnigent.spec.load() calls
# the parse name bound in the omnigent.spec namespace.
_SITECUSTOMIZE_SRC = r"""
import json
import os
import time

_LOG = os.environ.get("OMNIGENT_SPEC_PARSE_LOG")
if _LOG:
    try:
        import omnigent.spec.parser as _parser
        import omnigent.spec as _spec

        _orig_parse = _parser.parse

        def _counting_parse(root, *args, **kwargs):
            try:
                with open(_LOG, "a") as _fh:
                    _fh.write(json.dumps({"root": str(root), "t": time.time()}) + "\n")
            except Exception:
                pass
            return _orig_parse(root, *args, **kwargs)

        _parser.parse = _counting_parse
        # omnigent.spec.load() references the parse name in the omnigent.spec
        # module namespace; patch it there too.
        _spec.parse = _counting_parse

        _info_path = os.environ.get("OMNIGENT_SPEC_LOADER_INFO")
        if _info_path:
            import yaml as _yaml

            _loader = _parser._ConfigYamlLoader
            _csafe = getattr(_yaml, "CSafeLoader", None)
            with open(_info_path, "w") as _fh:
                json.dump(
                    {
                        "with_libyaml": bool(getattr(_yaml, "__with_libyaml__", False)),
                        "mro": [c.__name__ for c in _loader.__mro__],
                        "is_pure_safeloader": issubclass(_loader, _yaml.SafeLoader),
                        "is_csafeloader": bool(_csafe) and issubclass(_loader, _csafe),
                    },
                    _fh,
                )
    except Exception:
        # Never let instrumentation break the runner under test.
        pass
"""


def _parent_config(name: str, sub_names: list[str], model: str) -> dict[str, Any]:
    """Build the parent ``config.yaml`` dict (spec_version:1 directory format).

    :param name: Unique parent agent name.
    :param sub_names: Sub-agent directory/agent names referenced by ``tools.agents``.
    :param model: Mock-LLM model key for the parent's response queue.
    :returns: The parent config mapping.
    """
    return {
        "spec_version": 1,
        "name": name,
        "prompt": (
            "You are a dispatcher. When the user asks you to run, dispatch EACH "
            "of your sub-agents exactly once via sys_session_send, then wait for "
            "their replies and report done."
        ),
        "executor": {"type": "omnigent", "model": model, "config": {"harness": "openai-agents"}},
        "tools": {"agents": list(sub_names)},
        "os_env": {"type": "caller_process", "cwd": "."},
    }


def _sub_config(name: str, model: str) -> dict[str, Any]:
    """Build one sub-agent ``config.yaml`` dict.

    :param name: Sub-agent name.
    :param model: Mock-LLM model key for this sub-agent's queue.
    :returns: The sub-agent config mapping.
    """
    return {
        "spec_version": 1,
        "name": name,
        "description": f"Echo sub-agent {name}.",
        "prompt": "You are an echo bot. Reply with a short acknowledgement.",
        "executor": {"type": "omnigent", "model": model, "config": {"harness": "openai-agents"}},
        "os_env": {"type": "caller_process", "cwd": "."},
    }


def _build_bundle(parent_cfg: dict[str, Any], subs: dict[str, dict[str, Any]]) -> bytes:
    """Pack a directory-format bundle (config.yaml + agents/<name>/config.yaml).

    The root ``config.yaml`` arcname routes the bundle through the native
    ``omnigent/spec/parser.py`` ``parse()`` path (the code the bug lives in),
    which recursively parses each ``agents/<name>/config.yaml``.

    :param parent_cfg: Parent config mapping.
    :param subs: Mapping of sub-agent name -> its config mapping.
    :returns: gzip tarball bytes ready for multipart upload.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def _add(arcname: str, cfg: dict[str, Any]) -> None:
            data = yaml.safe_dump(cfg, sort_keys=False).encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        _add("config.yaml", parent_cfg)
        for sub_name, sub_cfg in subs.items():
            _add(f"agents/{sub_name}/config.yaml", sub_cfg)
    return buf.getvalue()


@contextmanager
def _live_server_and_instrumented_runner(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, httpx.Client, str]]:
    """Spawn a real server + a real runner whose spec parses are counted.

    Mirrors the ``live_server`` fixture in ``tests/e2e/conftest.py`` but is
    self-contained so the runner subprocess can carry a ``sitecustomize`` shim
    (on its ``PYTHONPATH``) that records every ``parse()`` call. The server
    points its LLM connections at the mock gateway.

    :param tmp_path: Per-test temp dir for the DB, logs, and counter files.
    :param mock_llm_server_url: Session-scoped mock LLM base URL.
    :yields: ``(base_url, http_client, runner_id)``.
    """
    import secrets

    # Counter shim on a dir we prepend to the runner's PYTHONPATH.
    counter_dir = tmp_path / "instrument"
    counter_dir.mkdir(parents=True)
    (counter_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE_SRC)
    parse_log = tmp_path / "parse_log.jsonl"
    loader_info = tmp_path / "loader_info.json"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True)
    server_log = tmp_path / "server.log"
    runner_log = tmp_path / "runner.log"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # Server env: point all LLM traffic at the mock gateway.
    server_env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "ANTHROPIC_API_KEY": "",
    }
    apply_server_env(server_env, _REPO_ROOT)
    server_env["OMNIGENT_RUNNER_TUNNEL_TOKEN"] = binding_token

    # Server-level llm block for the prompt-policy classifier (kept parallel to
    # the conftest live_server so nothing 401s trying to reach api.openai.com).
    server_cfg = tmp_path / "server.yaml"
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

    server_argv = [
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
        "--config",
        str(server_cfg),
    ]

    # Runner env: same LLM wiring, plus the parse-counter shim on PYTHONPATH.
    runner_pythonpath = os.pathsep.join(
        [str(counter_dir), str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]
    )
    runner_env = apply_runner_env(
        {
            **server_env,
            "PYTHONPATH": runner_pythonpath,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_SPEC_PARSE_LOG": str(parse_log),
            "OMNIGENT_SPEC_LOADER_INFO": str(loader_info),
        }
    )

    server_log_fh = open(server_log, "w")  # noqa: SIM115 -- closed in finally
    runner_log_fh = open(runner_log, "w")  # noqa: SIM115 -- closed in finally
    server_proc = subprocess.Popen(
        server_argv,
        env=server_env,
        cwd=compat_server_cwd(),
        stdout=server_log_fh,
        stderr=subprocess.STDOUT,
    )
    runner_proc = subprocess.Popen(
        [runner_executable(), "-m", "omnigent.runner._entry"],
        env=runner_env,
        cwd=compat_runner_cwd(),
        stdout=runner_log_fh,
        stderr=subprocess.STDOUT,
    )

    client = httpx.Client(base_url=base_url, timeout=30.0)
    try:
        deadline = time.monotonic() + 90.0
        ready = False
        last = "not polled"
        while time.monotonic() < deadline:
            if server_proc.poll() is not None:
                last = f"server exited early ({server_proc.returncode})"
                break
            if runner_proc.poll() is not None:
                last = f"runner exited early ({runner_proc.returncode})"
                break
            try:
                health = httpx.get(f"{base_url}/health", timeout=2)
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if (
                    health.status_code == 200
                    and status.status_code == 200
                    and status.json().get("online") is True
                ):
                    ready = True
                    break
                last = f"health={health.status_code} status={status.status_code}"
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(POLL_INTERVAL_S)
        if not ready:
            srv = server_log.read_text()[-3000:] if server_log.exists() else ""
            run = runner_log.read_text()[-3000:] if runner_log.exists() else ""
            raise RuntimeError(
                f"server/runner did not come online within 90s (last={last}).\n"
                f"--- server.log ---\n{srv}\n--- runner.log ---\n{run}"
            )

        # Never let the server-side classifier queue starve the turn.
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )

        yield base_url, client, runner_id
    finally:
        client.close()
        for proc in (runner_proc, server_proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_log_fh.close()
        runner_log_fh.close()


def _find_free_port() -> int:
    """Bind to port 0 to obtain a free TCP port.

    :returns: An available port number.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _create_runner_bound_session_from_bundle(
    client: httpx.Client,
    bundle: bytes,
    runner_id: str,
) -> str:
    """Upload *bundle* (creating a session + agent) and bind it to *runner_id*.

    :param client: HTTP client pointed at the live server.
    :param bundle: gzip tarball bytes for the directory-format bundle.
    :param runner_id: Runner id to bind the session to.
    :returns: The parent session/conversation id.
    """
    create = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    patch = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
    patch.raise_for_status()
    return session_id


def _dispatch_turn(client: httpx.Client, session_id: str, text: str) -> None:
    """POST a user message to *session_id* to start a turn.

    :param client: HTTP client pointed at the live server.
    :param session_id: Runner-bound session id.
    :param text: User prompt text.
    """
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    resp = client.post(f"/v1/sessions/{session_id}/events", json=body)
    resp.raise_for_status()


def _wait_for_child_sessions(
    client: httpx.Client,
    parent_session_id: str,
    expected: int,
    timeout: float = 240.0,
) -> list[str]:
    """Poll the sub-agent session list until *expected* children of the parent appear.

    :param client: HTTP client pointed at the live server.
    :param parent_session_id: The dispatching parent session id.
    :param expected: Number of sub-agent sessions to wait for.
    :param timeout: Max seconds to wait.
    :returns: The discovered child session ids.
    """
    deadline = time.monotonic() + timeout
    seen: set[str] = set()
    while time.monotonic() < deadline:
        resp = client.get("/v1/sessions", params={"kind": "sub_agent", "limit": 1000})
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            cid = str(item.get("id"))
            if cid in seen:
                continue
            snap = client.get(f"/v1/sessions/{cid}")
            if snap.status_code != 200:
                continue
            if snap.json().get("parent_session_id") == parent_session_id:
                seen.add(cid)
        if len(seen) >= expected:
            return sorted(seen)
        time.sleep(POLL_INTERVAL_S)
    return sorted(seen)


def _wait_for_parent_done(
    client: httpx.Client,
    session_id: str,
    timeout: float = 240.0,
) -> None:
    """Poll the parent session until it is idle after having run its turn.

    :param client: HTTP client pointed at the live server.
    :param session_id: The parent session id.
    :param timeout: Max seconds to wait.
    """
    deadline = time.monotonic() + timeout
    seen_running = False
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        body = resp.json()
        status = body.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        blob = json.dumps(body.get("items", []))
        if _PARENT_DONE in blob:
            return
        if status == "idle" and seen_running:
            return
        time.sleep(POLL_INTERVAL_S)


def _parse_counts_by_root(parse_log: Path) -> dict[str, int]:
    """Read the runner's parse-log JSONL into a per-root call count.

    :param parse_log: Path to the JSONL parse log the runner wrote.
    :returns: Mapping of parsed directory path -> number of parse() calls.
    """
    counts: dict[str, int] = {}
    if not parse_log.exists():
        return counts
    for line in parse_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            root = json.loads(line)["root"]
        except (json.JSONDecodeError, KeyError):
            continue
        counts[root] = counts.get(root, 0) + 1
    return counts


def _parent_bundle_root(counts: dict[str, int]) -> str | None:
    """Identify the top-level bundle directory among parsed roots.

    The parent bundle dir is the root under which the sub-agent roots live,
    i.e. some other root starts with ``<root>/agents``.

    :param counts: Per-root parse counts.
    :returns: The parent bundle root path, or ``None`` if undetermined.
    """
    roots = list(counts)
    for root in roots:
        needle = os.path.join(root, "agents")
        if any(other != root and other.startswith(needle) for other in roots):
            return root
    return None


def test_config_loader_uses_libyaml_csafeloader() -> None:
    """Facet A: the config loader must use the libyaml ``CSafeLoader`` when available.

    ``_ConfigYamlLoader`` currently subclasses the pure-Python
    ``yaml.SafeLoader`` even though ``yaml.CSafeLoader`` (libyaml, ~20x faster)
    is importable in the runtime. That is the ~20x parse tax the bug describes.
    Fails on the buggy build; passes once the loader is rebased onto
    ``CSafeLoader`` (with its custom implicit-resolver / bool overrides ported).
    """
    from omnigent.spec.parser import _ConfigYamlLoader

    assert yaml.__with_libyaml__, (
        "libyaml is not available in this runtime; the CSafeLoader speedup "
        "cannot apply. (Not the bug -- an environment prerequisite.)"
    )
    assert hasattr(yaml, "CSafeLoader"), "yaml.CSafeLoader missing despite libyaml"

    is_csafe = issubclass(_ConfigYamlLoader, yaml.CSafeLoader)
    assert is_csafe, (
        "_ConfigYamlLoader is built on the pure-Python yaml.SafeLoader "
        f"(MRO={[c.__name__ for c in _ConfigYamlLoader.__mro__]}) even though "
        "the libyaml CSafeLoader is available -- spec parsing pays the ~20x "
        "pure-Python tax. It should subclass yaml.CSafeLoader."
    )


def test_runner_does_not_reparse_shared_bundle_per_session(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Facet B: the runner must not re-parse a shared bundle once per session.

    Drives the reported journey -- a parent fans out to three sub-agents that
    share the parent's ``(agent_id, version)`` bundle -- and counts how many
    times the runner parsed that bundle directory. On the buggy build the
    session-spec cache is keyed by ``session_id`` and the resolver re-parses
    unconditionally, so the parent bundle is parsed once per session (> 1). A
    build that memoizes the parsed ``AgentSpec`` by ``(agent_id, version)``
    parses it exactly once, making this test pass.
    """
    uid = uuid.uuid4().hex[:8]
    parent_token = f"reparse-run-{uid}"
    parent_model = f"mock-reparse-parent-{uid}"
    sub_names = [f"echo_{i}_{uid}" for i in range(_N_SUBAGENTS)]
    sub_tokens = [f"reparse-echo-{i}-{uid}" for i in range(_N_SUBAGENTS)]
    sub_models = [f"mock-reparse-echo-{i}-{uid}" for i in range(_N_SUBAGENTS)]

    reset_mock_llm(mock_llm_server_url)

    # Parent: one response dispatching all three sub-agents, then two texts
    # (dispatch ack + the post-wake done sentinel).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_dispatch_{i}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": sub_names[i],
                                "title": "dispatch",
                                "args": f"Please acknowledge. Routing marker: {sub_tokens[i]}",
                            }
                        ),
                    }
                    for i in range(_N_SUBAGENTS)
                ]
            },
            {"text": "Dispatched all sub-agents; waiting for replies."},
            {"text": _PARENT_DONE},
        ],
        key=parent_model,
        match=parent_token,
    )
    # Each sub-agent: a single scripted acknowledgement, content-routed.
    for i in range(_N_SUBAGENTS):
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": f"echo {i} acknowledged: {sub_tokens[i]}"}],
            key=sub_models[i],
            match=sub_tokens[i],
        )

    parent_name = f"reparse-parent-{uid}"
    parent_cfg = _parent_config(parent_name, sub_names, parent_model)
    subs = {name: _sub_config(name, sub_models[i]) for i, name in enumerate(sub_names)}
    bundle = _build_bundle(parent_cfg, subs)

    parse_log = tmp_path / "parse_log.jsonl"

    with _live_server_and_instrumented_runner(tmp_path, mock_llm_server_url) as (
        _base_url,
        client,
        runner_id,
    ):
        parent_session_id = _create_runner_bound_session_from_bundle(client, bundle, runner_id)
        _dispatch_turn(
            client,
            parent_session_id,
            f"RUN: dispatch every sub-agent exactly once. Routing marker: {parent_token}",
        )
        _wait_for_parent_done(client, parent_session_id)
        children = _wait_for_child_sessions(client, parent_session_id, _N_SUBAGENTS)

        # Give the last child's session-create resolve a moment to land in the
        # parse log before we read it.
        time.sleep(3.0)
        counts = _parse_counts_by_root(parse_log)

    assert children, (
        "No sub-agent sessions were created for the parent -- the fan-out "
        "journey did not run, so the re-parse could not be exercised. "
        f"parse counts: {counts}"
    )

    parent_root = _parent_bundle_root(counts)
    assert parent_root is not None, (
        "Could not identify the parent bundle directory among the runner's "
        f"parsed roots. parse counts: {counts}"
    )

    parent_parses = counts[parent_root]
    # The defect is that the parse count *scales with the number of sessions*:
    # the runner re-parses the identical bundle YAML once per session (parent
    # create + every sub-agent session), because the session-spec cache is
    # keyed by session_id and the parsed AgentSpec is not memoized by
    # (agent_id, version). A correct memoization parses the shared bundle a
    # small, constant number of times regardless of how many sessions or
    # sub-agents reference it -- once, or at most twice if the extract-validate
    # and resolve steps parse separately on the first miss. We assert the
    # count does not scale with sessions (<= 2). On the buggy build this count
    # equals the number of resolving sessions (13 for parent + 3 sub-agents in
    # a representative run) and grows as more sessions/sub-agents fan out.
    _MAX_ALLOWED_PARSES = 2
    assert parent_parses <= _MAX_ALLOWED_PARSES, (
        f"The runner parsed the shared bundle directory {parent_root!r} "
        f"{parent_parses} times across the parent + {len(children)} sub-agent "
        "sessions that share its (agent_id, version). The parsed AgentSpec is "
        "not memoized by (agent_id, version): the session-spec cache is keyed "
        "by session_id, so every session re-parses the identical bundle YAML, "
        "and the parse count scales with the number of sessions instead of "
        f"staying constant (expected <= {_MAX_ALLOWED_PARSES}). "
        f"Full parse counts by root: {counts}"
    )
