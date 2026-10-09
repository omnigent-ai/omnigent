"""CLI e2e: Pi must enable thinking for reasoning-capable gateway models.

Drives the reported user journey through the *real* ``omnigent pi`` CLI under a
pseudo-TTY (pexpect), rendering the TUI with ``pyte``:

1. Configure ``~/.omnigent/config.yaml`` with a ``kind: databricks`` provider
   whose profile points at a local mock of the Unity Catalog model-services
   listing. The listing serves authentic ``system.ai.*`` ids on their real
   surfaces (Claude on Anthropic Messages, GPT on OpenAI Responses, Gemini and
   DeepSeek on chat completions); a seeded MLflow catalog cache marks all of
   them reasoning-capable.
2. Launch a pi-native session (``omnigent pi``) and read the footer for the
   Claude model Pi boots with (the control).
3. ``/model`` -> pick the GPT model -> read the footer; repeat for Gemini.

Pi's status footer renders ``<model> • <level>`` (for example ``• medium`` or
``• thinking off``) only for ``models.json`` entries flagged ``reasoning:
true``; a non-reasoning model shows just ``<model>`` with no bullet. On the
buggy build Omnigent flags only ids containing ``deepseek``/``claude``, so the
GPT and Gemini footers drop the thinking indicator while Claude keeps it. The
test asserts the GPT and Gemini footers still show the indicator, so it fails
on the buggy build and passes once the catalog capability drives the flag.

The footer indicator is the thinking surface available on every Pi build this
suite runs against: the ``/thinking`` slash command only exists in newer Pi,
while the footer reflects ``model.reasoning`` with no picker, model request, or
network round-trip.

Modelled on ``test_pi_native_gateway_claude_misroute_e2e.py`` (pexpect + fake
``HOME`` against ``omnigent pi``) and ``test_pi_native_model_scope_e2e.py``
(``pyte`` screen reads of the Pi TUI).
"""

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
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason

pexpect = pytest.importorskip("pexpect")
pyte = pytest.importorskip("pyte")

pytestmark = [
    pytest.mark.skipif(
        (_reason := cli_unavailable_reason("pi")) is not None,
        reason=f"pi-native thinking e2e requires a runnable 'pi' CLI; {_reason}.",
    ),
    pytest.mark.timeout(900),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCH_TIMEOUT = 180
_SCREEN_COLS, _SCREEN_ROWS = 140, 40
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>=]")

CLAUDE_MODEL = "system.ai.claude-fable-5-1"
GPT_MODEL = "system.ai.gpt-6-luna"
GEMINI_MODEL = "system.ai.gemini-3-8-flash"
DEEPSEEK_MODEL = "system.ai.deepseek-v4"

# Unity Catalog model-services rows: id -> supported_api_types.
WORKSPACE_MODEL_SERVICES: dict[str, list[str]] = {
    CLAUDE_MODEL: ["anthropic/v1/messages", "mlflow/v1/chat/completions"],
    GPT_MODEL: ["mlflow/v1/chat/completions", "openai/v1/responses"],
    GEMINI_MODEL: ["mlflow/v1/chat/completions"],
    DEEPSEEK_MODEL: ["mlflow/v1/chat/completions"],
}

# Thinking levels Pi can render after the footer's bullet. Any of them (even
# "thinking off") means Pi treats the model as reasoning-capable.
PI_FOOTER_LEVELS: frozenset[str] = frozenset(
    {"thinking off", "minimal", "low", "medium", "high", "xhigh", "max"}
)


