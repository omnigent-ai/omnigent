"""Kimi Code CLI executor.

Drives Moonshot AI's upstream ``kimi`` CLI from
https://github.com/MoonshotAI/Kimi-Code (the curl-installed
single-binary build at https://code.kimi.com/kimi-code/install.sh).
The legacy pypi ``kimi-cli`` package is **not** supported — its
command-line surface (``--print``, list-of-blocks content, etc.) is
incompatible with the upstream binary the issue (#271) targets.

One ``kimi -p <prompt> --output-format stream-json`` subprocess per
Omnigent turn:

- parses each JSONL line on stdout into one or more
  :class:`ExecutorEvent` (assistant text, tool-call request, tool-call
  result, session metadata),
- captures the kimi session id from the ``role:"meta"`` /
  ``type:"session.resume_hint"`` line for resume on the next turn,
- uses the subprocess's ``cwd=`` for the working directory (upstream
  has no ``--work-dir`` flag),
- after the subprocess exits, best-effort sums the turn's ``usage.record``
  rows from kimi's persisted ``wire.jsonl`` (the stream-json stdout carries
  no usage or model records) into ``TurnComplete.usage``, stamping the
  effective model from ``llm.request``.

Kimi runs its own agent loop and its own tools (Bash, edit, read, web,
…) — Omnigent does not re-execute them. The executor advertises
``handles_tools_internally=True`` and forwards ``tool_calls`` /
``role:"tool"`` events from kimi's transcript as informational
:class:`ToolCallRequest` / :class:`ToolCallComplete` so the Omnigent
UI can render them, but the Session layer does not dispatch them.

Env-var contract (read once at construction by
:mod:`omnigent.inner.kimi_harness`):

- ``HARNESS_KIMI_MODEL``: Kimi-side model id, e.g. ``"kimi-k2-turbo"``.
  ``None`` lets the kimi config's ``default_model`` win.
- ``HARNESS_KIMI_CWD``: working directory the kimi subprocess runs in.
  Upstream has no ``--work-dir`` flag so this is threaded through
  ``cwd=`` on the subprocess. ``None`` falls back to the runner's cwd.
- ``OMNIGENT_KIMI_PATH``: explicit path to the ``kimi`` binary, e.g.
  ``"/Users/x/.kimi-code/bin/kimi"``. Defaults to ``"kimi"`` looked up
  on ``PATH``. (Legacy ``HARNESS_KIMI_PATH`` still honored, deprecated.)
- ``HARNESS_KIMI_PLAN``: truthy → ``--plan`` (read-only plan mode).
- ``HARNESS_KIMI_CONTINUE_LAST``: truthy → ``--continue`` (resume the
  most recent session for the working directory). Mutually exclusive
  with ``HARNESS_KIMI_SESSION_ID``; the explicit session id wins.
- ``HARNESS_KIMI_SKILLS_DIRS``: JSON list of paths forwarded as one
  ``--skills-dir <path>`` per entry. Empty / unset = use kimi's
  default skill discovery (user + project dirs).

Per-invocation provider routing (``--config-file`` / ``--mcp-config-file``
/ gateway env vars) is **not** wired: upstream kimi has no per-spawn
config override. Provider configuration lives in ``~/.kimi-code/config.toml``
and is managed out-of-band via ``kimi provider add`` (Omnigent-side
provider injection is a deferred follow-up).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.agent_env import clean_agent_env, declared_passthrough
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolArgs,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)
from omnigent.kimi_native_credentials import resolve_user_kimi_home
from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict

_logger = logging.getLogger(__name__)

# Per-line cap for the stdout StreamReader. Kimi emits whole messages (not
# deltas), so a single JSONL line can be large; 16 MiB keeps a big file-read
# tool result or long assistant message from overrunning asyncio's 64 KiB
# default and crashing the turn.
_STREAM_LIMIT = 16 * 1024 * 1024

# Matches the resume hint kimi also prints to stderr / stdout (best-effort
# fallback for when the ``role:"meta"`` JSON event isn't seen). The session
# id format is ``session_<hex-uuid>`` — we accept the broader ``\S+`` to
# survive minor format drift.
_SESSION_RESUME_RE = re.compile(
    r"To resume this session:\s+\S+\s+-r\s+(\S+)",
    re.IGNORECASE,
)


#: One-shot log guard keys (schema drift logs once per process, not per row).
_WARNED_KEYS: set[str] = set()


def _warn_once(key: str, msg: str, *args: object) -> None:
    """Log *msg* at warning level only the first time *key* is seen."""
    if key in _WARNED_KEYS:
        return
    _WARNED_KEYS.add(key)
    _logger.warning(msg, *args)


def _token_count(raw: object) -> int | None:
    """A pinned token-count field: a non-boolean, non-negative int, else None."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def _find_wire_log(home: Path, session_id: str) -> Path | None:
    """Locate the session's persisted wire log under *home*, or ``None``.

    The session dir name equals the resume-hint session id
    (``session_<uuid>``); workspaces (``wd_*``) are globbed because the
    hint doesn't say which one the session lives under.
    """
    try:
        for wire in (home / "sessions").glob(f"*/{session_id}/agents/main/wire.jsonl"):
            if wire.is_file():
                return wire
    except OSError:
        return None
    return None


