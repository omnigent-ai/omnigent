"""End-to-end guard: ``omnigent claude --use-native-config`` must hold on the
host-daemon launch path.

Every ``omnigent claude`` launch is daemon-routed: the runner — not the CLI —
launches the ``claude`` terminal and derives its provider routing itself, so
the ``--use-native-config`` intent must ride on the session for the runner to
honour it. When it is dropped, the launched Claude is routed at the configured
gateway provider (``ANTHROPIC_BASE_URL`` + gateway ``--model``) instead of
Claude Code's own ``~/.claude`` config.

This test drives the real journey end to end: a real CLI invocation, which
ensures the host daemon, which spawns a local server + a runner, which launches
the ``claude`` terminal itself. A gateway provider is configured as the
Anthropic default in an isolated config home. Only the ``claude`` binary is a
stub (via ``harness.claude-native.command``): it records the exact launch env +
argv the runner handed it, then idles like a TUI. The launch is honoured only
when that record shows no gateway ``ANTHROPIC_BASE_URL`` and no gateway
``--model``.

Needs only ``tmux`` (the native wrapper launches Claude through the runner's
tmux terminal). No real ``claude`` binary, no Claude login, and no network: the
gateway base URL is an unroutable sentinel the stub only records, never dials.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e._native_resume_helpers import cli_env, omnigent_console_script

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None or sys.platform == "win32",
    reason="the native Claude wrapper launches Claude through the runner's tmux terminal",
)

# The gateway provider the config sets as the Anthropic default. The base URL is
# an unroutable sentinel: nothing connects to it here — the stub only records
# whether the runner injected it into Claude's launch env.
_GATEWAY_BASE_URL = "http://127.0.0.1:9/gateway/anthropic"
_GATEWAY_MODEL = "databricks-claude-sonnet-4-5"

# Basename of the launch record the recording stub writes into the config home.
_RECORD_NAME = "claude-launch.json"

# How long to wait for the daemon-spawned runner to launch the Claude terminal.
# Kept well under the E2E job's 180s per-test timeout so the body's own
# diagnostic assertion fires instead of being killed by that outer timeout.
_LAUNCH_DEADLINE_S = 150

# The recording ``claude`` stub: atomically writes the launch argv + routing env
# it was handed, renders a Claude-Code-like composer for the runner's readiness
# poll, then idles like a live TUI until the pane is torn down.
_RECORDING_CLAUDE = """\
import json
import os
import sys
import time

record_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "{record_name}")
routing_env = {{
    k: v
    for k, v in os.environ.items()
    if k.startswith(("ANTHROPIC", "CLAUDE_", "ENABLE_TOOL"))
}}
record = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": routing_env}}
tmp = record_path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(record, fh)
os.replace(tmp, record_path)

rule = "\\u2500" * 30
sys.stdout.write("\\x1b[2J\\x1b[H")
sys.stdout.write("fake Claude Code TUI\\r\\n")
sys.stdout.write(rule + "\\r\\n")
sys.stdout.write("\\u276f \\r\\n")
sys.stdout.write(rule + "\\r\\n")
sys.stdout.flush()
while True:
    time.sleep(1)