class _WorkspaceHandler(BaseHTTPRequestHandler):
    """Mock Databricks workspace: model-services listing, 404 elsewhere."""

    requests: list[dict[str, Any]] = []

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        _WorkspaceHandler.requests.append({"method": self.command, "path": self.path})
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
            body = json.dumps(
                {
                    "model_services": [
                        {"name": f"model-services/{name}", "supported_api_types": api_types}
                        for name, api_types in WORKSPACE_MODEL_SERVICES.items()
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = _handle  # type: ignore[assignment]

    def log_message(self, *args: object) -> None:
        return


@contextlib.contextmanager
def mock_workspace() -> Iterator[str]:
    """Serve the mock workspace; yield its base URL."""
    _WorkspaceHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WorkspaceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def write_fake_home(home: Path, workspace_url: str) -> None:
    """Seed *home* with the Databricks provider config and catalog cache.

    Only ``HOME`` reaches the daemon-spawned runner, so every file lives under
    the default locations: ``~/.omnigent/config.yaml``, ``~/.databrickscfg``
    and the MLflow catalog cache under ``~/.cache``.
    """
    from omnigent.onboarding import providers as catalog_providers

    config_home = home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "providers:\n"
        "  databricks:\n"
        "    kind: databricks\n"
        "    default: true\n"
        "    profile: repro\n"
    )
    (home / ".databrickscfg").write_text(f"[repro]\nhost = {workspace_url}\ntoken = repro-token\n")

    # Offline CI has no real ``databricks`` binary, but Pi resolves the gateway
    # provider's apiKey by shelling out to ``databricks auth token``. Provide a
    # stand-in on PATH that mints the fake profile's PAT the way a configured
    # workstation's CLI would, so the auth command succeeds without network.
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    databricks_shim = bin_dir / "databricks"
    databricks_shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "auth" ] && [ "$2" = "token" ]; then\n'
        '  for arg in "$@"; do\n'
        '    [ "$arg" = "--help" ] && { echo "Usage: databricks auth token"; exit 0; }\n'
        "  done\n"
        '  printf \'{"access_token": "repro-token"}\\n\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    databricks_shim.chmod(0o755)

    def row(reasoning: bool) -> dict[str, Any]:
        return {
            "mode": "chat",
            "capabilities": {"function_calling": True, "reasoning": reasoning, "vision": True},
            "context_window": {"max_input": 200000, "max_output": 8192},
        }

    catalog = {
        "schema_version": 1,
        "models": {
            f"databricks-{model.removeprefix('system.ai.')}": row(True)
            for model in WORKSPACE_MODEL_SERVICES
        }
        | {"databricks-llama-4-maverick": row(False)},
    }
    cache_path = home / ".cache" / "omnigent" / "model-catalog" / "databricks.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "cache_schema_version": 1,
                "catalog_schema_version": catalog["schema_version"],
                "source_url": catalog_providers._catalog_source_url("databricks"),
                "fetched_at": time.time(),
                "catalog": catalog,
            }
        )
    )


def cli_env(home: Path) -> dict[str, str]:
    """Environment for ``omnigent pi`` so the spawned runner uses *home*."""
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "PYTHONPATH": os.pathsep.join(
            str(p)
            for p in (
                _REPO_ROOT,
                _REPO_ROOT / "sdks" / "python-client",
                _REPO_ROOT / "sdks" / "ui",
            )
        ),
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "PATH": os.pathsep.join([str(home / "bin"), os.environ.get("PATH", "")]),
    }
    # The test suite disables catalog lookups; the runner must read the seeded
    # cache. Ambient Databricks credentials would shadow the fake profile.
    for key in (
        "OMNIGENT_CONFIG",
        "OMNIGENT_DISABLE_CATALOG_LOOKUP",
        "XDG_CACHE_HOME",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
    ):
        env.pop(key, None)
    return env


def omnigent_bin() -> Path:
    path = Path(sys.executable).parent / "omnigent"
    assert path.exists(), f"omnigent CLI not found at {path}"
    return path


def stop_local_server(env: dict[str, str]) -> None:
    """Stop the auto-spawned managed server and the local host daemon."""
    omni = Path(sys.executable).parent / "omni"
    if omni.exists():
        with contextlib.suppress(Exception):
            subprocess.run([str(omni), "server", "stop"], env=env, capture_output=True, timeout=60)


class PiTui:
    """Render the ``omnigent pi`` PTY stream and wait for screen states."""

    def __init__(self, process: Any) -> None:
        self.process = process
        self.screen = pyte.Screen(_SCREEN_COLS, _SCREEN_ROWS)
        self.stream = pyte.Stream(self.screen)
        self.raw: list[str] = []

    def _pump(self) -> None:
        try:
            chunk = self.process.read_nonblocking(65536, timeout=0.2)
        except pexpect.TIMEOUT:
            return
        self.raw.append(chunk)
        self.stream.feed(chunk)

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def raw_text(self) -> str:
        return _ANSI_RE.sub("", "".join(self.raw))

    def wait_for(self, predicate: Callable[[str], bool], *, timeout: float, what: str) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._pump()
            except pexpect.EOF:
                pytest.fail(f"omnigent pi exited before {what}:\n{self.text()}")
            rendered = self.text()
            if predicate(rendered):
                return rendered
        pytest.fail(f"Pi did not show {what} within {timeout:.0f}s:\n{self.text()}")

    def wait_raw(self, pattern: str, *, timeout: float, what: str) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._pump()
            except pexpect.EOF:
                pytest.fail(f"omnigent pi exited before {what}:\n{self.raw_text()[-4000:]}")
            match = re.search(pattern, self.raw_text())
            if match:
                return match
        pytest.fail(
            f"omnigent pi did not print {what} within {timeout:.0f}s:\n{self.raw_text()[-4000:]}"
        )

    def send(self, text: str) -> None:
        self.process.send(text)


