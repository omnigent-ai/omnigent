"""E2E regression: a web approval card approved in the terminal must clear even
when the approved tool was ``EnterWorktree``, which relocates Claude's transcript.

Guarded bug
-----------
On a claude-native session the user approves ``EnterWorktree({"path": ...})``
in the terminal view; the command continues there, but the chat view stays
stuck on the "Approval required" card until the user clicks Approve on the
web card too (or until the turn ends, minutes later).

Mechanism (the seam this drives)
--------------------------------
claude-native has exactly one "the terminal already answered this" signal for
a parked web approval: the forwarder mirrors the gated tool's
``function_call_output`` from Claude's JSONL transcript, and the server
correlates it back to the parked prompt (``resolved_elsewhere``), ending the
hook's long-poll and publishing ``response.elicitation_resolved``. Claude Code
never signals the abandoned ``PermissionRequest`` hook itself (no signal, the
stdout pipe stays open), so the mirror IS the signal.

``EnterWorktree`` into a worktree outside ``.claude/worktrees/`` (and
``ExitWorktree`` back) MOVES the session transcript from
``~/.claude/projects/<old-cwd-slug>/<sid>.jsonl`` to
``~/.claude/projects/<worktree-slug>/<sid>.jsonl``. The bridge learns the
transcript path only from the observed hooks (``Stop``, ``UserPromptSubmit``,
...); nothing fires between the approval and the turn's ``Stop``, so the
forwarder silently keeps tailing a path that no longer exists: no items are
mirrored, the tool result never arrives, the card never clears.

Environment fidelity
--------------------
Real ``omnigent server`` subprocess, a real claude-native session, the hook
settings the runner really writes (``build_hook_settings``), the real hook
subprocesses those settings name (the observer hook and the
``permission-request`` long-poll), and the real
``forward_claude_transcript_to_session`` loop. Claude Code itself is replayed:
the test performs the exact file-system + hook sequence captured from Claude
Code 2.1.269 — transcript records in the shapes it writes, hooks fired through
the generated settings with its payload shapes, and the transcript moved on
approval the way ``EnterWorktree`` moves it.

Desired behavior (asserted): after the terminal approval, the parked
``permission-request`` hook returns, the session has no pending elicitation,
and the ``EnterWorktree`` result is in ``GET /v1/sessions/<id>/items`` — all
without a ``Stop`` hook. Returning via ``ExitWorktree`` must also mirror its
result, both on a fresh forwarder and one started in reattach mode. Buggy
behavior: the hook stays parked and nothing past the relocation is mirrored.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_worktree_relocation_e2e.py -v

No ``--llm-api-key`` / ``--profile`` needed -- no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)

# The spawned server resolves worktree imports from the repo root and the SDKs.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.25
# Wall time for the forwarder to notice a change and the server to act on it.
# The buggy path never converges (the hook's own long-poll lasts a day), so a
# short deadline is what turns the bug into a failure.
_CONVERGE_S = 20.0

_CLAUDE_VERSION = "2.1.269"
_CLAUDE_SESSION_ID = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
_TOOL_USE_ID = "toolu_bdrk_01EnterWorktreeApprovedInTui"
_EXIT_TOOL_USE_ID = "toolu_bdrk_01ExitWorktreeKeep"
_PROMPT = "marker-user-prompt-work-in-the-existing-universe-worktree"
_ASSISTANT_TEXT = "marker-assistant-entering-the-worktree-first"
_REATTACH_PROMPT = "marker-preexisting-history-before-reattach"


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy/credentials in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Header auth + single-user keeps the spawned server out of login
        # mode; ambient auth/OIDC vars would otherwise 401 every call.
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    for name in list(env):
        if (
            name.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or name.endswith("_SECRET")
            or name
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
            env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[Any] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _project_dir(claude_home: Path, cwd: Path) -> Path:
    """Claude Code's per-project transcript dir: the cwd with every non-alphanumeric
    character replaced by ``-`` (``/tmp/a.b`` -> ``-tmp-a-b``).

    :param claude_home: Fake ``~/.claude`` root.
    :param cwd: Session working directory the dir is keyed on.
    :returns: ``<claude_home>/projects/<slug>``.
    """
    return claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _record(
    record_type: str,
    *,
    uuid: str,
    parent_uuid: str | None,
    cwd: Path,
    git_branch: str,
    message: dict[str, Any],
    **extra: Any,
) -> str:
    """One transcript line in the envelope shape Claude Code 2.1.269 writes."""
    return json.dumps(
        {
            "parentUuid": parent_uuid,
            "isSidechain": False,
            "type": record_type,
            "message": message,
            "uuid": uuid,
            "timestamp": "2026-09-13T18:55:19.000Z",
            "session_id": _CLAUDE_SESSION_ID,
            "userType": "external",
            "entrypoint": "cli",
            "cwd": str(cwd),
            "sessionId": _CLAUDE_SESSION_ID,
            "version": _CLAUDE_VERSION,
            "gitBranch": git_branch,
            **extra,
        }
    )


def _pre_approval_transcript(cwd: Path, worktree: Path) -> str:
    """The turn as Claude wrote it before asking permission for EnterWorktree."""
    return (
        "\n".join(
            [
                _record(
                    "user",
                    uuid="user-prompt-uuid",
                    parent_uuid=None,
                    cwd=cwd,
                    git_branch="main",
                    message={"role": "user", "content": _PROMPT},
                    promptSource="typed",
                ),
                _record(
                    "assistant",
                    uuid="assistant-text-uuid",
                    parent_uuid="user-prompt-uuid",
                    cwd=cwd,
                    git_branch="main",
                    message={
                        "role": "assistant",
                        "content": [{"type": "text", "text": _ASSISTANT_TEXT}],
                    },
                ),
                _record(
                    "assistant",
                    uuid="assistant-tool-use-uuid",
                    parent_uuid="assistant-text-uuid",
                    cwd=cwd,
                    git_branch="main",
                    message={
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": _TOOL_USE_ID,
                                "name": "EnterWorktree",
                                "input": {"path": str(worktree)},
                            }
                        ],
                    },
                    wireToolInputs={_TOOL_USE_ID: {"path": str(worktree)}},
                    apiBlockIndex=0,
                ),
            ]
        )
        + "\n"
    )


def _post_approval_records(cwd: Path, worktree: Path, result_message: str) -> str:
    """What Claude appends once the user approves and EnterWorktree runs."""
    worktree_state = json.dumps(
        {
            "type": "worktree-state",
            "worktreeSession": {
                "originalCwd": str(cwd),
                "preEnterOriginalCwd": str(cwd),
                "worktreePath": str(worktree),
                "worktreeName": worktree.name,
                "worktreeBranch": worktree.name,
                "sessionId": _CLAUDE_SESSION_ID,
                "enteredExisting": True,
            },
            "sessionId": _CLAUDE_SESSION_ID,
        }
    )
    tool_result = _record(
        "user",
        uuid="user-tool-result-uuid",
        parent_uuid="assistant-tool-use-uuid",
        cwd=worktree,
        git_branch=worktree.name,
        message={
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": result_message,
                    "tool_use_id": _TOOL_USE_ID,
                }
            ],
        },
        toolUseResult={
            "worktreePath": str(worktree),
            "worktreeBranch": worktree.name,
            "message": result_message,
        },
        sourceToolAssistantUUID="assistant-tool-use-uuid",
        promptId="prompt-id-uuid",
    )
    return worktree_state + "\n" + tool_result + "\n"


def _hook_payload(event_name: str, *, cwd: Path, transcript_path: Path, **fields: Any) -> str:
    """A Claude Code hook stdin payload (the common envelope + event fields)."""
    return json.dumps(
        {
            "session_id": _CLAUDE_SESSION_ID,
            "transcript_path": str(transcript_path),
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": event_name,
            **fields,
        }
    )


def _matching_hook_commands(
    settings: dict[str, Any], event_name: str, subject: str
) -> list[tuple[str, float]]:
    """Resolve the commands Claude Code would run for one hook event.

    Mirrors Claude Code's dispatch: every entry registered for *event_name*
    whose ``matcher`` is absent, empty, ``"*"``, or a regex that matches the
    event's subject (``tool_name`` for tool events, ``source`` for
    ``SessionStart``) runs each of its command hooks.

    :param settings: The ``build_hook_settings`` fragment Claude was launched with.
    :param event_name: Hook event, e.g. ``"PostToolUse"``.
    :param subject: Matcher subject, e.g. ``"EnterWorktree"``.
    :returns: ``(shell command, timeout seconds)`` pairs in registration order.
    """
    commands: list[tuple[str, float]] = []
    for entry in settings.get("hooks", {}).get(event_name, []):
        matcher = entry.get("matcher")
        if matcher not in (None, "", "*") and not re.search(str(matcher), subject):
            continue
        for hook in entry.get("hooks", []):
            if hook.get("type") == "command":
                commands.append((str(hook["command"]), float(hook.get("timeout", 60))))
    return commands


def _run_hooks(
    settings: dict[str, Any],
    event_name: str,
    payload: str,
    *,
    cwd: Path,
    env: dict[str, str],
    subject: str = "",
) -> None:
    """Run every matching command hook to completion, payload on stdin."""
    for command, timeout in _matching_hook_commands(settings, event_name, subject):
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(
                ["/bin/sh", "-c", command],
                input=payload.encode(),
                cwd=cwd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )


def _spawn_permission_request_hook(
    settings: dict[str, Any], payload: str, *, cwd: Path, env: dict[str, str]
) -> subprocess.Popen[bytes]:
    """Start the ``PermissionRequest`` hook exactly as Claude does: one process,
    payload on stdin, left running while the user decides."""
    commands = _matching_hook_commands(settings, "PermissionRequest", "EnterWorktree")
    assert len(commands) == 1, f"expected one PermissionRequest hook, got {commands}"
    proc = subprocess.Popen(
        ["/bin/sh", "-c", f"exec {commands[0][0]}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(payload.encode())
    proc.stdin.close()
    return proc


def _pending_elicitations(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """The session's outstanding approval prompts as the web UI sees them."""
    resp = _http.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    resp.raise_for_status()
    return list(resp.json().get("pending_elicitations") or [])


