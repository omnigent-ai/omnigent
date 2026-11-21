"""Archive inactive sessions after a configured retention period.

The policy defaults to disabled. Inactivity is the persisted
``conversations.updated_at`` timestamp. A run archives only: it sets the
existing archive flag, stamps ``omnigent.archived_at`` and
``omnigent.archived_by=retention``, and leaves the transcript, grants, and
other labels in place so a normal unarchive still works.

Busy sessions (running work, waiting on the user, or an outstanding approval)
are never archived. Pinned, shared, and project-filed sessions are protected
when those rules are on. A session labeled ``omnigent.retain``, or with any
configured protect-label key, is always kept. The rules are returned with the
policy before it is enabled.

Repeated runs are idempotent. A sweep lease keeps two replicas from applying
the same policy at once; the per-session claim is a conditional false→true
transition, so a lease miss still cannot archive a session twice or reset its
archive clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlPreference, current_workspace_id, workspace_scope
from omnigent.db.utils import now_epoch, run_write_transaction
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.conversation_store import (
    ARCHIVED_AT_LABEL_KEY,
    ARCHIVED_BY_LABEL_KEY,
    PROJECT_LABEL_KEY,
    RetentionCandidate,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_logger = logging.getLogger(__name__)

# Explicit opt-out. Users set it through the normal session-label API.
RETAIN_LABEL_KEY = "omnigent.retain"
_PREFERENCE_KEY = "session_archive_retention"
_SECONDS_PER_DAY = 24 * 60 * 60
_PAGE_SIZE = 200
_RESPONSE_CAP = 500
_STORED_SKIP_CAP = 100
_MAX_LABEL_KEYS = 32
_MAX_LABEL_KEY_LEN = 128
_MIN_INACTIVE_DAYS = 1
_MAX_INACTIVE_DAYS = 3650
_SWEEP_INTERVAL_S = 15 * 60

_REASON_ORDER = (
    "active_work",
    "pending_user_input",
    "pinned",
    "shared",
    "project",
    "labeled",
)
_BUSY_WORK = frozenset({"running"})
_PENDING_INPUT = frozenset({"waiting"})


@dataclass(frozen=True)
class SessionArchiveRetentionPolicy:
    """Stored retention policy for one user in one workspace.

    :param enabled: When false, sweeps and manual runs archive nothing.
    :param inactive_days: Days without activity before a session is eligible.
        ``None`` until a period is configured.
    :param protect_pinned: Keep sessions the owner has pinned.
    :param protect_shared: Keep sessions that have a grant besides the owner.
    :param protect_project: Keep sessions filed in a project.
    :param protect_label_keys: Extra label keys that keep a session.
    :param last_run: Audit of the latest apply, or ``None`` if it has never run.
    :param sweep_lease_until: Epoch seconds until which a replica owns the sweep.
    """

    enabled: bool = False
    inactive_days: int | None = None
    protect_pinned: bool = True
    protect_shared: bool = True
    protect_project: bool = True
    protect_label_keys: tuple[str, ...] = ()
    last_run: dict[str, Any] | None = None
    sweep_lease_until: int = 0


@dataclass(frozen=True)
class RetentionDecision:
    """How one inactive session would be treated.

    :param candidate: The session the rules were applied to.
    :param reasons: Empty when the session would be archived. Otherwise the
        stable skip reasons, in :data:`_REASON_ORDER`.
    """

    candidate: RetentionCandidate
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RetentionApplyResult:
    """Outcome of one policy run.

    :param applied: False when the policy is disabled or has no period.
    :param archived_ids: Sessions this call transitioned to archived.
    :param skipped: Inactive sessions left in place, with reasons.
    :param truncated: True when the response omitted rows past the cap.
    :param ran_at: Epoch seconds of the run, or ``None`` when nothing ran.
    """

    applied: bool
    archived_ids: tuple[str, ...]
    skipped: tuple[RetentionDecision, ...]
    truncated: bool
    ran_at: int | None


def default_policy() -> SessionArchiveRetentionPolicy:
    """Return the disabled policy used when nothing has been saved."""
    return SessionArchiveRetentionPolicy()


def validate_protect_label_keys(keys: list[str]) -> tuple[str, ...]:
    """Normalize configured protect-label keys.

    :param keys: Raw keys from a client.
    :returns: Sorted unique keys.
    :raises ValueError: When a key is empty, too long, reserved, or too many
        keys were sent.
    """
    if len(keys) > _MAX_LABEL_KEYS:
        raise ValueError(f"protect_label_keys accepts at most {_MAX_LABEL_KEYS} keys")
    normalized: list[str] = []
    for key in keys:
        cleaned = key.strip()
        if not cleaned or any(char.isspace() for char in cleaned):
            raise ValueError("protect_label_keys entries must be non-empty and contain no spaces")
        if len(cleaned) > _MAX_LABEL_KEY_LEN:
            raise ValueError(f"protect label keys must be at most {_MAX_LABEL_KEY_LEN} characters")
        if cleaned in {ARCHIVED_AT_LABEL_KEY, ARCHIVED_BY_LABEL_KEY}:
            raise ValueError(f"label {cleaned!r} is server-internal and cannot protect a session")
        normalized.append(cleaned)
    return tuple(sorted(dict.fromkeys(normalized)))


def protection_rules(policy: SessionArchiveRetentionPolicy) -> dict[str, Any]:
    """Describe the rules a caller will get before the policy is enabled.

    :param policy: The saved or proposed policy.
    :returns: A JSON-ready description of the inactivity basis and protections.
    """
    label_keys = [RETAIN_LABEL_KEY, *policy.protect_label_keys]
    # ``omnigent.retain`` is always first and is not duplicated when configured.
    seen: dict[str, None] = {}
    for key in label_keys:
        seen.setdefault(key, None)
    return {
        "inactivity_basis": "updated_at",
        "always_exclude": ["active_work", "pending_user_input"],
        "protections": [
            {
                "kind": "pinned",
                "enabled": policy.protect_pinned,
                "label_keys": [],
            },
            {
                "kind": "shared",
                "enabled": policy.protect_shared,
                "label_keys": [],
            },
            {
                "kind": "project",
                "enabled": policy.protect_project,
                "label_keys": [],
            },
            {
                "kind": "labeled",
                "enabled": True,
                "label_keys": list(seen),
            },
        ],
    }


def classify_candidate(
    candidate: RetentionCandidate,
    policy: SessionArchiveRetentionPolicy,
    *,
    memory_pending: bool = False,
) -> tuple[str, ...]:
    """Return the skip reasons for one inactive session.

    An empty tuple means the session would be archived. Reason order is
    stable so a preview and a later run describe the same session the same way.

    :param candidate: Inactive session facts from the store.
    :param policy: Protections to apply. Busy sessions are excluded regardless.
    :param memory_pending: True when this process holds an unconsumed composer
        message or approval for the session or one of its children.
    :returns: Skip reasons in :data:`_REASON_ORDER`.
    """
    reasons: list[str] = []
    statuses = set(candidate.tree_live_statuses)
    if statuses & _BUSY_WORK:
        reasons.append("active_work")
    if memory_pending or statuses & _PENDING_INPUT or candidate.tree_pending_elicitation_count > 0:
        reasons.append("pending_user_input")
    if policy.protect_pinned and candidate.pinned:
        reasons.append("pinned")
    if policy.protect_shared and candidate.shared:
        reasons.append("shared")
    label_keys = set(candidate.label_keys)
    if policy.protect_project and (
        candidate.project_id is not None or PROJECT_LABEL_KEY in label_keys
    ):
        reasons.append("project")
    protected_labels = {RETAIN_LABEL_KEY, *policy.protect_label_keys}
    if label_keys & protected_labels:
        reasons.append("labeled")
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _memory_pending(candidate: RetentionCandidate) -> bool:
    """Whether this process has pending composer input or an approval prompt."""
    from omnigent.runtime import pending_elicitations, pending_inputs

    for session_id in candidate.tree_ids:
        if (
            pending_inputs.has_pending(session_id)
            or pending_elicitations.count_for(session_id) > 0
        ):
            return True
    return False


def _decode_policy(raw: str | None) -> SessionArchiveRetentionPolicy:
    """Parse a stored policy. Corrupt or partial JSON falls back to disabled."""
    if not raw:
        return default_policy()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("ignoring unreadable session archive retention policy")
        return default_policy()
    if not isinstance(payload, dict):
        return default_policy()
    inactive_days = payload.get("inactive_days")
    if inactive_days is not None and (
        isinstance(inactive_days, bool)
        or not isinstance(inactive_days, int)
        or not _MIN_INACTIVE_DAYS <= inactive_days <= _MAX_INACTIVE_DAYS
    ):
        inactive_days = None
    label_keys = payload.get("protect_label_keys") or []
    if not isinstance(label_keys, list):
        label_keys = []
    try:
        normalized_keys = validate_protect_label_keys(
            [key for key in label_keys if isinstance(key, str)]
        )
    except ValueError:
        normalized_keys = ()
    last_run = payload.get("last_run")
    if not isinstance(last_run, dict):
        last_run = None
    lease = payload.get("sweep_lease_until")
    if isinstance(lease, bool) or not isinstance(lease, int):
        lease = 0
    enabled = bool(payload.get("enabled")) and inactive_days is not None
    return SessionArchiveRetentionPolicy(
        enabled=enabled,
        inactive_days=inactive_days,
        protect_pinned=bool(payload.get("protect_pinned", True)),
        protect_shared=bool(payload.get("protect_shared", True)),
        protect_project=bool(payload.get("protect_project", True)),
        protect_label_keys=normalized_keys,
        last_run=last_run,
        sweep_lease_until=lease,
    )


def _encode_policy(policy: SessionArchiveRetentionPolicy) -> str:
    """Serialize a policy for the preferences table."""
    return json.dumps(
        {
            "enabled": policy.enabled,
            "inactive_days": policy.inactive_days,
            "protect_pinned": policy.protect_pinned,
            "protect_shared": policy.protect_shared,
            "protect_project": policy.protect_project,
            "protect_label_keys": list(policy.protect_label_keys),
            "last_run": policy.last_run,
            "sweep_lease_until": policy.sweep_lease_until,
        },
        separators=(",", ":"),
    )


class SessionArchiveRetentionService:
    """Read and apply one user's inactive-session archive policy."""

    def __init__(
        self,
        store: SqlAlchemyConversationStore,
        *,
        enforce_ownership: bool,
        clock: Callable[[], int] = now_epoch,
    ) -> None:
        self.store = store
        self._enforce_ownership = enforce_ownership
        self._clock = clock

    def get_policy(self, user_id: str | None) -> SessionArchiveRetentionPolicy:
        """Return the caller's policy, or the disabled default when unset."""
        return _decode_policy(self._read_preference(self._user(user_id)))

    def save_policy(
        self,
        user_id: str | None,
        policy: SessionArchiveRetentionPolicy,
    ) -> SessionArchiveRetentionPolicy:
        """Replace the caller's configurable fields and keep the last-run audit."""
        owner = self._user(user_id)
        current = self.get_policy(owner)
        stored = SessionArchiveRetentionPolicy(
            enabled=policy.enabled and policy.inactive_days is not None,
            inactive_days=policy.inactive_days,
            protect_pinned=policy.protect_pinned,
            protect_shared=policy.protect_shared,
            protect_project=policy.protect_project,
            protect_label_keys=policy.protect_label_keys,
            last_run=current.last_run,
            sweep_lease_until=current.sweep_lease_until,
        )
        self._write_preference(owner, stored)
        return stored

    def preview(
        self,
        user_id: str | None,
        *,
        inactive_days: int | None = None,
        now: int | None = None,
    ) -> tuple[SessionArchiveRetentionPolicy, int | None, tuple[RetentionDecision, ...], bool]:
        """Show which sessions a run would archive. Does not write.

        :param user_id: Policy owner. ``None`` is the single-user local sentinel.
        :param inactive_days: Optional period override that is not saved.
        :param now: Epoch seconds. Defaults to the service clock.
        :returns: ``(policy, cutoff, decisions, truncated)``. ``cutoff`` is
            ``None`` when no period is configured.
        """
        owner = self._user(user_id)
        policy = self.get_policy(owner)
        period = inactive_days if inactive_days is not None else policy.inactive_days
        if period is None:
            return policy, None, (), False
        evaluated = SessionArchiveRetentionPolicy(
            enabled=policy.enabled,
            inactive_days=period,
            protect_pinned=policy.protect_pinned,
            protect_shared=policy.protect_shared,
            protect_project=policy.protect_project,
            protect_label_keys=policy.protect_label_keys,
        )
        reference = self._clock() if now is None else now
        cutoff = reference - period * _SECONDS_PER_DAY
        decisions, truncated = self._evaluate(owner, evaluated, cutoff)
        return evaluated, cutoff, decisions, truncated

    def apply(self, user_id: str | None, *, now: int | None = None) -> RetentionApplyResult:
        """Archive eligible sessions when the policy is enabled.

        Disabled policies and policies without a period return without writing.
        """
        owner = self._user(user_id)
        policy = self.get_policy(owner)
        reference = self._clock() if now is None else now
        if not policy.enabled or policy.inactive_days is None:
            return RetentionApplyResult(
                applied=False,
                archived_ids=(),
                skipped=(),
                truncated=False,
                ran_at=None,
            )
        cutoff = reference - policy.inactive_days * _SECONDS_PER_DAY
        archived: list[str] = []
        archived_count = 0
        skipped: list[RetentionDecision] = []
        truncated = False
        cursor: tuple[int, str] | None = None
        while True:
            page = self.store.list_retention_candidates(
                owner_user_id=owner,
                cutoff=cutoff,
                enforce_ownership=self._enforce_ownership,
                limit=_PAGE_SIZE,
                after=cursor,
            )
            if not page:
                break
            for candidate in page:
                decision = RetentionDecision(
                    candidate=candidate,
                    reasons=classify_candidate(
                        candidate,
                        policy,
                        memory_pending=_memory_pending(candidate),
                    ),
                )
                if decision.reasons:
                    if len(skipped) < _RESPONSE_CAP:
                        skipped.append(decision)
                    else:
                        truncated = True
                    continue
                claimed = self.store.claim_retention_archive(
                    candidate.id,
                    cutoff=cutoff,
                    now=reference,
                )
                if claimed:
                    archived_count += 1
                    if len(archived) < _RESPONSE_CAP:
                        archived.append(candidate.id)
                    else:
                        truncated = True
                elif len(skipped) < _RESPONSE_CAP:
                    skipped.append(
                        RetentionDecision(
                            candidate=candidate,
                            reasons=("no_longer_eligible",),
                        )
                    )
                else:
                    truncated = True
            if len(page) < _PAGE_SIZE:
                break
            last = page[-1]
            cursor = (last.updated_at, last.id)
        if archived_count:
            _logger.info(
                "session archive retention archived %s session(s) for %s",
                archived_count,
                owner,
            )
        self._record_run(owner, policy, reference, archived, skipped, truncated)
        return RetentionApplyResult(
            applied=True,
            archived_ids=tuple(archived),
            skipped=tuple(skipped),
            truncated=truncated,
            ran_at=reference,
        )

    def claim_sweep_lease(self, user_id: str, now: int, lease_s: int) -> bool:
        """Take the sweep lease for one saved policy. False when another replica holds it."""
        owner = self._user(user_id)

        def write(session: Session) -> bool:
            row = self._locked_preference(session, owner)
            if row is None:
                return False
            policy = _decode_policy(row.value)
            if not policy.enabled or policy.sweep_lease_until > now:
                return False
            row.value = _encode_policy(
                SessionArchiveRetentionPolicy(
                    enabled=policy.enabled,
                    inactive_days=policy.inactive_days,
                    protect_pinned=policy.protect_pinned,
                    protect_shared=policy.protect_shared,
                    protect_project=policy.protect_project,
                    protect_label_keys=policy.protect_label_keys,
                    last_run=policy.last_run,
                    sweep_lease_until=now + lease_s,
                )
            )
            return True

        return run_write_transaction(
            self.store._session_immediate,
            "claim_session_archive_retention_lease",
            write,
        )

    def list_enabled_policies(self) -> list[tuple[int, str, SessionArchiveRetentionPolicy]]:
        """Return enabled policies across workspaces for the sweep."""
        with self.store._session("list_session_archive_retention_policies") as session:
            rows = session.execute(
                select(
                    SqlPreference.workspace_id,
                    SqlPreference.user_id,
                    SqlPreference.value,
                ).where(SqlPreference.key == _PREFERENCE_KEY)
            ).all()
        enabled: list[tuple[int, str, SessionArchiveRetentionPolicy]] = []
        for workspace_id, user_id, raw in rows:
            policy = _decode_policy(raw)
            if policy.enabled and policy.inactive_days is not None:
                enabled.append((workspace_id, user_id, policy))
        return enabled

    def _evaluate(
        self,
        owner: str,
        policy: SessionArchiveRetentionPolicy,
        cutoff: int,
    ) -> tuple[tuple[RetentionDecision, ...], bool]:
        decisions: list[RetentionDecision] = []
        cursor: tuple[int, str] | None = None
        truncated = False
        while True:
            page = self.store.list_retention_candidates(
                owner_user_id=owner,
                cutoff=cutoff,
                enforce_ownership=self._enforce_ownership,
                limit=_PAGE_SIZE,
                after=cursor,
            )
            if not page:
                break
            for candidate in page:
                if len(decisions) >= _RESPONSE_CAP:
                    truncated = True
                    return tuple(decisions), truncated
                decisions.append(
                    RetentionDecision(
                        candidate=candidate,
                        reasons=classify_candidate(
                            candidate,
                            policy,
                            memory_pending=_memory_pending(candidate),
                        ),
                    )
                )
            if len(page) < _PAGE_SIZE:
                break
            last = page[-1]
            cursor = (last.updated_at, last.id)
        return tuple(decisions), truncated

    def _record_run(
        self,
        owner: str,
        policy: SessionArchiveRetentionPolicy,
        ran_at: int,
        archived: list[str],
        skipped: list[RetentionDecision],
        truncated: bool,
    ) -> None:
        current = self.get_policy(owner)
        last_run = {
            "ran_at": ran_at,
            "archived_session_ids": archived[:_STORED_SKIP_CAP],
            "skipped": [
                {
                    "id": decision.candidate.id,
                    "title": decision.candidate.title,
                    "updated_at": decision.candidate.updated_at,
                    "reasons": list(decision.reasons),
                }
                for decision in skipped[:_STORED_SKIP_CAP]
            ],
            "truncated": truncated
            or len(archived) > _STORED_SKIP_CAP
            or len(skipped) > _STORED_SKIP_CAP,
        }
        self._write_preference(
            owner,
            SessionArchiveRetentionPolicy(
                enabled=policy.enabled,
                inactive_days=policy.inactive_days,
                protect_pinned=policy.protect_pinned,
                protect_shared=policy.protect_shared,
                protect_project=policy.protect_project,
                protect_label_keys=policy.protect_label_keys,
                last_run=last_run,
                sweep_lease_until=current.sweep_lease_until,
            ),
        )

    def _user(self, user_id: str | None) -> str:
        return RESERVED_USER_LOCAL if user_id is None else user_id

    def _read_preference(self, user_id: str) -> str | None:
        with self.store._session("read_session_archive_retention") as session:
            return session.scalar(
                select(SqlPreference.value).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == user_id,
                    SqlPreference.key == _PREFERENCE_KEY,
                )
            )

    def _write_preference(self, user_id: str, policy: SessionArchiveRetentionPolicy) -> None:
        payload = _encode_policy(policy)

        def write(session: Session) -> None:
            row = self._locked_preference(session, user_id)
            if row is None:
                session.add(
                    SqlPreference(
                        workspace_id=current_workspace_id(),
                        user_id=user_id,
                        key=_PREFERENCE_KEY,
                        value=payload,
                    )
                )
            else:
                row.value = payload

        run_write_transaction(
            self.store._session_immediate,
            "save_session_archive_retention",
            write,
        )

    def _locked_preference(self, session: Session, user_id: str) -> SqlPreference | None:
        query = select(SqlPreference).where(
            SqlPreference.workspace_id == current_workspace_id(),
            SqlPreference.user_id == user_id,
            SqlPreference.key == _PREFERENCE_KEY,
        )
        if self.store._engine.dialect.name != "sqlite":
            query = query.with_for_update()
        return session.scalar(query)


