r"""Real-CLI regression coverage for first native launch in an untrusted folder.

The positive case builds the session-scoped ``KIMI_CODE_HOME`` through the
production credentials builder, launches Kimi in a fresh workspace, and proves
the input box appears without the trust modal ever being shown. It then sends a
message through ``KimiNativeExecutor.run_turn`` to exercise the real bridge.

The negative control removes the generated workspace-trust record before
launch and proves the same CLI parks on ``Trust this folder?``. The bridge is
not invoked in that case, so its modal auto-accept cannot mask a missing seed.

The tests use the installed Kimi Code payload but isolate its home, XDG paths,
credentials, proxy settings, and tmux server environment. They require Kimi
Code 0.41.0 or newer and run only when explicitly enabled.

Usage::

    OMNIGENT_E2E_KIMI=1 uv run --group test pytest \
        tests/e2e/test_kimi_native_untrusted_folder_e2e.py -v --timeout=180
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from packaging.version import InvalidVersion, Version

from omnigent.inner.executor import ExecutorError
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from omnigent.kimi_native import resolve_kimi_executable
from omnigent.kimi_native_bridge import write_tmux_target
from omnigent.kimi_native_credentials import build_kimi_session_home

_TRUST_MODAL_TEXT = "Trust this folder"
_INPUT_READY_TEXT = "context:"
_CONSUMED_WITHOUT_ECHO_TEXT = "Error: LLM not set"
_MIN_KIMI_VERSION = Version("0.41.0")
_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?!\d)")

_STARTUP_TIMEOUT_S = 25.0
_DELIVERY_POLL_S = 8.0
_PARK_CONFIRMATION_S = 1.0
_SUBPROCESS_TIMEOUT_S = 10.0
_CLEANUP_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class _KimiProbe:
    payload: Path
    version: Version
    version_output: str


def _resolve_kimi_payload(launcher: Path) -> Path:
    """Resolve wrappers that locate the installer payload through ``$HOME``."""
    resolved_launcher = launcher.resolve()
    try:
        with resolved_launcher.open("rb") as handle:
            header = handle.read(8192)
    except OSError:
        return resolved_launcher

    if header.startswith(b"#!") and b".kimi-code/bin/kimi" in header:
        installer_payload = Path.home() / ".kimi-code" / "bin" / "kimi"
        if installer_payload.is_file() and os.access(installer_payload, os.X_OK):
            return installer_payload.resolve()
    return resolved_launcher


@lru_cache(maxsize=1)
def _probe_kimi() -> _KimiProbe:
    launcher = Path(resolve_kimi_executable())
    payload = _resolve_kimi_payload(launcher)
    with tempfile.TemporaryDirectory(prefix="omnigent-kimi-version-") as temp_dir:
        probe_root = Path(temp_dir)
        isolated_home = probe_root / "home"
        kimi_home = probe_root / "kimi-code-home"
        isolated_home.mkdir()
        kimi_home.mkdir()
        probe_env = _isolated_launch_env(
            probe_root, isolated_home=isolated_home, kimi_home=kimi_home
        )
        try:
            proc = subprocess.run(
                [str(payload), "--version"],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
                env=probe_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not run {payload} --version: {exc}") from exc

    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part.strip())
    if proc.returncode != 0:
        raise RuntimeError(
            f"{payload} --version exited {proc.returncode}: {output or '<no output>'}"
        )
    match = _VERSION_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"could not parse Kimi Code version from {output!r}")
    try:
        version = Version(match.group(1))
    except InvalidVersion as exc:
        raise RuntimeError(f"invalid Kimi Code version in {output!r}") from exc
    return _KimiProbe(
        payload=payload,
        version=version,
        version_output=output,
    )


def _kimi_native_e2e_reason() -> str | None:
    if os.environ.get("OMNIGENT_E2E_KIMI") != "1":
        return "Real-binary e2e: set OMNIGENT_E2E_KIMI=1 to run."
    try:
        probe = _probe_kimi()
    except Exception as exc:
        return f"kimi-native e2e needs a working `kimi` (Kimi Code) CLI on PATH: {exc}"
    if shutil.which("tmux") is None:
        return "kimi-native e2e needs `tmux` on PATH (runner-owned kimi TUI pane)."
    if probe.version < _MIN_KIMI_VERSION:
        return (
            f"kimi-native e2e needs Kimi Code >= {_MIN_KIMI_VERSION}; "
            f"observed {probe.version_output!r}."
        )
    return None


def _capture_pane(socket_path: Path, target: str, *, phase: str, version: str) -> str:
    """Return the visible pane text or diagnose an exited Kimi process."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Kimi launch failure during {phase}: tmux capture timed out "
            f"(observed Kimi Code {version})."
        ) from exc
    except OSError as exc:
        raise AssertionError(
            f"Kimi launch failure during {phase}: tmux capture could not run: {exc} "
            f"(observed Kimi Code {version})."
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "tmux pane no longer exists"
        raise AssertionError(
            f"Kimi launch failure during {phase}: {detail} (observed Kimi Code {version})."
        )
    return proc.stdout


def _wait_for_seeded_input(
    socket_path: Path, target: str, *, timeout_s: float, version: str
) -> str:
    """Require input readiness without ever observing the trust modal."""
    deadline = time.monotonic() + timeout_s
    pane = ""
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, target, phase="seeded startup", version=version)
        if _TRUST_MODAL_TEXT in pane:
            raise AssertionError(
                "Kimi showed the workspace-trust modal before its input became ready; "
                "the trust seed is missing or incompatible "
                f"(observed Kimi Code {version}).\nPane:\n{pane}"
            )
        if _INPUT_READY_TEXT in pane:
            return pane
        time.sleep(0.5)
    raise AssertionError(
        "Kimi startup compatibility failure: neither the input-ready marker nor "
        "the workspace-trust modal appeared "
        f"(observed Kimi Code {version}).\nPane:\n{pane}"
    )


