"""Shared machinery for the native-CLI resume end-to-end tests.

Both ``omnigent claude`` and ``omnigent codex`` support resuming a prior
conversation (``--resume <conv_id>``). The regression these tests guard: a
resumed session must come back with its **history intact** — sending a new
message to the resumed session must let the model answer from the earlier
turns. (The original bug: the runner auto-created a fresh terminal for
CLI-driven sessions, so the resumed Claude/Codex started empty.)

The check here is harness-agnostic and outcome-based, which matters because
the two harnesses resume by different mechanisms — Claude relaunches with a
``--resume <claude_session_id>`` CLI flag; Codex re-opens a thread via its
app-server ``thread_id``. Asserting on launch args would only work for one of
them. Instead both tests verify the outcome:

1. **fresh** — a one-shot ``-p`` run that makes the model *emit* a distinctive
   passphrase as its own reply, so the passphrase lands in the model's own
   transcript output (the most reliably recalled context) and the server
   captures the conversation's ``external_session_id`` (the native
   session/thread id).
2. **resume** — launch ``--resume <conv_id>`` as a kept-alive session, send a
   recall message **through the server** (``POST /v1/sessions/{id}/events`` —
   the path the web UI uses) asking the model to repeat its own earlier
   passphrase, and poll the persisted items until an assistant reply echoes
   it. The passphrase coming back proves the resumed session actually had the
   prior history.

Recall asks for the model's *own* prior output rather than "what the user
said", to sidestep the model declining to attribute a value it was merely
told.

Reverting the host-spawned auto-create gate turns these red: the resumed
session launches empty, so the model cannot produce the passphrase (and, in
practice, the un-gated auto-create race also wedges the run).

The shared server fixture and the orchestrator live here so the per-harness
test files (``test_claude_native_cli_resume_e2e.py`` /
``test_codex_native_cli_resume_e2e.py``) stay thin and free of duplication.
"""

from __future__ import annotations

import contextlib
import os
import pty
import re
import select
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from omnigent.entities.session_resources import terminal_resource_id
from tests.e2e.helpers import POLL_INTERVAL_S

# Worktree root: tests/e2e/<this file> -> parents[2]. Threaded onto the CLI
# and server subprocesses' PYTHONPATH so they import THIS worktree's code
# (with the fix), not the editable install in the shared .venv.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Generous PTY width so the CLI's "Omnigent: <url>/c/<conv_id>" and
# "Resume with: … --resume <conv_id>" lines (which carry the full id) are not
# wrapped/truncated by the terminal.
_PTY_ROWS = 60
_PTY_COLS = 220

# Env vars that, leaked from a parent omnigent/Claude/Codex process into the
# code under test, would mis-route the runner or shadow the harness's own auth.
# Stripped from every CLI subprocess env.
_STALE_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY",
    "CODEX",
    "CLAUDE_CODE",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "TMUX",
    "RUNNER_SERVER_URL",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
)


def omnigent_console_script() -> Path:
    """
    Return the ``omnigent`` console-script path in the active venv.

    Driving the installed console script (rather than ``python -m omnigent``)
    matches how users invoke the CLI; ``PYTHONPATH`` (set by :func:`cli_env`)
    points it at the worktree's code.

    :returns: Absolute path to the ``omnigent`` entry point next to the
        running interpreter, e.g.
        ``"/Users/…/omnigent/.venv/bin/omnigent"``.
    :raises RuntimeError: If the console script is not found beside
        ``sys.executable``.
    """
    candidate = Path(sys.executable).parent / "omnigent"
    if not candidate.is_file():
        raise RuntimeError(
            f"`omnigent` console script not found at {candidate}; the test venv "
            f"must have omnigent installed."
        )
    return candidate


