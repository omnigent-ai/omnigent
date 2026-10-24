"""Protocol pieces of the Claude Code mod channel.

The mod in ``claude_mod/`` is a Claude Code function-hooks plugin. It runs
inside the Claude process, reports turn boundaries to the serve-mcp control
server and long-polls it for commands. This module holds what both ends agree
on: the closed operation allowlist, request validation, the command queue that
matches each result to its caller, and the records written to the bridge
directory. The HTTP and file plumbing lives in
:mod:`omnigent.harnesses.claude_native.bridge`.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, cast

from omnigent.models.claude_model_vocabulary import is_claude_model_alias
from omnigent.util.json_types import JsonObject
from omnigent.util.reasoning_effort import CLAUDE_EFFORTS

MOD_PROTOCOL_VERSION: Final = 1
MOD_PLUGIN_DIR_NAME: Final = "claude-mod"
MOD_PACKAGE_DIR: Final = Path(__file__).resolve().parent / "claude_mod"
MOD_STATE_FILE: Final = "mod.json"
MOD_EVENTS_FILE: Final = "mod_events.jsonl"
# The mod heartbeats every 30 s, so 90 s means three missed beats.
MOD_LIVE_MAX_AGE_S: Final = 90.0
# Long-poll hold for ``GET /mod/next``; the mod polls again as soon as one returns.
MOD_POLL_HOLD_S: Final = 20.0
MOD_MAX_BODY_BYTES: Final = 256 * 1024
MOD_SUBMIT_MAX_BYTES: Final = 128 * 1024
MOD_EVENT_KINDS: Final = ("turn.start", "turn.complete")
MOD_TURN_REASONS: Final = ("answer", "aborted", "refusal", "error")

# The mod was checked on this release and on 2.1.308; older CLIs keep the tmux path.
_CLAUDE_MOD_MIN_VERSION: Final = "2.1.301"

_COMPACT_INSTRUCTIONS_MAX_BYTES: Final = 16 * 1024
_TEXT_FIELD_MAX_CHARS: Final = 256
_VERSION_FIELD_MAX_CHARS: Final = 64
_ERROR_MAX_CHARS: Final = 4096
_COMMAND_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")

ModOperation = Literal["submit", "abort", "model", "effort", "compact"]
ModCommandStatus = Literal["ok", "rejected", "not_started", "unanswered"]
MOD_COMMAND_STATUSES: Final = ("ok", "rejected", "not_started", "unanswered")


@dataclass(frozen=True)
class ModCommandOutcome:
    """What a harness-side caller learns from one mod command.

    * ``ok``: the mod performed the operation.
    * ``rejected``: the mod answered that it did not perform it (e.g. busy).
    * ``not_started``: no live channel, or the command was still queued when
      the wait ended, so the mod never received it.
    * ``unanswered``: the mod took the command and no answer came back, so it
      may have run.
    """

    status: ModCommandStatus
    result: JsonObject = field(default_factory=dict)
    error: str | None = None


def may_fall_back_to_tmux(outcome: ModCommandOutcome) -> bool:
    """Return whether the tmux path may still perform the operation.

    A command the mod refused or never received cannot have run, so tmux can
    take over. An unanswered command may have landed, and typing it again
    through tmux could apply it twice, so that case never falls back.

    :param outcome: Result of ``send_mod_command``.
    :returns: ``True`` when the caller should fall back to tmux.
    """
    return outcome.status in ("rejected", "not_started")


_NormalizeArguments = Callable[[JsonObject], JsonObject]


def _normalize_submit(arguments: JsonObject) -> JsonObject:
    return {"text": _text_argument(arguments, "text", MOD_SUBMIT_MAX_BYTES, required=True)}


def _normalize_abort(arguments: JsonObject) -> JsonObject:
    del arguments
    return {}


def _normalize_model(arguments: JsonObject) -> JsonObject:
    model = arguments.get("model")
    # The mod switches through Claude's config row, which takes aliases only.
    if not isinstance(model, str) or not is_claude_model_alias(model):
        raise ValueError("model must be a Claude alias such as sonnet or opus[1m]")
    return {"model": model.strip().lower()}


def _normalize_effort(arguments: JsonObject) -> JsonObject:
    level = arguments.get("level")
    if level not in CLAUDE_EFFORTS:
        raise ValueError(f"level must be one of {', '.join(CLAUDE_EFFORTS)}")
    return {"level": level}


def _normalize_compact(arguments: JsonObject) -> JsonObject:
    if arguments.get("instructions") is None:
        return {}
    instructions = _text_argument(
        arguments, "instructions", _COMPACT_INSTRUCTIONS_MAX_BYTES, required=True
    )
    return {"instructions": instructions}


# Operation name -> (argument names it accepts, normalizer). The closed set of
# what the mod will run; anything else is refused before it is queued.
_OPERATIONS: Final[dict[str, tuple[tuple[str, ...], _NormalizeArguments]]] = {
    "submit": (("text",), _normalize_submit),
    "abort": ((), _normalize_abort),
    "model": (("model",), _normalize_model),
    "effort": (("level",), _normalize_effort),
    "compact": (("instructions",), _normalize_compact),
}


def normalize_mod_command(operation: object, arguments: object) -> tuple[ModOperation, JsonObject]:
    """Validate one command against the closed allowlist.

    :param operation: Requested operation, e.g. ``"model"``.
    :param arguments: Operation arguments, e.g. ``{"model": "haiku"}``.
    :returns: The operation with its normalized arguments.
    :raises ValueError: For an unknown operation or malformed arguments.
    """
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError(f"unknown mod operation: {operation!r}")
    if not isinstance(arguments, dict):
        raise ValueError("mod operation arguments must be a JSON object")
    accepted_names, normalize = _OPERATIONS[operation]
    unexpected = sorted(set(arguments) - set(accepted_names))
    if unexpected:
        raise ValueError(f"unexpected {operation} argument(s): {', '.join(unexpected)}")
    return cast(ModOperation, operation), normalize(arguments)


def _text_argument(arguments: JsonObject, key: str, max_bytes: int, *, required: bool) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or (required and not value):
        raise ValueError(f"{key} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes or "\x00" in value:
        raise ValueError(f"{key} is too long or contains a NUL byte")
    return value


def normalize_mod_hello(payload: object) -> JsonObject:
    """Validate a ``POST /mod/hello`` body.

    :param payload: Decoded request body from the mod.
    :returns: ``mod_version``, ``claude_version``, ``claude_session_id`` and
        ``main_turn_id`` (the main-loop turn the mod believes is running, or
        ``None``).
    :raises ValueError: If the body is not a version 1 hello.
    """
    body = _protocol_object(payload, "hello")
    if "main_turn_id" not in body:
        raise ValueError("hello must carry main_turn_id (a turn id or null)")
    main_turn_id = body["main_turn_id"]
    return {
        "mod_version": _bounded_text(body, "mod_version", _VERSION_FIELD_MAX_CHARS),
        "claude_version": _bounded_text(body, "claude_version", _VERSION_FIELD_MAX_CHARS),
        "claude_session_id": _bounded_text(body, "claude_session_id", _TEXT_FIELD_MAX_CHARS),
        "main_turn_id": None
        if main_turn_id is None
        else _bounded_text(body, "main_turn_id", _TEXT_FIELD_MAX_CHARS),
    }


def normalize_mod_event(payload: object) -> JsonObject:
    """Validate a ``POST /mod/event`` body.

    :param payload: Decoded request body from the mod.
    :returns: ``kind``, ``claude_session_id``, ``turn_id``, ``agent_id`` (``None``
        for the main loop) and, for ``turn.complete``, ``reason``.
    :raises ValueError: If the body is not a known version 1 event.
    """
    body = _protocol_object(payload, "event")
    kind = body.get("kind")
    if kind not in MOD_EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")
    agent_id = body.get("agent_id")
    normalized: JsonObject = {
        "kind": kind,
        "claude_session_id": _bounded_text(body, "claude_session_id", _TEXT_FIELD_MAX_CHARS),
        "turn_id": _bounded_text(body, "turn_id", _TEXT_FIELD_MAX_CHARS),
        "agent_id": None
        if agent_id is None
        else _bounded_text(body, "agent_id", _TEXT_FIELD_MAX_CHARS),
    }
    if kind == "turn.complete":
        reason = body.get("reason")
        if reason not in MOD_TURN_REASONS:
            raise ValueError(f"unknown turn reason: {reason!r}")
        normalized["reason"] = reason
    return normalized


def normalize_mod_result(payload: object) -> tuple[str, bool, JsonObject, str | None]:
    """Validate a ``POST /mod/result`` body.

    :param payload: Decoded request body, e.g.
        ``{"v": 1, "id": "<32 hex digits>", "ok": true, "result": {}}``.
    :returns: ``(command_id, ok, result, error)``.
    :raises ValueError: If the body is not a well-formed version 1 result.
    """
    body = _protocol_object(payload, "result")
    command_id = body.get("id")
    if not isinstance(command_id, str) or not _COMMAND_ID_PATTERN.fullmatch(command_id):
        raise ValueError("result id is not a command id")
    ok = body.get("ok")
    if not isinstance(ok, bool):
        raise ValueError("result ok must be a boolean")
    if ok:
        result = body.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("result payload must be a JSON object")
        return command_id, True, result, None
    error = body.get("error")
    if not isinstance(error, str):
        raise ValueError("a failed result needs an error string")
    return command_id, False, {}, error[:_ERROR_MAX_CHARS]


def _protocol_object(payload: object, what: str) -> JsonObject:
    if not isinstance(payload, dict) or payload.get("v") != MOD_PROTOCOL_VERSION:
        raise ValueError(f"{what} must be a version {MOD_PROTOCOL_VERSION} JSON object")
    return payload


def _bounded_text(payload: JsonObject, key: str, max_chars: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ValueError(f"{key} must be a non-empty string of at most {max_chars} characters")
    return value


def mod_state_record(hello: JsonObject, *, server_pid: int, now: float) -> JsonObject:
    """Build the ``mod.json`` document for one heartbeat.

    :param hello: Output of :func:`normalize_mod_hello`.
    :param server_pid: PID of the serve-mcp process that received the hello.
    :param now: Server clock reading, e.g. ``time.time()``.
    :returns: The version 1 ``mod.json`` object.
    """
    return {
        "v": MOD_PROTOCOL_VERSION,
        "mod_version": hello["mod_version"],
        "claude_version": hello["claude_version"],
        "claude_session_id": hello["claude_session_id"],
        "main_turn_id": hello["main_turn_id"],
        "server_pid": server_pid,
        "updated_at": now,
    }


def mod_event_record(event: JsonObject, *, now: float) -> JsonObject:
    """Build one ``mod_events.jsonl`` line from a validated event.

    :param event: Output of :func:`normalize_mod_event`.
    :param now: Server clock reading on receipt.
    :returns: The version 1 event object, keys in contract order.
    """
    record: JsonObject = {
        "v": MOD_PROTOCOL_VERSION,
        "kind": event["kind"],
        "at": now,
        "claude_session_id": event["claude_session_id"],
        "turn_id": event["turn_id"],
        "agent_id": event["agent_id"],
    }
    if event["kind"] == "turn.complete":
        record["reason"] = event["reason"]
    return record


_GENERATED_MOD_NAMES: Final = frozenset({"types", "tsconfig.json", "__pycache__", "node_modules"})
_MOD_SHIPPED_FILES: Final = ("hooks/hooks.json", "hooks/register.ts")
MOD_PLUGIN_MANIFEST: Final[JsonObject] = {
    "name": "omnigent-bridge",
    "version": "0.1.0",
    "description": (
        "Connects this Claude Code session to Omnigent: reports turn boundaries and runs "
        "the commands Omnigent sends."
    ),
}


def install_claude_mod(bridge_dir: Path) -> Path:
    """Copy the shipped mod into the bridge directory and write its plugin manifest.

    Claude Code writes ``tsconfig.json`` and ``.claude-plugin/types`` into any
    folder it loads a mod from, so each session loads a copy and the package
    stays untouched. The manifest is written here, not shipped: git ignores
    dot-folders in the source tree.

    :param bridge_dir: Session bridge directory, e.g. ``/tmp/.../<digest>``.
    :returns: ``<bridge_dir>/claude-mod``.
    :raises FileNotFoundError: If this install lacks a file the mod ships with.
    """
    missing = [name for name in _MOD_SHIPPED_FILES if not (MOD_PACKAGE_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Claude mod files missing from the package: {', '.join(missing)}")
    destination = bridge_dir / MOD_PLUGIN_DIR_NAME
    # A copy left by an older install may hold files this version no longer ships.
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(
        MOD_PACKAGE_DIR,
        destination,
        ignore=lambda _directory, names: _GENERATED_MOD_NAMES.intersection(names),
    )
    manifest_dir = destination / ".claude-plugin"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(MOD_PLUGIN_MANIFEST, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def claude_cli_supports_mod(claude_binary: str) -> bool:
    """Return whether the Claude CLI at *claude_binary* meets the mod's version floor.

    :param claude_binary: Resolved path of the Claude executable.
    :returns: ``False`` when the version probe fails or the version is older.
    """
    from omnigent.onboarding.harness_install import binary_meets_min_version  # noqa: FlagLocalImports

    return binary_meets_min_version(claude_binary, _CLAUDE_MOD_MIN_VERSION)


def claude_mod_enabled_for_launch(*, feature_enabled: bool, claude_binary: str | None) -> bool:
    """Decide whether a Claude launch loads the mod.

    :param feature_enabled: Whether the ``claude_mod_channel`` feature is on.
    :param claude_binary: Resolved Claude executable, ``None`` when missing.
    :returns: ``True`` only when the feature is on and the binary meets the floor.
    """
    return feature_enabled and claude_binary is not None and claude_cli_supports_mod(claude_binary)


class PendingModCommand:
    """One queued command, its life cycle and the outcome it resolves to."""

    def __init__(self, *, command_id: str, operation: ModOperation, arguments: JsonObject) -> None:
        self.command_id = command_id
        self.operation = operation
        self.arguments = arguments
        self.state: Literal["queued", "in_flight", "finished", "abandoned"] = "queued"
        self.outcome: ModCommandOutcome | None = None
        self.finished = threading.Event()

    def wire(self) -> JsonObject:
        """Return the ``GET /mod/next`` body the mod executes."""
        return {"id": self.command_id, "op": self.operation, "args": self.arguments}


class ModCommandQueue:
    """Hands commands to the mod's long poll and matches each result to its caller.

    Commands leave in FIFO order, and several may be in flight at once, so an
    abort is not stuck behind a submit the mod is still running.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queued: deque[PendingModCommand] = deque()
        self._in_flight: dict[str, PendingModCommand] = {}

    def enqueue(self, operation: ModOperation, arguments: JsonObject) -> PendingModCommand:
        """Queue one normalized command.

        :param operation: Allowlisted operation, e.g. ``"submit"``.
        :param arguments: Output of :func:`normalize_mod_command`.
        :returns: The pending command the caller waits on.
        """
        pending = PendingModCommand(
            command_id=uuid.uuid4().hex, operation=operation, arguments=arguments
        )
        with self._condition:
            self._queued.append(pending)
            self._condition.notify_all()
        return pending

    def take(self, hold_s: float) -> PendingModCommand | None:
        """Hand the oldest queued command to the mod, waiting up to *hold_s*.

        :param hold_s: Seconds to hold the long poll open, e.g. ``20.0``.
        :returns: The command now in flight, or ``None`` when none arrived.
        """
        deadline = time.monotonic() + hold_s
        with self._condition:
            while not self._queued:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            pending = self._queued.popleft()
            pending.state = "in_flight"
            self._in_flight[pending.command_id] = pending
            return pending

    def hand_back(self, pending: PendingModCommand) -> None:
        """Requeue a command whose long-poll response never reached the mod.

        :param pending: A command returned by :meth:`take`.
        """
        with self._condition:
            if self._in_flight.pop(pending.command_id, None) is None:
                return
            pending.state = "queued"
            self._queued.appendleft(pending)
            self._condition.notify_all()

    def finish(self, command_id: str, *, ok: bool, result: JsonObject, error: str | None) -> bool:
        """Record the mod's answer for an in-flight command.

        :param command_id: Id echoed by the mod's ``POST /mod/result``.
        :param ok: Whether the operation was performed.
        :param result: Operation result when *ok*.
        :param error: Failure reason when not *ok*.
        :returns: ``False`` for an unknown or already abandoned command.
        """
        with self._condition:
            pending = self._in_flight.pop(command_id, None)
            if pending is None:
                return False
            if ok:
                pending.outcome = ModCommandOutcome(status="ok", result=result)
            else:
                pending.outcome = ModCommandOutcome(
                    status="rejected", error=error or "the mod rejected the command"
                )
            pending.state = "finished"
        pending.finished.set()
        return True

    def await_outcome(self, pending: PendingModCommand, timeout_s: float) -> ModCommandOutcome:
        """Wait for the mod's answer, abandoning the command if none comes in time.

        :param pending: Command returned by :meth:`enqueue`.
        :param timeout_s: Seconds to wait, e.g. ``5.0``.
        :returns: The mod's outcome, or a timeout status when none arrived.
        """
        pending.finished.wait(timeout_s)
        with self._condition:
            if pending.outcome is None:
                status: ModCommandStatus
                if pending.state == "queued":
                    self._queued.remove(pending)
                    status = "not_started"
                else:
                    self._in_flight.pop(pending.command_id, None)
                    status = "unanswered"
                pending.state = "abandoned"
                pending.outcome = ModCommandOutcome(
                    status=status, error=f"the mod gave no answer within {timeout_s:g}s"
                )
            return pending.outcome