def _sum_wire_usage(
    wire_path: Path,
    *,
    offset: int,
    turn_start_ms: int,
) -> tuple[dict[str, int] | None, str | None, int]:
    """Sum the turn's ``usage.record`` rows from the wire log's new bytes.

    Reads from byte *offset* (the per-session checkpoint). On the first read
    of a wire log (``offset == 0``) the file may carry a resumed session's
    history (``-C`` / ``-S`` into an existing session), so rows are also
    gated on a STRICT ``time >= turn_start_ms`` — no backward allowance, or a
    turn finished moments before the resume would be re-billed. With a
    checkpoint, every new row belongs to this turn and no time gate applies.

    Rows are validated against the pinned Kimi Code 0.34.0 schema (four
    non-boolean, non-negative int counts + a model string); drifted rows are
    skipped whole and logged once — never partially counted.

    :returns: ``(token sums or None when no row counted, effective model or
        None, new checkpoint offset)``. The model prefers the ``llm.request``
        (provider-resolved id, then alias) PAIRED with a counted record —
        each record consumes the request immediately preceding it — and
        falls back to the newest counted record's own model, so a resumed
        log's historical request can never stamp a later turn whose own
        request row is missing. Tolerant of a missing/unreadable file and
        malformed rows.
    """
    try:
        with wire_path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = offset if 0 <= offset <= size else 0
            fh.seek(start)
            blob = fh.read()
    except OSError as exc:
        _warn_once(
            f"wire-unreadable:{wire_path}",
            "kimi executor: cannot read wire log %s (no usage reported): %s",
            wire_path,
            exc,
        )
        return None, None, offset
    first_read = start == 0
    # Collection runs only after the subprocess has exited, so these bytes are
    # stable: a valid final JSON row without a newline must bill, while an
    # invalid fragment is deliberately consumed because no writer can finish it.
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    counted = False
    # Request→usage pairing: each usage.record is produced by the llm.request
    # immediately preceding it, so a request only attributes tokens when one
    # of ITS usage rows is counted. A timeless historical request followed by
    # its own (uncounted, pre-turn) usage row is consumed by that row and can
    # never mis-stamp a later counted row whose own request went missing.
    request_model: str | None = None
    pending_request: str | None = None
    record_model: str | None = None
    for raw in blob.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        row: object = None
        if line.startswith("{"):
            try:
                row = json.loads(line)
            except ValueError:
                row = None
        if not isinstance(row, dict):
            # Torn write / non-JSON noise: skip, but keep the diagnostic.
            _warn_once(
                f"wire-badrow:{wire_path}",
                "kimi executor: skipping unparseable wire row(s) in %s "
                "(further rows logged at debug)",
                wire_path,
            )
            _logger.debug("kimi executor: unparseable wire row: %.200s", line)
            continue
        row_type = row.get("type")
        if row_type == "llm.request":
            # A resumed log's historical requests must not attribute this
            # turn — but real 0.34.0 ``llm.request`` rows carry NO ``time``
            # (pinned fixtures), so the first-read gate applies only to rows
            # that DO have one. A timeless historical row is harmless: the
            # newest request wins, and this turn's own (also timeless)
            # request comes later in the log. Usage rows stay strictly
            # time-gated below — attribution can tolerate this, billing
            # cannot.
            if first_read:
                request_time = _token_count(row.get("time"))
                if request_time is not None and request_time < turn_start_ms:
                    continue
            # Provider-resolved model id, falling back to the configured alias.
            candidate = row.get("model")
            if not (isinstance(candidate, str) and candidate):
                candidate = row.get("modelAlias")
            if isinstance(candidate, str) and candidate:
                pending_request = candidate
            continue
        if row_type == "turn.ended":
            # Failed turns can end without usage; their request must not stamp a
            # later record whose own request row is absent.
            pending_request = None
            continue
        if row_type != "usage.record":
            continue
        # Every usage scope consumes the request that produced it. Aggregate
        # records are not billed here, but leaving their request pending would
        # misattribute the next turn-scoped row.
        consumed_request, pending_request = pending_request, None
        if row.get("usageScope") != "turn":
            continue
        model = row.get("model")
        row_time = _token_count(row.get("time"))
        usage = row.get("usage")
        counts: dict[str, int] | None = None
        if isinstance(usage, dict):
            counts = {}
            for wire_key, out_key in (
                ("inputOther", "input_tokens"),
                ("output", "output_tokens"),
                ("inputCacheRead", "cache_read_input_tokens"),
                ("inputCacheCreation", "cache_creation_input_tokens"),
            ):
                value = _token_count(usage.get(wire_key))
                if value is None:
                    counts = None
                    break
                counts[out_key] = value
        if counts is None or not (isinstance(model, str) and model) or row_time is None:
            # Schema drift: skip the whole record — partial/zero sums would
            # be silently wrong and the checkpoint advances irreversibly.
            _warn_once(
                f"usage.record-drift:{wire_path}",
                "kimi executor: skipping usage.record not matching the pinned "
                "0.34.0 schema (further drift logged at debug): %r",
                row,
            )
            _logger.debug("kimi executor: skipped drifted usage.record: %r", row)
            continue
        if first_read and row_time < turn_start_ms:
            continue
        for out_key, value in counts.items():
            totals[out_key] += value
        record_model = model
        if consumed_request is not None:
            request_model = consumed_request
        counted = True
    if counted:
        # The relay path stores total_tokens as its own accumulator (never
        # derived), so omitting it would report 0 total forever.
        totals["total_tokens"] = sum(totals.values())
    # Checkpoint exactly what was consumed: complete lines only (any trailing
    # fragment was cut above), and never bytes appended after the read.
    return (totals if counted else None), (request_model or record_model), start + len(blob)


