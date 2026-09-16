"""Opt-in real tmux fixture whose only pane command is a local dummy program."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from tests._helpers.provider_setup_runtime import ProviderSetupRuntime

_FAKE_BODY = """import json, os, sys
from pathlib import Path
root = Path(os.environ["PROVIDER_FIXTURE_ROOT"])
log = root / "terminal-fixture-inputs.jsonl"
with log.open("a") as output:
    output.write(json.dumps({"event":"started", "pid":os.getpid()}) + "\\n")
print("FIXTURE ONLY - no real sign-in", flush=True)
print("Device code: TEST-1234", flush=True)
print("Input: ", end="", flush=True)
with log.open("a") as output:
    output.write(json.dumps({"event":"prompt_written", "pid":os.getpid()}) + "\\n")
value = sys.stdin.readline().rstrip("\\r\\n")
with log.open("a") as output:
    output.write(json.dumps({"event":"input", "pid":os.getpid(), "value":value}) + "\\n")
print("FIXTURE INPUT RECEIVED", flush=True)
print("Press Enter to finish: ", end="", flush=True)
sys.stdin.readline()
with log.open("a") as output:
    output.write(json.dumps({"event":"completed", "pid":os.getpid()}) + "\\n")
print("FIXTURE LOGIN COMPLETE", flush=True)
"""

_FAKE_AGY_BODY = """import json, os, sys
from pathlib import Path
root = Path(os.environ["PROVIDER_FIXTURE_ROOT"])
log = root / "terminal-fixture-inputs.jsonl"
with log.open("a") as output:
    output.write(json.dumps({"event":"agy_started", "pid":os.getpid()}) + "\\n")
print("FIXTURE ANTIGRAVITY ONLY - no real sign-in", flush=True)
print("Enter fixture-signin to complete the simulated browser sign-in: ", end="", flush=True)
with log.open("a") as output:
    output.write(json.dumps({"event":"agy_prompt_written", "pid":os.getpid()}) + "\\n")
value = sys.stdin.readline().rstrip("\\r\\n")
with log.open("a") as output:
    output.write(json.dumps({"event":"agy_input", "pid":os.getpid(), "value":value}) + "\\n")
if value == "fixture-signin":
    (root / "agy-signed-in").write_text("fixture only\\n")
    print("FIXTURE STATUS READY - terminal remains open", flush=True)
while sys.stdin.readline():
    pass