def _items_containing(base_url: str, session_id: str, marker: str) -> int:
    """Count committed conversation items whose payload contains *marker*."""
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


async def _await_condition(predicate: Callable[[], bool], what: str, timeout_s: float) -> bool:
    """Poll *predicate* off the event loop until true or *timeout_s* elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(_POLL_S)
    return False


async def _replay_terminal_approved_enter_worktree(
    *,
    base_url: str,
    session_id: str,
    bridge_dir: Path,
    settings: dict[str, Any],
    claude_home: Path,
    cwd: Path,
    worktree: Path,
    hook_env: dict[str, str],
    start_at_end: bool,
) -> dict[str, Any]:
    """Replay the journey against the real forwarder loop.

    :returns: Observations for the assertions: whether the parked hook
        returned, the pending elicitations left, and how many items carry the
        EnterWorktree result.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    old_dir = _project_dir(claude_home, cwd)
    new_dir = _project_dir(claude_home, worktree)
    old_transcript = old_dir / f"{_CLAUDE_SESSION_ID}.jsonl"
    new_transcript = new_dir / f"{_CLAUDE_SESSION_ID}.jsonl"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    result_message = (
        f"Entered worktree at {worktree} on branch {worktree.name}. The session is "
        "now working in the worktree. Use ExitWorktree to leave mid-session, or "
        "exit the session to be prompted."
    )

    initial_history = (
        _record(
            "user",
            uuid="preexisting-user-prompt-uuid",
            parent_uuid=None,
            cwd=cwd,
            git_branch="main",
            message={"role": "user", "content": _REATTACH_PROMPT},
        )
        + "\n"
        if start_at_end
        else ""
    )
    old_transcript.write_text(initial_history, encoding="utf-8")
    source = "resume" if start_at_end else "startup"
    await asyncio.to_thread(
        _run_hooks,
        settings,
        "SessionStart",
        _hook_payload("SessionStart", cwd=cwd, transcript_path=old_transcript, source=source),
        cwd=cwd,
        env=hook_env,
        subject=source,
    )

    forwarder = asyncio.create_task(
        fwd.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=start_at_end,
            poll_interval_s=0.05,
        )
    )
    hook_proc: subprocess.Popen[bytes] | None = None
    try:
        # Seed the initial cursor before the new turn; reattach skips only
        # the history that was already present when the forwarder started.
        assert await _await_condition(
            lambda: fwd._read_forward_state(bridge_dir) is not None,
            "initial transcript cursor seeded",
            _CONVERGE_S,
        ), "precondition: the forwarder never seeded its initial cursor"
        with old_transcript.open("a", encoding="utf-8") as handle:
            handle.write(_pre_approval_transcript(cwd, worktree))
        await asyncio.to_thread(
            _run_hooks,
            settings,
            "UserPromptSubmit",
            _hook_payload(
                "UserPromptSubmit",
                cwd=cwd,
                transcript_path=old_transcript,
                prompt=_PROMPT,
            ),
            cwd=cwd,
            env=hook_env,
        )
        assert await _await_condition(
            lambda: _items_containing(base_url, session_id, _TOOL_USE_ID) >= 1,
            "EnterWorktree function_call mirrored",
            _CONVERGE_S,
        ), "precondition: the forwarder never mirrored the pre-approval transcript"

        # Claude asks permission: the PermissionRequest hook parks a web card
        # and long-polls the server for the verdict.
        hook_proc = _spawn_permission_request_hook(
            settings,
            _hook_payload(
                "PermissionRequest",
                cwd=cwd,
                transcript_path=old_transcript,
                tool_name="EnterWorktree",
                tool_input={"path": str(worktree)},
            ),
            cwd=cwd,
            env=hook_env,
        )
        assert await _await_condition(
            lambda: len(_pending_elicitations(base_url, session_id)) == 1,
            "web approval card parked",
            _CONVERGE_S,
        ), "precondition: the PermissionRequest hook never parked a web elicitation"

        # The user approves in the terminal. Claude runs EnterWorktree: the
        # transcript MOVES to the worktree's project dir, the tool result is
        # appended there, and PostToolUse fires reporting the new path. The
        # parked hook is told nothing (verified against Claude Code 2.1.269).
        os.replace(old_transcript, new_transcript)
        with new_transcript.open("a", encoding="utf-8") as handle:
            handle.write(_post_approval_records(cwd, worktree, result_message))
        await asyncio.to_thread(
            _run_hooks,
            settings,
            "PostToolUse",
            _hook_payload(
                "PostToolUse",
                cwd=worktree,
                transcript_path=new_transcript,
                tool_name="EnterWorktree",
                tool_input={"path": str(worktree)},
                tool_response={
                    "worktreePath": str(worktree),
                    "worktreeBranch": worktree.name,
                    "message": result_message,
                },
                tool_use_id=_TOOL_USE_ID,
            ),
            cwd=worktree,
            env=hook_env,
            subject="EnterWorktree",
        )

        hook_returned = await _await_condition(
            lambda: hook_proc.poll() is not None,
            "permission-request hook long-poll returned",
            _CONVERGE_S,
        )
        observed = {
            "hook_returned": hook_returned,
            "hook_exit_code": hook_proc.poll(),
            "hook_stderr": (
                hook_proc.stderr.read().decode(errors="replace")
                if hook_returned and hook_proc.stderr is not None
                else ""
            ),
            "pending": _pending_elicitations(base_url, session_id),
            "result_items": _items_containing(base_url, session_id, result_message),
            "reattach_history_items": _items_containing(base_url, session_id, _REATTACH_PROMPT),
            "bridge_transcript_path": (
                json.loads((bridge_dir / "state.json").read_text()).get("transcript_path")
            ),
        }
        if not hook_returned:
            return observed

        # ExitWorktree keeps the checkout, moves the same transcript back,
        # and reports the restored path through its own PostToolUse hook.
        exit_input = {"action": "keep"}
        exit_response = {
            "action": "keep",
            "originalCwd": str(cwd),
            "worktreePath": str(worktree),
            "worktreeBranch": worktree.name,
            "message": (
                f"Exited worktree. Your work is preserved at {worktree} on branch "
                f"{worktree.name}. Session is now back in {cwd}."
            ),
        }
        exit_call = _record(
            "assistant",
            uuid="exit-assistant-tool-use-uuid",
            parent_uuid="user-tool-result-uuid",
            cwd=worktree,
            git_branch=worktree.name,
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": _EXIT_TOOL_USE_ID,
                        "name": "ExitWorktree",
                        "input": exit_input,
                    }
                ],
            },
        )
        with new_transcript.open("a", encoding="utf-8") as handle:
            handle.write(exit_call + "\n")
        assert await _await_condition(
            lambda: _items_containing(base_url, session_id, _EXIT_TOOL_USE_ID) >= 1,
            "ExitWorktree function_call mirrored",
            _CONVERGE_S,
        ), "precondition: the forwarder never mirrored the ExitWorktree call"
        os.replace(new_transcript, old_transcript)
        exit_result = _record(
            "user",
            uuid="exit-user-tool-result-uuid",
            parent_uuid="exit-assistant-tool-use-uuid",
            cwd=cwd,
            git_branch="main",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": exit_response["message"],
                        "tool_use_id": _EXIT_TOOL_USE_ID,
                    }
                ],
            },
            toolUseResult=exit_response,
            sourceToolAssistantUUID="exit-assistant-tool-use-uuid",
        )
        with old_transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "worktree-state",
                        "worktreeSession": None,
                        "sessionId": _CLAUDE_SESSION_ID,
                    }
                )
                + "\n"
                + exit_result
                + "\n"
            )
        await asyncio.to_thread(
            _run_hooks,
            settings,
            "PostToolUse",
            _hook_payload(
                "PostToolUse",
                cwd=cwd,
                transcript_path=old_transcript,
                tool_name="ExitWorktree",
                tool_input=exit_input,
                tool_response=exit_response,
                tool_use_id=_EXIT_TOOL_USE_ID,
            ),
            cwd=cwd,
            env=hook_env,
            subject="ExitWorktree",
        )
        assert await _await_condition(
            lambda: _items_containing(base_url, session_id, exit_response["message"]) == 1,
            "ExitWorktree result mirrored",
            _CONVERGE_S,
        ), "ExitWorktree's result was not mirrored after the transcript moved back"
        observed["result_items"] = _items_containing(base_url, session_id, result_message)
        return observed
    finally:
        _terminate(hook_proc)
        forwarder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await forwarder


