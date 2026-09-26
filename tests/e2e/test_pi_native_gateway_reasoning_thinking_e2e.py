"""CLI e2e: reasoning-capable Databricks gateway models expose Pi's thinking controls."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason

pexpect = pytest.importorskip("pexpect")

pytestmark = pytest.mark.skipif(
    (_reason := cli_unavailable_reason("pi")) is not None,
    reason=f"pi-native reasoning/thinking e2e requires a runnable 'pi' CLI; {_reason}.",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>=]")
# Launch budget mirrors the CLI's internal host/runner cold-start ceiling.
_LAUNCH_TIMEOUT = 180

# Databricks profile name; the mock host is wired to it via ``.databrickscfg``.
_PROFILE = "repro"

# Reasoning models on each gateway surface; the seeded catalog marks every one
# reasoning-capable.
_CLAUDE_ID = "system.ai.claude-fable-5-1"  # anthropic-messages
_GPT_ID = "system.ai.gpt-6-luna"  # openai-responses
_GEMINI_ID = "system.ai.gemini-3-8-flash"  # openai-completions
_DEEPSEEK_ID = "system.ai.deepseek-v4"  # openai-completions

_MODEL_SERVICES = [
    {"name": f"model-services/{_CLAUDE_ID}", "supported_api_types": ["anthropic/v1/messages"]},
    {
        "name": f"model-services/{_GPT_ID}",
        "supported_api_types": ["openai/v1/responses", "openai/v1/chat/completions"],
    },
    {
        "name": f"model-services/{_GEMINI_ID}",
        "supported_api_types": ["openai/v1/chat/completions"],
    },
    {
        "name": f"model-services/{_DEEPSEEK_ID}",
        "supported_api_types": ["openai/v1/chat/completions"],
    },
]


class _WorkspaceHandler(BaseHTTPRequestHandler):
    """Mock Databricks workspace: serve the Unity Catalog model-services list."""

    def do_GET(self) -> None:
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
            payload = json.dumps({"model_services": _MODEL_SERVICES}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        # Gateway inference endpoints are only hit at turn time; this journey
        # navigates menus and never sends a turn.
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:  # keep pytest output quiet
        return


@pytest.fixture
def mock_workspace() -> Iterator[str]:
    """Start the mock Databricks workspace; yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WorkspaceHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def pi_home(tmp_path: Path, mock_workspace: str) -> Path:
    """A fake ``HOME`` seeded with the Databricks provider + catalog cache."""
    config_home = tmp_path / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "providers:\n"
        "  repro-workspace:\n"
        "    kind: databricks\n"
        "    default: true\n"
        f"    profile: {_PROFILE}\n"
    )
    databrickscfg = tmp_path / ".databrickscfg"
    databrickscfg.write_text(
        f"[{_PROFILE}]\nhost = {mock_workspace}\ntoken = dapi-fake-repro-token\n"
    )
    databrickscfg.chmod(0o600)

    # Seed the MLflow model-catalog cache so resolution stays offline; every
    # model is marked reasoning-capable.
    cache_dir = tmp_path / ".cache" / "omnigent" / "model-catalog"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def _model(max_in: int, max_out: int, date: str) -> dict[str, Any]:
        return {
            "mode": "chat",
            "capabilities": {"function_calling": True, "reasoning": True, "vision": True},
            "context_window": {"max_input": max_in, "max_output": max_out},
            "release_date": date,
        }

    catalog = {
        "schema_version": "1.0",
        "models": {
            "databricks-claude-fable-5-1": _model(200000, 16384, "2026-06-01"),
            "databricks-gpt-6-luna": _model(400000, 16384, "2026-07-01"),
            "databricks-gemini-3-8-flash": _model(1000000, 16384, "2026-05-01"),
            "databricks-deepseek-v4": _model(128000, 16384, "2026-04-01"),
        },
    }
    (cache_dir / "databricks.json").write_text(
        json.dumps(
            {
                "cache_schema_version": 1,
                "catalog_schema_version": "1.0",
                "source_url": (
                    "https://github.com/mlflow/mlflow/releases/download/"
                    "model-catalog%2Flatest/databricks.json"
                ),
                "fetched_at": time.time(),
                "catalog": catalog,
            }
        )
    )
    return tmp_path


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class _PiTerminal:
    """Accumulate the pi TUI's raw output for ANSI-stripped assertions."""

    def __init__(self, child: Any) -> None:
        self.child = child
        self.raw = ""

    def pump(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.raw += self.child.read_nonblocking(65536, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                return False
        return True

    def wait_for(self, needle: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.4)
            if needle in self.text():
                return True
        return False

    def text(self) -> str:
        return _strip_ansi(self.raw)


def _reasoning_by_id(models_json: Path) -> dict[str, bool]:
    data = json.loads(models_json.read_text())
    result: dict[str, bool] = {}
    for provider in data.get("providers", {}).values():
        for model in provider.get("models", []):
            result[model["id"]] = bool(model.get("reasoning"))
    return result


def test_pi_native_gateway_reasoning_models_expose_thinking(pi_home: Path) -> None:
    """models.json flags GPT/Gemini ``reasoning: true``; /thinking offers a level beyond off."""
    omnigent_bin = Path(sys.executable).parent / "omnigent"
    assert omnigent_bin.exists(), f"omnigent CLI not found at {omnigent_bin}"

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_", "ANTHROPIC_", "OPENAI_", "DATABRICKS_", "CLAUDE_"))
        and k not in {"TMUX", "TMUX_PANE", "XDG_CACHE_HOME"}
    }
    env.update(
        HOME=str(pi_home),
        OMNIGENT_CONFIG_HOME=str(pi_home / ".omnigent"),
        OMNIGENT_SKIP_ONBOARD="1",
        OMNIGENT_NO_UPDATE_CHECK="1",
        # Resolve omnigent + its in-repo SDK packages to this worktree.
        PYTHONPATH=os.pathsep.join(
            str(p)
            for p in (
                _REPO_ROOT,
                _REPO_ROOT / "sdks" / "python-client",
                _REPO_ROOT / "sdks" / "ui",
            )
        ),
        # A real TERM so the runner-owned Pi tmux pane attaches under the pty.
        TERM="xterm-256color",
        PROMPT_TOOLKIT_NO_CPR="1",
        PI_OFFLINE="1",
    )

    child = pexpect.spawn(
        str(omnigent_bin),
        ["pi", "--server", ""],  # auto-spawn a local server + runner
        cwd=str(_REPO_ROOT),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 140),
        timeout=_LAUNCH_TIMEOUT,
    )
    term = _PiTerminal(child)

    try:
        assert term.wait_for("Web UI:", _LAUNCH_TIMEOUT), "pi CLI never printed a Web UI url"
        # The Pi TUI boots with the resolved Claude-family default selected.
        assert term.wait_for(_CLAUDE_ID, _LAUNCH_TIMEOUT), "pi TUI never booted with a model"
        time.sleep(8)  # let prompt_toolkit's input loop go live before typing

        # (a) The managed models.json - the artifact the resolver wrote for this
        # session - must mark every reasoning-capable gateway model reasoning.
        matches = sorted((pi_home / ".omnigent" / "pi-native").glob("*/pi-agent/models.json"))
        assert matches, "pi-native session did not write a managed models.json"
        reasoning = _reasoning_by_id(matches[-1])
        assert reasoning.get(_CLAUDE_ID) is True, f"claude control lost reasoning: {reasoning}"
        assert reasoning.get(_DEEPSEEK_ID) is True, f"deepseek control lost reasoning: {reasoning}"
        assert reasoning.get(_GPT_ID) is True, (
            f"{_GPT_ID} (openai-responses) was written without reasoning: true, so Pi shows "
            f"'thinking: no' and hides thinking controls. models.json reasoning map: {reasoning}"
        )
        assert reasoning.get(_GEMINI_ID) is True, (
            f"{_GEMINI_ID} (openai-completions) was written without reasoning: true, so Pi shows "
            f"'thinking: no' and hides thinking controls. models.json reasoning map: {reasoning}"
        )

        # (b) The user-visible symptom: select the GPT model and open /thinking.
        child.send("/model")
        time.sleep(1)
        child.send("\r")
        assert term.wait_for(_GPT_ID, 30), "the /model picker never listed the GPT model"
        child.send(_GPT_ID.rsplit(".", 1)[-1])  # filter to "gpt-6-luna"
        time.sleep(1.5)
        child.send("\r")
        # The footer prints the active provider in parens once the GPT model is
        # selected (the picker used square brackets), confirming the switch.
        assert term.wait_for("(omnigent-openai)", 30), "the GPT model was never activated"

        think_mark = len(term.raw)
        child.send("/thinking")
        time.sleep(1)
        child.send("\r")
        term.pump(6)
        picker = _strip_ansi(term.raw[think_mark:])
        assert "Thinking Level" in picker or "No reasoning" in picker, (
            f"the /thinking picker did not open for the GPT model. tail: {picker[-800:]!r}"
        )
        offers_reasoning = (
            "minimal" in picker
            or "Very brief reasoning" in picker
            or bool(re.search(r"\b(low|medium|high)\b[^\n]*reasoning", picker, re.IGNORECASE))
        )
        assert offers_reasoning, (
            f"Pi's /thinking picker for {_GPT_ID} offered only 'off  No reasoning' - thinking "
            "controls are disabled for this reasoning-capable model. Expected a reasoning level "
            f"beyond 'off' (e.g. 'minimal'/'low'/'medium'). picker tail: {picker[-800:]!r}"
        )
    finally:
        _teardown(child, env)


def _teardown(child: Any, env: dict[str, str]) -> None:
    """Stop the CLI, then its server and daemon; ``pkill -f pi_native`` would hit pytest too."""
    with contextlib.suppress(Exception):
        child.kill(signal.SIGTERM)
    time.sleep(2)
    with contextlib.suppress(Exception):
        child.kill(signal.SIGKILL)
    omni_bin = Path(sys.executable).parent / "omni"
    if omni_bin.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                [str(omni_bin), "server", "stop"],
                env=env,
                capture_output=True,
                timeout=60,
            )