def _footer_shows(model: str) -> Callable[[str], bool]:
    pattern = re.compile(r"\(omnigent(?:-[a-z]+)?\)\s+" + re.escape(model))
    return lambda text: pattern.search(text) is not None


def footer_thinking_level(text: str, model: str) -> str | None:
    """Return the thinking level Pi's footer advertises for *model*, or None.

    Pi renders ``<model> • <level>`` only for reasoning-capable models; a
    non-reasoning model shows ``<model>`` with no bullet, so None means Pi
    believes the model cannot think.
    """
    match = re.search(re.escape(model) + r"[ \t]*•[ \t]*(thinking off|[a-z]+)", text)
    if match and match.group(1) in PI_FOOTER_LEVELS:
        return match.group(1)
    return None


def select_model(tui: PiTui, model: str) -> None:
    """``/model`` -> filter to *model* -> Enter; wait for the footer to switch."""
    tui.send("/model\r")
    tui.wait_for(lambda text: model in text, timeout=30, what="the model picker")
    tui.send(model.removeprefix("system.ai."))
    time.sleep(1)
    tui.send("\r")
    tui.wait_for(_footer_shows(model), timeout=30, what=f"the footer naming {model}")
    time.sleep(1)


def read_footer_thinking(tui: PiTui, model: str, *, settle: float = 8.0) -> str | None:
    """Return the thinking level Pi's footer advertises for *model*.

    Polls briefly so a reasoning-capable model's indicator has time to render,
    then returns None if the footer never shows one (the reported bug).
    """
    deadline = time.monotonic() + settle
    level: str | None = None
    while time.monotonic() < deadline:
        tui._pump()
        level = footer_thinking_level(tui.text(), model)
        if level is not None:
            return level
        time.sleep(0.2)
    return level


@pytest.fixture
def workspace_url() -> Iterator[str]:
    with mock_workspace() as url:
        yield url


@pytest.fixture
def pi_home(tmp_path: Path, workspace_url: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    write_fake_home(home, workspace_url)
    return home


def test_pi_native_gateway_reasoning_models_offer_thinking_levels(pi_home: Path) -> None:
    """GPT and Gemini gateway models must enable thinking like Claude does."""
    env = cli_env(pi_home)
    dump_dir = (
        Path(os.environ.get("OMNIGENT_E2E_RECORD_DIR") or pi_home.parent) / "pi-thinking-screens"
    )
    dump_dir.mkdir(parents=True, exist_ok=True)

    child = pexpect.spawn(
        str(omnigent_bin()),
        ["pi", "--server", ""],  # auto-spawn a local server + runner
        cwd=str(_REPO_ROOT),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(_SCREEN_ROWS, _SCREEN_COLS),
        timeout=_LAUNCH_TIMEOUT,
    )
    tui = PiTui(child)
    thinking: dict[str, str | None] = {}
    try:
        tui.wait_raw(r"Web UI:\s*(\S+)", timeout=_LAUNCH_TIMEOUT, what="its 'Web UI:' line")
        # Pi boots with the workspace's Claude model selected.
        tui.wait_for(_footer_shows(CLAUDE_MODEL), timeout=_LAUNCH_TIMEOUT, what="the Pi TUI")
        time.sleep(5)  # let the tmux attach settle before reading the footer

        thinking["claude"] = read_footer_thinking(tui, CLAUDE_MODEL)
        (dump_dir / "footer-claude.txt").write_text(tui.text())

        for name, model in (("gpt", GPT_MODEL), ("gemini", GEMINI_MODEL)):
            select_model(tui, model)
            thinking[name] = read_footer_thinking(tui, model)
            (dump_dir / f"footer-{name}.txt").write_text(tui.text())
    finally:
        with contextlib.suppress(Exception):
            child.kill(signal.SIGTERM)
        time.sleep(2)
        with contextlib.suppress(Exception):
            child.kill(signal.SIGKILL)
        stop_local_server(env)

    assert thinking["claude"] is not None, (
        f"control failed: Pi's footer showed no thinking indicator for {CLAUDE_MODEL}; "
        f"thinking looks disabled for every model, not just non-Claude ones. Screens: {dump_dir}"
    )
    disabled = sorted(name for name, level in thinking.items() if level is None)
    assert not disabled, (
        f"Pi shows thinking disabled for reasoning-capable gateway models {disabled}: "
        f"their footers lack the ' • <level>' indicator that {CLAUDE_MODEL} shows "
        f"('• {thinking['claude']}'). Screens: {dump_dir}"
    )