"""


@pytest.fixture
def native_config_gateway_env() -> Iterator[dict[str, object]]:
    """An isolated config/data/workspace home with a gateway Anthropic default
    and a recording ``claude`` stub.

    Yields the CLI subprocess env plus the launch-record path. The host daemon
    and local server this journey spawns are stopped on teardown, even when the
    body raises.
    """
    root = Path(tempfile.mkdtemp(prefix="omni-native-cfg-"))
    config_home = root / "cfg"
    data_dir = root / "data"
    workspace = root / "ws"
    for path in (config_home, data_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)

    stub_path = config_home / "recording-claude"
    stub_path.write_text(
        "#!/usr/bin/env python3\n" + _RECORDING_CLAUDE.format(record_name=_RECORD_NAME),
        encoding="utf-8",
    )
    stub_path.chmod(0o755)

    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  repro-gateway:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    anthropic:\n"
        f"      base_url: {_GATEWAY_BASE_URL}\n"
        "      api_key: repro-gateway-key\n"
        "      models:\n"
        f"        default: {_GATEWAY_MODEL}\n"
        f"        sonnet: {_GATEWAY_MODEL}\n"
        "harness:\n"
        "  claude-native:\n"
        f"    command: {stub_path}\n",
        encoding="utf-8",
    )

    # cli_env() pins PYTHONPATH to this worktree so the CLI, daemon, and runner
    # import the code under test, and strips runner/tmux/credential leaks. Also
    # drop the gateway hints this test asserts the native path never uses.
    env = cli_env()
    for stale in ("OMNIGENT_CLAUDE_PATH", "ANTHROPIC_BASE_URL"):
        env.pop(stale, None)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)

    try:
        yield {
            "env": env,
            "workspace": workspace,
            "record_path": config_home / _RECORD_NAME,
        }
    finally:
        with open(os.devnull, "wb") as devnull:
            # Stop the runners/sessions (host daemon) and the auto-spawned local
            # server; the latter outlives `host stop` and would otherwise leak.
            for stop_args in (
                ["host", "stop", "--all", "--force"],
                ["server", "stop", "--force"],
            ):
                # A hung stop must not skip the remaining cleanup below.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    subprocess.run(
                        [str(omnigent_console_script()), *stop_args],
                        env=env,
                        stdout=devnull,
                        stderr=devnull,
                        timeout=120,
                        check=False,
                    )
        shutil.rmtree(root, ignore_errors=True)


def test_use_native_config_not_ignored_by_host_daemon(
    native_config_gateway_env: dict[str, object],
) -> None:
    """``omnigent claude --use-native-config`` must launch Claude on its own
    native config even though the host daemon spawns the runner.

    With a ``kind: gateway`` provider as the Anthropic default, drive the real
    CLI journey and inspect what the runner actually handed the launched
    ``claude``. The flag is honoured only when the launch is NOT routed at the
    gateway (no gateway ``ANTHROPIC_BASE_URL``, no gateway ``--model``).
    """
    import pexpect

    env = native_config_gateway_env["env"]
    workspace = native_config_gateway_env["workspace"]
    record_path: Path = native_config_gateway_env["record_path"]

    transcript = io.StringIO()
    child = pexpect.spawn(
        str(omnigent_console_script()),
        ["claude", "--use-native-config"],
        cwd=str(workspace),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        timeout=_LAUNCH_DEADLINE_S + 10,
        dimensions=(24, 100),
    )
    child.logfile_read = transcript
    try:
        deadline = time.monotonic() + _LAUNCH_DEADLINE_S
        while time.monotonic() < deadline:
            if record_path.exists():
                break
            if not child.isalive():
                break
            try:
                child.read_nonblocking(size=4096, timeout=1)
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
        # The launch is daemon-routed, so the runner can finish writing the
        # record just after the foreground CLI child exits; re-poll briefly
        # before failing to avoid racing that write.
        if not record_path.exists():
            for _ in range(10):
                time.sleep(1)
                if record_path.exists():
                    break
        assert record_path.exists(), (
            "the daemon-spawned runner never launched the Claude terminal; "
            f"CLI output:\n{transcript.getvalue()}"
        )
        record = json.loads(record_path.read_text())
    finally:
        child.close(force=True)

    argv = record["argv"]
    launch_env = record["env"]
    launched_base_url = launch_env.get("ANTHROPIC_BASE_URL")
    if "--model" in argv:
        model_index = argv.index("--model")
        assert model_index + 1 < len(argv), f"the runner passed --model with no value: {argv!r}"
        launched_model = argv[model_index + 1]
    else:
        launched_model = None

    # Native config passes neither a managed gateway URL nor a catalog
    # ``--model`` — Claude resolves both from its own ``~/.claude`` config — so
    # assert absence, which also catches any other managed route.
    assert launched_base_url is None, (
        "--use-native-config was ignored: the daemon-spawned runner routed Claude "
        f"through a managed gateway (ANTHROPIC_BASE_URL={launched_base_url!r}) instead "
        "of Claude Code's own native config."
    )
    assert launched_model is None, (
        "--use-native-config was ignored: the daemon-spawned runner forced a catalog "
        f"model (--model {launched_model!r}) instead of letting Claude Code pick from "
        "its own native config."
    )
