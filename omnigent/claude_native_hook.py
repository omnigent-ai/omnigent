"""Claude Code hook recorder for the native Omnigent wrapper."""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    read_active_session_id,
    read_bridge_id,
    read_claude_session_id,
    read_claude_status_model,
    read_permission_hook_config,
    read_seen_claude_session_ids,
    record_hook_event,
    transcript_has_forked_from_marker,
    transcript_has_recent_local_command,
    url_component,
    write_active_session_id,
)
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native_policy_hook import (
    _is_login_redirect_or_unauthorized,
    evaluation_response_to_hook_output,
    fail_closed_hook_output,
    hook_payload_to_evaluation_request,
    policy_hook_reauth,
    post_evaluate_with_retry,
)

# Client-side budget for the permission-request long-poll to AP. Held
# at one day so the hook subprocess waits ~indefinitely for a verdict
# from the web UI (or for Claude to close the connection when the user
# answers in the terminal). Kept in lockstep with the server-side
# ``_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S`` and Claude Code's own
# command-hook ``timeout`` so no single layer caps the wait early.
#
# NOTE: this bounds a SINGLE long-poll (a slow human), NOT the retry loop.
# Re-POST attempts after a *failure* are bounded separately by
# ``_PERMISSION_MAX_CONSECUTIVE_FAILURES`` — see :func:`_post_hook_with_reattach`.
# Before #1782 the retry deadline was also one day, so a persistently
# sick/unreachable server made the hook re-POST (each re-driving the turn and
# respawning harness/tool subprocesses) every ≤30s for 24h — the spin-loop half
# of the zombie pileup.
_PERMISSION_TIMEOUT_S = 86400.0
# First retry must land inside the server's re-park grace (proxies
# sever idle long-polls); later retries back off.
_PERMISSION_RETRY_INITIAL_BACKOFF_S = 1.0
_PERMISSION_RETRY_MAX_BACKOFF_S = 30.0
# Fail unreachable-server connects fast into the backoff loop instead
# of inheriting the day-long read budget.
_PERMISSION_CONNECT_TIMEOUT_S = 30.0


