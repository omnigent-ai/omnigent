"""Tests for the fast ``MessageDisplay`` deltas-appender hook."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import message_display_hook as hook
from omnigent.harnesses.claude_native.bridge import read_message_deltas_from_offset


def _run_hook(
    payload: dict[str, object], bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    """
    Drive the hook ``main`` with one payload on stdin.

    :param payload: Hook JSON object to feed on stdin, e.g.
        ``{"hook_event_name": "MessageDisplay", "message_id": "m1",
        "index": 0, "final": False, "delta": "hi"}``.
    :param bridge_dir: Bridge directory the hook should append to.
    :param monkeypatch: Pytest monkeypatch fixture used to set stdin.
    :returns: The hook's process exit code.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return hook.main(["--bridge-dir", str(bridge_dir)])


def test_message_display_hook_appends_well_formed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A valid ``MessageDisplay`` payload is appended verbatim, in order.

    Fails if the hook drops fields, reorders, or writes a shape the
    bridge reader can't parse — which would break live streaming end
    to end (the forwarder would forward nothing).
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    assert (
        _run_hook(
            {
                "hook_event_name": "MessageDisplay",
                "message_id": "m1",
                "index": 0,
                "final": False,
                "delta": "Hello ",
            },
            bridge_dir,
            monkeypatch,
        )
        == 0
    )
    assert (
        _run_hook(
            {
                "hook_event_name": "MessageDisplay",
                "message_id": "m1",
                "index": 1,
                "final": True,
                "delta": "world",
            },
            bridge_dir,
            monkeypatch,
        )
        == 0
    )

    result = read_message_deltas_from_offset(bridge_dir, 0)
    # Both chunks land, in order, with every field preserved — proving
    # the on-disk shape round-trips through the reader the forwarder uses.
    assert [(d.message_id, d.index, d.final, d.delta) for d in result.deltas] == [
        ("m1", 0, False, "Hello "),
        ("m1", 1, True, "world"),
    ]