def cli_env(*, profile: str | None = None) -> dict[str, str]:
    """
    Build the subprocess environment for an ``omnigent <harness>`` PTY run.

    Points ``PYTHONPATH`` at the worktree so the CLI and its spawned runner
    load the fixed code; sets a wide PTY geometry so id lines are not
    truncated; and strips runner/tmux/credential env vars that would
    otherwise leak from this (possibly omnigent-hosted) process into the
    code under test (:data:`_STALE_ENV_VARS`). ``HOME`` is left intact — the
    native harness's interactive login (Claude Code's OAuth) is anchored to
    the real ``HOME`` and is NOT recoverable by symlinking ``~/.claude`` into
    a relocated one (a temp ``HOME`` yields "Not logged in").

    :param profile: Databricks profile for the LLM gateway. When set, an
        isolated ``OMNIGENT_CONFIG_HOME`` is created containing an
        ``auth: {type: databricks, profile: …}`` block — the supported
        replacement for the removed ``--profile`` CLI flag — and
        ``DATABRICKS_CONFIG_PROFILE`` is exported for ambient
        ``~/.databrickscfg`` lookups.
    :returns: The environment dict for ``pty``/``subprocess`` execution.
    """
    env = dict(os.environ)
    for stale in _STALE_ENV_VARS:
        env.pop(stale, None)
    if profile is not None:
        config_home = Path(tempfile.mkdtemp(prefix="omnigent-native-config-"))
        (config_home / "config.yaml").write_text(
            f"auth:\n  type: databricks\n  profile: {profile}\n",
            encoding="utf-8",
        )
        env["OMNIGENT_CONFIG_HOME"] = str(config_home)
        env["DATABRICKS_CONFIG_PROFILE"] = profile
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["TERM"] = "xterm-256color"
    env["LINES"] = str(_PTY_ROWS)
    env["COLUMNS"] = str(_PTY_COLS)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    return env


def run_cli_oneshot(args: list[str], *, env: dict[str, str], timeout: float) -> str:
    """
    Run ``omnigent <harness> … -p <prompt>`` under a PTY until it exits.

    The native CLIs attach a tmux-backed terminal and need a real TTY to
    render, so they are driven through a pseudo-terminal. With a one-shot
    ``-p`` prompt the model runs a single turn and the CLI exits on its own
    (the attach ends when the terminal closes) — no interactive keystrokes.

    :param args: Full argv, e.g.
        ``["/…/omnigent", "claude", "--server", url, "-p", "hi"]``.
    :param env: Subprocess environment from :func:`cli_env`.
    :param timeout: Max seconds to wait for the child to exit.
    :returns: The full decoded PTY output (ANSI sequences retained).
    :raises AssertionError: If the CLI does not exit within *timeout* (it was
        killed) — a hang usually means the launch or attach wedged.
    """
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(args[0], args, env)
        except OSError:
            os._exit(127)
    chunks: list[bytes] = []
    exited = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if data:
                chunks.append(data)
            else:
                # EOF on the PTY master: the child closed the slave, i.e. the
                # one-shot run finished and the CLI returned.
                exited = True
                break
    os.close(fd)
    if not exited:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    _reap(pid)
    output = b"".join(chunks).decode("utf-8", "replace")
    assert exited, (
        f"one-shot CLI did not exit within {timeout}s (killed); output tail:\n{output[-2000:]}"
    )
    return output


@dataclass
class PtyHandle:
    """
    A backgrounded ``omnigent <harness>`` PTY session kept alive for the test.

    A daemon thread drains the PTY master into :attr:`_buf` so the child never
    blocks on a full pty buffer while the test talks to it over HTTP.

    :param pid: Child process id (the CLI).
    :param fd: PTY master file descriptor.
    :param _buf: Accumulated raw output chunks (drained by the reader thread).
    :param _lock: Guards :attr:`_buf` against the reader thread / readers.
    :param _stop: Set on :meth:`terminate` to stop the drain thread.
    """

    pid: int
    fd: int
    _buf: list[bytes] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)

    def output(self) -> str:
        """
        Return everything the session has printed so far, decoded.

        :returns: The drained PTY output (ANSI sequences retained).
        """
        with self._lock:
            return b"".join(self._buf).decode("utf-8", "replace")

    def terminate(self) -> None:
        """
        Kill the CLI process and stop the drain thread.

        Idempotent and exception-safe — used in test teardown, so it must not
        raise even if the child already exited.

        :returns: None.
        """
        self._stop.set()
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        _reap(self.pid)
        with contextlib.suppress(OSError):
            os.close(self.fd)


