"""Scoring helpers shared by the routing CUJs.

Every assertion in this suite is a routing artifact — a decision item, a
session field, a bridge-dir file, a pane capture — never the text a model
answered with. Answers here run against a real gateway, so a slow or failed
generation must not be able to redden a routing test.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx

_T = TypeVar("_T")

#: Poll cadence for every wait in this suite, in seconds.
POLL_INTERVAL_S = 0.5


def wait_for(
    predicate: Callable[[], _T | None],
    *,
    timeout: float,
    what: str,
    interval: float = POLL_INTERVAL_S,
) -> _T:
    """Poll *predicate* until it returns something truthy.

    :param predicate: Called repeatedly; a non-``None`` return ends the wait.
    :param timeout: Max seconds to wait.
    :param what: What is being waited for, used in the failure message.
    :param interval: Seconds between polls.
    :returns: The predicate's first non-``None`` value.
    :raises AssertionError: When *timeout* elapses first.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def settle(seconds: float) -> None:
    """Sleep *seconds*, for the negative checks that need a quiet window.

    "Nothing else happened" can only be shown by waiting, so the few
    assertions that need it say so explicitly instead of hiding a sleep.

    :param seconds: How long to stay quiet.
    """
    time.sleep(seconds)


# ── Decisions ───────────────────────────────────────────────────────────────