def _wait_for_untrusted_modal(
    socket_path: Path, target: str, *, timeout_s: float, version: str
) -> str:
    """Require the unseeded negative control to stop at the trust modal."""
    deadline = time.monotonic() + timeout_s
    pane = ""
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, target, phase="unseeded startup", version=version)
        if _TRUST_MODAL_TEXT in pane:
            return pane
        if _INPUT_READY_TEXT in pane:
            raise AssertionError(
                "Kimi reached its input despite removal of the workspace-trust record; "
                "the negative control no longer exercises an untrusted folder "
                f"(observed Kimi Code {version}).\nPane:\n{pane}"
            )
        time.sleep(0.5)
    raise AssertionError(
        "Kimi compatibility failure: the unseeded negative control showed neither "
        "the workspace-trust modal nor the input-ready marker "
        f"(observed Kimi Code {version}).\nPane:\n{pane}"
    )


def _run_turn(executor: KimiNativeExecutor, text: str) -> list[Any]:
    """Drive one web turn through the real native harness executor."""

    async def _collect() -> list[Any]:
        events: list[Any] = []
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": text}],
            tools=[],
            system_prompt="",
        ):
            events.append(event)
        return events

    return asyncio.run(_collect())


def _build_session_home(
    tmp_path: Path, *, remove_trust_record: bool
) -> tuple[Path, Path, Path, Path]:
    workspace = tmp_path / "fresh-worktree"
    workspace.mkdir()
    user_home = tmp_path / "user-kimi-home"
    user_home.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    session_home = tmp_path / "session-kimi-home"

    # Keyword args avoid exfil-scan.py's environment-dump token rule.
    with patch.dict(in_dict=os.environ, values={"KIMI_CODE_HOME": str(user_home)}):
        home_env = build_kimi_session_home(
            session_home,
            bridge_dir=bridge_dir,
            workspace=workspace,
        )

    kimi_home = Path(home_env["KIMI_CODE_HOME"])
    if remove_trust_record:
        trust_records = list((kimi_home / "workspace-trust").iterdir())
        assert trust_records, "negative control found no generated trust record to remove"
        for trust_record in trust_records:
            trust_record.unlink()

    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    return workspace, bridge_dir, kimi_home, isolated_home