def spawn_cli_background(
    args: list[str], *, env: dict[str, str], cwd: str | None = None
) -> PtyHandle:
    """
    Spawn ``omnigent <harness> …`` under a PTY and keep it running.

    The session stays running (this does not wait for exit) — it returns a
    :class:`PtyHandle` whose reader thread drains output while the test sends
    the session a message over HTTP. The caller MUST
    :meth:`PtyHandle.terminate` it in a ``finally``.

    :param args: Full argv (no ``-p`` — the session stays interactive), e.g.
        ``["/…/omnigent", "codex", "--server", url, "--resume", "conv_abc"]``.
    :param env: Subprocess environment from :func:`cli_env`.
    :param cwd: Working directory to ``chdir`` into before exec, e.g.
        ``"/tmp/x/pwd"``. ``None`` inherits the parent's cwd. The native
        wrappers launch the runner in their own cwd, so this controls the
        agent's working directory.
    :returns: A live :class:`PtyHandle`.
    """
    pid, fd = pty.fork()
    if pid == 0:
        try:
            if cwd is not None:
                os.chdir(cwd)
            os.execve(args[0], args, env)
        except OSError:
            os._exit(127)
    handle = PtyHandle(pid=pid, fd=fd)

    def _drain() -> None:
        while not handle._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except OSError:
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            with handle._lock:
                handle._buf.append(data)

    threading.Thread(target=_drain, name=f"pty-drain-{pid}", daemon=True).start()
    return handle


_CONV_ID_RE = re.compile(r"(conv_[0-9a-f]+)")


def wait_for_conversation_id(handle: PtyHandle, *, timeout: float) -> str:
    """
    Poll a backgrounded session's output until it prints its conversation id.

    Both CLIs print ``Omnigent: <url>/c/conv_<hex>`` shortly after creating
    the session. The wide PTY geometry keeps the id from wrapping.

    :param handle: The backgrounded session from :func:`spawn_cli_background`.
    :param timeout: Max seconds to wait for the id line.
    :returns: The conversation id, e.g. ``"conv_25cf39e3b0ea4d0c8721277215"``.
    :raises AssertionError: If no id appears within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = _CONV_ID_RE.search(handle.output())
        if match:
            return match.group(1)
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"no conversation id printed within {timeout}s; output tail:\n{handle.output()[-2000:]}"
    )


def conversation_id_from_output(output: str) -> str:
    """
    Extract the conversation id from a finished one-shot run's output.

    The CLI prints ``Resume with: … --resume conv_<hex>`` on exit (the full,
    un-truncated id); falls back to the ``…/c/conv_<hex>`` web link.

    :param output: Captured PTY output of a fresh ``-p`` run.
    :returns: The conversation id.
    :raises AssertionError: If no id is found (the run never created a session).
    """
    match = re.search(r"--resume (conv_[0-9a-f]+)", output) or _CONV_ID_RE.search(output)
    assert match is not None, f"no conversation id in CLI output:\n{output[-2000:]}"
    return match.group(1)


def wait_for_terminal_ready(
    client: httpx.Client, *, conversation_id: str, harness: str, timeout: float
) -> None:
    """
    Poll ``GET /v1/sessions/{id}/resources`` until the harness terminal exists.

    The session must have its terminal resource registered before a message
    injected via ``/events`` can reach the model; sending earlier risks the
    message being dropped.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: The session to wait on, e.g. ``"conv_abc"``.
    :param harness: Terminal harness name, ``"claude"`` or ``"codex"``.
    :param timeout: Max seconds to wait.
    :returns: None.
    :raises AssertionError: If the terminal never registers within *timeout*.
    """
    expected = terminal_resource_id(harness, "main")
    deadline = time.monotonic() + timeout
    last_seen: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{conversation_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last_seen = [r.get("id") for r in data]
            if any(r.get("id") == expected and r.get("type") == "terminal" for r in data):
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"terminal resource {expected!r} never appeared for {conversation_id} "
        f"within {timeout}s; saw {last_seen!r}."
    )


def inject_user_message(client: httpx.Client, *, conversation_id: str, text: str) -> None:
    """
    Send a user message to a session through the server (the web-UI path).

    POSTs to ``/v1/sessions/{id}/events`` exactly as the web UI does; the
    server routes it to the runner, which injects it into the native
    terminal. This is the "send it a message via the server" half of the
    history check.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: Target session id, e.g. ``"conv_abc"``.
    :param text: The user message text, e.g.
        ``"What passphrase did I give you?"``.
    :returns: None.
    :raises httpx.HTTPStatusError: If the server rejects the event.
    """
    resp = client.post(
        f"/v1/sessions/{conversation_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    )
    resp.raise_for_status()


def _assistant_text(item: dict[str, object]) -> str:
    """
    Extract concatenated assistant text from a session item.

    :param item: One element of ``GET /v1/sessions/{id}/items`` data, e.g.
        ``{"role": "assistant", "content": [{"type": "output_text",
        "text": "…"}]}``.
    :returns: The joined text of all text blocks, or ``""`` for non-assistant
        items or items without text blocks.
    """
    if item.get("role") != "assistant":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def poll_for_assistant_marker(
    client: httpx.Client, *, conversation_id: str, marker: str, timeout: float
) -> str:
    """
    Poll session items until an assistant message contains *marker*.

    The transcript forwarder mirrors the model's reply back into the session
    as an assistant item, so the marker appearing proves the resumed session
    answered with the prior-history value.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: Session id, e.g. ``"conv_abc"``.
    :param marker: Literal string the model should echo, e.g. ``"ZEPHYR-Q7H3K"``.
    :param timeout: Max seconds to wait for the reply.
    :returns: The matching assistant message text.
    :raises AssertionError: If no assistant message contains *marker* within
        *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            f"/v1/sessions/{conversation_id}/items", params={"limit": 50, "order": "desc"}
        )
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                text = _assistant_text(item)
                if marker in text:
                    return text
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"no assistant message containing {marker!r} for {conversation_id} within "
        f"{timeout}s — the resumed session did not answer from prior history."
    )


