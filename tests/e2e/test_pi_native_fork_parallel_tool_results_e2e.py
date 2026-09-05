"""End-to-end: pi-native fork/resume rebuild must keep parallel tool results adjacent.

Regression coverage: when Omnigent rebuilds a native Pi session from copied
conversation items (a fork with carry-history, or a cold resume -- both go
through :func:`omnigent.pi_native_resume.ensure_local_pi_resume_session` ->
:func:`omnigent.pi_native_resume.pi_session_records_from_session_items`), each
``function_call`` is emitted as its OWN single-``toolCall`` assistant message
and each ``function_call_output`` as an independent ``toolResult`` message. For
a response with multiple (parallel) tool calls the rebuilt Pi JSONL therefore
places unrelated assistant messages (the response's assistant text, and the
sibling tool call) between a tool call and its result. Pi maps each rebuilt
assistant message to its own Anthropic Messages API message, so on the next
turn the provider rejects the reconstructed history with HTTP 400
``unexpected tool_use_id found in tool_result blocks`` and the resumed session
can never continue.

The journey drives the REAL product stack -- a real ``pi`` CLI launched by a
real runner in its own tmux pane, the real native bridge + extension, and the
real fork route (``POST /v1/sessions/{id}/fork``) whose carry-history rebuild
runs :func:`ensure_local_pi_resume_session`. Pi's provider is pointed at an
in-process Anthropic Messages sidecar that both serves scripted SSE responses
AND enforces the documented tool_use/tool_result pairing contract, returning
Anthropic's real 400 body on a violation. No browser is involved (the pi-native
TUI is a runner-owned tmux pane, so this backend/terminal journey is driven and
observed over the HTTP API, exactly like the qwen-native sub-agent wake e2e).

Journey:

1. Create a pi-native session bound to a live runner; send a prompt whose
   scripted response makes TWO tool calls in ONE assistant response, so the
   mirrored Omnigent transcript holds ``function_call A``, ``function_call B``,
   then both ``function_call_output``s.
2. Fork the session (full history carry). pi-native carries history, so the
   runner rebuilds the clone's Pi session JSONL from the copied items.
3. Bind the fork to the runner and send a follow-up prompt in the clone.
4. The clone's next Anthropic Messages request must present every
   ``tool_result`` adjacent to the assistant message holding its ``tool_use``.
   With the bug the rebuilt history orphans the parallel results, the sidecar
   answers with the real API's HTTP 400, and the clone's turn never completes.

The test asserts the CORRECT (post-fix) behavior -- no pairing violation and a
completed clone turn -- so it FAILS on the buggy build (the reproduction) and
PASSES once the rebuild groups each parallel call with its result.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke::

    pytest tests/e2e/test_pi_native_fork_parallel_tool_results_e2e.py -v --timeout=900
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import signal
import socket
import tarfile
import tempfile
import threading
import time
import uuid as _uuid_mod
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import apply_runner_env, apply_server_env
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.server.integration.mock_llm_server import anthropic_sse_text_response

_REPO_ROOT = Path(__file__).resolve().parents[2]

_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = [
    pytest.mark.skipif(
        _pi_unavailable is not None,
        reason=(
            "pi-native fork e2e requires a runnable 'pi' CLI; "
            f"{_pi_unavailable}. Install/fix Pi to run this test."
        ),
    ),
    pytest.mark.timeout(900, method="signal"),
]

# Model id the sidecar echoes and the mock provider's default model.
_PI_MOCK_MODEL = "pi-mock-sonnet"

# Content-routing markers (see _PiAnthropicSidecar._route_sse).
_PARALLEL_MARKER = "parallel-read-probe"
_FOLLOWUP_MARKER = "post-fork-recall"
_DONE_TOKEN = "PARALLEL-TOOLS-DONE"
_FORK_OK_TOKEN = "FORK-RESUME-OK"

# Deterministic tool_use ids so the transcript/rebuild assertions are exact.
_CALL_ID_A = "toolu_par_a"
_CALL_ID_B = "toolu_par_b"

_HEALTH_TIMEOUT_S = 90.0
# pi boot (tmux + extension + managed models.json) + two scripted turns.
_TURN_TIMEOUT_S = 300.0
_POLL_INTERVAL_S = 2.0
_LOOPBACK_NO_PROXY = "localhost,127.0.0.1"

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars that
# must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False, timeout=30.0)


# ── Anthropic pairing contract ──────────────────────────────────────────────


def _anthropic_tool_pairing_violations(messages: list[Any]) -> list[str]:
    """Return Anthropic tool_use/tool_result pairing violations in *messages*.

    Implements the documented Anthropic Messages contract the live API
    enforces: each ``tool_result`` block must reference a ``tool_use`` block in
    the immediately preceding assistant message. This is exactly the check
    behind the real API's HTTP 400 ``unexpected tool_use_id found in
    tool_result blocks``.

    :param messages: The request's ``messages`` array.
    :returns: One ``"messages.<i>: <tool_use_id>"`` entry per orphaned
        ``tool_result`` block; empty when the history is valid.
    """
    violations: list[str] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        result_ids = [
            b.get("tool_use_id")
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if not result_ids:
            continue
        prev = messages[i - 1] if i > 0 else None
        prev_tool_use_ids: set[Any] = set()
        if isinstance(prev, dict) and prev.get("role") == "assistant":
            prev_content = prev.get("content")
            if isinstance(prev_content, list):
                prev_tool_use_ids = {
                    b.get("id")
                    for b in prev_content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                }
        violations.extend(
            f"messages.{i}: {rid}" for rid in result_ids if rid not in prev_tool_use_ids
        )
    return violations


def _sse_text_and_parallel_tool_calls(
    text: str,
    tool_calls: list[dict[str, Any]],
    model: str,
) -> str:
    """Build an Anthropic SSE stream: one text block plus N tool_use blocks.

    The shared mock's ``anthropic_sse_tool_call_response`` emits tool_use
    blocks only; the reported bug's shape ("multiple function_call items in the
    same response, plus assistant text") needs both in ONE assistant response,
    so this local builder emits the text block first, then the tool calls.

    :param text: Assistant text emitted before the tool calls.
    :param tool_calls: ``{"call_id", "name", "input"}`` dicts, one per parallel
        call (``input`` a JSON-serializable object).
    :param model: Model id echoed into ``message_start``.
    :returns: The full SSE body.
    """
    msg_id = f"msg_{_uuid_mod.uuid4().hex[:12]}"
    events: list[str] = []

    def _evt(evt_type: str, data: dict) -> None:
        events.append(f"event: {evt_type}\ndata: {json.dumps(data)}\n\n")

    _evt(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
    )
    _evt(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    _evt(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
    )
    _evt("content_block_stop", {"type": "content_block_stop", "index": 0})
    for offset, tc in enumerate(tool_calls, start=1):
        _evt(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": offset,
                "content_block": {
                    "type": "tool_use",
                    "id": tc["call_id"],
                    "name": tc["name"],
                    "input": {},
                },
            },
        )
        _evt(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": offset,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(tc.get("input", {})),
                },
            },
        )
        _evt("content_block_stop", {"type": "content_block_stop", "index": offset})
    _evt(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 10},
        },
    )
    _evt("message_stop", {"type": "message_stop"})
    return "".join(events)


# ── tool selection (adapt to whatever tools Pi advertises) ──────────────────


def _read_tool_from_request(body: Any) -> dict[str, Any] | None:
    """Pick a side-effect-free file-read tool from a request's advertised tools.

    Pi advertises its available tools in each ``POST /v1/messages`` request's
    ``tools`` array. Rather than hard-code Pi's built-in tool names/schemas
    (which vary by Pi version), pick a read-like tool at request time and build
    a minimal valid input from its schema.

    :param body: Parsed ``POST /v1/messages`` JSON body.
    :returns: The chosen tool dict (``{"name", "input_schema", ...}``) or
        ``None`` when no tool is advertised.
    """
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list) or not tools:
        return None
    named = [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
    if not named:
        return None
    read_hints = ("read", "view", "cat", "open", "get_file", "show")
    for hint in read_hints:
        for tool in named:
            if hint in tool["name"].lower():
                return tool
    # No obvious read tool; fall back to the first advertised tool.
    return named[0]


def _fill_tool_input(tool: dict[str, Any], file_path: str) -> dict[str, Any]:
    """Build a minimal valid input for *tool* that references *file_path*.

    Fills every required (and, failing that, every) string property: a
    path-like property name gets *file_path*; anything else gets an innocuous
    placeholder. Best-effort -- the transcript only needs Pi to attempt both
    calls and mirror a ``function_call_output`` for each (even an error
    result), which is enough to reproduce the parallel-tool rebuild shape.

    :param tool: A tool dict from the request's ``tools`` array.
    :param file_path: Absolute path to reference for path-like properties.
    :returns: A JSON-serializable input object.
    """
    schema = tool.get("input_schema")
    if not isinstance(schema, dict):
        return {"path": file_path}
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    required = schema.get("required")
    required = required if isinstance(required, list) else list(props)
    path_hints = ("path", "file", "filename", "target", "uri")
    out: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str):
            continue
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        prop_type = spec.get("type")
        if any(h in name.lower() for h in path_hints):
            out[name] = file_path
        elif prop_type in (None, "string"):
            out[name] = file_path if not out else ""
        elif prop_type == "integer" or prop_type == "number":
            out[name] = 0
        elif prop_type == "boolean":
            out[name] = False
        elif prop_type == "array":
            out[name] = []
        elif prop_type == "object":
            out[name] = {}
    if not out:
        out = {"path": file_path}
    return out


# ── in-process Anthropic Messages sidecar ───────────────────────────────────


class _PiAnthropicSidecar:
    """Minimal Anthropic Messages endpoint for the native ``pi`` CLI.

    Serves ``POST /v1/messages`` with content-routed scripted SSE responses,
    adapts turn-1's parallel tool calls to whatever tools Pi advertises, and
    enforces the documented tool_use/tool_result pairing contract -- answering
    a violation with the real API's HTTP 400 error body. Records every request
    (parsed body, advertised tool names, computed violations, response status)
    for the test's assertions and diagnostics.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.file_a = ""
        self.file_b = ""
        sidecar = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                if self.path.split("?", 1)[0].rstrip("/") != "/v1/messages":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                messages = body.get("messages") if isinstance(body, dict) else None
                violations = _anthropic_tool_pairing_violations(
                    messages if isinstance(messages, list) else []
                )
                tool_names = (
                    [t.get("name") for t in (body.get("tools") or []) if isinstance(t, dict)]
                    if isinstance(body, dict)
                    else []
                )
                if violations:
                    payload = json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": (
                                    "unexpected tool_use_id found in tool_result "
                                    "blocks: "
                                    + ", ".join(v.split(": ", 1)[1] for v in violations)
                                    + ". Each tool_result block must have a "
                                    "corresponding tool_use block in the previous "
                                    "message."
                                ),
                            },
                        }
                    ).encode()
                    status = 400
                    content_type = "application/json"
                else:
                    payload = sidecar._route_sse(body).encode()
                    status = 200
                    content_type = "text/event-stream"
                with sidecar.lock:
                    sidecar.requests.append(
                        {
                            "body": body,
                            "violations": violations,
                            "status": status,
                            "tool_names": tool_names,
                        }
                    )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def _route_sse(self, body: Any) -> str:
        """Pick the scripted SSE response for a (valid) request body.

        :param body: Parsed ``POST /v1/messages`` JSON body.
        :returns: The SSE stream to serve.
        """
        messages = body.get("messages") if isinstance(body, dict) else None
        messages = messages if isinstance(messages, list) else []
        last_user_text = ""
        has_tool_results = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                has_tool_results = True
            if msg.get("role") != "user":
                continue
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = ""
            if text.strip():
                last_user_text = text

        if _FOLLOWUP_MARKER in last_user_text:
            # The post-fork turn: with a correctly rebuilt history this request
            # validates and the clone's turn completes with this token (the
            # test's user-visible pass signal).
            return anthropic_sse_text_response(_FORK_OK_TOKEN, model=_PI_MOCK_MODEL)
        if _PARALLEL_MARKER in last_user_text and not has_tool_results:
            tool = _read_tool_from_request(body)
            if tool is not None:
                name = str(tool["name"])
                return _sse_text_and_parallel_tool_calls(
                    "Reading both seeded files.",
                    [
                        {
                            "call_id": _CALL_ID_A,
                            "name": name,
                            "input": _fill_tool_input(tool, self.file_a),
                        },
                        {
                            "call_id": _CALL_ID_B,
                            "name": name,
                            "input": _fill_tool_input(tool, self.file_b),
                        },
                    ],
                    _PI_MOCK_MODEL,
                )
            return anthropic_sse_text_response(_DONE_TOKEN, model=_PI_MOCK_MODEL)
        if has_tool_results:
            # Tool-cycle completion inside the live (pre-fork) session.
            return anthropic_sse_text_response(_DONE_TOKEN, model=_PI_MOCK_MODEL)
        return anthropic_sse_text_response("OK", model=_PI_MOCK_MODEL)

    def close(self) -> None:
        """Shut the HTTP server down and join its thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ── rig: real server + runner wired to the sidecar, pi-native launchable ────


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ambient_free_environ() -> dict[str, str]:
    """``os.environ`` minus ambient runner/host identity vars (hermetic stack)."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        and k
        not in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_REMOTE_AUTH_TOKEN",
            "OMNIGENT_PROCESS_LOG_FILE",
        )
    }