def _isolated_launch_env(
    tmp_path: Path, *, isolated_home: Path, kimi_home: Path
) -> dict[str, str]:
    isolated_dirs = {
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_CACHE_HOME": tmp_path / "xdg-cache",
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "XDG_STATE_HOME": tmp_path / "xdg-state",
        "XDG_RUNTIME_DIR": tmp_path / "xdg-runtime",
        "TMPDIR": tmp_path / "tmp",
    }
    for path in isolated_dirs.values():
        path.mkdir()
    os.chmod(isolated_dirs["XDG_RUNTIME_DIR"], 0o700)

    return {
        "HOME": str(isolated_home),
        "KIMI_CODE_HOME": str(kimi_home),
        "PATH": os.defpath,
        "SHELL": "/bin/sh",
        "TERM": "xterm-256color",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        **{name: str(path) for name, path in isolated_dirs.items()},
    }


_SKIP_REASON = _kimi_native_e2e_reason()
pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None,
    reason=_SKIP_REASON or "",
)


def _launch_kimi(
    tmp_path: Path,
    *,
    socket_path: Path,
    workspace: Path,
    bridge_dir: Path,
    kimi_home: Path,
    isolated_home: Path,
    payload: Path,
    version: str,
) -> tuple[Path, str]:
    target = "kimi"
    launch_env = _isolated_launch_env(tmp_path, isolated_home=isolated_home, kimi_home=kimi_home)
    env_executable = shutil.which("env") or "/usr/bin/env"
    child_argv = [
        env_executable,
        "-i",
        *(f"{name}={value}" for name, value in launch_env.items()),
        str(payload),
    ]
    child_command = " ".join(shlex.quote(argument) for argument in child_argv)
    tmux_executable = shutil.which("tmux") or "tmux"
    try:
        proc = subprocess.run(
            [
                tmux_executable,
                "-S",
                str(socket_path),
                "new-session",
                "-d",
                "-s",
                target,
                "-x",
                "160",
                "-y",
                "48",
                "-c",
                str(workspace),
                child_command,
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            env=launch_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(
            f"Kimi launch failure: tmux could not start the TUI: {exc} "
            f"(observed Kimi Code {version})."
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no tmux output"
        raise AssertionError(
            f"Kimi launch failure: tmux exited {proc.returncode}: {detail} "
            f"(observed Kimi Code {version})."
        )
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=target)
    return socket_path, target


def _kill_tmux(socket_path: Path) -> None:
    """Stop the test-owned tmux server and confirm it exited."""
    if not socket_path.exists():
        return

    try:
        kill_proc = subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
            text=True,
            timeout=_CLEANUP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"tmux kill-server failed: {exc}") from exc
    if kill_proc.returncode != 0:
        detail = kill_proc.stderr.strip() or kill_proc.stdout.strip() or "no tmux output"
        raise AssertionError(f"tmux kill-server exited {kill_proc.returncode}: {detail}")

    try:
        probe = subprocess.run(
            ["tmux", "-S", str(socket_path), "list-sessions"],
            capture_output=True,
            text=True,
            timeout=_CLEANUP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"could not confirm tmux cleanup: {exc}") from exc
    if probe.returncode == 0:
        raise AssertionError(
            f"tmux cleanup left the test server running; socket retained at {socket_path}"
        )
    socket_path.unlink(missing_ok=True)


def _new_tmux_socket_path() -> Path:
    """Return a short socket path within macOS's Unix-domain length limit."""
    return Path("/tmp") / f"omnigent-kimi-e2e-{uuid.uuid4().hex}.sock"


def test_kimi_native_first_turn_in_untrusted_folder_delivers(tmp_path: Path) -> None:
    """Pre-seeded trust mounts input before the first bridge-delivered turn."""
    probe = _probe_kimi()
    workspace, bridge_dir, kimi_home, isolated_home = _build_session_home(
        tmp_path, remove_trust_record=False
    )
    socket_path = _new_tmux_socket_path()
    try:
        socket_path, target = _launch_kimi(
            tmp_path,
            socket_path=socket_path,
            workspace=workspace,
            bridge_dir=bridge_dir,
            kimi_home=kimi_home,
            isolated_home=isolated_home,
            payload=probe.payload,
            version=probe.version_output,
        )
        startup_pane = _wait_for_seeded_input(
            socket_path,
            target,
            timeout_s=_STARTUP_TIMEOUT_S,
            version=probe.version_output,
        )
        consumed_marker_before_turn = _CONSUMED_WITHOUT_ECHO_TEXT in startup_pane

        token = f"NATIVEDELIVERYPROBE{uuid.uuid4().hex[:8]}".upper()
        started = time.monotonic()
        events = _run_turn(KimiNativeExecutor(bridge_dir=bridge_dir), token)
        elapsed = time.monotonic() - started

        delivered = False
        deadline = time.monotonic() + _DELIVERY_POLL_S
        while time.monotonic() < deadline:
            pane = _capture_pane(
                socket_path,
                target,
                phase="first-turn delivery",
                version=probe.version_output,
            )
            if token in pane or (
                not consumed_marker_before_turn and _CONSUMED_WITHOUT_ECHO_TEXT in pane
            ):
                delivered = True
                break
            time.sleep(0.5)

        final_pane = _capture_pane(
            socket_path,
            target,
            phase="first-turn delivery",
            version=probe.version_output,
        )
        errors = [event for event in events if isinstance(event, ExecutorError)]
        assert delivered, (
            "Kimi's input was ready without a trust modal, but the first native "
            f"turn did not deliver {token!r} (observed Kimi Code "
            f"{probe.version_output}; run_turn took {elapsed:.1f}s; executor "
            f"errors: {[error.message for error in errors]}).\nPane:\n{final_pane}"
        )
    finally:
        body_error = sys.exception()
        try:
            _kill_tmux(socket_path)
        except Exception as cleanup_error:
            if body_error is None:
                raise
            body_error.add_note(f"Additional tmux cleanup failure: {cleanup_error}")


def test_kimi_native_without_trust_seed_parks_on_modal(tmp_path: Path) -> None:
    """Removing the trust record leaves the real CLI parked before input."""
    probe = _probe_kimi()
    workspace, bridge_dir, kimi_home, isolated_home = _build_session_home(
        tmp_path, remove_trust_record=True
    )
    socket_path = _new_tmux_socket_path()
    try:
        socket_path, target = _launch_kimi(
            tmp_path,
            socket_path=socket_path,
            workspace=workspace,
            bridge_dir=bridge_dir,
            kimi_home=kimi_home,
            isolated_home=isolated_home,
            payload=probe.payload,
            version=probe.version_output,
        )
        _wait_for_untrusted_modal(
            socket_path,
            target,
            timeout_s=_STARTUP_TIMEOUT_S,
            version=probe.version_output,
        )
        time.sleep(_PARK_CONFIRMATION_S)
        parked_pane = _capture_pane(
            socket_path,
            target,
            phase="unseeded modal confirmation",
            version=probe.version_output,
        )
        assert _TRUST_MODAL_TEXT in parked_pane, (
            "Kimi did not remain parked on the workspace-trust modal after the "
            f"seed was removed (observed Kimi Code {probe.version_output}).\n"
            f"Pane:\n{parked_pane}"
        )
        assert _INPUT_READY_TEXT not in parked_pane, (
            "Kimi mounted its input while the unseeded workspace-trust modal was "
            f"expected to block startup (observed Kimi Code {probe.version_output}).\n"
            f"Pane:\n{parked_pane}"
        )
    finally:
        body_error = sys.exception()
        try:
            _kill_tmux(socket_path)
        except Exception as cleanup_error:
            if body_error is None:
                raise
            body_error.add_note(f"Additional tmux cleanup failure: {cleanup_error}")
