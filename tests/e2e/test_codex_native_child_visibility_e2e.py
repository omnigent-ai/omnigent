"""Manual regression through Chrome, the real host/runner, and native Codex.

Requires the checked-out UI's locked dependencies, Chrome, agent-browser, tmux,
and an explicitly supplied Codex configuration with working model credentials.
No protocol events, sessions, terminals, or browser responses are fabricated.
"""

import contextlib
import json
import os
import pty
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import httpx
import pytest
import tomlkit

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import find_free_port, isolated_local_server

pytestmark = pytest.mark.posix_only


COMMAND = """from pathlib import Path
import os
import sys
import time

gate = Path(sys.argv[1])
(gate / "started.tmp").write_text(str(os.getpid()))
(gate / "started.tmp").rename(gate / "started")
deadline = time.monotonic() + 180
while not (gate / "emit").exists():
    if time.monotonic() > deadline:
        raise TimeoutError("test did not release the output gate")
    time.sleep(0.05)
print((gate / "emit").read_text(), flush=True)
(gate / "emitted").touch()
while not (gate / "release").exists():
    if time.monotonic() > deadline:
        raise TimeoutError("test did not release the command")
    time.sleep(0.05)
(gate / "finished").touch()
"""

COMMAND_BUTTON = 'button[aria-expanded][title*="native_child_command.py"]'


def wait_for(predicate, description, timeout=90):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (httpx.HTTPError, OSError) as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}; last error: {last_error}")