"""


def prepare(root: Path, python: Path, tmux: Path, *, antigravity: bool = False) -> None:
    """Write the only allowed pane program and a symlink to reviewed tmux."""
    binary_dir = root / "fixture-bin"
    binary_dir.mkdir(exist_ok=True)
    fake = binary_dir / "codex"
    fake.write_text(f"#!{python}\n" + _FAKE_BODY)
    fake.chmod(0o700)
    if antigravity:
        agy = binary_dir / "agy"
        agy.write_text(f"#!{python}\n" + _FAKE_AGY_BODY)
        agy.chmod(0o700)
    target = binary_dir / "tmux"
    if not target.exists():
        target.symlink_to(tmux)


class ProviderSetupTerminalRuntime(ProviderSetupRuntime):
    """Opt-in runtime permitting only reviewed tmux and the disposable dummy CLI."""

    def __init__(
        self,
        root: Path,
        checkout: Path,
        *,
        tmux: Path,
        port: int | None = None,
        timeout_test: bool = False,
        antigravity: bool = False,
    ):
        super().__init__(root, checkout, port=port)
        self.tmux = tmux.resolve(strict=True)
        self.timeout_test = timeout_test
        self.antigravity = antigravity

    def prepare(self) -> None:
        super().prepare()
        prepare(self.root, self.python, self.tmux, antigravity=self.antigravity)

    def environment(self, component: str) -> dict[str, str]:
        env = super().environment(component)
        env.update(
            {
                "PATH": str(self.root / "fixture-bin") + ":/usr/bin:/bin",
                "PROVIDER_FIXTURE_TERMINAL": "1",
                "PROVIDER_FIXTURE_TMUX": str(self.tmux),
                "PROVIDER_FIXTURE_TIMEOUT_TEST": "1" if self.timeout_test else "0",
                "PROVIDER_FIXTURE_ANTIGRAVITY": "1" if self.antigravity else "0",
            }
        )
        env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = ",".join(env)
        return env


def install(root: Path, socket_root: Path, python: Path, tmux: Path):
    """Inject only operation discovery/fake verification; retain the real PTY bridge."""
    from omnigent.host.setup_operations import SetupOperationManager, _SetupTerminal
    from omnigent.host.setup_transport import HostSetupDispatcher
    from omnigent.inner.terminal import (
        _TMUX_CONVERSATION_LINK_OPTION,
        _TMUX_EMPTY_OPTION_VALUE,
        _tmux_command_sequence,
        _tmux_managed_option_commands,
    )

    fake = root / "fixture-bin/codex"
    agy = root / "fixture-bin/agy"
    expected_body = f"#!{python}\n" + _FAKE_BODY
    expected_agy_body = f"#!{python}\n" + _FAKE_AGY_BODY
    antigravity = os.environ.get("PROVIDER_FIXTURE_ANTIGRAVITY") == "1"
    registered: dict[str, set[tuple[str, ...]]] = {}

    def create_terminal(plan: Any, operation_id: str):
        executable = Path(plan.executable)
        if executable == fake and tuple(plan.args) in {("login",), ("logout",)}:
            expected = expected_body
        elif antigravity and executable == agy and tuple(plan.args) == ():
            expected = expected_agy_body
        else:
            raise RuntimeError("Fixture only supports reviewed local dummy commands")
        if executable.read_text() != expected:
            raise RuntimeError("Fixture dummy executable changed")
        private = Path(tempfile.mkdtemp(prefix="t-", dir=socket_root))
        private.chmod(0o700)
        (private / "owner.pid").write_text(str(os.getpid()))
        terminal = _SetupTerminal(
            name="setup",
            session_key=operation_id,
            socket_path=private / "tmux.sock",
            private_dir=private,
            command=str(executable),
            args=list(plan.args),
            env={"SHELL": "/bin/sh"},
            env_unset=["BASH_ENV", "ENV"],
            scrollback=1000,
            keep_alive_after_exit=True,
        )
        command = " ".join(shlex.quote(arg) for arg in [str(executable), *plan.args])
        launch = _tmux_command_sequence(
            [
                *_tmux_managed_option_commands(
                    1000,
                    allow_passthrough=terminal.tmux_allow_passthrough,
                    keep_alive_after_exit=True,
                ),
                ["set-option", "-g", _TMUX_CONVERSATION_LINK_OPTION, _TMUX_EMPTY_OPTION_VALUE],
                [
                    "new-session",
                    "-d",
                    "-s",
                    "main",
                    "-x",
                    "80",
                    "-y",
                    "24",
                    "-c",
                    str(private),
                    command,
                ],
                ["set-hook", "-w", "pane-died", "detach-client -a"],
            ]
        )
        regular = [
            ("kill-server",),
            ("has-session", "-t", "main"),
            ("capture-pane", "-t", "main", "-p", "-e"),
            ("capture-pane", "-e", "-p", "-J", "-t", "main"),
            ("detach-client", "-s", "main"),
            ("-C", "attach", "-t", "main"),
        ]
        for fmt in ("#{pane_dead}", "#{pane_dead} #{pane_dead_status}", "#{pane_pid}"):
            regular.append(("list-panes", "-t", "main", "-F", fmt))
        regular.append(
            (
                "display-message",
                "-p",
                "-t",
                "main",
                "#{cursor_x},#{cursor_y},#{cursor_flag},#{alternate_on},"
                "#{mouse_standard_flag},#{mouse_button_flag},#{mouse_all_flag},"
                "#{mouse_sgr_flag},#{mouse_utf8_flag},#{keypad_cursor_flag},#{bracket_paste_flag}",
            )
        )
        registered[str(terminal.socket_path.resolve())] = {tuple(launch), *regular}
        with (root / "terminal-fixture-sockets.jsonl").open("a") as log:
            log.write(
                json.dumps(
                    {
                        "operation_id": operation_id,
                        "socket": str(terminal.socket_path),
                        "private_dir": str(private),
                    }
                )
                + "\n"
            )
        return terminal

    def resolver(name: str) -> str | None:
        if name == "codex":
            return str(fake)
        if antigravity and name == "agy":
            return str(agy)
        return str(tmux) if name == "tmux" else None

    def verify(*args: Any) -> bool:
        if antigravity and str(args[0]) == "antigravity-login":
            return (root / "agy-signed-in").exists()
        log = root / "terminal-fixture-inputs.jsonl"
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return bool(rows and rows[-1].get("event") == "completed")

    def operations(self: Any):
        if self._manager is None:
            self._manager = SetupOperationManager(
                terminal_factory=create_terminal,
                executable_resolver=resolver,
                verifier=verify,
                **(
                    {"command_timeout_seconds": 3}
                    if os.environ.get("PROVIDER_FIXTURE_TIMEOUT_TEST") == "1"
                    else {}
                ),
            )
        return self._manager

    HostSetupDispatcher._operations = operations
    if antigravity:
        from omnigent.onboarding import harness_install, harness_readiness
        from omnigent.onboarding.provider_config import GEMINI_FAMILY

        installed = harness_install.harness_cli_installed
        harness_install.harness_cli_installed = lambda key, **kwargs: (
            key == GEMINI_FAMILY or installed(key, **kwargs)
        )
        harness_readiness.harness_cli_installed = harness_install.harness_cli_installed
        harness_readiness.resolve_cli_binary = resolver
        harness_readiness._gemini_auth.gemini_login_detected = lambda: (
            root / "agy-signed-in"
        ).exists()
        harness_install.harness_cli_logged_in = lambda key, **kwargs: (
            (root / "agy-signed-in").exists() if key == GEMINI_FAMILY else False
        )
        import omnigent.host.setup_operations as setup_operations

        setup_operations.resolve_cli_binary = lambda binary, **kwargs: resolver(binary)

    def permitted(command: Any, env: Any, executable: Any) -> bool:
        if not isinstance(command, (tuple, list)) or not command:
            return False
        binaries = {"tmux", str(tmux), str(root / "fixture-bin/tmux")}
        if command[0] not in binaries or executable not in binaries:
            return False
        effective_env = os.environ if env is None else env
        if (
            effective_env.get("PROVIDER_FIXTURE_ROOT") != str(root)
            or effective_env.get("OMNIGENT_DISABLE_KEYRING") != "1"
            or effective_env.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.null.Keyring"
        ):
            return False
        if str(root / "boot") not in effective_env.get("PYTHONPATH", "").split(os.pathsep):
            return False
        if (root / "fixture-bin/tmux").resolve() != tmux.resolve():
            return False
        if list(command[1:]) == ["-V"]:
            return True
        if len(command) < 4 or command[1] != "-S":
            return False
        socket = str(Path(command[2]).resolve())
        allowed = registered.get(socket)
        if allowed is None:
            return False
        tail = list(command[3:])
        if tail[:2] == ["-f", "/dev/null"]:
            tail = tail[2:]
        if tuple(tail) not in allowed:
            return False
        if "new-session" in tail:
            if list(command[3:5]) != ["-f", "/dev/null"]:
                return False
            if (
                not env
                or env.get("SHELL") != "/bin/sh"
                or any(key in env for key in ("ENV", "BASH_ENV", "HOME", "CODEX_HOME"))
            ):
                return False
            if fake.read_text() != expected_body or (
                antigravity and agy.read_text() != expected_agy_body
            ):
                return False
        return True

    return permitted