def poll_external_session_id(client: httpx.Client, *, conversation_id: str, timeout: float) -> str:
    """
    Poll ``GET /v1/sessions/{id}`` until ``external_session_id`` is captured.

    The fresh run mirrors the native session/thread id onto the conversation
    once the model has run a turn; its presence proves the fresh run produced
    a resumable session.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: The conversation created by the fresh run.
    :param timeout: Max seconds to wait.
    :returns: The captured native session/thread id.
    :raises AssertionError: If no id is captured within *timeout*.
    """
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{conversation_id}")
        if resp.status_code == 200:
            last = resp.json().get("external_session_id")
            if isinstance(last, str) and last:
                return last
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"external_session_id was never captured for {conversation_id} within "
        f"{timeout}s (last: {last!r}); the fresh run produced no resumable session."
    )


def poll_for_pending_elicitation(
    client: httpx.Client,
    *,
    conversation_id: str,
    timeout: float,
    needle: str | None = None,
) -> dict[str, object]:
    """
    Poll ``GET /v1/sessions/{id}`` until a pending elicitation is published.

    The server mirrors harness-originated approval prompts to the web UI as
    ``response.elicitation_request`` events, listed in
    ``SessionResponse.pending_elicitations``. This polls until one appears
    (optionally one whose rendered message/preview contains *needle*) and
    returns it, so a test can read its ``elicitation_id`` and resolve it via
    :func:`resolve_elicitation`.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: Session id, e.g. ``"conv_abc"``.
    :param timeout: Max seconds to wait for the prompt to surface.
    :param needle: Optional substring required in the elicitation's
        ``params.message`` or ``params.content_preview`` (to pin a specific
        prompt). ``None`` accepts the first pending elicitation.
    :returns: The pending elicitation event dict (carries ``elicitation_id``).
    :raises AssertionError: If no matching elicitation appears within *timeout*.
    """
    deadline = time.monotonic() + timeout
    last: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{conversation_id}")
        if resp.status_code == 200:
            pending = resp.json().get("pending_elicitations", [])
            if isinstance(pending, list):
                last = pending
                for event in pending:
                    if isinstance(event, dict) and (
                        needle is None or _elicitation_matches(event, needle)
                    ):
                        return event
        time.sleep(POLL_INTERVAL_S)
    suffix = f" containing {needle!r}" if needle else ""
    raise AssertionError(
        f"no pending elicitation{suffix} for {conversation_id} within "
        f"{timeout}s; last pending_elicitations seen: {last!r}"
    )