def test_message_display_hook_writes_owner_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The deltas file is created with owner-only (0600) permissions.

    Fails if the streamed assistant text (same content as the message)
    becomes world-readable on a shared host.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _run_hook(
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "index": 0, "delta": "x"},
        bridge_dir,
        monkeypatch,
    )
    mode = (bridge_dir / hook.MESSAGE_DELTAS_FILE).stat().st_mode
    assert oct(mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "MessageDisplay", "delta": "no id"},
        {"hook_event_name": "MessageDisplay", "message_id": "", "delta": "empty id"},
        {"hook_event_name": "MessageDisplay", "message_id": "m1"},
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "delta": 123},
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "delta": None},
    ],
    ids=["missing-id", "empty-id", "missing-delta", "non-string-delta", "null-delta"],
)
def test_message_display_hook_skips_unforwardable_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    """
    Payloads lacking a usable ``message_id``/``delta`` write nothing.

    Fails if the hook appends a record the forwarder couldn't turn into
    a valid delta event (e.g. an empty message id or a non-string delta),
    which would surface as a malformed SSE event downstream.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    assert _run_hook(payload, bridge_dir, monkeypatch) == 0
    # No file at all — there was nothing forwardable to record.
    assert not (bridge_dir / hook.MESSAGE_DELTAS_FILE).exists()


def test_message_display_hook_defaults_missing_index_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A single-chunk message with no ``index`` still forwards (index 0).

    Fails if a missing index is dropped or coerced to something the
    reader rejects, which would lose the one-and-only chunk of a short
    assistant message.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _run_hook(
        {"hook_event_name": "MessageDisplay", "message_id": "m1", "final": True, "delta": "hi"},
        bridge_dir,
        monkeypatch,
    )
    result = read_message_deltas_from_offset(bridge_dir, 0)
    assert [(d.message_id, d.index, d.final, d.delta) for d in result.deltas] == [
        ("m1", 0, True, "hi")
    ]


def test_message_display_hook_swallows_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    Malformed stdin exits 0 (never blocks Claude) and writes nothing.

    Claude blocks on command hooks, so a parse error must be a silent
    no-op for the TUI; fails if the hook raises or appends garbage.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert hook.main(["--bridge-dir", str(bridge_dir)]) == 0
    assert not (bridge_dir / hook.MESSAGE_DELTAS_FILE).exists()
    # Diagnostic goes to stderr so it never lands in Claude's stdout
    # (which Claude would interpret as hook output).
    assert "malformed JSON" in capsys.readouterr().err


def test_message_display_hook_many_appends_stay_line_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Many successive appends to the shared file stay newline-framed.

    Each ``MessageDisplay`` chunk is a separate hook invocation appending
    one line; this asserts every line is independently parseable and the
    reader's reported offset reaches EOF after consuming them all. Fails
    if the hook ever wrote a record without a trailing newline (which
    would make the next record's bytes glom onto it and break the
    reader's per-line decode). NOTE: this exercises sequential appends,
    not true concurrent subprocesses — the O_APPEND atomicity that makes
    real per-chunk parallelism safe is a POSIX guarantee, not asserted
    here.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    for i in range(50):
        _run_hook(
            {
                "hook_event_name": "MessageDisplay",
                "message_id": "m1",
                "index": i,
                "delta": f"c{i} ",
            },
            bridge_dir,
            monkeypatch,
        )
    result = read_message_deltas_from_offset(bridge_dir, 0)
    # All 50 chunks parse and arrive in index order — no line was torn.
    assert [d.index for d in result.deltas] == list(range(50))
    assert result.byte_offset == os.path.getsize(bridge_dir / hook.MESSAGE_DELTAS_FILE)


# ── import-cost regression guards ────────────────────────────
#
# Claude Code blocks its TUI on command hooks, so every module on a hook's
# import path is paid per streamed chunk / statusline tick / tool call.
# ``omnigent/__init__`` re-exports lazily (PEP 562) precisely to keep these
# subprocesses cheap. The guards below pin the import graph in a fresh
# interpreter — the deterministic form of the latency claim; the wall-clock
# form is the ``native_hook_spawn`` benchmark journey (dev/benchmarks).

_HEAVY_IMPORTS = (
    "fastapi",
    "httpx",
    "omnigent.harnesses.claude_native.bridge",
    "omnigent.inner.databricks_executor",
    "omnigent.inner.datamodel",
    "omnigent.models.model_catalog",
    "omnigent.spec.parser",
    "pydantic",
)


def _adopt_import_roots() -> str:
    """
    Statement that gives a child interpreter this process's import roots.

    A ``python -I`` child ignores ``PYTHONPATH`` and the cwd, so without it
    ``omnigent`` would resolve from whatever is installed rather than the
    checkout under test.
    """
    roots = [path for path in sys.path if path]
    return f"sys.path[:0] = {roots!r}"


def _heavy_imports_after(statements: str) -> list[str]:
    """
    Run *statements* in a fresh interpreter and report loaded heavy modules.

    :param statements: Newline-joined Python statements to execute, e.g.
        ``"import omnigent"``.
    :returns: The subset of :data:`_HEAVY_IMPORTS` present in the child's
        ``sys.modules`` after *statements* ran.
    """
    code = "\n".join(
        (
            "import json, sys",
            _adopt_import_roots(),
            statements,
            f"heavy = [m for m in {list(_HEAVY_IMPORTS)!r} if m in sys.modules]",
            "print(json.dumps(heavy))",
        )
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_package_init_defers_the_heavy_import_graph() -> None:
    """
    ``import omnigent`` alone loads none of the heavy graph.

    The package init used to eagerly import the datamodel/executor graph,
    taxing every hook subprocess ~250 ms before its first line ran. The
    same child also proves the init's one eager side effect (the FIPS md5
    patch) still applies.
    """
    statements = "\n".join(
        (
            "import omnigent",
            "import hashlib",
            "assert hashlib.md5.__name__ == '_fips_safe_md5', hashlib.md5.__name__",
        )
    )
    assert _heavy_imports_after(statements) == []


def test_package_lazy_exports_resolve_on_access() -> None:
    """
    The lazy re-exports keep the package's public import contract.

    Plain and optional exports, ``from omnigent import`` forms, bare
    submodule attribute access, and ``dir()`` all resolve exactly as the
    eager init did — laziness must never be observable beyond timing.
    """
    statements = "\n".join(
        (
            "import omnigent",
            "from omnigent import AgentDef, Executor, load_agent_def",
            "assert omnigent.TurnComplete is not None",
            "assert 'omnigent.inner.executor' in sys.modules",
            "_ = omnigent.DatabricksExecutor  # optional: a class or None, never a raise",
            "assert omnigent.inner is not None",
            "assert 'TurnComplete' in dir(omnigent)",
        )
    )
    _heavy_imports_after(statements)  # the child's asserts are the test


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("omnigent.harnesses.claude_native.message_display_hook", frozenset()),
        ("omnigent.harnesses.claude_native.status", frozenset()),
        ("omnigent.harnesses.claude_native.bridge_state", frozenset()),
        (
            "omnigent.harnesses.claude_native.hook",
            # Every subcommand pays this import per spawn: stdlib + the
            # stdlib-only bridge-state readers; httpx, the policy machinery
            # and ``bridge`` load inside the subcommands that need them.
            frozenset(),
        ),
    ],
)
def test_hook_entrypoints_stay_import_light(module: str, allowed: frozenset[str]) -> None:
    """
    A hook entrypoint's fresh-interpreter import graph stays bounded.

    Claude Code spawns these per streamed chunk / statusline tick / tool
    call and blocks on them, so a heavy dependency creeping onto any of
    these import paths is a direct TUI-latency regression even while
    every functional test still passes.
    """
    loaded = _heavy_imports_after(f"import {module}")
    assert set(loaded) <= allowed, f"{module} newly imports {sorted(set(loaded) - allowed)}"


# Modules a policy evaluation never touches. Each one on the evaluate-policy run
# path is paid per hook spawn; ``bridge`` alone pulls in most of the others.
_POLICY_UNRELATED_IMPORTS = (
    "asyncio",
    "filelock",
    "http.server",
    "omnigent.harnesses.claude_native.bridge",
    "omnigent.harnesses.claude_native.failure_telemetry",
    "omnigent.harnesses.kiro_native.bridge",
    "omnigent.host.local_server",
    "omnigent.tools.base",
)


class _PolicyAllowServer(ThreadingHTTPServer):
    """Loopback stand-in for the Omnigent server's ``/policies/evaluate``."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _PolicyAllowHandler)
        self.posts: list[str] = []