def _parse_truthy(value: str | None) -> bool:
    """Return True for "1"/"true"/"yes"/"on" (case-insensitive)."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _resolve_kimi_binary() -> str:
    """Resolve the ``kimi`` binary path.

    ``OMNIGENT_KIMI_PATH`` wins (legacy ``HARNESS_KIMI_PATH`` still honored
    via :func:`resolve_harness_path`, which emits a deprecation warning; lets
    users point at a custom build or a non-standard install location).
    Otherwise default to ``"kimi"`` and rely on ``shutil.which`` so a missing
    binary surfaces clearly at ``run_turn``.

    The legacy pypi ``kimi-cli`` package is intentionally NOT detected —
    its command-line surface is incompatible with the upstream binary
    Omnigent supports.
    """
    explicit = resolve_harness_path("kimi")
    if explicit:
        return explicit
    return "kimi"


def _latest_user_text(messages: list[Message]) -> str:
    """Extract the most recent user message's text.

    Kimi receives the conversation history via ``--session <id>``, not
    via stdin, so we only need the most recent user turn to drive
    ``-p <text>``. Image / file / audio content blocks are dropped with
    a single warning per turn (multimodal input is a deferred follow-up).
    """
    dropped_blocks = 0
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in ("text", "input_text") and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type in ("input_image", "input_file", "input_audio"):
                    dropped_blocks += 1
            if dropped_blocks:
                _logger.warning(
                    "kimi harness: dropped %d non-text content block(s) on the "
                    "latest user message (multimodal input not yet wired)",
                    dropped_blocks,
                )
            return "".join(text_parts)
    return ""


def _resolve_skills_dirs(raw: str | None) -> list[str]:
    """Parse ``HARNESS_KIMI_SKILLS_DIRS`` (JSON list of paths) into a list.

    Returns ``[]`` when unset / malformed so kimi falls back to its
    default discovery (user + project skill dirs).
    """
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning("HARNESS_KIMI_SKILLS_DIRS is not valid JSON (%s); ignoring", exc)
        return []
    if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
        _logger.warning(
            "HARNESS_KIMI_SKILLS_DIRS must be a JSON array of strings; got %r; ignoring",
            parsed,
        )
        return []
    return list(parsed)


class KimiExecutor(Executor):
    """Drive ``kimi -p`` per Omnigent turn.

    See module docstring for env-var contract and lifecycle.
    """

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        binary_path: str | None = None,
        plan: bool = False,
        continue_last_session: bool = False,
        skills_dirs: list[str] | None = None,
    ) -> None:
        self._cwd = cwd
        self._os_env = os_env
        self._model = model
        self._binary_path = binary_path or _resolve_kimi_binary()
        self._plan = plan
        self._continue_last_session = continue_last_session
        self._skills_dirs = list(skills_dirs or [])

        # Per-session state: kimi session id captured from the prior turn's
        # ``role:"meta"`` event, fed to ``-S <id>`` on the next turn.
        self._session_id: str | None = None
        # Byte-offset checkpoints into each kimi session's persisted wire log,
        # so a turn only sums the usage records new since the prior turn.
        self._wire_offsets: dict[str, int] = {}
        # Sessions whose usage collection already failed (no/empty wire):
        # maps to the FIRST failed turn's start, so a later first read bills
        # those turns' late-appearing rows instead of gating on the current
        # turn's start (and seeding must not snapshot EOF past them).
        self._first_read_floor_ms: dict[str, int] = {}
        # Tracks whether we've already warned this session about tools
        # being declared without a provider-injection bridge (one warning
        # per session; the tool-injection bridge is a deferred follow-up).
        self._warned_tools_without_bridge = False
        # Active subprocess handle, captured so interrupt can target it.
        self._active_process: asyncio.subprocess.Process | None = None

    # -- capabilities --------------------------------------------------------

    def handles_tools_internally(self) -> bool:
        return True

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    # -- helpers -------------------------------------------------------------

    def _build_spawn_env(self) -> dict[str, str]:
        """The env handed to the kimi subprocess.

        Inherits the harness wrap's own env (so ``KIMI_*`` auth vars
        the user exported reach the subprocess) and adds nothing — all
        ``HARNESS_KIMI_*`` knobs are read on the wrap side and
        translated into CLI flags.
        """
        # Deny-by-default: base + kimi's own families + the spec's
        # env_passthrough. Keeps the documented ambient KIMI_/MOONSHOT_ auth
        # while no longer handing the CLI every other provider's key (#3445).
        return clean_agent_env(
            allow_prefixes=("KIMI_", "MOONSHOT_"),
            extra_allowed=declared_passthrough(self._os_env),
        )

    def _sandbox_launch_path(self, spawn_env_names: Sequence[str]) -> str:
        """Return the path to spawn for kimi — sandbox launcher or bare binary.

        Mirrors :meth:`omnigent.inner.qwen_executor.QwenExecutor._sandbox_launch_path`.
        Upstream kimi has no sandbox flag of its own and runs its built-in
        Bash / edit / read tools (and any shell child processes) in-process.
        When the spec's ``os_env.sandbox`` requests confinement, wrap the
        whole kimi process tree in the platform sandbox
        (``linux_bwrap`` / ``darwin_seatbelt``) so even an *allowed* tool
        call can't touch paths outside the spec's read/write roots — the
        OS-level guarantee kimi's own approval flow can't give.

        Falls back to the bare binary (never blocks startup) when no sandbox
        is requested, the resolved policy is inactive, or the backend is
        unavailable on this platform.

        :param spawn_env_names: Env-var names we deliberately set on the
            subprocess ``env=``; baked into the policy so the launcher prunes
            anything else it inherits (host-env leak defense).
        :returns: The path to pass as argv[0] to ``create_subprocess_exec``.
        """
        os_env = self._os_env
        if os_env is None:
            return self._binary_path
        sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
        if sandbox_spec.type == "none":
            return self._binary_path
        try:
            from .sandbox import (
                create_exec_launcher,
                resolve_sandbox,
                with_additional_read_roots,
                with_additional_write_roots,
                with_spawn_env_allowlist,
            )

            cwd = Path(self._cwd or os.getcwd()).resolve(strict=False)
            sandbox = resolve_sandbox(os_env, cwd)
            if not sandbox.active:
                return self._binary_path
            # kimi is a curl-installed single binary: it must read its own
            # install dir and write its config dir ($KIMI_CODE_HOME, default
            # ~/.kimi-code) and /tmp, or it can't start inside the jail.
            from omnigent.kimi_native_credentials import resolve_user_kimi_home

            resolved_bin = shutil.which(self._binary_path) or self._binary_path
            bin_dir = Path(resolved_bin).resolve(strict=False).parent
            sandbox = with_additional_read_roots(sandbox, [bin_dir])
            sandbox = with_additional_write_roots(
                sandbox, [resolve_user_kimi_home(), Path("/tmp")]
            )
            sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
            return create_exec_launcher(resolved_bin, sandbox)
        except (OSError, ImportError, NotImplementedError) as exc:
            _logger.warning("Could not apply sandbox for kimi; running unsandboxed: %s", exc)
            return self._binary_path

    def _build_argv(self, *, prompt_text: str) -> list[str]:
        """Assemble the kimi argv for one turn.

        Upstream ``-p <text>`` is the headless print mode (mutually
        exclusive with ``--yolo`` because ``-p`` already auto-approves).
        ``--output-format stream-json`` makes stdout a JSONL transcript.
        ``-S`` resumes a session; ``-C`` continues the last session for
        this cwd. The explicit session id wins when both are set.
        """
        argv: list[str] = [
            self._binary_path,
            "--output-format",
            "stream-json",
        ]

        if self._model:
            argv.extend(["-m", self._model])

        if self._plan:
            argv.append("--plan")

        for skills_dir in self._skills_dirs:
            argv.extend(["--skills-dir", skills_dir])

        if self._session_id:
            argv.extend(["-S", self._session_id])
        elif self._continue_last_session:
            argv.append("-C")

        # ``-p`` must come last because it consumes a single argument; placing
        # other flags after it would be parsed as part of the prompt.
        argv.extend(["-p", prompt_text])
        return argv

    def _seed_wire_checkpoint(self) -> None:
        """Snapshot the resumed session's wire EOF before spawning.

        A known session id with no checkpoint (e.g. an earlier extraction
        failure) must not bill pre-existing records to this turn: everything
        already in the file predates the spawn. Best-effort.
        """
        session_id = self._session_id
        if not session_id or session_id in self._wire_offsets:
            return
        if session_id in self._first_read_floor_ms:
            # An earlier turn's collection yielded no usage (missing, empty,
            # or unreadable wire), so rows written since that turn are still
            # unbilled: snapshotting EOF now would skip them forever. The
            # first read starts at 0, gated on the recorded floor instead.
            return
        try:
            wire = _find_wire_log(resolve_user_kimi_home(), session_id)
            if wire is not None:
                self._wire_offsets[session_id] = wire.stat().st_size
        except Exception:
            # Best-effort boundary (e.g. Path.home() in a HOME-less env):
            # seeding must never fail the turn.
            _logger.exception("kimi executor: wire checkpoint seeding failed")
            return

    def _collect_turn_usage(self, turn_start_ms: int) -> dict[str, object] | None:
        """Best-effort per-turn token usage from kimi's persisted wire log.

        Kimi's stream-json stdout carries no usage/model records, but the
        session's ``wire.jsonl`` does. Sums the turn's ``usage.record`` rows
        (new since this session's byte checkpoint) into ``TurnComplete.usage``
        keys, stamping the effective model from ``llm.request`` so the server
        attributes tokens per model. Never raises — any failure logs at debug
        and reports no usage.
        """
        session_id = self._session_id
        if not session_id:
            return None
        try:
            wire_path = _find_wire_log(resolve_user_kimi_home(), session_id)
            if wire_path is None:
                _warn_once(
                    f"wire-missing:{session_id}",
                    "kimi executor: no wire log found for %s (no usage reported "
                    "this turn; retried next turn)",
                    session_id,
                )
                # Remember the FIRST failed turn's start: its rows may appear
                # late and must still bill on the eventual first read.
                self._first_read_floor_ms.setdefault(session_id, turn_start_ms)
                return None
            offset = self._wire_offsets.get(session_id, 0)
            floor_ms = self._first_read_floor_ms.get(session_id, turn_start_ms)
            totals, model, new_offset = _sum_wire_usage(
                wire_path, offset=offset, turn_start_ms=floor_ms
            )
            self._wire_offsets[session_id] = new_offset
            if totals is None:
                # ANY first collection that yields no usage — wire missing
                # (handled above), empty, unreadable, or holding zero billable
                # rows — records the FIRST such turn's start, so rows flushed
                # late still bill on the eventual productive read instead of
                # failing a later turn's time gate.
                if offset == 0:
                    self._first_read_floor_ms.setdefault(session_id, turn_start_ms)
                return None
            # A read actually consumed billable rows; the pending-first-read
            # floor has served its purpose.
            self._first_read_floor_ms.pop(session_id, None)
            usage: dict[str, object] = dict(totals)
            if model:
                usage["model"] = model
            _notify_usage_from_dict(model=model or self._model or "kimi", usage=usage)
            return usage
        except Exception:
            # Best-effort boundary: usage extraction must never fail the turn.
            _logger.exception("kimi executor: wire-log usage extraction failed")
            return None

    def _translate_event(self, payload: Mapping[str, object]) -> list[ExecutorEvent]:
        """Translate one kimi stream-json line into Omnigent events.

        Upstream emits whole messages (not deltas). Roles seen:

        - ``"assistant"``: may carry ``content`` (a plain string with the
          assistant's reply) and/or ``tool_calls`` (the model invoking
          one of kimi's internal tools).
        - ``"tool"``: kimi's own tool execution result delivered back to
          its loop. Surfaced as a ``ToolCallComplete`` so the Omnigent
          UI can render it; the Session layer does not re-execute
          (``handles_tools_internally=True``).
        - ``"meta"`` with ``type:"session.resume_hint"``: carries the
          kimi session id we capture for resume on the next turn.

        Unknown roles / types are silently ignored — kimi may grow new
        event types in future versions.
        """
        events: list[ExecutorEvent] = []
        role = payload.get("role")

        if role == "assistant":
            content = payload.get("content")
            if isinstance(content, str) and content:
                events.append(TextChunk(text=content))
            tool_calls = payload.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    if not isinstance(fn, dict):
                        continue
                    name = fn.get("name") or ""
                    raw_args = fn.get("arguments")
                    args: ToolArgs = {}
                    if isinstance(raw_args, str):
                        with contextlib.suppress(json.JSONDecodeError):
                            parsed = json.loads(raw_args)
                            if isinstance(parsed, dict):
                                args = parsed
                    elif isinstance(raw_args, dict):
                        args = raw_args
                    call_id = call.get("id") or ""
                    if name:
                        events.append(
                            ToolCallRequest(
                                name=name,
                                args=args,
                                metadata={"call_id": call_id} if call_id else {},
                            )
                        )
        elif role == "tool":
            # Kimi has already executed the tool. Emit a synthetic completion
            # so the Omnigent UI can render the result. The Session layer
            # will not double-execute (handles_tools_internally=True).
            result = payload.get("content")
            call_id = payload.get("tool_call_id") or ""
            events.append(
                ToolCallComplete(
                    name="",  # kimi doesn't repeat the name in tool results
                    status=ToolCallStatus.SUCCESS,
                    result=result,
                    metadata={"call_id": call_id} if call_id else {},
                )
            )
        elif role == "meta" and payload.get("type") == "session.resume_hint":
            captured = payload.get("session_id")
            if isinstance(captured, str) and captured:
                self._session_id = captured
        # Anything else: ignore silently.
        return events

    # -- main entrypoint -----------------------------------------------------

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,  # noqa: ARG002 — kimi's own agent spec carries instructions
        config: ExecutorConfig | None = None,  # noqa: ARG002 — per-turn override not yet plumbed
    ) -> AsyncIterator[ExecutorEvent]:
        if tools and not self._warned_tools_without_bridge:
            _logger.warning(
                "kimi executor received %d declared tool(s) but Omnigent has no "
                "tool-injection bridge for the upstream kimi binary yet (no "
                "per-spawn --mcp-config-file). The tools will not be exposed to "
                "kimi for this session (MCP tool-injection is a deferred follow-up).",
                len(tools),
            )
            self._warned_tools_without_bridge = True

        if shutil.which(self._binary_path) is None and not Path(self._binary_path).exists():
            yield ExecutorError(
                message=(
                    f"kimi harness: binary {self._binary_path!r} not found on PATH. "
                    "Install via `curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash` "
                    "or set OMNIGENT_KIMI_PATH (legacy HARNESS_KIMI_PATH) to its"
                    " absolute location."
                ),
                retryable=False,
            )
            return

        prompt_text = _latest_user_text(messages)
        if not prompt_text:
            yield TurnComplete(response=None)
            return

        argv = self._build_argv(prompt_text=prompt_text)
        env = self._build_spawn_env()
        # Resolve argv[0]: the bare binary, or a sandbox launcher wrapping it
        # when the spec's os_env requests confinement (so kimi's in-process
        # Bash/edit/read tools run inside the spec's read/write roots).
        argv[0] = self._sandbox_launch_path(tuple(env.keys()))

        started_at = time.monotonic()
        # Checkpoint a resumed session's wire EOF before spawning, so the
        # usage read after exit never bills pre-existing records to this turn.
        self._seed_wire_checkpoint()
        # Wall-clock floor for the wire-log usage read: on the first read of a
        # resumed session's log, only records stamped at/after this turn count.
        # Truncated to whole ms — record ``time`` stamps are ms-resolution, so
        # the strict gate must compare at the same granularity.
        turn_start_ms = int(time.time() * 1000)
        process: asyncio.subprocess.Process | None = None
        stderr_buf = bytearray()
        any_text_emitted = False
        final_text_parts: list[str] = []
        try:
            process = await _create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd or None,
                env=env,
                # Kimi emits whole messages (not deltas) as JSONL: a single
                # tool result echoing a large file read or a long assistant
                # message can exceed asyncio's default 64 KiB per-line cap and
                # raise LimitOverrunError out of the ``async for`` below. Use a
                # generous 16 MiB limit (parity with the qwen executor).
                limit=_STREAM_LIMIT,
            )
            self._active_process = process

            assert process.stdout is not None
            assert process.stderr is not None

            async def _drain_stderr() -> None:
                """Buffer stderr so the resume-hint fallback regex can read it after exit."""
                assert process is not None and process.stderr is not None
                while True:
                    chunk = await process.stderr.read(4096)
                    if not chunk:
                        return
                    stderr_buf.extend(chunk)

            stderr_task = asyncio.create_task(_drain_stderr())
            try:
                async for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        # Kimi sometimes prints informational lines on stdout
                        # (e.g. ``Shell cwd was reset to ...``). Log at debug
                        # and move on — never crash on non-JSON.
                        _logger.debug("kimi executor: non-JSON stdout line: %s", line[:200])
                        continue
                    if not isinstance(payload, dict):
                        continue
                    for event in self._translate_event(payload):
                        if isinstance(event, TextChunk):
                            any_text_emitted = True
                            final_text_parts.append(event.text)
                        yield event
            finally:
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
        except asyncio.CancelledError:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            raise
        finally:
            self._active_process = None
            if process is not None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()

        # Fallback: if the ``role:"meta"`` JSON event wasn't seen but the
        # stderr footer carries the resume hint, capture from there. Mostly
        # belt-and-suspenders against minor stream-json schema drift.
        if not self._session_id:
            stderr_text = stderr_buf.decode("utf-8", errors="replace")
            match = _SESSION_RESUME_RE.search(stderr_text)
            if match:
                self._session_id = match.group(1)
        # If no resume hint surfaced anywhere, leave ``_session_id`` as None.
        # The next turn then omits ``-S`` and starts a fresh kimi session,
        # which is safer than minting an arbitrary id: an invented value isn't
        # in kimi's documented ``session_<uuid>`` shape, and passing an
        # unknown id risks upstream erroring on every subsequent turn.

        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        if process is not None and process.returncode not in (None, 0):
            stderr_text = stderr_buf.decode("utf-8", errors="replace")
            yield ExecutorError(
                message=(
                    f"kimi exited with code {process.returncode} after "
                    f"{elapsed_ms:.0f}ms. stderr: {stderr_text.strip()[:500]}"
                ),
                retryable=False,
            )
            return

        yield TurnComplete(
            response="".join(final_text_parts) if any_text_emitted else None,
            usage=self._collect_turn_usage(turn_start_ms),
        )

    # -- session lifecycle ---------------------------------------------------

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002 — per-session id is the kimi UUID, no extra teardown
        """Drop the captured session id so the next turn starts fresh.

        The kimi subprocess is per-turn, so there is no long-lived
        resource to release. We just forget the cached session id.
        """
        self._session_id = None

    async def interrupt_session(self, session_key: str) -> bool:  # noqa: ARG002 — best-effort process terminate
        """Terminate the active kimi process, if any.

        Returns True when a process was actually signalled. The next
        ``run_turn`` will start a fresh process (and a fresh ``-S``
        resume if the cached id is still valid).
        """
        process = self._active_process
        if process is None or process.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
            return True
        return False

    async def enqueue_session_message(
        self,
        session_key: str,  # noqa: ARG002 — per-turn subprocess model; no live queue
        content: EnqueuedContent,  # noqa: ARG002 — per-turn subprocess model; no live queue
    ) -> bool:
        """Not supported under the per-turn subprocess model.

        The ``kimi acp`` long-lived path would unlock this (a deferred
        follow-up).
        """
        return False


async def _create_subprocess_exec(
    program: str,
    *args: str,
    stdin: int,
    stdout: int,
    stderr: int,
    cwd: str | None,
    env: Mapping[str, str],
    limit: int,
) -> asyncio.subprocess.Process:
    """Indirection point so tests can stub subprocess creation.

    Direct patching of ``asyncio.create_subprocess_exec`` in tests is
    tricky because asyncio caches the bound method. Tests patch this
    module-level helper instead.
    """
    return await asyncio.create_subprocess_exec(
        program,
        *args,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        env=env,
        limit=limit,
    )