@pytest.mark.timeout(300)
@pytest.mark.parametrize("start_at_end", [False, True], ids=["fresh", "reattach"])
def test_terminal_approved_enter_worktree_clears_the_web_approval_card(
    tmp_path: Path,
    start_at_end: bool,
) -> None:
    """Approving ``EnterWorktree`` in the terminal must clear the web card.

    Journey (the reporter's): on a claude-native session Claude asks to enter
    an existing worktree; the user approves from the terminal page and the
    command continues there, yet the chat page stays stuck on the approval
    card. Only the mirrored tool result can clear that card, and
    ``EnterWorktree`` moves the transcript the forwarder mirrors from.

    Expected: with no ``Stop`` hook, the parked ``permission-request`` hook
    returns, the session has no pending elicitation, and the tool result is
    committed to ``/items``. Buggy behavior: the forwarder keeps tailing the
    vanished pre-relocation path, so none of that happens before the
    deadline -- this test FAILS naming the stale path the bridge still holds.

    :param tmp_path: Per-test temp dir (server DB, artifacts, fake Claude home).
    :param start_at_end: Reattach skips pre-existing history before the new turn.
    """
    from omnigent.harnesses.claude_native.bridge import build_hook_settings, prepare_bridge_dir

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    database_uri = f"sqlite:///{tmp_path / 'chat.db'}"
    claude_home = tmp_path / "home" / ".claude"
    # The reporter's layout: an original checkout and a sibling worktree,
    # both under the same worktrees root.
    cwd = tmp_path / "home" / "universe-worktrees" / "worktree-bec13db6"
    worktree = tmp_path / "home" / "universe-worktrees" / "lw6125-mfn"
    cwd.mkdir(parents=True)
    worktree.mkdir(parents=True)
    bridge_dir: Path | None = None

    server_log = (tmp_path / "server.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        session_id = _create_claude_native_session(base_url)
        bridge_dir = prepare_bridge_dir(session_id, workspace=cwd)
        # The very settings fragment the runner hands Claude via --settings.
        settings = build_hook_settings(
            bridge_dir,
            python_executable=sys.executable,
            ap_server_url=base_url,
            ap_auth_headers={},
        )
        # Hook subprocesses run with ``python -I`` (no PYTHONPATH), so they
        # import the installed omnigent, as in production.
        hook_env = _localhost_env({"HOME": str(tmp_path / "home")})
        hook_env.pop("PYTHONPATH", None)

        observed = asyncio.run(
            _replay_terminal_approved_enter_worktree(
                base_url=base_url,
                session_id=session_id,
                bridge_dir=bridge_dir,
                settings=settings,
                claude_home=claude_home,
                cwd=cwd,
                worktree=worktree,
                hook_env=hook_env,
                start_at_end=start_at_end,
            )
        )
        server_tail = (tmp_path / "server.log").read_text()[-2000:]
        diagnostics = (
            f"bridge transcript_path={observed['bridge_transcript_path']!r} "
            f"pending={observed['pending']!r} "
            f"result_items={observed['result_items']} "
            f"hook_exit={observed['hook_exit_code']!r} "
            f"hook_stderr={observed['hook_stderr']!r}\nserver log tail:\n{server_tail}"
        )

        # The bug: EnterWorktree moved the transcript, the bridge never heard,
        # the forwarder tailed the vanished path -- so the terminal's answer
        # never reached the web card. The hook is still parked and nothing
        # after the relocation is in the conversation store.
        assert observed["hook_returned"], (
            "The web approval card stayed stuck after the user approved "
            "EnterWorktree in the terminal: the permission-request hook was "
            f"still long-polling {_CONVERGE_S:.0f}s after Claude ran the tool "
            "(no mirrored tool result reached the server). " + diagnostics
        )
        assert observed["hook_exit_code"] == 0, diagnostics
        assert observed["pending"] == [], (
            "Session still shows a pending elicitation after the terminal "
            "answered it. " + diagnostics
        )
        assert observed["result_items"] == 1, (
            "EnterWorktree's result must be mirrored exactly once across both "
            "transcript relocations. " + diagnostics
        )
        assert observed["reattach_history_items"] == 0, diagnostics
    finally:
        _terminate(server_proc)
        server_log.close()
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)