class _PolicyAllowHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.posts.append(self.path)
        body = json.dumps({"result": "POLICY_ACTION_ALLOW"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@contextlib.contextmanager
def _policy_allow_server() -> Iterator[tuple[str, list[str]]]:
    """Serve ALLOW verdicts on a loopback port; yields ``(base_url, POSTed paths)``."""
    server = _PolicyAllowServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.posts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_isolated_hook(code: str, payload: str, env: dict[str, str]) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("direct_server", [False, True], ids=["no_config", "direct_server"])
def test_evaluate_policy_run_imports_only_policy_modules(
    tmp_path: Path, direct_server: bool
) -> None:
    """
    An evaluate-policy hook run loads none of the modules unrelated to the check.

    ``no_config`` stops at the governed-session checks. ``direct_server`` adds
    ``permission_hook.json`` so the run builds request headers, POSTs the
    evaluation to a loopback server and parses the verdict: the Python fallback
    the runner's hook command takes when the relay is not up.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "bridge.json").write_text(
        json.dumps({"bridge_id": "bridge_test", "active_session_id": "sess_test"}),
        encoding="utf-8",
    )
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        }
    )
    code = "\n".join(
        (
            "import json, runpy, sys",
            _adopt_import_roots(),
            f"sys.argv = ['hook', 'evaluate-policy', '--bridge-dir', {str(bridge_dir)!r}]",
            "status = 0",
            "try:",
            "    runpy.run_module('omnigent.harnesses.claude_native.hook', run_name='__main__')",
            "except SystemExit as exc:",
            "    status = exc.code if isinstance(exc.code, int) else 1",
            f"loaded = [m for m in {list(_POLICY_UNRELATED_IMPORTS)!r} if m in sys.modules]",
            "print(json.dumps({'status': status, 'loaded': loaded}))",
        )
    )
    # A bare environment keeps the probe hermetic: CI/pytest markers would switch
    # telemetry off and skip its installation-id lookup, and a private HOME keeps
    # the run away from the real data dir.
    home = tmp_path / "home"
    home.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home))
    allowed: set[str] = set()
    if direct_server:
        with _policy_allow_server() as (base_url, posts):
            (bridge_dir / "permission_hook.json").write_text(
                json.dumps(
                    {"ap_server_url": base_url, "ap_auth_headers": {"Authorization": "Bearer t"}}
                ),
                encoding="utf-8",
            )
            result = _run_isolated_hook(code, payload, env)
        assert posts == ["/v1/sessions/sess_test/policies/evaluate"]
        # The real POST goes through httpcore, whose anyio layer imports asyncio.
        allowed.add("asyncio")
    else:
        result = _run_isolated_hook(code, payload, env)
    assert result["status"] == 0
    unexpected = [module for module in result["loaded"] if module not in allowed]
    assert unexpected == [], (
        f"evaluate-policy run imports modules the policy check never uses: {unexpected}"
    )
