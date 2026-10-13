r"""hermes-native: the policy-hook wrapper must point at a hook file that exists.

The runner sets up every ``hermes-native``
session by writing a per-session ``HERMES_HOME`` whose ``config.yaml`` registers
a ``pre_tool_call`` shell hook. That hook is a wrapper script that ``exec``s
``<python> <hook_script_path>``. :func:`write_policy_hook_config` builds

    Path(__file__).resolve().parent / "inner" / "hermes_policy_hook.py"

from ``omnigent/harnesses/hermes_native/bridge.py``, which resolves to
``omnigent/harnesses/hermes_native/inner/hermes_policy_hook.py`` -- a path that
does not exist (the shipped hook is ``omnigent/inner/hermes_policy_hook.py``).
So every Hermes tool call runs a hook that Python cannot open: it exits 2 with
``can't open file ... [Errno 2] No such file or directory``, which Hermes treats
as a ``pre_tool_call`` block. The tool never executes and its result is that
ENOENT error.

The journey is a user's: a hermes-native session sends a prompt that makes the
model call a tool. Here the real ``hermes`` CLI is launched with the exact
``HERMES_HOME`` the runner writes (via :func:`write_policy_hook_config`), driven
against the mock LLM, with a local policy endpoint that answers ALLOW. On a
correct build the tool runs; today it is blocked by the missing-hook ENOENT.

Two guards, both failing on the buggy build and passing once the wrapper points
at the real hook:

* the hook path baked into the wrapper resolves to an existing file, and
* the real Hermes turn executes the tool (no ENOENT hook error in the
  transcript, and the tool's side effect is observed).

Skips (never fails) when the ``hermes`` CLI is not installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.hermes_native.bridge import write_policy_hook_config

from .conftest import configure_mock_llm

pytestmark = pytest.mark.skipif(
    shutil.which("hermes") is None,
    reason="hermes-native policy-hook e2e needs the `hermes` CLI on PATH.",
)

_ENOENT_HOOK_ERROR = re.compile(r"hermes_policy_hook\.py': \[Errno 2\] No such file")


class _AllowPolicyHandler(BaseHTTPRequestHandler):
    """Answer every policy evaluate POST with ALLOW so a reachable hook runs."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"result": "POLICY_ACTION_ALLOW", "reason": ""}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:  # silence access logs
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hook_script_path_from_wrapper(wrapper: Path) -> str:
    """Extract the hook script the wrapper ``exec``s (its last ``exec`` line)."""
    for line in reversed(wrapper.read_text().splitlines()):
        if line.startswith("exec "):
            return line.split()[-1].strip("'\"")
    raise AssertionError(f"no exec line in policy-hook wrapper:\n{wrapper.read_text()}")


def _transcript(hermes_home: Path) -> list[tuple[str, str]]:
    """Return the canonical (role, content) transcript Hermes persisted."""
    con = sqlite3.connect(str(hermes_home / "state.db"))
    try:
        rows = con.execute("select role, content from messages order by id")
        return [(r, c or "") for r, c in rows]
    finally:
        con.close()


def _disable_auto_title(hermes_home: Path) -> None:
    """Turn off Hermes' auto-title auxiliary model call for this session.

    It shares the turn's model queue, so against the FIFO mock LLM it would
    race the turn for the queued tool-call and make the drive nondeterministic;
    it is unrelated to the policy hook under test.
    """
    config = hermes_home / "config.yaml"
    data = json.loads(config.read_text())
    data["auxiliary"] = {"title_generation": {"enabled": False, "model_upgrade_enabled": False}}
    config.write_text(json.dumps(data, indent=2))


@pytest.mark.timeout(300)
def test_hermes_native_policy_hook_path_lets_the_tool_run(
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = f"hook-proof-{uuid.uuid4().hex[:12]}"
    proof = tmp_path / f"{marker}.txt"
    model = f"hook-mock-{uuid.uuid4().hex[:8]}"

    # The model's first turn asks Hermes to run a shell tool; later turns wrap up.
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "c1",
                        "name": "terminal",
                        "arguments": json.dumps({"command": f"echo {marker} > {proof}"}),
                    }
                ]
            },
            {"text": "tool complete"},
            {"text": "tool complete"},
            {"text": "tool complete"},
        ],
        key=model,
    )

    # Local policy endpoint the hook posts to; ALLOW so a reachable hook proceeds.
    policy_port = _free_port()
    policy = ThreadingHTTPServer(("127.0.0.1", policy_port), _AllowPolicyHandler)
    threading.Thread(target=policy.serve_forever, daemon=True).start()
    policy_url = f"http://127.0.0.1:{policy_port}"

    # A private HOME whose ~/.hermes/config.yaml routes Hermes at the mock LLM,
    # so write_policy_hook_config carries model/provider into the per-session home.
    fake_home = tmp_path / "home"
    (fake_home / ".hermes").mkdir(parents=True)
    (fake_home / ".hermes" / "config.yaml").write_text(
        "model:\n"
        f'  default: "{model}"\n'
        '  provider: "custom"\n'
        f'  base_url: "{isolated_mock_llm_server_url}/v1"\n'
        '  api_key: "mock-key"\n'
        "  context_length: 131072\n"
    )
    monkeypatch.setenv("HOME", str(fake_home))

    # The exact per-session HERMES_HOME the runner writes on a hermes-native bind.
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hermes_home = write_policy_hook_config(bridge_dir, policy_url, "hook-e2e-session")
    _disable_auto_title(hermes_home)
    wrapper = hermes_home / "omnigent-policy-hook.sh"

    # Guard 1: the hook the wrapper execs must exist. Fails on the buggy build.
    hook_script_path = _hook_script_path_from_wrapper(wrapper)
    assert Path(hook_script_path).is_file(), (
        f"policy-hook wrapper points at a non-existent hook: {hook_script_path}\n"
        f"wrapper:\n{wrapper.read_text()}"
    )

    # Guard 2: drive the real Hermes turn; the tool must run, not be blocked by
    # the missing-hook ENOENT. --yolo auto-accepts consent so the ALLOW hook is
    # the only gate deciding whether the tool executes.
    result = subprocess.run(
        ["hermes", "--cli", "--yolo", "-z", "please run the marker command", "chat"],
        env={**os.environ, "HOME": str(fake_home), "HERMES_HOME": str(hermes_home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=240,
    )
    policy.shutdown()

    transcript = _transcript(hermes_home)
    rendered = "\n".join(f"{role}: {content}" for role, content in transcript)
    assert not _ENOENT_HOOK_ERROR.search(rendered), (
        "Hermes tool call was blocked by the missing policy-hook file (ENOENT).\n"
        f"hermes rc={result.returncode}\ntranscript:\n{rendered}"
    )
    assert proof.is_file() and marker in proof.read_text(), (
        "the tool never executed its side effect; the policy hook did not allow it.\n"
        f"hermes rc={result.returncode}\ntranscript:\n{rendered}"
    )