def _elicitation_matches(event: dict[str, object], needle: str) -> bool:
    """Return whether an elicitation event's message/preview contains *needle*."""
    params = event.get("params")
    if not isinstance(params, dict):
        return False
    return any(
        isinstance(params.get(key), str) and needle in params[key]
        for key in ("message", "content_preview")
    )


def resolve_elicitation(
    client: httpx.Client,
    *,
    conversation_id: str,
    elicitation_id: str,
    action: str = "accept",
) -> None:
    """
    Resolve a pending elicitation via the web-UI approval path.

    POSTs ``type == "approval"`` to ``/v1/sessions/{id}/events`` exactly as the
    web UI does when a user clicks Approve/Decline on an ``ApprovalCard``.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: Session id, e.g. ``"conv_abc"``.
    :param elicitation_id: Correlation id from the pending elicitation event.
    :param action: ``"accept"``, ``"decline"``, or ``"cancel"``.
    :returns: None.
    :raises httpx.HTTPStatusError: If the server rejects the event.
    """
    resp = client.post(
        f"/v1/sessions/{conversation_id}/events",
        json={
            "type": "approval",
            "data": {"elicitation_id": elicitation_id, "action": action},
        },
        timeout=30.0,
    )
    resp.raise_for_status()


def _delete_local_native_transcript(external_session_id: str) -> int:
    """
    Delete any local Claude Code transcript for *external_session_id*.

    Claude stores per-session transcripts at
    ``~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl``. The project
    dir is keyed by the launch cwd, which is awkward to reconstruct here
    (the harness resolves symlinks), so this globs by the ``<session_id>``
    stem under ``~/.claude/projects`` and removes every match. Used by the
    ``force_cold_resume`` path to guarantee the resume cannot reuse the
    harness's own on-disk transcript.

    :param external_session_id: Native Claude session id (the transcript
        file stem), e.g. ``"c89aa8f6-1430-4757-99e7-848ba179eb76"``.
    :returns: Number of transcript files deleted.
    """
    projects = Path.home() / ".claude" / "projects"
    removed = 0
    if projects.is_dir():
        for path in projects.rglob(f"{external_session_id}.jsonl"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
    return removed


def assert_native_cli_resume_restores_history(
    *,
    harness: str,
    server: str,
    profile: str,
    tmp_path: Path,
    force_cold_resume: bool = False,
) -> None:
    """
    Drive a fresh-then-resume CLI flow and assert the resume restored history.

    Harness-agnostic regression check for ``omnigent <harness> --resume``:

    1. **fresh** — a one-shot ``-p`` run that makes the model *emit* a
       distinctive passphrase as its own reply (so the passphrase lands in the
       model's own transcript output, the most reliably recalled context);
       then confirm ``external_session_id`` was captured (a resumable session
       was produced).
    2. **resume** — start a kept-alive ``--resume`` session and, once its
       terminal is ready, send a recall message **through the server**
       (``/events`` — the web-UI path) asking the model to repeat the
       passphrase from its own earlier message; poll the persisted items until
       an assistant reply echoes it.

    Recall asks for the model's *own* prior output (not something "the user
    said") to sidestep the model declining to attribute a passphrase it was
    merely told. The passphrase coming back proves the resumed session loaded
    the earlier turn. With the auto-create gate reverted the resumed session
    starts empty (and the un-gated race tends to wedge the run), so this fails.

    :param harness: CLI subcommand and terminal harness name, ``"claude"`` or
        ``"codex"``.
    :param server: Base URL of the allow-list-free test server.
    :param profile: Databricks CLI profile for the model gateway, e.g.
        ``"oss"``. Supplied to the spawned CLI via the config-home
        ``auth:`` block + ``DATABRICKS_CONFIG_PROFILE`` (the omnigent
        CLI no longer accepts ``--profile``).
    :param tmp_path: Per-test temp dir (reserved for per-run artifacts).
    :param force_cold_resume: When ``True``, delete the harness's local
        transcript for the captured native session id between the fresh and
        resume legs, so the resume cannot reuse the harness's own on-disk
        transcript and must instead go through Omnigent' cold-resume
        *synthesis* (rebuild the transcript from server-side items). This is
        the cross-context scenario a real user hits when resuming a
        conversation created elsewhere / in another cwd / on another machine —
        the path that was silently losing history. When ``False`` the test
        exercises the same-machine fast path (the harness resumes its own
        transcript).
    :returns: None.
    """
    omni = str(omnigent_console_script())
    # Use the real ``HOME`` so the native harness's interactive login resolves
    # (Claude Code's credentials are anchored to the real HOME and are NOT
    # captured by symlinking ``~/.claude`` into a temp HOME — a relocated HOME
    # yields "Not logged in"). The cost is that a *concurrent* ``omnigent``
    # process on the same machine can thrash the shared host daemon
    # (``~/.omnigent/host.pid``); run this opt-in test on an otherwise-idle
    # machine.
    env = cli_env(profile=profile)
    # Distinctive passphrase (uppercase + digits, unique per run) so a match in
    # the resumed reply cannot be coincidental.
    passphrase = f"ZEPHYR-{uuid.uuid4().hex[:8].upper()}"
    base = [omni, harness, "--server", server]

    # ── fresh: one-shot turn where the model EMITS the passphrase, so it is
    #    part of the model's own transcript output (reliably recalled) ──
    fresh_output = run_cli_oneshot(
        [
            *base,
            "-p",
            f"Reply with ONLY this exact passphrase and nothing else: {passphrase}",
        ],
        env=env,
        timeout=240.0,
    )
    conversation_id = conversation_id_from_output(fresh_output)

    with httpx.Client(base_url=server, timeout=30) as client:
        # The native session/thread id must be captured for a resumable session.
        external_session_id = poll_external_session_id(
            client, conversation_id=conversation_id, timeout=120.0
        )

        if force_cold_resume:
            # Remove the harness's own local transcript for this native
            # session so the resume can't take the same-machine fast path
            # (``_ensure_local_claude_resume_transcript`` returns early when
            # the file already exists). Forcing its absence makes the runner
            # synthesize the transcript from server-side items — the exact
            # cross-context path that was dropping history. Delete wherever it
            # landed (the project dir is keyed by the launch cwd's realpath,
            # which the harness resolves) by globbing the session-id stem.
            removed = _delete_local_native_transcript(external_session_id)
            assert removed, (
                f"force_cold_resume: expected a local transcript for "
                f"{external_session_id!r} from the fresh run to delete, but found "
                f"none — the fresh run may not have written one, so the resume "
                f"would not exercise the cold-resume synthesis path."
            )

        # ── resume: reopen the conversation and ask the model to repeat its own
        #    earlier passphrase via the server; only a session that loaded the
        #    prior turn can answer ──
        resumed = spawn_cli_background([*base, "--resume", conversation_id], env=env)
        try:
            wait_for_terminal_ready(
                client, conversation_id=conversation_id, harness=harness, timeout=90.0
            )
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=(
                    "Earlier in this conversation you gave me a passphrase. Repeat "
                    "that exact passphrase now, replying with ONLY the passphrase."
                ),
            )
            # The passphrase was the model's own reply in the fresh turn. It can
            # reappear in an assistant reply now only if the resumed session
            # loaded that earlier turn — i.e. history was restored. Its absence
            # is the regression (resume launched an empty session).
            reply = poll_for_assistant_marker(
                client, conversation_id=conversation_id, marker=passphrase, timeout=180.0
            )
            assert passphrase in reply, (
                f"resumed {harness} session did not recall the passphrase "
                f"{passphrase!r}; assistant said {reply!r}. The resume launched an "
                f"empty session (history lost) — the bug this guards."
            )
        finally:
            resumed.terminate()


def _reap(pid: int) -> None:
    """
    Reap a child process, ignoring "already reaped" races.

    :param pid: Child process id.
    :returns: None.
    """
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