def decisions(
    client: httpx.Client,
    session_id: str,
    *,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Return this session's ``routing_decision`` items, oldest first.

    The session snapshot is the public read path for the decision rows the
    chips and cards render from (the same rows the UI
    same rows straight out of ``chat.db``).

    :param client: HTTP client pointed at the routing server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param scope: When given, keep only decisions of that scope, e.g.
        ``"native_subagent"``.
    :returns: The decision payloads, each flattened to its ``data`` fields.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    rows: list[dict[str, Any]] = []
    for item in resp.json().get("items", []):
        if item.get("type") != "routing_decision":
            continue
        data = item.get("data")
        payload = dict(data) if isinstance(data, dict) else {}
        payload["id"] = item.get("id")
        rows.append(payload)
    if scope is not None:
        rows = [row for row in rows if row.get("scope") == scope]
    return rows


def wait_for_decision(
    client: httpx.Client,
    session_id: str,
    *,
    scope: str,
    timeout: float,
) -> dict[str, Any]:
    """Wait for the first decision of *scope* on *session_id*.

    :param client: HTTP client pointed at the routing server.
    :param session_id: Session id.
    :param scope: Decision scope to wait for, e.g. ``"session"``.
    :param timeout: Max seconds to wait.
    :returns: The first matching decision payload.
    :raises AssertionError: When none appears in time.
    """
    return wait_for(
        lambda: next(iter(decisions(client, session_id, scope=scope)), None),
        timeout=timeout,
        what=f"a {scope}-scope routing decision on {session_id}",
    )


def user_messages(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return this session's user message items, oldest first.

    Used by the first-message CUJs: a block-and-replay that leaks the blocked
    prompt shows up as two user turns for one thing typed.

    :param client: HTTP client pointed at the routing server.
    :param session_id: Session id.
    :returns: The user message items.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    out = []
    for item in resp.json().get("items", []):
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if item.get("type") == "message" and (data or item).get("role") == "user":
            out.append(item)
    return out


def session_snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """Return the session row as the API serializes it.

    :param client: HTTP client pointed at the routing server.
    :param session_id: Session id.
    :returns: The session snapshot object.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


# ── Model-id comparison ─────────────────────────────────────────────────────


def same_arm(left: str | None, right: str | None) -> bool:
    """Whether two model ids name the same arm.

    Folds catalog prefixes, dots-vs-dashes and case, so a codex ``config.toml``
    spelling ``gpt-5.6-luna``, a catalog ``databricks-gpt-5-6-luna`` and the
    router's ``gpt-5-6-luna`` all compare equal. Mandatory: the codex apply
    layer deliberately writes codex's own slug, so a byte comparison against
    the router's vocabulary is a false red.

    :param left: A model id, or ``None``.
    :param right: A model id, or ``None``.
    :returns: ``True`` when both name the same arm.
    """
    from omnigent.codex_model_vocabulary import comparable_model_id

    if not left or not right:
        return False
    return comparable_model_id(left) == comparable_model_id(right)


def arm_in(model: str | None, arms: Iterable[str]) -> bool:
    """Whether *model* names any arm in *arms*.

    :param model: A model id, or ``None``.
    :param arms: Arm ids to compare against.
    :returns: ``True`` on a match.
    """
    return any(same_arm(model, arm) for arm in arms)


# ── Bridge dirs and process truth ───────────────────────────────────────────


def claude_bridge_dir(session_id: str) -> Path:
    """Return the claude-native bridge dir for *session_id*.

    :param session_id: Session id, which is also the bridge id.
    :returns: The bridge directory path (may not exist yet).
    """
    from omnigent.claude_native_bridge import bridge_dir_for_bridge_id

    return bridge_dir_for_bridge_id(session_id)


def codex_bridge_dir(session_id: str) -> Path:
    """Return the codex-native bridge dir for *session_id*.

    :param session_id: Session id, which is also the bridge id.
    :returns: The bridge directory path (may not exist yet).
    """
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id

    return bridge_dir_for_bridge_id(session_id)


def wait_for_bridge_file(bridge_dir: Path, name: str, *, timeout: float) -> Path:
    """Wait for ``bridge_dir/name`` to appear.

    :param bridge_dir: The session's bridge directory.
    :param name: File name inside it, e.g. ``"tmux.json"``.
    :param timeout: Max seconds to wait.
    :returns: The existing path.
    :raises AssertionError: When it never appears.
    """
    target = bridge_dir / name
    return wait_for(
        lambda: target if target.exists() else None,
        timeout=timeout,
        what=f"{target}",
    )


def tmux_target(bridge_dir: Path) -> tuple[str, str]:
    """Read the pane the harness advertised in ``tmux.json``.

    :param bridge_dir: The session's bridge directory.
    :returns: ``(socket_path, tmux_target)``.
    :raises AssertionError: When the advertisement is missing or incomplete.
    """
    raw = json.loads((bridge_dir / "tmux.json").read_text())
    socket_path = raw.get("socket_path") or raw.get("tmux_socket")
    target = raw.get("tmux_target")
    assert socket_path and target, f"incomplete tmux.json in {bridge_dir}: {raw}"
    return str(socket_path), str(target)


def wait_for_session_pane(
    client: httpx.Client,
    session_id: str,
    *,
    timeout: float,
) -> tuple[str, str]:
    """Wait for the session's running terminal and return its tmux pane.

    The terminals resource is the harness-agnostic way to find a pane: only
    claude-native publishes ``tmux.json`` into its bridge dir, while the
    registry knows the socket and target for every native harness.

    :param client: HTTP client pointed at the routing server.
    :param session_id: Session id.
    :param timeout: Max seconds to wait for a running terminal.
    :returns: ``(socket_path, tmux_target)``.
    :raises AssertionError: When no running terminal appears in time.
    """

    def _look() -> tuple[str, str] | None:
        resp = client.get(f"/v1/sessions/{session_id}/resources/terminals")
        if resp.status_code != 200:
            return None
        for row in resp.json().get("data", []):
            meta = row.get("metadata") or {}
            socket_path, target = meta.get("tmux_socket"), meta.get("tmux_target")
            if meta.get("running") and socket_path and target:
                return str(socket_path), str(target)
        return None

    return wait_for(_look, timeout=timeout, what=f"a running terminal on {session_id}")


def capture_pane(socket_path: str, target: str, *, lines: int = 2000) -> str:
    """Capture a tmux pane's visible text plus recent scrollback.

    :param socket_path: The tmux server socket the pane lives on.
    :param target: The pane target, e.g. ``"omnigent-abc:0.0"``.
    :param lines: How many scrollback lines to include.
    :returns: The pane text, or ``""`` when the pane is gone.
    """
    try:
        out = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-S", f"-{lines}", "-t", target],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout


def wait_for_pane_text(
    socket_path: str,
    target: str,
    needle: str,
    *,
    timeout: float,
) -> str:
    """Wait until *needle* shows up in the pane.

    :param socket_path: The tmux server socket.
    :param target: The pane target.
    :param needle: Literal text to look for.
    :param timeout: Max seconds to wait.
    :returns: The pane capture containing *needle*.
    :raises AssertionError: When it never appears.
    """

    return _wait_for_pane(
        socket_path,
        target,
        lambda text: needle in text,
        timeout=timeout,
        what=repr(needle),
    )


def wait_for_pane_identifier(
    socket_path: str,
    target: str,
    identifier: str,
    *,
    timeout: float,
) -> str:
    """Wait until *identifier* shows up in the pane, ignoring line wrapping.

    A long MCP tool name (``mcp__omnigent__sys_session_create``) is routinely
    broken across pane lines, so the comparison drops all whitespace on both
    sides. Anything shorter would be a false red on a narrow pane.

    :param socket_path: The tmux server socket.
    :param target: The pane target.
    :param identifier: The token to look for, e.g. a tool name.
    :param timeout: Max seconds to wait.
    :returns: The pane capture containing it.
    :raises AssertionError: When it never appears.
    """
    needle = "".join(identifier.split())
    return _wait_for_pane(
        socket_path,
        target,
        lambda text: needle in "".join(text.split()),
        timeout=timeout,
        what=repr(identifier),
    )


def _wait_for_pane(
    socket_path: str,
    target: str,
    matches: Callable[[str], bool],
    *,
    timeout: float,
    what: str,
) -> str:
    """Poll a pane until *matches* accepts its contents.

    On timeout the failure carries the pane's tail: "the text never appeared" and
    "the pane died / is showing a dialog" are otherwise indistinguishable, and
    that is the difference between a real routing bug and a launch problem.

    :param socket_path: The tmux server socket.
    :param target: The pane target.
    :param matches: Predicate over the captured pane text.
    :param timeout: Max seconds to wait.
    :param what: What is being waited for, for the failure message.
    :returns: The accepted pane capture.
    :raises AssertionError: When *matches* never accepts.
    """
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = capture_pane(socket_path, target)
        if matches(text):
            return text
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"timed out after {timeout}s waiting for {what} in pane {target}.\n"
        f"pane tail:\n{text[-3000:]}"
    )


def codex_config_model(session_id: str) -> str | None:
    """Read the codex ``config.toml`` model for *session_id*.

    Never the live model on its own (a running thread can be switched without
    the file catching up), but it is the file the routing apply layer mirrors
    the accepted switch into, in codex's own slug spelling.

    :param session_id: Session id.
    :returns: The top-level ``model`` value, or ``None``.
    """
    from omnigent.codex_native_bridge import read_codex_config_model

    return read_codex_config_model(codex_bridge_dir(session_id))


def wait_for_codex_config_arm(session_id: str, expected: str, *, timeout: float) -> str:
    """Wait until the session's ``config.toml`` names the *expected* arm.

    A wait, not a read: the private ``CODEX_HOME`` is seeded from the user's own
    config first and only then pinned to the session's model, so reading the file
    the moment it appears sees the developer's default arm and not the routed
    one. On timeout the failure names the value actually found, which is what
    tells a real mis-apply apart from a slow one.

    :param session_id: Session id.
    :param expected: The routed model id the file must name, in any spelling.
    :param timeout: Max seconds to wait.
    :returns: The value found in ``config.toml``, in codex's own spelling.
    :raises AssertionError: When the file never names *expected*.
    """
    observed: str | None = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = codex_config_model(session_id)
        if observed and same_arm(observed, expected):
            return observed
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"config.toml still names {observed!r} after {timeout}s, not the routed "
        f"{expected!r} (compared on the folded arm id, so a slug spelling is not "
        "the cause)"
    )


def rollout_turn_contexts(session_id: str) -> list[dict[str, Any]]:
    """Return the ``turn_context`` records from the session's newest rollout.

    The rollout is what the codex process actually ran — the only ground truth
    for the live model, which ``config.toml`` can lag.

    :param session_id: Session id.
    :returns: The decoded ``turn_context`` payloads in file order; empty when
        no rollout exists yet.
    """
    from omnigent.codex_native_bridge import codex_home_for_bridge_dir

    sessions_dir = codex_home_for_bridge_dir(codex_bridge_dir(session_id)) / "sessions"
    rollouts = sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not rollouts:
        return []
    contexts: list[dict[str, Any]] = []
    for line in rollouts[-1].read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        if record.get("type") == "turn_context" or "turn_context" in record:
            inner = record.get("turn_context")
            contexts.append(inner if isinstance(inner, dict) else payload)
    return contexts


# ── Isolation guards ────────────────────────────────────────────────────────


def claude_settings_apart_from_the_model() -> str | None:
    """Return the developer's ``~/.claude/settings.json`` minus its ``model`` key.

    Routing writes nothing into the user's global Claude settings — hook
    registration lives in the per-session bridge dir. Claude Code's own
    response to ``/model <id>`` does rewrite the file's ``model`` key, which
    is the accepted trade-off for the switch, so that one key is excluded and
    everything else is compared before and after each claude CUJ.

    :returns: A canonical rendering of the remaining settings, or ``None``
        when the file does not exist.
    """
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Unreadable is still comparable — hash the bytes.
        return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "model"}
    return json.dumps(payload, sort_keys=True)


def grep_log(path: Path, needles: Sequence[str]) -> list[str]:
    """Return the lines of *path* containing any of *needles*.

    :param path: A log file; a missing file yields no lines.
    :param needles: Literal substrings to look for.
    :returns: The matching lines.
    """
    if not path.exists():
        return []
    return [
        line
        for line in path.read_text(errors="replace").splitlines()
        if any(needle in line for needle in needles)
    ]