def _merged_no_proxy(env: dict[str, str]) -> str:
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    parts = [p for p in existing.split(",") if p]
    for host in _LOOPBACK_NO_PROXY.split(","):
        if host not in parts:
            parts.append(host)
    return ",".join(parts)


def _write_pi_provider_config(config_home: Path, sidecar_url: str) -> None:
    """Write a ``kind: key`` anthropic-family provider pointing at the sidecar.

    ``resolve_pi_native_provider`` reads ``$OMNIGENT_CONFIG_HOME/config.yaml``
    and translates this into Pi ``models.json`` config (api
    ``anthropic-messages``, baseUrl = sidecar), so the ``pi`` process the
    runner launches sends its turns to the sidecar.

    :param config_home: Directory used as ``$OMNIGENT_CONFIG_HOME``.
    :param sidecar_url: Base URL of the Anthropic Messages sidecar.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  mock-pi:\n"
        "    kind: key\n"
        "    default: [anthropic]\n"
        "    anthropic:\n"
        f'      base_url: "{sidecar_url}"\n'
        '      api_key: "mock-key"\n'
        "      models:\n"
        f"        default: {_PI_MOCK_MODEL}\n"
    )


@pytest.fixture()
def pi_sidecar() -> Iterator[_PiAnthropicSidecar]:
    """A running Anthropic Messages sidecar for the native pi CLI."""
    sidecar = _PiAnthropicSidecar()
    try:
        yield sidecar
    finally:
        sidecar.close()


@pytest.fixture()
def pi_fork_rig(
    pi_sidecar: _PiAnthropicSidecar,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """Server + runner wired so a pi-native session talks to the sidecar.

    The runner env carries ``OMNIGENT_CONFIG_HOME`` (a writable dir holding the
    mock-pi provider config), ``OMNIGENT_PI_PATH`` (the pi binary), and
    ``OMNIGENT_RUNNER_WORKSPACE`` (the runner-owned pi terminal needs a
    workspace to launch headlessly). ``HOME`` is redirected so an ambient
    ``~/.omnigent`` / ``~/.pi`` can't shadow the test's provider config.

    :returns: ``(base_url, runner_id, runner_log_path)``.
    """
    pi_path = os.environ.get("OMNIGENT_PI_PATH", "").strip() or _pi_which()
    if pi_path is None:
        pytest.skip("pi CLI is required for the pi-native fork parallel-tools repro")

    work = tmp_path_factory.mktemp("pi_fork")
    artifacts = work / "artifacts"
    workspace = work / "ws"
    home_dir = work / "home"
    config_home = work / "config-home"
    for path in (artifacts, workspace, home_dir, config_home):
        path.mkdir(parents=True, exist_ok=True)
    _write_pi_provider_config(config_home, pi_sidecar.url)
    # The parallel `read` calls target these; seed them so Pi's tools have real
    # files to read.
    (workspace / "alpha.txt").write_text("alpha-contents\n")
    (workspace / "beta.txt").write_text("beta-contents\n")
    pi_sidecar.file_a = str(workspace / "alpha.txt")
    pi_sidecar.file_b = str(workspace / "beta.txt")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = _ambient_free_environ()
    shared_env["NO_PROXY"] = _merged_no_proxy(shared_env)
    shared_env["no_proxy"] = shared_env["NO_PROXY"]

    # The runner-owned pi tmux socket lives under
    # ``tempfile.mkdtemp(prefix="omnigent-terminal-")``, which honors TMPDIR.
    # A Unix domain socket path is capped at ~108 bytes (``sun_path``); a deep
    # checkout/tmp prefix (e.g. a nested worktree or ``tmp_path_factory`` root)
    # blows that limit and pi-native fails to launch with ``tmux launch failed
    # ... File name too long``. Pin a short ``/tmp`` base for the stack so the
    # socket path stays well under the limit regardless of the ambient TMPDIR.
    short_tmp = Path(tempfile.mkdtemp(prefix="pi-fork-", dir="/tmp"))
    for _tmp_key in ("TMPDIR", "TMP", "TEMP"):
        shared_env[_tmp_key] = str(short_tmp)

    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    apply_server_env(server_env, _REPO_ROOT)

    runner_env = apply_runner_env(
        {
            **shared_env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
            "HOME": str(home_dir),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_PI_PATH": pi_path,
        }
    )
    sdk_path = str(_REPO_ROOT / "sdks" / "python-client")
    existing_pp = runner_env.get("PYTHONPATH") or server_env.get("PYTHONPATH", "")
    runner_env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_REPO_ROOT), sdk_path, existing_pp) if p
    )

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    import subprocess
    import sys

    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
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
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "pi-native fork rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )
        yield (base_url, runner_id, runner_log)
    finally:
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()
        import shutil as _shutil

        _shutil.rmtree(short_tmp, ignore_errors=True)


def _pi_which() -> str | None:
    import shutil

    return shutil.which("pi")


# ── API driving helpers ─────────────────────────────────────────────────────


def _create_native_pi_session(base_url: str, runner_id: str) -> str:
    """Register the ``pi-native`` wrapper agent and bind its session.

    Reuses the exact terminal-first spec ``omnigent pi`` ships
    (:func:`omnigent.pi_native._materialize_pi_agent_spec`) and stamps the same
    wrapper / terminal-first labels the CLI writes. Binding the session to the
    runner triggers the runner's pi-native auto-launch (tmux + bridge +
    extension + managed models.json).

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    from omnigent.pi_native import _SESSION_LABELS, _materialize_pi_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_pi_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("pi-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {"labels": dict(_SESSION_LABELS)}
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("pi-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_runner(base_url, session_id, runner_id)
    return session_id


def _bind_runner(base_url: str, session_id: str, runner_id: str) -> None:
    """PATCH *session_id* onto *runner_id* (the same bind the REPL/CLI does)."""
    patch = _client.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()


def _send_message(base_url: str, session_id: str, text: str) -> None:
    """POST a user message to the session's event stream.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param text: The user message body.
    """
    resp = _client.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    resp.raise_for_status()


def _session_items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Return the session's committed items in chronological order."""
    resp = _client.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [item for item in data if isinstance(item, dict)]


def _assistant_text_blob(items: list[dict[str, Any]]) -> str:
    """Concatenate all assistant message text in *items*."""
    out: list[str] = []
    for item in items:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    out.append(block["text"])
    return "\n".join(out)


def _wait_for_items(
    base_url: str,
    session_id: str,
    predicate: Any,
    *,
    timeout_s: float,
    what: str,
) -> list[dict[str, Any]]:
    """Poll ``/items`` until *predicate(items)* holds; return those items."""
    deadline = time.monotonic() + timeout_s
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = _session_items(base_url, session_id)
        if predicate(last):
            return last
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"Timed out waiting for {what}. Last item types: {[i.get('type') for i in last]}"
    )


def _wait_for_request(
    sidecar: _PiAnthropicSidecar,
    predicate: Any,
    *,
    timeout_s: float,
    what: str,
) -> dict[str, Any]:
    """Poll the sidecar until a recorded request matches *predicate*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with sidecar.lock:
            for record in sidecar.requests:
                if predicate(record):
                    return record
        time.sleep(0.5)
    with sidecar.lock:
        seen = len(sidecar.requests)
    raise AssertionError(f"Sidecar never received {what} (saw {seen} requests).")


def _last_user_text(record: dict[str, Any]) -> str:
    """Extract the last non-empty user text from a recorded request."""
    messages = record["body"].get("messages", [])
    last = ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = ""
        if text.strip():
            last = text
    return last


def _dump_diagnostics(
    sidecar: _PiAnthropicSidecar,
    runner_log: Path,
    items: list[dict[str, Any]],
) -> None:
    """Print sidecar/transcript/runner diagnostics (shown by pytest on fail)."""
    print("\n===== sidecar requests =====")
    with sidecar.lock:
        for i, rec in enumerate(sidecar.requests):
            print(
                f"[{i}] status={rec['status']} tools={rec.get('tool_names')} "
                f"violations={rec['violations']} last_user={_last_user_text(rec)!r}"
            )
    print("===== transcript item types =====")
    for item in items:
        print(
            f"  {item.get('type')} role={item.get('role')} "
            f"call_id={item.get('call_id')} name={item.get('name')}"
        )
    print("===== runner.log tail =====")
    try:
        print(runner_log.read_text()[-4000:])
    except OSError:
        print("(no runner log)")


# ── the reproduction ────────────────────────────────────────────────────────


def test_pi_native_fork_rebuild_keeps_parallel_tool_results_adjacent(
    pi_fork_rig: tuple[str, str, Path],
    pi_sidecar: _PiAnthropicSidecar,
) -> None:
    """Fork of a pi-native session with parallel tool calls must stay usable.

    Guards the defect where the fork's rebuilt Pi session JSONL orphans the
    parallel tool results behind unrelated assistant messages, so the clone's
    first request violates Anthropic's tool_use/tool_result pairing contract
    (HTTP 400 ``unexpected tool_use_id found in tool_result blocks``) and the
    clone can never answer again.
    """
    base_url, runner_id, runner_log = pi_fork_rig
    session_id = _create_native_pi_session(base_url, runner_id)
    items: list[dict[str, Any]] = []
    try:
        # --- Step 1: one turn whose response makes TWO parallel tool calls.
        _send_message(base_url, session_id, f"Read both seeded files. {_PARALLEL_MARKER}")
        items = _wait_for_items(
            base_url,
            session_id,
            lambda its: _DONE_TOKEN in _assistant_text_blob(its),
            timeout_s=_TURN_TIMEOUT_S,
            what="the pre-fork turn to complete (parallel tools + done token)",
        )

        # Precondition: the mirrored transcript holds the parallel-call shape
        # this bug needs -- both function_calls committed before both outputs.
        call_pos = {
            item.get("call_id"): idx
            for idx, item in enumerate(items)
            if item.get("type") == "function_call"
        }
        output_pos = {
            item.get("call_id"): idx
            for idx, item in enumerate(items)
            if item.get("type") == "function_call_output"
        }
        if not ({_CALL_ID_A, _CALL_ID_B} <= set(call_pos)) or not (
            {_CALL_ID_A, _CALL_ID_B} <= set(output_pos)
        ):
            _dump_diagnostics(pi_sidecar, runner_log, items)
        assert {_CALL_ID_A, _CALL_ID_B} <= set(call_pos), (
            "Both parallel function_calls must mirror into the transcript; "
            f"saw calls {sorted(call_pos)}."
        )
        assert {_CALL_ID_A, _CALL_ID_B} <= set(output_pos), (
            "Both parallel function_call_outputs must mirror into the transcript; "
            f"saw outputs {sorted(output_pos)}."
        )
        assert max(call_pos.values()) < min(output_pos.values()), (
            "Precondition drift: the extension no longer mirrors both parallel "
            "calls before their outputs, so this journey can't exercise the "
            f"rebuild bug (calls at {call_pos}, outputs at {output_pos})."
        )

        # --- Step 2: fork the session (full history carry). pi-native carries
        # history, so the runner rebuilds the clone's Pi JSONL from the items.
        fork = _client.post(
            f"{base_url}/v1/sessions/{session_id}/fork",
            json={},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=30.0,
        )
        fork.raise_for_status()
        # The fork route returns a SessionResponse (id field is ``id``); the
        # create route uses ``session_id``. Accept either so the id extraction
        # can't silently break on a response-shape refactor.
        fork_json = fork.json()
        fork_id = str(fork_json.get("id") or fork_json["session_id"])

        # --- Step 3: bind the clone and continue the conversation.
        _bind_runner(base_url, fork_id, runner_id)
        _send_message(base_url, fork_id, f"What did the tools return? {_FOLLOWUP_MARKER}")

        # --- Step 4: the clone's provider request must honor Anthropic's
        # pairing contract. With the bug the rebuilt history orphans the
        # parallel tool results and the sidecar answers with the real API's
        # HTTP 400; the assertion names the orphaned tool_use ids.
        record = _wait_for_request(
            pi_sidecar,
            lambda r: _FOLLOWUP_MARKER in _last_user_text(r),
            timeout_s=_TURN_TIMEOUT_S,
            what="the forked session's follow-up request",
        )
        if record["violations"]:
            _dump_diagnostics(pi_sidecar, runner_log, _session_items(base_url, fork_id))
        assert not record["violations"], (
            "Forked pi-native session sent a rebuilt history that violates "
            "Anthropic's tool_use/tool_result pairing contract -- the live API "
            "rejects it with HTTP 400 'unexpected tool_use_id found in "
            "tool_result blocks' and the clone cannot continue: "
            f"{record['violations']}."
        )

        # And the user-visible outcome: the clone's turn completes.
        _wait_for_items(
            base_url,
            fork_id,
            lambda its: _FORK_OK_TOKEN in _assistant_text_blob(its),
            timeout_s=_TURN_TIMEOUT_S,
            what="the forked session's turn to complete",
        )
    finally:
        for sid in (locals().get("fork_id"), session_id):
            if sid:
                with contextlib.suppress(httpx.HTTPError):
                    _client.delete(f"{base_url}/v1/sessions/{sid}", timeout=10.0)
