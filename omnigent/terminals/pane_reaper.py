"""Idle reaper for native-harness terminal panes.

Native CLI sessions (``claude-native`` / ``codex-native`` / ``cursor-native`` /
...) run their vendor CLI plus a full MCP fleet inside a persistent tmux pane
held for the whole conversation lifetime, next to per-session sidecars: a
forwarder or bridge task, a tool/comment relay, and for some harnesses a vendor
server (``codex app-server``, ``opencode serve``). Unlike the SDK harness
proxies — which ``HarnessProcessManager._idle_reaper_loop`` reaps after an idle
window — these have no idle reaper of their own, so on a shared runner memory
grows with every idle conversation. This reaps a native pane only when it is
genuinely unused.

Each scan the runner *assesses* every listed pane (:class:`PaneAssessment`):

* **Hard reasons** spare the pane outright (:class:`SpareReason`): a live runner
  turn, messages still queued for the pane (for one idle window), a running
  tool call, a human-answerable prompt (runner ASK, open prompt park, a dialog
  the agent reports, claude's approval marker), running sub-agents, an attached
  tmux client, or a probe that says the agent works. Human waits are bounded by
  :envvar:`OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S`, probe ACTIVE and sub-agents by
  :envvar:`OMNIGENT_NATIVE_PANE_MAX_TURN_S`; a bound held by the pre-reap check
  counts from when the wait began, and may run up to one idle window over.
* **Evidence age** is how long the pane has been silent: tmux's
  ``window_activity`` clock and, under the ``evidence`` claim policy, how long
  a recorded ``running`` has gone unrefreshed. Output within
  :data:`PANE_OUTPUT_BUSY_WINDOW_S` spares the pane.

A recorded ``running`` is a *claim*, not evidence: status channels are lossy
(a relayed idle can be dropped, a relay can re-assert ``running`` forever), so
the claim is judged against the pane and, before any teardown, against the
harness's own state (``confirm_reap``: a turn probe and the server's pending
prompts). A refutation lands only on the claim it judged: if a local channel
re-asserted it meanwhile (a new turn), nothing is recorded and the pane is
spared. A dialog recorded from the harness's own status file is re-read by its
probe the same way; a relayed dialog holds until its bound. A pane is reapable
at ``max(last output, last claim transition, last hard-busy scan, first sight)
+ idle_timeout``; the pane is re-assessed right before teardown to close the
select→reap race.

**Teardown** closes the pane and releases its sidecars under the harness's
per-session ensure lock, after re-testing live work, and that no turn was
dispatched since the pre-reap check began, with no await in between.
The session's primary OSEnv and server-side transcript stay intact: the next
message re-creates the pane and its sidecars, and the vendor CLI resumes via
its own ``--resume``.

**Observability.** Spare-reason changes and every reap are logged; WARNINGs
flag a pane held only by a status claim, a forgotten attach, long holds and a
TUI that seems to repaint while idle; a summary line is logged every
:data:`_SUMMARY_EVERY_SCANS` scans.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

# A pane whose window emitted output this recently counts as busy. tmux's own
# activity clock is evidence independent of the harness status pipeline, whose
# silent stall must not get a live, producing terminal reaped. Two reaper scan
# intervals, so any output between scans re-arms the idle clock.
PANE_OUTPUT_BUSY_WINDOW_S = 120.0

# Native CLI panes are keyed (conversation_id, <harness short name>, "main") in
# the terminal registry. These short names match the ``terminal_name`` the
# per-harness ``_auto_create_<harness>_terminal`` paths launch with. This is the
# cheap name pre-filter; the wiring additionally confirms the registry resource
# ROLE is a native harness (so a user terminal that merely shares the name is not
# reaped — see ``create_runner_app``).
#
# "kimi" is deliberately absent: kimi records no resumable chat id, so a reaped
# pane cannot be re-created with its context — the next turn would silently
# start a fresh TUI. Keep kimi panes alive until the session is torn down.
NATIVE_PANE_TERMINAL_NAMES: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "cursor",
        "goose",
        "hermes",
        "kiro",
        "qwen",
        "pi",
        "antigravity",
        "opencode",
    }
)

# Default idle window before an unused native pane is reaped. Mirrors
# ``HarnessProcessManager``'s 1-hour SDK-proxy default for consistency.
_DEFAULT_IDLE_TIMEOUT_S = 60 * 60
_DEFAULT_REAPER_INTERVAL_S = 60.0
_IDLE_TIMEOUT_ENV = "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S"

# How long a human-wait signal (open prompt park, dialog reported by the agent,
# a probe's PARKED) may hold a pane: the runner's own ASK wait budget.
_DEFAULT_APPROVAL_MAX_S = 86400.0
_APPROVAL_MAX_ENV = "OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S"
# How long a probe's ACTIVE (or running sub-agents) may hold a silent pane.
# The runner's idle watchdog reuses it (floored at an hour) as how long a
# recorded native turn may keep the runner up after its last evidence of work.
_DEFAULT_MAX_TURN_S = 86400.0
_MAX_TURN_ENV = "OMNIGENT_NATIVE_PANE_MAX_TURN_S"
# How a recorded ``running`` weighs in the assessment (see ClaimPolicy).
_CLAIM_POLICY_ENV = "OMNIGENT_NATIVE_PANE_CLAIM_POLICY"
# ``0`` skips asking the server for pending prompts before a reap.
_SERVER_CHECK_ENV = "OMNIGENT_NATIVE_PANE_REAP_SERVER_CHECK"
# How long an unreachable server may block reaps before local signals decide.
_SERVER_UNREACHABLE_GRACE_ENV = "OMNIGENT_NATIVE_PANE_SERVER_UNREACHABLE_GRACE_S"

# Scans between periodic summary log lines (an hour at the default interval).
_SUMMARY_EVERY_SCANS = 60
# A pane held by output alone this many idle windows, while its status says it
# is not working, suggests a TUI that repaints while idle.
_IDLE_REPAINT_WINDOWS = 3


class SpareReason(StrEnum):
    """Why a pane is not reaped this scan."""

    RUNNER_TURN = "runner_turn"
    # Messages buffered for the pane, still waiting to be delivered.
    QUEUED_INPUT = "queued_input"
    TOOL_CALL = "tool_call"
    AWAITING_HUMAN = "awaiting_human"
    CHILDREN = "children"
    CLIENT_ATTACHED = "client_attached"
    STATUS_CLAIM = "status_claim"
    TURN_PROBE = "turn_probe"
    SERVER_CHECK = "server_check"
    # A legacy boolean ``is_busy`` predicate said busy.
    BUSY = "busy"


class ClaimPolicy(StrEnum):
    """How a recorded ``running`` counts when nothing else explains it.

    * ``veto`` — it spares the pane outright (the pre-evidence behavior).
    * ``shadow`` — as ``veto``, but log when it would have expired.
    * ``evidence`` — it is soft evidence aged from its episode start: a stale
      claim is confirmed or refuted before teardown and otherwise expires.
    """

    VETO = "veto"
    SHADOW = "shadow"
    EVIDENCE = "evidence"


@dataclass(frozen=True)
class PaneAssessment:
    """One pane's liveness verdict for a scan.

    :param reasons: Hard reasons that spare the pane outright.
    :param evidence_age_s: Seconds since the last soft evidence of work
        (pane output, or an unexpired claim's episode start), or ``None`` when
        no such evidence could be read. Relative, never an absolute clock.
    :param facts: Diagnostics for logs, e.g. ``{"output_age_s": 12.0}``.
    """

    reasons: frozenset[SpareReason] = frozenset()
    evidence_age_s: float | None = None
    facts: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def busy(self) -> bool:
        """Legacy verdict: a hard reason, or evidence inside the output window."""
        return bool(self.reasons) or (
            self.evidence_age_s is not None and self.evidence_age_s < PANE_OUTPUT_BUSY_WINDOW_S
        )


@dataclass(frozen=True)
class ConfirmVerdict:
    """The deep pre-teardown check's answer.

    :param proceed: ``True`` to reap.
    :param reason: Why it spared, e.g. ``SpareReason.TURN_PROBE``, or ``""``.
    :param unknown: Spared only because a probe could not answer for a stale
        claim; the reaper proceeds after one more idle window.
    :param facts: Diagnostics for logs.
    :param held_s: How long the wait behind *reason* has already lasted, when
        the check knows (a dialog's recorded age), so its ceiling counts from
        there rather than from the first deep-check spare.
    """

    proceed: bool
    reason: str = ""
    unknown: bool = False
    facts: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    held_s: float | None = None


class PaneRef(NamedTuple):
    """A live native CLI pane the reaper may reclaim.

    :param conversation_id: AP-allocated conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Resource id of the native terminal, e.g.
        ``terminal_resource_id("claude", "main")`` — used for pane-scoped close.
    :param terminal_name: Harness short-name, e.g. ``"claude"``.
    :param socket_path: tmux socket for the attached-client probe.
    """

    conversation_id: str
    terminal_id: str
    terminal_name: str
    socket_path: Path


def _resolve_seconds_env(name: str, default: float) -> float:
    """Resolve a non-negative seconds knob, falling back on bad input.

    An unparseable, non-finite (``nan``, ``inf``) or negative value logs a
    warning and uses *default* rather than failing the runner at boot — an env
    typo shouldn't take the runner down or (worse) make the reaper act on a
    bogus window.

    :param name: Environment variable, e.g. ``"OMNIGENT_NATIVE_PANE_MAX_TURN_S"``.
    :param default: Value when unset or invalid.
    """
    raw = os.environ.get(name)
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("%s=%r is not a number; using default %ss", name, raw, default)
        return float(default)
    if not math.isfinite(value):
        _logger.warning("%s=%r is not a finite number; using default %ss", name, raw, default)
        return float(default)
    if value < 0:
        _logger.warning("%s=%r is negative; using default %ss", name, raw, default)
        return float(default)
    return value


def resolve_native_pane_idle_timeout_s() -> float:
    """Resolve the native-pane idle window in seconds.

    Honors :envvar:`OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S` (``0`` disables pane
    reaping); otherwise the 1-hour default.
    """
    return _resolve_seconds_env(_IDLE_TIMEOUT_ENV, _DEFAULT_IDLE_TIMEOUT_S)


def resolve_approval_max_s() -> float:
    """Longest a human-wait signal may hold a pane (default one day)."""
    return _resolve_seconds_env(_APPROVAL_MAX_ENV, _DEFAULT_APPROVAL_MAX_S)


def resolve_max_turn_s() -> float:
    """Longest a probe's ACTIVE or running sub-agents may hold a silent pane.

    The runner's idle watchdog reuses it, floored at an hour, as how long a
    recorded native turn may keep the runner up after its last evidence of work.
    """
    return _resolve_seconds_env(_MAX_TURN_ENV, _DEFAULT_MAX_TURN_S)


def resolve_claim_policy() -> ClaimPolicy:
    """Resolve :envvar:`OMNIGENT_NATIVE_PANE_CLAIM_POLICY` (default ``shadow``)."""
    raw = (os.environ.get(_CLAIM_POLICY_ENV) or "").strip().lower()
    if not raw:
        return ClaimPolicy.SHADOW
    try:
        return ClaimPolicy(raw)
    except ValueError:
        _logger.warning("%s=%r is not a claim policy; using shadow", _CLAIM_POLICY_ENV, raw)
        return ClaimPolicy.SHADOW


def resolve_server_check_enabled() -> bool:
    """Whether reaps first ask the server for pending prompts (default on)."""
    raw = (os.environ.get(_SERVER_CHECK_ENV) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def resolve_server_unreachable_grace_s(idle_timeout_s: float) -> float:
    """How long an unreachable server may block reaps (default one idle window)."""
    return _resolve_seconds_env(_SERVER_UNREACHABLE_GRACE_ENV, idle_timeout_s)


_RESERVED_LOG_KEYS = frozenset({"session_id", "turn_id", "user_id"})


def _pane_event(
    name: str,
    session_id: str | None,
    facts: Mapping[str, object] | None = None,
    /,
    **attrs: object,
) -> dict[str, object]:
    """A ``debug_event`` extra carrying *attrs* over the assessment *facts*."""
    extra = debug_event(name, session_id=session_id)
    attributes = {k: v for k, v in (facts or {}).items() if k not in _RESERVED_LOG_KEYS}
    attributes.update(attrs)
    extra["attributes"] = attributes
    return extra


class NativePaneReaper:
    """Background task that reaps idle, unattended native terminal panes.

    :param list_native_panes: Returns the currently-live native panes (already
        role-confirmed by the caller) as :class:`PaneRef` values.
    :param reap: ``async`` teardown of one pane. May return ``False`` to report
        that it spared the pane after all (it re-checked under a lock).
    :param assess: ``async`` pane assessment (see :class:`PaneAssessment`).
    :param is_busy: Legacy ``async`` boolean predicate, used when *assess* is
        not given.
    :param confirm_reap: Optional ``async`` deep check run right before
        teardown (turn probe, server's pending prompts). Also used to reconcile
        panes held only by a stale claim under the ``veto``/``shadow`` policies.
    :param idle_timeout_s: Idle window before reaping. ``None`` resolves the env
        knob; ``<= 0`` disables reaping.
    :param reaper_interval_s: Seconds between scans.
    :param max_turn_s: Ceiling on CHILDREN/TURN_PROBE holds; ``None`` resolves
        the env knob.
    :param approval_max_s: Ceiling on AWAITING_HUMAN/SERVER_CHECK holds from the
        deep check; ``None`` resolves the env knob.
    """

    def __init__(
        self,
        *,
        list_native_panes: Callable[[], list[PaneRef]],
        reap: Callable[[PaneRef], Awaitable[bool | None]],
        assess: Callable[[PaneRef], Awaitable[PaneAssessment]] | None = None,
        is_busy: Callable[[PaneRef], Awaitable[bool]] | None = None,
        confirm_reap: Callable[[PaneRef], Awaitable[ConfirmVerdict]] | None = None,
        idle_timeout_s: float | None = None,
        reaper_interval_s: float = _DEFAULT_REAPER_INTERVAL_S,
        max_turn_s: float | None = None,
        approval_max_s: float | None = None,
    ) -> None:
        if assess is None and is_busy is None:
            raise TypeError("NativePaneReaper needs assess= or is_busy=")
        self._list_native_panes = list_native_panes
        self._assess_fn = assess
        self._is_busy_fn = is_busy
        self._confirm_reap = confirm_reap
        self._reap = reap
        self._idle_timeout_s = (
            idle_timeout_s if idle_timeout_s is not None else resolve_native_pane_idle_timeout_s()
        )
        self._reaper_interval_s = reaper_interval_s
        self._max_turn_s = max_turn_s if max_turn_s is not None else resolve_max_turn_s()
        self._approval_max_s = (
            approval_max_s if approval_max_s is not None else resolve_approval_max_s()
        )
        # Monotonic time last observed busy (or, for an idle pane, the time of
        # its last evidence of work).
        # custom-lint: disable-next=session-status-single-source -- the reaper's own idle clock
        self._last_busy_at: dict[str, float] = {}
        # {reason: monotonic time it started holding}.
        self._reason_since: dict[str, dict[SpareReason, float]] = {}
        self._last_reasons: dict[str, frozenset[SpareReason]] = {}
        # Every reason that held the row while the reaper watched it, for the
        # reap line (the last scan before a reap has none by definition).
        self._reason_history: dict[str, set[SpareReason]] = {}
        # Deep-check spares: (reason, first spared at).
        self._confirm_spare_since: dict[str, tuple[str, float]] = {}
        self._unknown_since: dict[str, float] = {}
        self._reconciled_at: dict[str, float] = {}
        self._output_held_since: dict[str, float] = {}
        self._warned: dict[tuple[str, str], float] = {}
        self._summary: Counter[str] = Counter()
        self._scans = 0
        self._task: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        """Spawn the reaper loop (idempotent)."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._reap_loop(), name="native-pane-idle-reaper")
        _logger.info(
            "native pane reaper started (idle_timeout=%ss, interval=%ss%s)",
            self._idle_timeout_s,
            self._reaper_interval_s,
            "; DISABLED" if self._idle_timeout_s <= 0 else "",
        )

    async def shutdown(self) -> None:
        """Cancel the reaper loop."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._started = False

    # ── assessment ───────────────────────────────────────────────────────

    async def assess(self, pane: PaneRef) -> PaneAssessment:
        """Assess one pane (no ceilings, no state change)."""
        if self._assess_fn is not None:
            return await self._assess_fn(pane)
        assert self._is_busy_fn is not None
        busy = await self._is_busy_fn(pane)
        return PaneAssessment(frozenset({SpareReason.BUSY}) if busy else frozenset())

    async def _is_busy(self, pane: PaneRef) -> bool:
        """The legacy boolean verdict for *pane*."""
        return (await self.assess(pane)).busy

    def _ceiling(self, reason: SpareReason | str) -> float | None:
        if reason in (SpareReason.CHILDREN, SpareReason.TURN_PROBE):
            return self._max_turn_s
        if reason in (SpareReason.AWAITING_HUMAN, SpareReason.SERVER_CHECK):
            return self._approval_max_s
        return None

    def _effective(
        self, pane: PaneRef, assessment: PaneAssessment, now: float, *, track: bool
    ) -> PaneAssessment:
        """Drop hard reasons that have held a pane past their ceiling."""
        key = pane.conversation_id
        since = self._reason_since.setdefault(key, {}) if track else self._reason_since.get(key)
        since = since if since is not None else {}
        if track:
            for gone in set(since) - assessment.reasons:
                del since[gone]
            for reason in assessment.reasons:
                since.setdefault(reason, now)
        kept: set[SpareReason] = set()
        for reason in assessment.reasons:
            # Cheap-scan ceilings cover only the unbounded-by-source reasons;
            # human waits are bounded where they are read.
            ceiling = (
                self._max_turn_s
                if reason in (SpareReason.CHILDREN, SpareReason.TURN_PROBE)
                else None
            )
            if ceiling is not None and now - since.get(reason, now) > ceiling:
                self._warn_once(
                    pane,
                    f"ceiling:{reason}",
                    "native pane %s held by %s for over %.0fs; no longer honoring it",
                    pane.conversation_id,
                    reason.value,
                    ceiling,
                )
                continue
            kept.add(reason)
        if kept == set(assessment.reasons):
            return assessment
        return PaneAssessment(frozenset(kept), assessment.evidence_age_s, assessment.facts)

    # ── idle clock ───────────────────────────────────────────────────────

    def _classify(
        self,
        now: float,
        panes: list[PaneRef],
        busy_convs: set[str],
        evidence_age_s: Mapping[str, float] | None = None,
    ) -> list[PaneRef]:
        """Pure idle-clock decision: which panes are reapable right now.

        Given the conversation ids observed busy this scan, maintain each idle
        clock and return the panes idle for at least ``idle_timeout_s``. A busy
        pane re-arms its clock; a newly-observed idle pane gets one full window
        of grace before it is eligible. For an armed idle pane, *evidence_age_s*
        moves the clock forward to its last evidence of work (``now - age``),
        never back. No I/O, so it is unit-testable with an injected ``now``.
        """
        live: set[str] = set()
        reapable: list[PaneRef] = []
        ages = evidence_age_s or {}
        for pane in panes:
            key = pane.conversation_id
            live.add(key)
            if key in busy_convs:
                self._last_busy_at[key] = now
                continue
            if key not in self._last_busy_at:
                self._last_busy_at[key] = now
                continue
            if self._window_elapsed(key, now, ages.get(key)):
                reapable.append(pane)
        # Forget panes that are gone so the clock maps can't grow.
        for gone in self._last_busy_at.keys() - live:
            self._forget(gone)
        return reapable

    def _window_elapsed(self, key: str, now: float, age: float | None) -> bool:
        last = self._last_busy_at.get(key, now)
        if age is not None:
            last = max(last, now - age)
            self._last_busy_at[key] = last
        return now - last >= self._idle_timeout_s

    def _forget(self, key: str) -> None:
        for mapping in (
            self._last_busy_at,
            self._reason_since,
            self._last_reasons,
            self._reason_history,
            self._confirm_spare_since,
            self._unknown_since,
            self._reconciled_at,
            self._output_held_since,
        ):
            mapping.pop(key, None)
        for warned in [k for k in self._warned if k[0] == key]:
            del self._warned[warned]

    # ── scan ─────────────────────────────────────────────────────────────

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._reaper_interval_s)
            except asyncio.CancelledError:
                return
            # ``<= 0`` disables reaping entirely (mirrors the SDK reaper guard).
            if self._idle_timeout_s <= 0:
                continue
            try:
                await self._scan_once()
            except Exception:  # never let a scan error kill the loop
                _logger.exception("native pane reaper: scan failed")

    async def _assess_for_scan(self, pane: PaneRef) -> PaneAssessment:
        try:
            return await self.assess(pane)
        except Exception:
            # An unreadable pane is spared, never reaped on missing evidence.
            _logger.exception(
                "native pane reaper: assessment failed for conversation %s",
                pane.conversation_id,
            )
            return PaneAssessment(frozenset({SpareReason.BUSY}))

    async def _scan_once(self) -> None:
        panes = self._list_native_panes()
        now = time.monotonic()
        self._scans += 1
        assessments: dict[str, PaneAssessment] = {}
        for pane in panes:
            raw = await self._assess_for_scan(pane)
            assessment = self._effective(pane, raw, now, track=True)
            assessments[pane.conversation_id] = assessment
            self._observe(pane, assessment, now)
        busy_keys = {key for key, a in assessments.items() if a.busy}
        for key in busy_keys:
            # Deep-check holds are bounded while they alone hold a pane; any
            # other evidence of work ends that streak.
            self._confirm_spare_since.pop(key, None)
            self._unknown_since.pop(key, None)
        ages = {
            key: a.evidence_age_s
            for key, a in assessments.items()
            if not a.busy and a.evidence_age_s is not None
        }
        await self._reconcile_claim_held(panes, assessments, now)
        for pane in self._classify(now, panes, busy_keys, ages):
            await self._consider_reap(pane)
        self._maybe_log_summary(panes)

    async def _reconcile_claim_held(
        self, panes: list[PaneRef], assessments: Mapping[str, PaneAssessment], now: float
    ) -> None:
        """Let the deep check refute claims that alone hold silent panes."""
        if self._confirm_reap is None:
            return
        for pane in panes:
            key = pane.conversation_id
            assessment = assessments[key]
            if assessment.reasons != {SpareReason.STATUS_CLAIM}:
                continue
            silent = assessment.facts.get("claim_silent_s")
            if not isinstance(silent, (int, float)) or silent < self._idle_timeout_s:
                continue
            last = self._reconciled_at.get(key)
            if last is not None and now - last < self._idle_timeout_s:
                continue
            self._reconciled_at[key] = now
            try:
                await self._confirm_reap(pane)
            except Exception:
                _logger.exception(
                    "native pane reaper: reconcile failed for %s", pane.conversation_id
                )

    async def _consider_reap(self, pane: PaneRef) -> None:
        conv = pane.conversation_id
        key = pane.conversation_id
        # Re-check immediately before teardown: selection happened with
        # possibly-stale signals, and a turn / client / autonomous run may have
        # started since (the select→reap race). Re-arm and skip if so.
        now = time.monotonic()
        recheck = self._effective(pane, await self._assess_for_scan(pane), now, track=False)
        if recheck.busy or not self._window_elapsed(key, now, recheck.evidence_age_s):
            if recheck.busy:
                self._last_busy_at[key] = now
            return
        if self._confirm_reap is not None:
            try:
                verdict = await self._confirm_reap(pane)
            except Exception:
                _logger.exception("native pane reaper: pre-reap check failed for %s", conv)
                self._last_busy_at[key] = time.monotonic()
                return
            if not verdict.proceed and not self._override_spare(pane, verdict, now):
                self._last_busy_at[key] = time.monotonic()
                return
            # The deep check awaited I/O; a turn or client may have arrived since.
            final = self._effective(pane, await self._assess_for_scan(pane), now, track=False)
            if final.busy:
                self._last_busy_at[key] = time.monotonic()
                return
        history = sorted(self._summary_reasons_for(key))
        try:
            reaped = await self._reap(pane)
        except Exception:
            _logger.exception("native pane reaper: reap failed for conversation %s", conv)
            # Drop the clock entry: the grace window re-arms next scan instead
            # of permanently skipping the conversation.
            self._forget(key)
            return
        if reaped is False:
            self._last_busy_at[key] = time.monotonic()
            self._summary["spared:teardown"] += 1
            _logger.info("native pane teardown for %s did not close it; re-arming", conv)
            return
        _logger.info(
            "reaped idle native pane for conversation %s (%s; idle > %.0fs)",
            conv,
            pane.terminal_name,
            self._idle_timeout_s,
            extra=debug_event(
                "native_pane_reaped",
                session_id=conv,
                terminal_name=pane.terminal_name,
                idle_timeout_s=self._idle_timeout_s,
                recent_reasons=",".join(history),
            ),
        )
        self._forget(key)
        self._summary["reaped"] += 1

    def _override_spare(self, pane: PaneRef, verdict: ConfirmVerdict, now: float) -> bool:
        """Whether a deep-check spare has outlived its bound, so the reap proceeds."""
        conv = pane.conversation_id
        key = pane.conversation_id
        if verdict.unknown:
            first = self._unknown_since.setdefault(key, now)
            if now - first < self._idle_timeout_s:
                self._log_confirm_spare(pane, verdict)
                return False
            _logger.warning(
                "native pane %s: probe could not confirm a stale running claim for a "
                "second idle window; reaping on local evidence",
                conv,
                extra=_pane_event("native_pane_reap_unverified", conv, verdict.facts),
            )
            return True
        self._unknown_since.pop(key, None)
        started = now - verdict.held_s if verdict.held_s is not None else now
        reason, since = self._confirm_spare_since.get(key, (verdict.reason, started))
        if reason != verdict.reason:
            since = started
        since = min(since, started)
        self._confirm_spare_since[key] = (verdict.reason, since)
        ceiling = self._ceiling(verdict.reason)
        if ceiling is not None and now - since >= ceiling:
            _logger.warning(
                "native pane %s held by %s for over %.0fs; reaping",
                conv,
                verdict.reason,
                ceiling,
                extra=_pane_event("native_pane_hold_expired", conv, reason=reason),
            )
            return True
        self._log_confirm_spare(pane, verdict)
        return False

    # ── observability ────────────────────────────────────────────────────

    def _log_confirm_spare(self, pane: PaneRef, verdict: ConfirmVerdict) -> None:
        self._summary[f"confirm:{verdict.reason or 'unknown'}"] += 1
        _logger.info(
            "native pane %s spared at the pre-reap check: %s",
            pane.conversation_id,
            verdict.reason or "probe unknown",
            extra=_pane_event(
                "native_pane_spared",
                pane.conversation_id,
                verdict.facts,
                terminal_name=pane.terminal_name,
                reasons=verdict.reason,
                stage="confirm",
                unknown=verdict.unknown,
            ),
        )

    def _summary_reasons_for(self, key: str) -> frozenset[SpareReason]:
        return frozenset(self._reason_history.get(key, ()))

    def _observe(self, pane: PaneRef, assessment: PaneAssessment, now: float) -> None:
        """Log reason transitions and long holds for one assessed pane."""
        conv = pane.conversation_id
        key = pane.conversation_id
        reasons = assessment.reasons
        for reason in reasons:
            self._summary[reason.value] += 1
        self._reason_history.setdefault(key, set()).update(reasons)
        previous = self._last_reasons.get(key)
        if previous != reasons:
            self._last_reasons[key] = reasons
            if reasons or previous:
                _logger.info(
                    "native pane %s spare reasons: %s",
                    conv,
                    ",".join(sorted(r.value for r in reasons)) or "none",
                    extra=_pane_event(
                        "native_pane_spared",
                        conv,
                        assessment.facts,
                        terminal_name=pane.terminal_name,
                        reasons=",".join(sorted(r.value for r in reasons)),
                        stage="scan",
                        evidence_age_s=assessment.evidence_age_s,
                    ),
                )
        if assessment.facts.get("watcher_alive") is False:
            self._warn_once(
                pane,
                "watcher_dead",
                "native pane %s: its status watcher has stopped; pane status edges "
                "no longer arrive",
                conv,
            )
        since = self._reason_since.get(key, {})
        if reasons == {SpareReason.CLIENT_ATTACHED}:
            held = now - since.get(SpareReason.CLIENT_ATTACHED, now)
            if held > self._idle_timeout_s:
                self._warn_once(
                    pane,
                    "client_attached",
                    "native pane %s kept alive only by an attached tmux client for %.0fs",
                    conv,
                    held,
                )
        for reason in (
            SpareReason.RUNNER_TURN,
            SpareReason.TOOL_CALL,
            SpareReason.CHILDREN,
            SpareReason.AWAITING_HUMAN,
        ):
            held = now - since.get(reason, now)
            if reason in reasons and held > self._idle_timeout_s:
                self._warn_every(
                    pane,
                    f"long:{reason}",
                    3600.0,
                    now,
                    "native pane %s held by %s for %.0fs",
                    conv,
                    reason.value,
                    held,
                )
        output_only = not reasons and assessment.busy
        if output_only and assessment.facts.get("status") != "running":
            first = self._output_held_since.setdefault(key, now)
            if now - first > _IDLE_REPAINT_WINDOWS * self._idle_timeout_s:
                self._warn_once(
                    pane,
                    "idle_repaint",
                    "native pane %s keeps printing while its status is %s; "
                    "the TUI may repaint while idle",
                    conv,
                    assessment.facts.get("status"),
                )
        else:
            self._output_held_since.pop(key, None)

    def _warn_once(self, pane: PaneRef, kind: str, message: str, *args: object) -> None:
        warned = (pane.conversation_id, kind)
        if warned in self._warned:
            return
        self._warned[warned] = time.monotonic()
        _logger.warning(
            message,
            *args,
            extra=_pane_event("native_pane_warning", pane.conversation_id, kind=kind),
        )

    def _warn_every(
        self, pane: PaneRef, kind: str, period_s: float, now: float, message: str, *args: object
    ) -> None:
        warned = (pane.conversation_id, kind)
        last = self._warned.get(warned)
        if last is not None and now - last < period_s:
            return
        self._warned[warned] = now
        _logger.warning(
            message,
            *args,
            extra=_pane_event("native_pane_warning", pane.conversation_id, kind=kind),
        )

    def _maybe_log_summary(self, rows: list[PaneRef]) -> None:
        if self._scans % _SUMMARY_EVERY_SCANS:
            return
        counts = dict(self._summary)
        self._summary.clear()
        _logger.info(
            "native pane reaper summary: panes=%d %s",
            len(rows),
            " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no spares",
            extra=_pane_event(
                "native_pane_reaper_summary",
                None,
                {k.replace(":", "_"): v for k, v in counts.items()},
                panes=len(rows),
            ),
        )

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Reaper state for diagnostics: reasons and idle clock per pane."""
        now = time.monotonic()
        return {
            key: {
                "reasons": sorted(r.value for r in self._last_reasons.get(key, frozenset())),
                "idle_for_s": now - last,
            }
            for key, last in self._last_busy_at.items()
        }
