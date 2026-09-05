r"""E2E reproduction for OMNI-6236 / omnigent-ai/omnigent#6424.

``fix(kimi-native): first launch in an untrusted folder kills the session with
'Harness stream connection error.'``

The user journey
----------------
1. A ``kimi`` (Kimi Code, ``kimi-native``) session is launched against a
   brand-new directory the Kimi CLI has never seen before. Omnigent gives every
   task its own fresh git worktree, so **every** new worktree is a first launch.
2. On first launch in an untrusted folder the Kimi CLI parks on its interactive
   ``Trust this folder?`` modal before mounting its input box.
3. The user sends their first message from the web composer. The kimi-native
   harness delivers it by pasting into the resident TUI's tmux pane
   (:class:`omnigent.inner.kimi_native_executor.KimiNativeExecutor` →
   :func:`omnigent.harnesses.kimi_native.bridge.inject_user_message`).
4. Nothing established workspace trust before the CLI started, and the harness's
   in-pane auto-accept never recognises the live modal, so the paste is
   swallowed by the unanswered trust gate: the first message is **silently
   dropped** and the session is unusable on first launch. In the deployed UI
   this surfaces as ``Harness stream connection error.`` / "The connection to
   the agent dropped mid-turn."

This test reproduces the failure against the **real** Kimi Code CLI and the
**real** harness turn path (no mock): it launches a bare ``kimi`` TUI in a fresh,
untrusted workspace exactly as the runner's ``_auto_create_kimi_terminal`` does,
then drives one web turn through ``KimiNativeExecutor.run_turn`` and asserts the
user's message actually reached the CLI.

* On the current build the first message is dropped (the ``Trust this folder?``
  modal blocks the pane and is never answered), so the assertion **fails** —
  this is the reproduction.
* Once trust is established before launch (or the modal is correctly answered),
  the message lands in the CLI and the assertion **passes** — this is the
  fix's fail→pass target.

The session-scoped ``KIMI_CODE_HOME`` is built with the real
:func:`omnigent.harnesses.kimi_native.credentials.build_kimi_session_home` (the module the
recommended trust pre-seed fix targets), and the workspace is passed through when
the builder's signature accepts it, so a pre-seed fix flows through unchanged.

Real-binary test, gated on the ``kimi`` CLI and ``tmux`` being present; skipped
(not failed) otherwise so CI stays green without the upstream binary.

Usage::

    OMNIGENT_E2E_KIMI=1 uv run --group test pytest \
        tests/e2e/test_kimi_native_untrusted_folder_e2e.py -v --timeout=180
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.kimi_native.bridge import write_tmux_target
from omnigent.harnesses.kimi_native.credentials import build_kimi_session_home
from omnigent.harnesses.kimi_native.main import resolve_kimi_executable
from omnigent.inner.executor import ExecutorError
from omnigent.inner.kimi_native_executor import KimiNativeExecutor

# Distinctive substring the Kimi CLI prints for its first-run trust gate
# (verified against Kimi Code 0.41.0). Its presence at startup is the untrusted
# first-launch precondition; a trust pre-seed fix removes it.
_TRUST_MODAL_TEXT = "Trust this folder"
# Footer chrome that only renders once the CLI's input box is mounted.
_INPUT_READY_TEXT = "context:"

# The harness executor injects with a 30s tmux readiness budget
# (``_TMUX_READY_TIMEOUT_S``); allow the whole first turn to overrun it.
_STARTUP_TIMEOUT_S = 25.0
# How long to wait for the injected message to render in the pane after the turn.
_DELIVERY_POLL_S = 8.0
# Line kimi prints only when a submitted message reaches its turn loop without a
# configured provider. With this test's isolated (LLM-less) homes kimi may
# consume the submission and report the failed turn instead of echoing the text,
# so this line is delivery evidence; it is never printed unprompted.
_CONSUMED_WITHOUT_ECHO_TEXT = "Error: LLM not set"


def _kimi_native_e2e_reason() -> str | None:
    """Return a skip reason when the kimi-native prerequisites are absent.

    :returns: A human-readable skip reason, or ``None`` when prerequisites exist.
    """
    try:
        resolve_kimi_executable()
    except Exception:  # noqa: BLE001 - any resolution failure means "not installed here"
        return (
            "kimi-native e2e needs the `kimi` (Kimi Code) CLI on PATH. Install "
            "via `curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash`."
        )
    if shutil.which("tmux") is None:
        return "kimi-native e2e needs `tmux` on PATH (runner-owned kimi TUI pane)."
    return None


pytestmark = pytest.mark.skipif(
    _kimi_native_e2e_reason() is not None,
    reason=_kimi_native_e2e_reason() or "",
)


def _capture_pane(socket_path: Path, target: str) -> str:
    """Return the visible text of the kimi tmux pane."""
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-t", target],
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _wait_for_startup(socket_path: Path, target: str, *, timeout_s: float) -> str:
    """Wait until the CLI renders either the trust modal or its input box."""
    deadline = time.monotonic() + timeout_s
    pane = ""
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, target)
        if _TRUST_MODAL_TEXT in pane or _INPUT_READY_TEXT in pane:
            return pane
        time.sleep(0.5)
    return pane


def _run_turn(executor: KimiNativeExecutor, text: str) -> list[Any]:
    """Drive a single web turn through the real harness executor."""

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


def test_kimi_native_first_turn_in_untrusted_folder_delivers(tmp_path: Path) -> None:
    """First kimi-native turn in a brand-new folder must reach the CLI.

    Reproduces OMNI-6236: on the current build the ``Trust this folder?`` modal
    parks the CLI and the first message is silently dropped, so this fails; the
    fix (trust established before launch, or the modal answered) makes the
    message land and this passes.
    """
    # 1. A brand-new workspace + an empty user KIMI_CODE_HOME with no
    #    workspace-trust records — exactly what a fresh Omnigent worktree is.
    workspace = tmp_path / "fresh-worktree"
    workspace.mkdir()
    user_home = tmp_path / "kimi-code-home"
    user_home.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    session_home = tmp_path / "session-home"
    # Launch the CLI with an isolated, empty $HOME so this test observes only the
    # untrusted-folder trust gate this bug is about. The Kimi CLI's unrelated
    # first-run "Migrate from kimi-cli?" modal keys off a legacy `~/.kimi` home,
    # which some machines (and CI's harness setup) happen to seed — an ambient
    # gate that would otherwise leak in and park the CLI regardless of the fix.
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()

    # 2. Build the session-scoped KIMI_CODE_HOME via the real credentials builder
    #    (where the recommended trust pre-seed fix lives). Point the builder's
    #    notion of the user's global home at our empty dir so no pre-existing
    #    trust leaks in, and pass the workspace through when the (fixed)
    #    signature accepts it so a pre-seed fix takes effect here.
    import os as _os

    prior_home = _os.environ.get("KIMI_CODE_HOME")
    _os.environ["KIMI_CODE_HOME"] = str(user_home)
    try:
        build_kwargs: dict[str, Any] = {"bridge_dir": bridge_dir}
        if "workspace" in inspect.signature(build_kimi_session_home).parameters:
            build_kwargs["workspace"] = workspace
        home_env = build_kimi_session_home(session_home, **build_kwargs)
    finally:
        if prior_home is None:
            _os.environ.pop("KIMI_CODE_HOME", None)
        else:
            _os.environ["KIMI_CODE_HOME"] = prior_home

    kimi_home = home_env["KIMI_CODE_HOME"]
    kimi_bin = resolve_kimi_executable()

    # 3. Launch the bare interactive kimi TUI in the fresh workspace, mirroring
    #    the runner's ``_auto_create_kimi_terminal`` (bare ``kimi``, cwd = the
    #    workspace, session-scoped KIMI_CODE_HOME), and advertise the tmux target
    #    so the harness executor can inject into the same pane.
    socket_path = bridge_dir / "tmux.sock"
    target = "kimi"
    subprocess.run(
        [
            "tmux",
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
            f"env HOME={isolated_home} KIMI_CODE_HOME={kimi_home} {kimi_bin}",
        ],
        check=True,
    )
    try:
        write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=target)
        startup_pane = _wait_for_startup(
            socket_path, target, timeout_s=_STARTUP_TIMEOUT_S
        )
        modal_at_startup = _TRUST_MODAL_TEXT in startup_pane

        # 4. Drive the first web turn through the real harness executor.
        token = f"OMNI6236DELIVERYPROBE{uuid.uuid4().hex[:8]}".upper()
        started = time.monotonic()
        events = _run_turn(KimiNativeExecutor(bridge_dir=bridge_dir), token)
        elapsed = time.monotonic() - started

        # 5. Did the user's message actually reach the kimi CLI? Poll the pane
        #    for the token — when trust is in place it lands in the input box /
        #    transcript; when the trust modal ate it, it never appears.
        delivered = False
        deadline = time.monotonic() + _DELIVERY_POLL_S
        while time.monotonic() < deadline:
            pane = _capture_pane(socket_path, target)
            if token in pane:
                delivered = True
                break
            # No trust modal + the LLM-not-set turn error: the message passed
            # the trust gate and was submitted, but kimi consumed it without
            # echoing the text back into the pane.
            if not modal_at_startup and _CONSUMED_WITHOUT_ECHO_TEXT in pane:
                delivered = True
                break
            time.sleep(0.5)

        final_pane = _capture_pane(socket_path, target)
        errors = [e for e in events if isinstance(e, ExecutorError)]
        assert delivered, (
            "kimi-native first turn in an untrusted folder did not deliver the "
            f"user's message: token {token!r} never reached the kimi TUI. This "
            "is OMNI-6236 — the first-run 'Trust this folder?' modal parked the "
            "CLI and swallowed the message. "
            f"[trust modal at startup: {modal_at_startup}; run_turn took "
            f"{elapsed:.1f}s; executor errors: {[e.message for e in errors]}]\n"
            f"Final pane:\n{final_pane}"
        )
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
        )
