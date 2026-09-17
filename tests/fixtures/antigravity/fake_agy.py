#!/usr/bin/env python3
"""Scripted ``agy`` stand-in for antigravity-native e2e tests.

The real Antigravity CLI is OAuth-only (interactive Google sign-in), so CI
cannot run it. This binary emulates the two surfaces the omnigent runner and
reader actually drive, letting the REAL runner launch/cold-start/read/rotate
paths execute end to end:

* a minimal full-screen TUI in the tmux pane — a composer framed by ``─``
  separators with agy's ``? for shortcuts`` idle footer, echoing typed turns
  and handling ``/clear`` by minting a fresh cascade (exactly the signal a
  real ``/clear`` produces);
* agy's loopback TLS connect-RPC endpoint (self-signed, JSON bodies):
  Heartbeat, GetConversationMetadata, GetAvailableModels, StartCascade
  (which also writes ``<gemini_dir>/antigravity-cli/conversations/<id>.db``,
  the cold-start's ownership proof), GetCascadeTrajectorySteps, and
  GetAllCascadeTrajectories (the rotation detector's summary feed).

Runs on the system ``python3`` with stdlib only. Discovery finds it because
its script path contains ``bin/agy`` (the ``pgrep -f bin/agy`` cmdline match)
and its pid owns the TLS listener (lsof attribution).
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SERVICE_PREFIX = "/exa.language_server_pb.LanguageServerService/"
_SEPARATOR = "─" * 30
_IDLE_FOOTER = "  ? for shortcuts"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z~]")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _State:
    def __init__(self, gemini_dir: Path) -> None:
        self.lock = threading.Lock()
        self.gemini_dir = gemini_dir
        self.cascades: dict[str, dict] = {}
        self.current: str | None = None
        self.transcript: list[tuple[str, str]] = []

    def register_cascade(self, cascade_id: str, *, touched: bool) -> None:
        conversations = self.gemini_dir / "antigravity-cli" / "conversations"
        conversations.mkdir(parents=True, exist_ok=True)
        (conversations / f"{cascade_id}.db").touch()
        self.cascades[cascade_id] = {
            "steps": [],
            "trajectory_id": str(uuid.uuid4()),
            "lastUserInputTime": None,
            "lastModifiedTime": _now_iso() if touched else None,
        }
        self.current = cascade_id

    def add_turn(self, text: str) -> str:
        if self.current is None:
            self.register_cascade(str(uuid.uuid4()), touched=False)
        cascade_id = self.current
        cascade = self.cascades[cascade_id]
        turn_time = _now_iso()
        execution_id = str(uuid.uuid4())
        trajectory_id = cascade["trajectory_id"]
        cascade["steps"].append(
            {
                "type": "CORTEX_STEP_TYPE_USER_INPUT",
                "status": "CORTEX_STEP_STATUS_DONE",
                "metadata": {
                    "createdAt": turn_time,
                    "executionId": execution_id,
                    "sourceTrajectoryStepInfo": {
                        "trajectoryId": trajectory_id,
                        "cascadeId": cascade_id,
                    },
                },
                "userInput": {"userResponse": text, "items": [{"text": text}]},
            }
        )
        reply = f"FAKE_AGY_REPLY {text}"
        cascade["steps"].append(
            {
                "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                "status": "CORTEX_STEP_STATUS_DONE",
                "metadata": {
                    "createdAt": _now_iso(),
                    "executionId": execution_id,
                    "sourceTrajectoryStepInfo": {
                        "trajectoryId": trajectory_id,
                        "stepIndex": len(cascade["steps"]),
                        "cascadeId": cascade_id,
                    },
                },
                "plannerResponse": {"response": reply, "modifiedResponse": reply},
            }
        )
        cascade["lastUserInputTime"] = turn_time
        cascade["lastModifiedTime"] = turn_time
        return reply

    def summaries(self) -> dict:
        out = {}
        for cascade_id, cascade in self.cascades.items():
            out[cascade_id] = {
                "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
                "trajectoryMetadata": {"rootConversationId": cascade_id},
                "status": "CASCADE_RUN_STATUS_IDLE",
                "lastUserInputTime": cascade["lastUserInputTime"],
                "lastModifiedTime": cascade["lastModifiedTime"],
            }
        return {"trajectorySummaries": out}


def _make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return  # never write to the TUI's stdout/stderr

        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self.path.startswith(_SERVICE_PREFIX):
                self._reply(404, {"error": "unknown service"})
                return
            method = self.path[len(_SERVICE_PREFIX) :]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                request = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                request = {}
            with state.lock:
                if method == "Heartbeat":
                    self._reply(200, {})
                elif method == "GetConversationMetadata":
                    conversation_id = request.get("conversationId")
                    if conversation_id in state.cascades:
                        self._reply(200, {"metadata": {"rootConversationId": conversation_id}})
                    else:
                        self._reply(500, {"error": "trajectory not found"})
                elif method == "GetAvailableModels":
                    self._reply(200, {"models": {"MODEL_PLACEHOLDER_M20": {"name": "fake"}}})
                elif method == "StartCascade":
                    cascade_id = request.get("cascadeId") or str(uuid.uuid4())
                    if cascade_id not in state.cascades:
                        state.register_cascade(cascade_id, touched=False)
                    self._reply(200, {})
                elif method == "GetCascadeTrajectorySteps":
                    cascade = state.cascades.get(request.get("cascadeId") or "")
                    steps = list(cascade["steps"]) if cascade else []
                    self._reply(200, {"steps": steps})
                elif method == "GetAllCascadeTrajectories":
                    self._reply(200, state.summaries())
                else:
                    self._reply(404, {"error": f"unimplemented method {method}"})

    return Handler


def _start_rpc_server(state: _State) -> ThreadingHTTPServer:
    cert_dir = Path(tempfile.mkdtemp(prefix="fake-agy-tls-"))
    cert = cert_dir / "cert.pem"
    key = cert_dir / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _draw(state: _State, notice: str = "") -> None:
    lines = [" Antigravity (fake e2e stand-in)", ""]
    for role, text in state.transcript[-8:]:
        lines.append(f" {role}: {text}")
    if notice:
        lines.append(f" {notice}")
    out = sys.stdout
    out.write("\x1b[2J\x1b[H")
    out.write("\n".join(lines) + "\n")
    out.write(_SEPARATOR + "\n")
    out.write("> \n")
    out.write(_SEPARATOR + "\n")
    out.write(_IDLE_FOOTER)
    # Park the cursor after the "> " prompt (two rows up) so typed/pasted
    # drafts echo inside the composer region the bridge inspects.
    out.write("\x1b[2A\r\x1b[2C")
    out.flush()


def _clean(line: str) -> str:
    line = line.replace("\x1b[200~", "").replace("\x1b[201~", "")
    line = _ANSI_RE.sub("", line)
    return "".join(ch for ch in line if ch.isprintable()).strip()


def main() -> int:
    gemini_dir = Path.home() / ".gemini"
    for arg in sys.argv[1:]:
        if arg.startswith("--gemini_dir="):
            gemini_dir = Path(arg.partition("=")[2])
    gemini_dir.mkdir(parents=True, exist_ok=True)
    state = _State(gemini_dir)
    _start_rpc_server(state)
    _draw(state)
    while True:
        try:
            raw = sys.stdin.readline()
        except KeyboardInterrupt:
            return 0
        if raw == "":
            return 0
        text = _clean(raw)
        if not text:
            _draw(state)
            continue
        if text == "/clear":
            with state.lock:
                state.register_cascade(str(uuid.uuid4()), touched=True)
                state.transcript = []
            _draw(state, notice="Started a new conversation.")
            continue
        with state.lock:
            reply = state.add_turn(text)
            state.transcript.append(("you", text))
            state.transcript.append(("agy", reply))
        _draw(state)


if __name__ == "__main__":
    sys.exit(main())