class SessionArchiveRetentionSweeper:
    """Periodically apply every enabled retention policy.

    Each replica runs the loop. A per-policy lease makes one replica the
    writer for an interval; the archive claim stays safe if two replicas
    overlap anyway.
    """

    def __init__(
        self,
        service: SessionArchiveRetentionService,
        *,
        interval_s: int = _SWEEP_INTERVAL_S,
        clock: Callable[[], int] = now_epoch,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._service = service
        self._interval_s = interval_s
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the sweep loop. A second start while running does nothing."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="session-archive-retention")

    async def shutdown(self) -> None:
        """Cancel the sweep loop and wait for it to finish."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sweep_once(self) -> int:
        """Apply every enabled policy once. Returns how many sessions were archived."""
        policies = await asyncio.to_thread(self._service.list_enabled_policies)
        archived = 0
        now = self._clock()
        for workspace_id, user_id, _policy in policies:
            claimed = await asyncio.to_thread(
                self._claim_and_apply,
                workspace_id,
                user_id,
                now,
            )
            archived += claimed
        return archived

    def _claim_and_apply(self, workspace_id: int, user_id: str, now: int) -> int:
        with workspace_scope(workspace_id):
            if not self._service.claim_sweep_lease(user_id, now, self._interval_s):
                return 0
            result = self._service.apply(user_id, now=now)
            return len(result.archived_ids)

    async def _run(self) -> None:
        while True:
            try:
                archived = await self.sweep_once()
                if archived:
                    _logger.info(
                        "session archive retention sweep archived %s session(s)",
                        archived,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("session archive retention sweep failed; retrying later")
            await asyncio.sleep(self._interval_s)
