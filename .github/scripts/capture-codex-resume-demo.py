"""Observe the existing real-Codex recovery case with configured permission args."""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
import yaml
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path.cwd()))

assert os.environ.get("GITHUB_ACTIONS") == "true", "This capture only runs in hosted CI"
output = Path(os.environ["OMNIGENT_E2E_RECORD_DIR"])
output.mkdir(parents=True, exist_ok=True)
control_home = Path(os.environ["CODEX_HOME"]).resolve()
assert control_home.is_relative_to(Path.cwd().resolve() / "artifacts")
control_home.mkdir(parents=True, exist_ok=True)
permission_args = ["--sandbox", "danger-full-access", "--ask-for-approval", "never"]
binary = shutil.which("codex")
assert binary is not None
version = subprocess.check_output([binary, "--version"], text=True).strip()
assert version == "codex-cli 0.154.0", version
observation: dict[str, Any] = {"cli_version": version, "frames": []}
scenario: Any = None
post_session: Any = None
read_state: Any = None
wait_pane: Any = None


class CaptureConfig:
    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        global scenario, post_session, read_state, wait_pane
        assert len(items) == 1
        scenario = items[0].module
        assert scenario.__name__.endswith("test_codex_native_terminal_recovery_e2e")
        # Conftest owns data isolation before any bridge module is imported.
        assert scenario.bridge_dir_for_bridge_id("demo-probe").is_relative_to(
            Path(os.environ["OMNIGENT_DATA_DIR"])
        )
        post_session = scenario.post_operator_session
        read_state = scenario.read_bridge_state
        wait_pane = scenario._wait_for_pane_text
        scenario.post_operator_session = capture_session
        scenario.read_bridge_state = capture_state
        scenario._wait_for_pane_text = capture_pane

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef: Any) -> Iterator[None]:
        if fixturedef.argname == "live_server":
            from omnigent.config import global_config_path

            assert os.environ.get("OMNIGENT_CONFIG_HOME")
            path = global_config_path().resolve()
            assert path.is_relative_to(Path.cwd()) or path.is_relative_to(Path("/tmp"))
            config = yaml.safe_load(path.read_text()) if path.exists() else {}
            config = config or {}
            codex = config.setdefault("harness", {}).setdefault("codex-native", {})
            assert not codex.get("command"), "The demo expects args-only user configuration"
            codex["args"] = permission_args
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(config))
            observation["configured_permission_args"] = list(permission_args)
            observation["config_path"] = str(path)
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: Any) -> Iterator[Any]:
        result = yield
        if not result.get_result().failed:
            return
        from omnigent.process_logging import redact_log_text

        factory = item.funcargs.get("tmp_path_factory")
        roots = [Path(os.environ["OMNIGENT_DATA_DIR"])]
        if factory is not None:
            roots.append(factory.getbasetemp())
        for index, root in enumerate(roots):
            for source in root.rglob("*.log"):
                target = output / "diagnostics" / str(index) / source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(redact_log_text(source.read_text(errors="replace")[-65536:]))


current_url: str | None = None
page: Any = None
context: Any = None
browser: Any = None
headers: dict[str, str] = {}


def capture_session(post: Any, *args: Any, **kwargs: Any) -> Any:
    global current_url, headers
    response = post_session(post, *args, **kwargs)
    if response.is_success:
        client = post.__self__
        current_url = str(client.base_url).rstrip("/") + "/c/" + response.json()["session_id"]
        headers = dict(client.headers)
    return response


def frame(name: str) -> None:
    global context, page
    from tests.e2e_ui.messages.test_native_codex_render_parity import (
        _open_terminal_view,
        _wait_terminal_connected,
    )

    assert current_url and browser is not None
    if context is None:
        context = browser.new_context(extra_http_headers=headers, record_video_dir=str(output))
        page = context.new_page()
    page.goto(current_url)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    # Keep each observed checkpoint on screen long enough to read the recording.
    page.wait_for_timeout(1000)
    path = output / (name + ".png")
    page.screenshot(path=str(path))
    observation["frames"].append({"name": name, "path": str(path)})


def run_control(argv: list[str]) -> dict[str, Any]:
    """Capture the interactive CLI on an owned PTY, including its exit status."""
    master, slave = pty.openpty()
    try:
        termios.tcsetwinsize(slave, (40, 120))
        try:
            process = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True
            )
        finally:
            os.close(slave)
        raw = bytearray()
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    raw.extend(chunk)
                elif process.poll() is not None:
                    break
        finally:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            timed_out = process.poll() is None
            if timed_out:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        return {
            "argv": argv,
            "exit_code": process.returncode,
            "stdout": raw.decode(errors="replace"),
            "stderr": "",
            "capture": "PTY stdout/stderr combined",
            "timed_out": timed_out,
        }
    finally:
        os.close(master)


def capture_state(*args: Any, **kwargs: Any) -> Any:
    state = read_state(*args, **kwargs)
    if state is not None and state.thread_id and "control" not in observation:
        control_argv = [
            binary,
            *permission_args,
            "resume",
            "--remote",
            state.socket_path,
            state.thread_id,
        ]
        observation["control"] = run_control(control_argv)
        frame("initial-configured-terminal")
    return state


def capture_pane(socket: str, text: str) -> None:
    wait_pane(socket, text)
    index = len(observation["frames"])
    pane = scenario._tmux(socket, "capture-pane", "-p", "-S", "-1000")
    observation.setdefault("terminal_captures", []).append({"expected": text, "screen": pane})
    frame("resumed-terminal-" + str(index))
    if len(observation["frames"]) == 5:
        context.close()


junit = Path("artifacts/pytest-attempt-1.xml")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(args=["--no-sandbox"])
    try:
        code = pytest.main(
            [
                "tests/e2e/test_codex_native_terminal_recovery_e2e.py",
                "--reruns",
                "0",
                "--force-reruns",
                "0",
                "--timeout",
                "300",
                "-q",
                "-s",
                "--junitxml=" + str(junit),
            ],
            plugins=[CaptureConfig()],
        )
    finally:
        browser.close()
cases = list(ET.fromstring(junit.read_bytes()).iter("testcase")) if junit.exists() else []
passed = bool(cases) and all(
    case.find(tag) is None for case in cases for tag in ("failure", "error", "skipped")
)
control = observation.get("control", {})
control_text = (control.get("stdout", "") + control.get("stderr", "")).lower()
control_rejected = (
    control.get("exit_code", 0) != 0
    and not control.get("timed_out", True)
    and "remote" in control_text
    and any(word in control_text for word in ("permission", "sandbox", "approval", "override"))
)
qualified = code == 0 and passed and len(observation["frames"]) == 5 and control_rejected
observation.update(
    pytest_exit_code=int(code),
    app_test_pass=code == 0 and passed,
    control_rejected=control_rejected,
    actual_test_pass=qualified,
)
(output / "codex-resume-results.json").write_text(json.dumps(observation, indent=2) + "\n")
raise SystemExit(0 if qualified else 1)