class CodexNativeChildVisibilityE2ETest(unittest.TestCase):
    def setUp(self):
        source_home_value = os.environ.get("OMNIGENT_NATIVE_E2E_CODEX_HOME")
        if not source_home_value:
            self.skipTest("Set OMNIGENT_NATIVE_E2E_CODEX_HOME to opt into real Codex inference")
        self.repository = Path(__file__).resolve().parents[2]
        source_home = Path(source_home_value)
        for executable in ("codex", "tmux", "agent-browser", "node", "google-chrome"):
            self.assertIsNotNone(
                shutil.which(executable), f"Missing E2E prerequisite: {executable}"
            )
        self.root = Path(tempfile.mkdtemp(prefix="omnigent-native-child-e2e-"))
        self.output = Path(
            os.environ.get("OMNIGENT_NATIVE_E2E_OUTPUT_DIR", self.root / "evidence")
        )
        self.output.mkdir(parents=True, exist_ok=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.gate = self.workspace / "gate"
        self.gate.mkdir()
        self.command = self.workspace / "native_child_command.py"
        self.command.write_text(COMMAND)
        self.data = self.root / "host-data"
        self.data.mkdir()
        self.processes = []
        self.logs = []
        self.browser_session = "native-child-" + uuid.uuid4().hex
        self.streams = []
        self.stream_events = {}
        self.server_context = contextlib.ExitStack()
        self.addCleanup(self.server_context.close)
        self.addCleanup(self._stop)

        # Keep only inference configuration: inherited MCP/hooks would connect
        # this test to the developer's active agent session.
        source_config = tomlkit.parse((source_home / "config.toml").read_text())
        private_config = tomlkit.document()
        for key in ("model", "model_provider", "model_providers", "model_reasoning_effort"):
            if key in source_config:
                private_config[key] = source_config[key]
        private_config["features"] = {"multi_agent": True, "hooks": True}
        private_config["approval_policy"] = "never"
        private_config["sandbox_mode"] = "danger-full-access"
        private_config["projects"] = {str(self.workspace): {"trust_level": "trusted"}}
        native_home = self.root / "native-config"
        native_home.mkdir(mode=0o700)
        (native_home / "config.toml").write_text(tomlkit.dumps(private_config))
        (native_home / "config.toml").chmod(0o600)
        for filename in ("auth.json", ".credentials.json"):
            if (source_home / filename).is_file():
                shutil.copyfile(source_home / filename, native_home / filename)
                (native_home / filename).chmod(0o600)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT", "CODEX", "PYTEST", "TEST_"))
        }
        self.env.update(
            PYTHONPATH=os.pathsep.join(path for path in sys.path if path),
            CODEX_HOME=str(native_home),
            OMNIGENT_DATA_DIR=str(self.data),
            OMNIGENT_CONFIG_HOME=str(self.data),
            OMNIGENT_AUTH_ENABLED="0",
            OMNIGENT_LOCAL_SINGLE_USER="1",
            OMNIGENT_RUNNER_ZYGOTE="0",
            OMNIGENT_WRAPPER_BYPASS="1",
            OMNIGENT_NO_UPDATE_CHECK="1",
            OMNIGENT_CODEX_NATIVE_STATE_DIR=str(self.root / "codex-state"),
            NO_PROMPT="1",
            TERM="xterm-256color",
        )
        self.server_url = self.server_context.enter_context(
            isolated_local_server(
                self.root / "server",
                base_env=self.env,
                env={"OMNIGENT_AUTH_ENABLED": "0", "OMNIGENT_WRAPPER_BYPASS": "1"},
                cwd=self.workspace,
            )
        )
        ui_port = find_free_port()
        self.ui_url = f"http://127.0.0.1:{ui_port}"
        self._start(
            "ui",
            [
                "node",
                str(self.repository / "web/node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                str(ui_port),
                "--strictPort",
            ],
            env={**self.env, "OMNIGENT_URL": self.server_url},
            cwd=self.repository / "web",
        )
        wait_for(lambda: httpx.get(self.ui_url, timeout=2).is_success, "Vite readiness")
        self._browser("open", self.ui_url)

    def _start(self, name, args, *, env=None, cwd=None, tty=False):
        log = (self.root / f"{name}.log").open("wb")
        self.logs.append(log)
        master = slave = None
        if tty:
            master, slave = pty.openpty()
        process = subprocess.Popen(
            args,
            env=env or self.env,
            cwd=cwd or self.workspace,
            stdin=slave if tty else subprocess.DEVNULL,
            stdout=slave if tty else log,
            stderr=slave if tty else log,
            start_new_session=True,
        )
        self.processes.append(process)
        if tty:
            assert slave is not None
            os.close(slave)

            def drain():
                assert master is not None
                try:
                    while chunk := os.read(master, 65536):
                        log.write(chunk)
                        log.flush()
                except OSError:
                    pass
                finally:
                    os.close(master)

            threading.Thread(target=drain, daemon=True).start()
        return process

    def _get(self, path):
        response = httpx.get(self.server_url + path, timeout=5)
        response.raise_for_status()
        return response.json()

    def _browser(self, *args):
        chrome = shutil.which("google-chrome")
        assert chrome is not None
        result = subprocess.run(
            [
                "agent-browser",
                "--session",
                self.browser_session,
                "--executable-path",
                chrome,
                "--json",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = json.loads(result.stdout)
        self.assertTrue(result.get("success"), result)
        return result.get("data")

    def _page(self):
        return self._browser(
            "eval",
            "({text: document.body.innerText, "
            "terminal: !!document.querySelector('[data-testid=\"view-mode-terminal\"]'), "
            "composer: !!document.querySelector('textarea'), "
            f"commands: document.querySelectorAll({json.dumps(COMMAND_BUTTON)}).length, "
            f"commandOpen: document.querySelector({json.dumps(COMMAND_BUTTON)})"
            "?.getAttribute('aria-expanded') === 'true'"
            "})",
        )["result"]

    def _observe_stream(self, session_id):
        connected = threading.Event()
        events = self.stream_events[session_id] = []

        def observe():
            try:
                with httpx.stream(
                    "GET",
                    self.server_url + f"/v1/sessions/{session_id}/stream",
                    timeout=httpx.Timeout(10, read=None),
                ) as response:
                    response.raise_for_status()
                    self.streams.append(response)
                    connected.set()
                    for line in response.iter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            events.append(json.loads(line[6:]))
            except (httpx.HTTPError, OSError):
                pass

        threading.Thread(target=observe, daemon=True).start()
        self.assertTrue(connected.wait(10), "Real session event stream did not connect")
        return events

    def _stop(self):
        (self.gate / "emit").touch(exist_ok=True)
        (self.gate / "release").touch()
        for session_id, events in self.stream_events.items():
            (self.output / f"{session_id}-events.json").write_text(json.dumps(events, indent=2))
        for response in self.streams:
            with contextlib.suppress(Exception):
                response.close()
        with contextlib.suppress(Exception):
            self._browser("close")
        for process in reversed(self.processes):
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        _, survivors = reap_leaked_omnigent_processes(self.data)
        self.assertFalse(survivors, "Test-owned Omnigent processes survived cleanup")
        for log in self.logs:
            log.close()
        # Raw native logs and credentials remain private; only deliberate test
        # evidence is exported to the requested output directory.
        print(f"Private runtime diagnostics: {self.root}", flush=True)

    def test_native_child_terminal_ownership_and_live_progress(self):
        command = shlex.join(["python3", str(self.command), str(self.gate)])
        prompt = (
            "This is an authorized native-subagent E2E test. Use the native spawn_agent tool "
            "to create exactly one child. Do not use sys_session_create. The child's entire "
            f"task is to execute this exact harmless command with exec_command: {command}. "
            "Set yield_time_ms=30000, and keep the command running until it exits; the test "
            "controls its gates. Do not read, edit, or release the gate files. The child must "
            "use native exec_command, not an MCP terminal or sys_os tool. You (the parent) "
            "must not execute the command. Wait for the child and then report its exit status."
        )
        self._start(
            "codex-cli",
            [
                sys.executable,
                "-m",
                "omnigent",
                "codex",
                "--server",
                self.server_url,
                "--prompt",
                prompt,
            ],
            tty=True,
        )
        parent = wait_for(
            lambda: next(
                (
                    session
                    for session in self._get("/v1/sessions?visibility=all")["data"]
                    if session.get("agent_name") == "codex-native-ui"
                    and session.get("parent_session_id") is None
                ),
                None,
            ),
            "real Codex parent session",
        )
        parent_id = parent["id"]
        self._observe_stream(parent_id)
        self._browser("open", self.ui_url + "/c/" + parent_id)
        wait_for(lambda: self._page()["terminal"], "parent terminal toggle")
        child = wait_for(
            lambda: next(
                iter(self._get(f"/v1/sessions/{parent_id}/child_sessions")["data"]), None
            ),
            "native child registration",
            timeout=180,
        )
        child_id = child["id"]
        child_events = self._observe_stream(child_id)
        self._browser("open", self.ui_url + "/c/" + child_id)
        wait_for(lambda: self._page()["composer"], "child chat loaded")
        wait_for(
            lambda: (self.gate / "started").exists(), "real child command execution", timeout=120
        )
        os.kill(int((self.gate / "started").read_text()), 0)
        self.assertFalse((self.gate / "finished").exists())
        snapshot = self._get(f"/v1/sessions/{child_id}")
        self.assertTrue(
            snapshot["runner_online"], "Positive control: the child shares a live runner"
        )
        self.assertEqual(
            snapshot["runner_id"], self._get(f"/v1/sessions/{parent_id}")["runner_id"]
        )
        self.assertEqual(self._get(f"/v1/sessions/{child_id}/resources/terminals")["data"], [])
        parent_terminals = self._get(f"/v1/sessions/{parent_id}/resources/terminals")["data"]
        self.assertTrue(
            any(terminal["id"] == "terminal_codex_main" for terminal in parent_terminals)
        )
        (self.output / "child-running.json").write_text(json.dumps(snapshot, indent=2))

        with self.subTest("mirror does not claim to own a terminal"):
            self.assertNotEqual(snapshot.get("labels", {}).get("omnigent.ui"), "terminal")
        page = self._page()
        with self.subTest("browser does not offer a nonexistent child terminal"):
            self.assertFalse(page["terminal"], "Native mirror incorrectly offers Terminal view")
        if page["terminal"]:
            self._browser("click", '[data-testid="view-mode-terminal"]')
            terminal_page = self._page()
            (self.output / "child-terminal.txt").write_text(terminal_page["text"])
            self._browser("screenshot", str(self.output / "child-terminal.png"))
            with self.subTest("online child is not presented as stopped"):
                self.assertNotIn("The harness is not running", terminal_page["text"])
            self._browser("click", '[data-testid="view-mode-chat"]')
        with self.subTest("running command appears before completion"):
            wait_for(lambda: self._page()["commands"], "live command card", timeout=5)

        marker = "CHILD_LIVE_" + uuid.uuid4().hex
        (self.gate / "emit").write_text(marker)
        wait_for(lambda: (self.gate / "emitted").exists(), "real stdout emission", timeout=5)
        with self.subTest("stdout streams to the child before the command exits"):
            wait_for(
                lambda: any(
                    marker in json.dumps(event) and "delta" in json.dumps(event)
                    for event in child_events
                ),
                "child output delta",
                timeout=5,
            )
        with self.subTest("browser displays the live stdout"):
            if self._page()["commands"]:
                self._browser("click", COMMAND_BUTTON)
            wait_for(lambda: marker in self._page()["text"], "live stdout in Chrome", timeout=5)
        self.assertFalse(
            (self.gate / "finished").exists(), "Assertions must run before command completion"
        )
        self.assertFalse(
            any(marker in json.dumps(event) for event in self.stream_events[parent_id]),
            "Child output leaked into the parent stream",
        )
        self._browser("screenshot", str(self.output / "child-running.png"))
        (self.gate / "release").touch()
        wait_for(lambda: (self.gate / "finished").exists(), "normal command completion", timeout=5)
        completed = wait_for(
            lambda: (
                value
                if marker in json.dumps(value := self._get(f"/v1/sessions/{child_id}"))
                else None
            ),
            "completed child transcript",
            timeout=90,
        )
        calls = [
            item
            for item in completed["items"]
            if item["type"] == "function_call"
            and self.command.name in item["data"].get("arguments", "")
        ]
        self.assertEqual(
            len(calls), 1, "The live call and completed call must reconcile without duplicates"
        )
        outputs = [
            item
            for item in completed["items"]
            if item["type"] == "function_call_output"
            and item["data"].get("call_id") == calls[0]["data"]["call_id"]
        ]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["data"]["output"], marker + "\n")
        wait_for(lambda: self._page()["commands"] == 1, "completed command card")
        if not self._page()["commandOpen"]:
            self._browser("click", COMMAND_BUTTON)
        wait_for(lambda: marker in self._page()["text"], "completed stdout in Chrome")
        (self.output / "child-completed.json").write_text(json.dumps(completed, indent=2))
        (self.output / "identities.json").write_text(
            json.dumps({"parent": parent_id, "child": child_id, "marker": marker}, indent=2)
        )


if __name__ == "__main__":
    unittest.main()