def _env_int(name: str, default: int) -> int:
    """Read an int env override, ignoring a malformed value.

    A non-integer value must not crash the hook subprocess at import time —
    that would defeat the terminal-fallback design elsewhere in this file. Bad
    input logs a diagnostic and falls back to *default*.

    :param name: Environment variable name.
    :param default: Value used when unset or unparseable.
    :returns: The parsed int, or *default*.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"omnigent hook: ignoring non-integer {name}={raw!r}; using {default}",
            file=sys.stderr,
        )
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env override, ignoring a malformed value.

    Float sibling of :func:`_env_int`; a bad value logs and falls back to
    *default* rather than crashing the hook at import time.

    :param name: Environment variable name.
    :param default: Value used when unset or unparseable.
    :returns: The parsed float, or *default*.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    # Reject non-finite too: ``float("inf"/"nan")`` parses without ValueError,
    # but an ``inf`` floor would classify every sever as a held poll (disabling
    # flap detection) and ``nan`` makes every ``held_s < floor`` comparison
    # False — both silent footguns, so treat them as malformed.
    if not math.isfinite(value):
        print(
            f"omnigent hook: ignoring non-finite {name}={raw!r}; using {default}",
            file=sys.stderr,
        )
        return default
    return value


# Cap on CONSECUTIVE HARD failures before the reattach loop gives up and lets
# the caller fail-ask. A "hard failure" is the server being sick or unreachable
# (5xx, or a connection that never established) — i.e. the #1782 spin. It is
# distinguished from a proxy severing a *held* poll (a connection that WAS
# established and then dropped mid-wait), which is the re-park mechanism working
# as intended and does NOT count — see :func:`_post_hook_with_reattach`. So a
# legitimately-parked approval behind a severing proxy is never capped, while a
# down server can no longer re-POST for a day. Overridable for operators who
# want more slack against a flaky upstream.
_PERMISSION_MAX_CONSECUTIVE_FAILURES = max(1, _env_int("OMNIGENT_HOOK_MAX_RETRIES", 8))
# httpx errors that mean the request never reached a live server (no response
# was ever begun). These are unambiguous hard failures — the server is down /
# unreachable, not holding a poll. Everything else under ``httpx.HTTPError``
# that is not a 4xx/5xx status (RemoteProtocolError, ReadError, ReadTimeout, …)
# means the connection was established and then severed mid-poll.
_NEVER_CONNECTED_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)
# An established connection that drops in under this many seconds is treated as
# a flapping/crash-looping server (a hard failure), NOT a genuinely-parked poll
# a proxy severed. Comfortably below any real idle-proxy timeout (typically
# 30-60s+) but above an instant accept-then-reset crash drop, so it tells a
# legitimate long-poll sever from a tight establish-drop spin. Operators behind
# an unusually aggressive proxy/LB whose idle timeout is under 10s can lower
# this via OMNIGENT_HOOK_HELD_POLL_FLOOR_S so their legitimate slow-human severs
# stay classified as held polls (not flaps) and are never capped; the default
# suits typical proxies. Floored at 0 (a negative would make every sever a
# held poll, disabling flap detection).
_PERMISSION_HELD_POLL_FLOOR_S = max(0.0, _env_float("OMNIGENT_HOOK_HELD_POLL_FLOOR_S", 10.0))
# Fail-fast budget for the synchronous ``/clear`` and ``/fork`` session
# rotations that run inside the SessionStart hook to gate Claude's
# welcome banner. Unlike the permission long-poll these are quick
# request/reply calls, so they must NOT inherit the day-long permission
# budget — an unresponsive Omnigent server would otherwise hang the banner.
# On timeout the rotation returns ``None`` and the background forwarder
# performs it from the recorded hook event instead.
_SESSION_ROTATION_TIMEOUT_S = 70.0
# Evaluate-policy hooks normally return immediately, but a TOOL_CALL
# ASK now parks server-side (URL-based elicitation) until a human
# resolves it via the approve URL — so the client must wait as long as
# the permission long-poll. Held at one day; the server caps the real
# wait via the deciding policy's ``ask_timeout``.
_EVALUATE_POLICY_TIMEOUT_S = 86400.0
_FORK_COMMAND_NAMES = frozenset({"/branch", "/fork"})
_FORK_TRANSCRIPT_WAIT_S = 1.0
_FORK_TRANSCRIPT_POLL_S = 0.05


def main(argv: list[str] | None = None) -> int:
    """
    Record one Claude Code hook payload from stdin.

    :param argv: Optional argv override excluding program name.
        ``None`` reads :data:`sys.argv`.
    :returns: Process exit code. Returns ``0`` for malformed input
        after writing a diagnostic to stderr so Claude Code itself
        is not blocked by an observer failure.
    """
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "permission-request":
        return _main_permission_request(raw_argv[1:])
    if raw_argv and raw_argv[0] == "ask-user-question":
        return _main_ask_user_question(raw_argv[1:])
    if raw_argv and raw_argv[0] == "evaluate-policy":
        return _main_evaluate_policy(raw_argv[1:])
    # Backwards compat: older bridge dirs may still reference the
    # pre-tool-use subcommand before the terminal is restarted.
    if raw_argv and raw_argv[0] == "pre-tool-use":
        return _main_evaluate_policy(raw_argv[1:])
    args = _parse_args(raw_argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent claude hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("omnigent claude hook: expected JSON object", file=sys.stderr)
        return 0
    bridge_dir = Path(args.bridge_dir)
    _annotate_resume_session_context(bridge_dir, payload)
    if payload.get("hook_event_name") == "SessionStart" and payload.get("source") == "clear":
        rotated_session_id = _rotate_session_on_clear(bridge_dir)
        if rotated_session_id:
            payload["omnigent_clear_rotated_to"] = rotated_session_id
    elif _is_claude_branch_session_start(payload):
        payload["omnigent_fork_detected"] = True
        rotated_session_id = _rotate_session_on_fork(bridge_dir)
        if rotated_session_id:
            payload["omnigent_fork_rotated_to"] = rotated_session_id
    try:
        record_hook_event(bridge_dir, payload)
    except Exception as exc:  # noqa: BLE001 - hook must not break Claude Code.
        print(f"omnigent claude hook: failed to record hook: {exc}", file=sys.stderr)
    conversation_url = _conversation_url_for_active_session(bridge_dir, args.conversation_url)
    if conversation_url and payload.get("hook_event_name") == "SessionStart":
        print(
            json.dumps({"systemMessage": (f"Open this session in Omnigent: {conversation_url}")})
        )
    return 0


def _annotate_resume_session_context(bridge_dir: Path, payload: dict[str, object]) -> None:
    """
    Attach pre-recording Claude resume context to a hook payload.

    The hook updates bridge state only after this payload is recorded.
    Capturing the prior session id and seen-status here lets the
    background forwarder distinguish a new Claude branch from a later
    ordinary resume into an already-seen branch.

    :param bridge_dir: Native Claude bridge directory.
    :param payload: Hook payload read from Claude Code stdin, e.g.
        ``{"hook_event_name": "SessionStart", "source": "resume"}``.
    :returns: None.
    """
    if payload.get("hook_event_name") != "SessionStart" or payload.get("source") != "resume":
        return
    new_claude_session_id = payload.get("session_id")
    if not isinstance(new_claude_session_id, str) or not new_claude_session_id:
        return
    seen = read_seen_claude_session_ids(bridge_dir)
    current_claude_session_id = read_claude_session_id(bridge_dir)
    if (
        isinstance(current_claude_session_id, str)
        and current_claude_session_id
        and current_claude_session_id != new_claude_session_id
    ):
        payload["omnigent_previous_claude_session_id"] = current_claude_session_id
    payload["omnigent_claude_session_was_seen"] = new_claude_session_id in seen


def _is_claude_branch_session_start(payload: dict[str, object]) -> bool:
    """
    Return whether a ``SessionStart`` payload represents Claude ``/fork``.

    Claude Code reports both ordinary resumes and branch/fork switches
    as ``SessionStart`` with ``source="resume"``. The branch path is
    identified by a transcript fork marker plus a new Claude session
    id that differs from the current bridge session and has not been
    seen by this wrapper.

    :param payload: Hook payload read from Claude Code stdin, e.g.
        ``{"hook_event_name": "SessionStart", "source": "resume"}``.
    :returns: ``True`` when the event should fork the active Omnigent session.
    """
    if payload.get("hook_event_name") != "SessionStart" or payload.get("source") != "resume":
        return False
    new_claude_session_id = payload.get("session_id")
    if not isinstance(new_claude_session_id, str) or not new_claude_session_id:
        return False
    current_claude_session_id = payload.get("omnigent_previous_claude_session_id")
    if not isinstance(current_claude_session_id, str) or not current_claude_session_id:
        return False
    if payload.get("omnigent_claude_session_was_seen") is True:
        return False
    return _payload_transcript_has_recent_branch_command(
        payload,
        time.time(),
        source_claude_session_id=current_claude_session_id,
        wait_timeout_s=_FORK_TRANSCRIPT_WAIT_S,
    )


def _payload_transcript_has_recent_branch_command(
    payload: dict[str, object],
    recorded_at: float,
    *,
    source_claude_session_id: str | None = None,
    wait_timeout_s: float = 0.0,
) -> bool:
    """
    Return whether Claude's transcript reports a fork signal.

    This avoids using the conversation display title to infer
    ``/fork``. The primary signal is Claude's structured
    ``forkedFrom`` transcript metadata; local command records are kept
    as an extra signal for Claude versions that emit them.

    :param payload: Hook payload read from Claude Code stdin, e.g.
        ``{"transcript_path": "/tmp/claude.jsonl", "session_id": "..."}``.
    :param recorded_at: Unix timestamp for the current hook event,
        e.g. ``1779922393.222``.
    :param source_claude_session_id: Expected source Claude session
        uuid, e.g. ``"9abc..."``. ``None`` accepts any different
        non-empty source id.
    :param wait_timeout_s: Maximum seconds to wait for Claude to
        flush the fork marker, e.g. ``1.0``.
    :returns: ``True`` when the transcript contains a recent
        ``/branch`` or ``/fork`` signal for the hook session.
    """
    transcript_path = payload.get("transcript_path")
    claude_session_id = payload.get("session_id")
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    if not isinstance(claude_session_id, str) or not claude_session_id:
        return False
    path = Path(transcript_path)
    deadline = time.monotonic() + max(wait_timeout_s, 0.0)
    while True:
        if transcript_has_forked_from_marker(
            path,
            claude_session_id=claude_session_id,
            source_claude_session_id=source_claude_session_id,
        ) or transcript_has_recent_local_command(
            path,
            claude_session_id=claude_session_id,
            recorded_at=recorded_at,
            command_names=_FORK_COMMAND_NAMES,
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_FORK_TRANSCRIPT_POLL_S)


def _rotate_session_on_clear(bridge_dir: Path) -> str | None:
    """
    Rotate Omnigent sessions synchronously for a Claude ``/clear`` SessionStart.

    The SessionStart hook output is what Claude renders as the welcome
    banner. Rotating here lets the banner point at the new Omnigent session
    before Claude prints it. Failures return ``None`` so the background
    forwarder can still perform the rotation from the recorded hook event.

    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_new"``, or ``None`` when
        rotation could not be completed from the hook.
    """
    old_session_id = read_active_session_id(bridge_dir)
    if not old_session_id:
        return None
    config = read_permission_hook_config(bridge_dir)
    ap_server_url = config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        return None
    raw_headers = config.get("ap_auth_headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    try:
        with httpx.Client(
            headers=headers, timeout=httpx.Timeout(_SESSION_ROTATION_TIMEOUT_S)
        ) as client:
            new_session_id = _create_clear_replacement_session(
                client,
                ap_server_url.rstrip("/"),
                old_session_id,
                bridge_dir,
            )
    except httpx.HTTPError as exc:
        print(f"omnigent claude clear hook: Omnigent rotation failed: {exc}", file=sys.stderr)
        return None
    except RuntimeError as exc:
        print(f"omnigent claude clear hook: rotation failed: {exc}", file=sys.stderr)
        return None
    return new_session_id


def _rotate_session_on_fork(bridge_dir: Path) -> str | None:
    """
    Fork Omnigent sessions synchronously for a Claude ``/fork``/``/branch``.

    Claude renders ``SessionStart`` hook output as the welcome banner
    for the new branch. Forking here lets that banner point at the
    forked Omnigent session before Claude prints it. Failures return
    ``None`` so the background forwarder can perform the fork from the
    annotated hook record.

    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_fork"``, or ``None``
        when rotation could not be completed from the hook.
    """
    old_session_id = read_active_session_id(bridge_dir)
    if not old_session_id:
        return None
    config = read_permission_hook_config(bridge_dir)
    ap_server_url = config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        return None
    raw_headers = config.get("ap_auth_headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    try:
        with httpx.Client(
            headers=headers, timeout=httpx.Timeout(_SESSION_ROTATION_TIMEOUT_S)
        ) as client:
            new_session_id = _create_fork_replacement_session(
                client,
                ap_server_url.rstrip("/"),
                old_session_id,
                bridge_dir,
            )
    except httpx.HTTPError as exc:
        print(f"omnigent claude fork hook: Omnigent fork failed: {exc}", file=sys.stderr)
        return None
    except RuntimeError as exc:
        print(f"omnigent claude fork hook: fork failed: {exc}", file=sys.stderr)
        return None
    return new_session_id


def _create_clear_replacement_session(
    client: httpx.Client,
    ap_server_url: str,
    old_session_id: str,
    bridge_dir: Path,
) -> str:
    """
    Create and activate the fresh Omnigent session for ``/clear``.

    :param client: Sync Omnigent HTTP client.
    :param ap_server_url: Omnigent server base URL without a trailing slash,
        e.g. ``"http://127.0.0.1:8787"``.
    :param old_session_id: Session being rotated away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_new"``.
    :raises httpx.HTTPError: If Omnigent rejects session creation,
        new-session binding, or terminal transfer.
    :raises RuntimeError: If Omnigent returns malformed session data.
    """
    old_resp = client.get(f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}")
    old_resp.raise_for_status()
    old = old_resp.json()
    if not isinstance(old, dict):
        raise RuntimeError(f"session {old_session_id!r} snapshot was not an object")
    agent_id = old.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise RuntimeError(f"session {old_session_id!r} has no agent_id")
    runner_id = old.get("runner_id")
    labels = old.get("labels") if isinstance(old.get("labels"), dict) else {}
    labels = {str(key): str(value) for key, value in labels.items()}
    labels.setdefault(BRIDGE_ID_LABEL_KEY, read_bridge_id(bridge_dir) or old_session_id)

    create_resp = client.post(
        f"{ap_server_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "labels": labels,
        },
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    if not isinstance(created, dict):
        raise RuntimeError("clear replacement session response was not an object")
    new_session_id = created.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("clear replacement session response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = client.patch(
            f"{ap_server_url}/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    terminal_id = terminal_resource_id("claude", "main")
    transfer_resp = client.post(
        (
            f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_active_session_id(bridge_dir, new_session_id)
    clear_resp = client.patch(
        f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}",
        json={
            "runner_id": "",
            # Re-key the superseded session onto a DISTINCT "-cleared" bridge id
            # so its later resume gets its own isolated dir instead of the new
            # session's live one (which would double-mirror the transcript and
            # trip the executor guard). Mirrors the async forwarder rotation;
            # ``_auto_create_claude_terminal`` recognises this marker.
            "labels": {BRIDGE_ID_LABEL_KEY: f"{old_session_id}-cleared"},
        },
    )
    if clear_resp.status_code >= 400:
        print(
            (
                "omnigent claude clear hook: failed to clear old runner binding: "
                f"{clear_resp.status_code} {clear_resp.text}"
            ),
            file=sys.stderr,
        )
    return new_session_id


def _create_fork_replacement_session(
    client: httpx.Client,
    ap_server_url: str,
    old_session_id: str,
    bridge_dir: Path,
) -> str:
    """
    Create and activate the forked Omnigent session for Claude ``/fork``.

    :param client: Sync Omnigent HTTP client.
    :param ap_server_url: Omnigent server base URL without a trailing slash,
        e.g. ``"http://127.0.0.1:8787"``.
    :param old_session_id: Session being forked away from, e.g.
        ``"conv_old"``.
    :param bridge_dir: Native Claude bridge directory.
    :returns: New Omnigent session id, e.g. ``"conv_fork"``.
    :raises httpx.HTTPError: If Omnigent rejects session fetch, fork,
        new-session binding, or terminal transfer.
    :raises RuntimeError: If Omnigent returns malformed session data.
    """
    old_resp = client.get(f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}")
    old_resp.raise_for_status()
    old = old_resp.json()
    if not isinstance(old, dict):
        raise RuntimeError(f"session {old_session_id!r} snapshot was not an object")
    runner_id = old.get("runner_id")

    fork_resp = client.post(
        f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}/fork",
        json={},
    )
    fork_resp.raise_for_status()
    forked = fork_resp.json()
    if not isinstance(forked, dict):
        raise RuntimeError("fork replacement session response was not an object")
    new_session_id = forked.get("id")
    if not isinstance(new_session_id, str) or not new_session_id:
        raise RuntimeError("fork replacement session response did not include id")

    if isinstance(runner_id, str) and runner_id:
        bind_resp = client.patch(
            f"{ap_server_url}/v1/sessions/{url_component(new_session_id)}",
            json={"runner_id": runner_id},
        )
        bind_resp.raise_for_status()

    terminal_id = terminal_resource_id("claude", "main")
    transfer_resp = client.post(
        (
            f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}"
            f"/resources/terminals/{url_component(terminal_id)}/transfer"
        ),
        json={"target_session_id": new_session_id},
    )
    transfer_resp.raise_for_status()

    write_active_session_id(bridge_dir, new_session_id)
    clear_resp = client.patch(
        f"{ap_server_url}/v1/sessions/{url_component(old_session_id)}",
        json={"runner_id": ""},
    )
    if clear_resp.status_code >= 400:
        print(
            (
                "omnigent claude fork hook: failed to clear old runner binding: "
                f"{clear_resp.status_code} {clear_resp.text}"
            ),
            file=sys.stderr,
        )
    return new_session_id


def _conversation_url_for_active_session(
    bridge_dir: Path,
    fallback_url: str | None,
) -> str | None:
    """
    Build the web URL for the bridge's current active Omnigent session.

    :param bridge_dir: Native Claude bridge directory.
    :param fallback_url: Legacy URL supplied by old hook settings, e.g.
        ``"http://127.0.0.1:8787/c/conv_old"``.
    :returns: Web URL for the active session, e.g.
        ``"http://127.0.0.1:8787/c/conv_new"``, or ``None`` when no
        URL can be constructed.
    """
    config = read_permission_hook_config(bridge_dir)
    ap_server_url = config.get("ap_server_url")
    session_id = read_active_session_id(bridge_dir)
    if isinstance(ap_server_url, str) and ap_server_url and session_id:
        # ``ap_server_url`` is the API base; route through the shared
        # builder so workspace-hosted servers land on the ``/omnigent``
        # SPA mount (with ``?o=<org>``) rather than the JSON API mount.
        from omnigent.conversation_browser import conversation_url

        return conversation_url(ap_server_url, session_id)
    return fallback_url


def _post_hook_with_reattach(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    hook_label: str,
    reauth: Callable[[], dict[str, str] | None] | None = None,
) -> httpx.Response | None:
    """
    POST one permission-style hook payload, surviving severed long-polls.

    Proxies cut the day-long poll and the server can restart mid-wait;
    a single failed POST used to fail-ask into a terminal prompt no one
    watches on a headless sub-agent. Re-POSTs with one stable
    ``_omnigent_elicitation_id`` so the server re-parks the SAME
    elicitation and can hand back a gap verdict via its pre-resolved
    tombstone. Retries transport errors and 5xx; a 4xx is final.

    Retries are bounded by ``_PERMISSION_MAX_CONSECUTIVE_FAILURES`` consecutive
    *hard* failures, classified by kind — NOT by wall-clock. This is the #1782
    fix: before it the retry deadline was ``_PERMISSION_TIMEOUT_S`` (a day), so
    a persistently down server re-POSTed — re-driving the turn and respawning
    harness/tool subprocesses — every ≤30s for 24h.

    Failure classification:

    * **Hard failure → count toward the cap.** A 5xx, or a connection that
      never established (:data:`_NEVER_CONNECTED_ERRORS`), or an established
      connection that dropped in under :data:`_PERMISSION_HELD_POLL_FLOOR_S`
      (a flapping/crash-looping server). This is the spin.
    * **Held-poll sever → reset the counter.** An established connection that
      dropped mid-poll after being held ≥ the floor. That is a proxy severing
      an idle long-poll — the re-park mechanism working as intended — so it
      must NOT count, or a legitimately-parked human approval behind a
      severing proxy would be capped after a handful of severs. Classifying by
      *how the request failed* (established-then-severed vs never-connected),
      not by elapsed wall-clock, is what makes "a slow human is never capped"
      hold even when the proxy severs every 30-60s.

    A hard 4xx is final (bad request won't succeed on retry).

    Residual limitation (by design): a *sick* backend behind a proxy/LB that
    accepts the connection and then silently severs it after its upstream
    timeout (>= the floor) is indistinguishable at the transport layer from a
    proxy severing a genuinely-parked human poll — both surface as an
    established-then-severed error after N seconds, and the server holds the
    POST silently with no client-visible "parked" ack. So this case resets the
    counter and is NOT caught by the consecutive-hard-failure cap; it is bounded
    only by the absolute :data:`_PERMISSION_TIMEOUT_S` ceiling below (the same
    day-long human-answer window). That is the tightest safe client-side bound:
    capping it sooner would necessarily cap a real slow human on the same
    topology. The blast radius is limited — this loop only re-POSTs over HTTP
    from one hook process (it does not itself respawn harness/tool
    subprocesses), and the host-side orphan reaper (#1782 Bug A) reclaims any
    subprocesses a re-driven turn does spawn — so the worst case is one hook
    slow-retrying for up to a day, not the original zombie pileup.

    The absolute :data:`_PERMISSION_TIMEOUT_S` ceiling bounds the total wait on
    every path, matching the day-long human-answer window.

    :param url: Absolute hook endpoint URL, e.g.
        ``"http://127.0.0.1:8787/v1/sessions/conv_x/hooks/permission-request"``.
    :param headers: Outbound auth headers for the Omnigent server.
    :param payload: Hook payload to POST. Not mutated; the re-attach id
        rides on a copy.
    :param hook_label: Diagnostic prefix for stderr lines, e.g.
        ``"permission"`` or ``"ask-user-question"``.
    :param reauth: Optional callable that re-mints fresh auth headers when the
        server bounces the POST to its OAuth login flow (Apps 302→``/oidc/``)
        or returns ``401`` — i.e. the one-shot ``ap_auth_headers`` token lapsed.
        Called at most once; new headers trigger an immediate retry with them.
        ``None`` keeps the legacy behavior.
    :returns: The successful (2xx) response, or ``None`` when rejected
        or out of budget — callers fail-ask as before.
    """
    body = {
        **payload,
        "_omnigent_elicitation_id": f"elicit_claude_{secrets.token_hex(16)}",
    }
    backoff_s = _PERMISSION_RETRY_INITIAL_BACKOFF_S
    timeout = httpx.Timeout(_PERMISSION_TIMEOUT_S, connect=_PERMISSION_CONNECT_TIMEOUT_S)
    # Absolute backstop: even a run of held-poll severs (which don't count
    # toward the hard-failure cap) can't loop past the day-long human-answer
    # window — matches the read budget and the server's ask_timeout.
    deadline = time.monotonic() + _PERMISSION_TIMEOUT_S
    reauthed = False
    consecutive_hard_failures = 0
    while True:
        attempt_started = time.monotonic()
        try:
            with httpx.Client(headers=headers, timeout=timeout) as client:
                resp = client.post(url, json=body)
                if (
                    reauth is not None
                    and not reauthed
                    and _is_login_redirect_or_unauthorized(resp)
                ):
                    # One-shot ``ap_auth_headers`` token lapsed (~1h OAuth
                    # lifetime): re-mint and retry once rather than fail-asking
                    # into a terminal prompt no one watches. Mirrors the
                    # evaluate-policy hook and ``_RunnerDatabricksAuth``.
                    refreshed = reauth()
                    if refreshed:
                        headers = refreshed
                        reauthed = True
                        print(
                            f"omnigent {hook_label} hook: Omnigent auth expired "
                            "(login redirect/401); re-minted token and retrying",
                            file=sys.stderr,
                        )
                        continue
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                print(
                    f"omnigent {hook_label} hook: Omnigent request rejected: {exc}",
                    file=sys.stderr,
                )
                return None
            # 5xx: server responded but is sick — a hard failure (the spin).
            is_hard_failure = True
            print(
                f"omnigent {hook_label} hook: Omnigent request failed; retrying: {exc}",
                file=sys.stderr,
            )
        except httpx.HTTPError as exc:
            # Classify by HOW it failed, not by elapsed time (a proxy severs a
            # legitimately-held poll in seconds-to-minutes, so wall-clock can't
            # tell it from a down server — #1782 Polly review).
            never_connected = isinstance(exc, _NEVER_CONNECTED_ERRORS)
            held_s = time.monotonic() - attempt_started
            # Hard failure iff the server was never reached, OR an established
            # connection dropped so fast it's a flap rather than a parked poll.
            is_hard_failure = never_connected or held_s < _PERMISSION_HELD_POLL_FLOOR_S
            kind = (
                "unreachable"
                if never_connected
                else ("flapping" if is_hard_failure else "held-poll severed")
            )
            print(
                f"omnigent {hook_label} hook: Omnigent request failed ({kind}); retrying: {exc}",
                file=sys.stderr,
            )
        if is_hard_failure:
            consecutive_hard_failures += 1
        else:
            # A proxy severed a genuinely-held poll — the re-park mechanism
            # working as intended. Reset so a slow human is never capped.
            consecutive_hard_failures = 0
        if consecutive_hard_failures >= _PERMISSION_MAX_CONSECUTIVE_FAILURES:
            print(
                f"omnigent {hook_label} hook: giving up after "
                f"{consecutive_hard_failures} consecutive hard failures "
                "(server unreachable/sick) — failing ask",
                file=sys.stderr,
            )
            return None
        # Two-line backoff; not worth a retry lib in this dependency-light hook.
        time.sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _PERMISSION_RETRY_MAX_BACKOFF_S)
        if time.monotonic() >= deadline:
            print(
                f"omnigent {hook_label} hook: retry budget exhausted "
                f"({_PERMISSION_TIMEOUT_S:.0f}s) — failing ask",
                file=sys.stderr,
            )
            return None


def _main_permission_request(argv: list[str]) -> int:
    """
    Forward one Claude ``PermissionRequest`` hook to the active Omnigent session.

    :param argv: CLI argv after the ``permission-request`` subcommand,
        e.g. ``["--bridge-dir", "/tmp/x", "--omnigent-server-url",
        "http://127.0.0.1:8787"]``.
    :returns: Process exit code. Returns ``0`` on transport failures so
        Claude Code falls back to its terminal prompt.
    """
    args = _parse_permission_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent claude permission hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("omnigent claude permission hook: expected JSON object", file=sys.stderr)
        return 0
    bridge_dir = Path(args.bridge_dir)
    session_id = read_active_session_id(bridge_dir)
    if not session_id:
        print("omnigent claude permission hook: active session missing", file=sys.stderr)
        return 0
    config = read_permission_hook_config(bridge_dir)
    ap_server_url = args.omnigent_server_url or config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        print("omnigent claude permission hook: Omnigent server URL missing", file=sys.stderr)
        return 0
    headers = _parse_headers(args.omnigent_auth_headers_json)
    if not headers:
        raw_headers = config.get("ap_auth_headers")
        if isinstance(raw_headers, dict):
            headers = {str(key): str(value) for key, value in raw_headers.items()}
    url = (
        f"{ap_server_url.rstrip('/')}/v1/sessions/"
        f"{url_component(session_id)}/hooks/permission-request"
    )
    resp = _post_hook_with_reattach(
        url,
        headers,
        payload,
        "claude permission",
        reauth=policy_hook_reauth(ap_server_url, headers),
    )
    if resp is None:
        return 0
    if resp.content:
        sys.stdout.write(resp.text)
    return 0


def _main_ask_user_question(argv: list[str]) -> int:
    """
    Handle a ``PreToolUse`` hook for Claude's built-in ``AskUserQuestion`` tool.

    In ``bypassPermissions`` mode the ``PermissionRequest`` hook never fires,
    so ``AskUserQuestion`` questions are silently swallowed — the web UI never
    sees them. This handler intercepts the tool via ``PreToolUse`` (which fires
    in all permission modes), POSTs the same payload to the Omnigent server's
    ``/hooks/permission-request`` endpoint, waits for the user's web-UI answer,
    and converts the ``PermissionRequest``-format response into a
    ``PreToolUse``-format output (``updatedInput``) so Claude receives the
    answers and skips its own TUI picker.

    When ``permission_mode`` is anything other than ``"bypassPermissions"``
    (including absent/unknown), this is a no-op: the ``PermissionRequest`` hook
    will handle the call, and returning empty output here prevents a duplicate
    elicitation from appearing.

    :param argv: CLI argv after the ``ask-user-question`` subcommand,
        e.g. ``["--bridge-dir", "/tmp/x"]``.
    :returns: Process exit code. Returns ``0`` on any failure so Claude Code
        falls back to its terminal TUI prompt rather than blocking.
    """
    args = _parse_permission_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent ask-user-question hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("omnigent ask-user-question hook: expected JSON object", file=sys.stderr)
        return 0
    # Only intercept in bypassPermissions mode. In any other mode the
    # PermissionRequest hook fires independently and owns the elicitation;
    # returning empty output here is "no opinion" so Claude's own flow
    # continues unimpeded and we avoid surfacing the form twice.
    if payload.get("permission_mode") != "bypassPermissions":
        return 0
    bridge_dir = Path(args.bridge_dir)
    session_id = read_active_session_id(bridge_dir)
    if not session_id:
        print("omnigent ask-user-question hook: active session missing", file=sys.stderr)
        return 0
    config = read_permission_hook_config(bridge_dir)
    ap_server_url = args.omnigent_server_url or config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        print("omnigent ask-user-question hook: Omnigent server URL missing", file=sys.stderr)
        return 0
    headers = _parse_headers(args.omnigent_auth_headers_json)
    if not headers:
        raw_headers = config.get("ap_auth_headers")
        if isinstance(raw_headers, dict):
            headers = {str(key): str(value) for key, value in raw_headers.items()}
    url = (
        f"{ap_server_url.rstrip('/')}/v1/sessions/"
        f"{url_component(session_id)}/hooks/permission-request"
    )
    resp = _post_hook_with_reattach(
        url,
        headers,
        payload,
        "ask-user-question",
        reauth=policy_hook_reauth(ap_server_url, headers),
    )
    if resp is None or not resp.content:
        return 0
    # The Omnigent server returns a PermissionRequest-shaped response:
    #   {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
    #                           "decision": {"behavior": "allow",
    #                                        "updatedInput": {...}}}}
    # Convert to PreToolUse format so Claude applies updatedInput:
    #   {"hookSpecificOutput": {"hookEventName": "PreToolUse",
    #                           "permissionDecision": "allow",
    #                           "updatedInput": {...}}}
    try:
        body = resp.json()
    except ValueError as exc:
        print(f"omnigent ask-user-question hook: invalid JSON from AP: {exc}", file=sys.stderr)
        return 0
    decision = (
        body.get("hookSpecificOutput", {}).get("decision", {}) if isinstance(body, dict) else {}
    )
    behavior = decision.get("behavior") if isinstance(decision, dict) else None
    if behavior not in ("allow", "deny"):
        # Unexpected shape — return empty output so Claude falls back to TUI.
        return 0
    pre_tool_use_output: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": behavior,
        },
    }
    updated_input = decision.get("updatedInput") if isinstance(decision, dict) else None
    if isinstance(updated_input, dict):
        pre_tool_use_output["hookSpecificOutput"]["updatedInput"] = updated_input  # type: ignore[index]
    sys.stdout.write(json.dumps(pre_tool_use_output))
    return 0


def _main_evaluate_policy(argv: list[str]) -> int:
    """
    Evaluate a Claude Code ``PreToolUse`` / ``PostToolUse`` /
    ``UserPromptSubmit`` hook against Omnigent policies.

    Reads the hook JSON payload from stdin, converts it into the
    proto-compatible ``EvaluationRequest`` schema (``PHASE_TOOL_CALL``
    for PreToolUse, ``PHASE_TOOL_RESULT`` for PostToolUse,
    ``PHASE_REQUEST`` for UserPromptSubmit), POSTs to
    ``/v1/sessions/{id}/policies/evaluate``, and converts the
    ``EvaluationResponse`` back into Claude Code's hook output format.

    For ``PreToolUse``, only the constraining ``POLICY_ACTION_DENY``
    verdict maps to a ``permissionDecision`` (``"deny"``); ASK is
    resolved server-side (the endpoint parks via ``_hold_native_ask_gate``
    and returns a hard ALLOW/DENY). ``POLICY_ACTION_ALLOW`` (the engine's
    default when no policy matches) emits no output — "no opinion" — so
    Claude's own permission prompt still fires and the
    ``PermissionRequest`` hook can route it to the web UI. See
    :func:`omnigent.native_policy_hook.evaluation_response_to_hook_output`.

    For ``UserPromptSubmit``, this is the request-phase gate for native
    sessions (the server-level ``_evaluate_input_policy`` skips native
    message events). A DENY emits top-level ``decision: "block"``, which
    drops the prompt before the model sees it; ASK is resolved
    server-side; ALLOW proceeds with no output.

    For ``PostToolUse``, policy denials are surfaced as
    ``additionalContext`` (Claude sees the warning but the tool result
    is already committed — PostToolUse hooks are observational).

    Failure handling is phase-aware (mirroring the runner-side default
    from PR #163). Once the session is known to be governed (an active
    session id and a configured ``ap_server_url``) and the round-trip to
    ``/policies/evaluate`` cannot yield a usable verdict — the server is
    unreachable, returns non-2xx, or returns an empty / malformed body —
    a ``PreToolUse`` (``PHASE_TOOL_CALL``) call fails CLOSED with a
    ``deny`` (this hook is the sole enforcement point for native tools, so
    a transient outage must not silently let a gated call through), while
    ``UserPromptSubmit`` and ``PostToolUse`` fail OPEN. Pre-evaluation
    conditions that mean the session simply is not governed — no active
    session, no ``ap_server_url``, an unparseable hook payload, or an
    ``mcp__omnigent__*`` tool already gated on the relay path — still
    return exit 0 with no output ("no opinion") so non-Omnigent tool
    calls are never blocked.

    :param argv: CLI argv after the ``evaluate-policy`` subcommand,
        e.g. ``["--bridge-dir", "/tmp/x"]``.
    :returns: Process exit code. Always ``0`` — blocking verdicts
        are expressed via the JSON output, not exit codes.
    """
    args = _parse_evaluate_policy_args(argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent evaluate-policy hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("omnigent evaluate-policy hook: expected JSON object", file=sys.stderr)
        return 0
    bridge_dir = Path(args.bridge_dir)
    session_id = read_active_session_id(bridge_dir)
    if not session_id:
        return 0
    config = read_permission_hook_config(bridge_dir)
    ap_server_url = config.get("ap_server_url")
    if not isinstance(ap_server_url, str) or not ap_server_url:
        return 0
    headers: dict[str, str] = {}
    raw_headers = config.get("ap_auth_headers")
    if isinstance(raw_headers, dict):
        headers = {str(key): str(value) for key, value in raw_headers.items()}

    hook_event = payload.get("hook_event_name", "")
    eval_request = hook_payload_to_evaluation_request(hook_event, payload)
    if eval_request is None:
        # Unrecognized hook event — no policy to evaluate.
        return 0

    # Stamp the live model from this session's statusLine capture (the
    # statusLine wrapper writes the active model id into ``context.json`` on
    # every render — including right after an in-pane ``/model`` switch). This
    # is the cost gate's source of truth at hook time, race-free, unlike the
    # forwarder's async ``model_override`` mirror which lags a poll behind.
    # Without it the cost-budget gate can see an unresolved model (None) and
    # fail closed — blocking a cheap-model (sonnet/haiku) session over budget,
    # even though only expensive tiers should be gated (the server prefers a
    # stamped model over its own resolution; see ``PolicyEngine._inject_model``).
    # hook_payload_to_evaluation_request always returns an event with a
    # "context" dict, so index it directly (fail loud if that contract changes).
    context = eval_request["event"]["context"]
    # Stamp the harness so the over-budget message names claude-native's
    # model-switch surface (the in-pane ``/model`` picker).
    context["harness"] = "claude-native"
    status_model = read_claude_status_model(bridge_dir)
    if status_model:
        context["model"] = status_model

    # The session is governed (active id + ap_server_url) and we have a
    # policy-relevant event: from here a failure to obtain a usable verdict
    # fails CLOSED for the tool-call gate (see ``fail_closed_hook_output``).
    reauth = policy_hook_reauth(ap_server_url, headers)

    def _fail_closed(detail: str | None = None) -> int:
        out = fail_closed_hook_output(hook_event, detail)
        if out is not None:
            sys.stdout.write(json.dumps(out))
        return 0

    url = f"{ap_server_url.rstrip('/')}/v1/sessions/{url_component(session_id)}/policies/evaluate"
    resp, api_error = post_evaluate_with_retry(
        url,
        headers,
        eval_request,
        _EVALUATE_POLICY_TIMEOUT_S,
        "evaluate-policy hook",
        reauth=reauth,
    )
    if resp is None:
        return _fail_closed(api_error or reauth.failure_reason)
    if not resp.content:
        print("omnigent evaluate-policy hook: empty Omnigent response", file=sys.stderr)
        return _fail_closed()

    try:
        eval_response = resp.json()
    except json.JSONDecodeError:
        print("omnigent evaluate-policy hook: malformed Omnigent response", file=sys.stderr)
        return _fail_closed()

    hook_output = evaluation_response_to_hook_output(hook_event, eval_response)
    if hook_output is not None:
        sys.stdout.write(json.dumps(hook_output))
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse hook recorder arguments.

    :param argv: CLI argv excluding program name, e.g.
        ``["--bridge-dir", "/tmp/x"]``.
    :returns: Parsed argparse namespace.
    """
    parser = argparse.ArgumentParser(prog="python -m omnigent.claude_native_hook")
    parser.add_argument("--bridge-dir", required=True)
    parser.add_argument("--conversation-url")
    return parser.parse_args(argv)


def _parse_evaluate_policy_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse evaluate-policy hook arguments.

    :param argv: CLI argv excluding program name and subcommand.
    :returns: Parsed argparse namespace.
    """
    parser = argparse.ArgumentParser(prog="python -m omnigent.claude_native_hook evaluate-policy")
    parser.add_argument("--bridge-dir", required=True)
    return parser.parse_args(argv)


def _parse_permission_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse permission hook forwarding arguments.

    :param argv: CLI argv excluding program name and subcommand.
    :returns: Parsed argparse namespace.
    """
    parser = argparse.ArgumentParser(
        prog="python -m omnigent.claude_native_hook permission-request"
    )
    parser.add_argument("--bridge-dir", required=True)
    parser.add_argument("--omnigent-server-url")
    parser.add_argument("--omnigent-auth-headers-json")
    return parser.parse_args(argv)


def _parse_headers(raw: str | None) -> dict[str, str]:
    """
    Parse serialized Omnigent auth headers for the permission hook.

    :param raw: JSON object string, e.g.
        ``"{\"Authorization\": \"Bearer token\"}"``. ``None`` means no
        headers.
    :returns: Header dict with string keys and values.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"omnigent claude permission hook: bad headers JSON: {exc}", file=sys.stderr)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


if __name__ == "__main__":
    raise SystemExit(main())
