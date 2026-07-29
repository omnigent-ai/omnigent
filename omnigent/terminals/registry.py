"""Per-AP-process registry of conversation-scoped tmux terminals.

Replaces the legacy OSC 633 / pexpect ``TerminalManagerRegistry``
documented in ``designs/PERSISTENT_TERMINAL_RESEARCH.md``. The legacy
class kept ``dict[conv_id, TerminalManager]`` where ``TerminalManager``
owned ``Shell`` (pexpect-based) instances keyed by ``shell_name``.

Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.2, this rewrite swaps to:

- One terminal abstraction (``inner.terminal.TerminalInstance``) for
  the whole project — tmux-based.
- Three-level keying: ``(conversation_id, terminal_name, session_key)``.
  Multiple session keys per terminal name allow independent tmux
  sessions of the same configured terminal (e.g. ``bash:s1`` and
  ``bash:s2`` running in parallel).
- No idle reaper: terminals are explicit-launch only and the LLM is
  expected to ``sys_terminal_close`` when done. Omnigent shutdown still
  closes everything; per-conversation cleanup runs from the workflow's
  finally block.

The registry is constructed once at Omnigent startup
(``omnigent.runtime._globals.init``) and accessed via
``omnigent.runtime.get_terminal_registry()`` from tools and the
workflow.

**Locking.** A ``threading.Lock`` (not ``asyncio.Lock``) protects the
map. Tool invocations run on background threads via
``asyncio.to_thread`` (see ``runtime/workflow.py:1787``); each thread
spins up its own ``asyncio.run`` loop to drive the registry's async
methods. An ``asyncio.Lock`` would be bound to whichever loop created
it and would silently fail to synchronize concurrent invocations from
different threads. The registry lock is held only for short map
mutations. Terminal operations capture a snapshot, acquire the stable
instance-owned ``op_lock``, revalidate under the registry lock, then
perform tmux I/O while retaining only ``op_lock``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance, create_terminal_instance

logger = logging.getLogger(__name__)

# Per-instance close timeout. Bounds the cleanup window so a wedged
# tmux session can't block Omnigent shutdown or workflow finalization
# indefinitely. 5s mirrors the legacy ``_RELEASE_GRACE_S`` in
# ``runtime/harnesses/process_manager.py`` — long enough for a
# well-behaved tmux to flush and exit, short enough that cleanup
# doesn't stall.
_CLOSE_TIMEOUT_S = 5.0


def conversation_link_for_id(
    conversation_id: str,
    *,
    base_url: str | None = None,
) -> str:
    """
    Build the web UI link for a conversation.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :param base_url: Optional Omnigent server base URL, e.g.
        ``"http://127.0.0.1:6767"``. When provided, the returned
        link is absolute.
    :returns: Web UI link, e.g. ``"/c/conv_abc123"`` or
        ``"http://127.0.0.1:6767/c/conv_abc123"``.
    """
    if base_url is None or not base_url.strip():
        return f"/c/{quote(conversation_id, safe='')}"
    # Delegate to the shared builder so workspace-hosted servers get the
    # API→UI mount swap (``/api/2.0/omnigent`` → ``/omnigent``) and the
    # ``?o=<org>`` selector — keeping the terminal status-bar link in
    # lockstep with the CLI's ``Web UI:`` link instead of pointing at the
    # JSON API mount.
    from omnigent.conversation_browser import conversation_url

    return conversation_url(base_url.strip(), conversation_id)


@dataclass(frozen=True)
class TerminalListEntry:
    """One entry returned by :meth:`TerminalRegistry.list_for_conversation`.

    :param terminal_name: The terminal's spec name, e.g. ``"bash"``.
    :param session_key: The per-launch session key, e.g. ``"s1"``.
    :param instance: The :class:`TerminalInstance` at snapshot time.
        Callers may inspect metadata; tmux operations must use an atomic
        :class:`ResolveSnapshot` and revalidate ownership first.
    """

    terminal_name: str
    session_key: str
    instance: TerminalInstance


@dataclass(frozen=True)
class ResolveSnapshot:
    """Atomic terminal ownership snapshot used to fence one tmux operation."""

    entry: TerminalListEntry
    instance: TerminalInstance
    op_lock: threading.Lock
    owner_tuple: tuple[str, str, str]
    owner_generation: int


@dataclass(frozen=True)
class AmbiguousResourceId:
    """Fail-closed result for legacy registry entries sharing one public id."""

    session_id: str
    terminal_id: str
    code: str = "ambiguous_resource_id"


class AmbiguousResourceIdError(RuntimeError):
    """Raised when a mutating terminal operation receives an ambiguous id."""


ResolveResult = ResolveSnapshot | AmbiguousResourceId | None
_FencedResultT = TypeVar("_FencedResultT")
_FencedInvalidT = TypeVar("_FencedInvalidT")


class TerminalRegistry:
    """The single registry of per-conversation tmux terminal instances.

    All public methods take ``conversation_id`` as the first
    positional argument. State is keyed on the (conversation_id,
    terminal_name, session_key) tuple so distinct conversations can't
    see each other's terminals and the same terminal name can spawn
    multiple sessions in parallel.

    :class:`TerminalInstance` itself maintains a per-instance lock for
    serializing its own tmux ops; the registry lock is purely for
    map-level consistency.
    """

    def __init__(self, *, conversation_link_base_url: str | None = None) -> None:
        """
        Construct an empty registry.

        :param conversation_link_base_url: Optional Omnigent server base URL
            for terminal status links, e.g. ``"http://127.0.0.1:6767"``.
            ``None`` keeps links relative.
        """
        self._conversation_link_base_url = conversation_link_base_url
        # Two-level dict: conversation_id -> (name, key) -> instance.
        # Per-conversation maps make ``cleanup_conversation`` cheap
        # (one pop) and ``list_for_conversation`` direct.
        self._by_conversation: dict[str, dict[tuple[str, str], TerminalInstance]] = {}
        # Threading lock — see module docstring for the rationale.
        # It protects the map and every owner-generation transition.
        self._lock = threading.Lock()
        self._shutting_down = False

    def conversation_link_for_id(self, conversation_id: str) -> str:
        """
        Build a status-bar conversation link using this registry's base URL.

        :param conversation_id: Conversation/session id, e.g.
            ``"conv_abc123"``.
        :returns: Web UI link, e.g. ``"/c/conv_abc123"`` or
            ``"http://127.0.0.1:6767/c/conv_abc123"``.
        """
        return conversation_link_for_id(
            conversation_id,
            base_url=self._conversation_link_base_url,
        )

    async def launch(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        *,
        parent_os_env: OSEnvSpec | None = None,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
    ) -> TerminalInstance:
        """Launch a terminal session, or return the existing one.

        If the (conversation_id, terminal_name, session_key) triple
        already maps to a running instance, that instance is returned
        without spawning a new tmux session — matches the legacy
        Session ``_terminal_launch`` "already_running" semantics. The
        caller (the ``sys_terminal_launch`` tool) reports the result
        with the correct status by inspecting whether a new instance
        was created.

        :param conversation_id: The owning conversation id, e.g.
            ``"conv_abc123"``.
        :param terminal_name: The terminal's spec name from
            ``AgentSpec.terminals``, e.g. ``"bash"``.
        :param session_key: The per-launch session key, e.g.
            ``"s1"``. Free-form; different keys give independent
            tmux sessions of the same terminal.
        :param spec: The :class:`TerminalEnvSpec` for *terminal_name*.
            Caller looks this up from ``AgentSpec.terminals`` before
            calling.
        :param parent_os_env: The agent's primary
            :class:`OSEnvSpec`. Used by
            :func:`inner.terminal.create_terminal_instance` to
            resolve the terminal's effective os_env when the
            terminal spec doesn't declare one of its own.
        :param cwd_override: Optional cwd override, already vetted
            by the caller against the terminal spec's
            ``allow_cwd_override`` flag.
        :param sandbox_override: Optional sandbox override, already
            vetted against ``allow_sandbox_override``.
        :returns: The (possibly newly created) :class:`TerminalInstance`.
        :raises RuntimeError: If tmux isn't on PATH or the launch
            fails. Inner code surfaces a clear error; the caller
            tool wraps in a JSON error envelope.
        """
        key = (terminal_name, session_key)
        existing_snapshot: ResolveSnapshot | None = None
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("terminal registry is shutting down")
            self._assert_resource_id_available_locked(
                conversation_id,
                terminal_name,
                session_key,
            )
            existing = self._by_conversation.get(conversation_id, {}).get(key)
            if existing is not None:
                existing_snapshot = ResolveSnapshot(
                    entry=TerminalListEntry(terminal_name, session_key, existing),
                    instance=existing,
                    op_lock=existing.op_lock,
                    owner_tuple=(conversation_id, terminal_name, session_key),
                    owner_generation=existing.owner_generation,
                )
        if existing_snapshot is not None:
            if await asyncio.to_thread(
                self.run_fenced,
                existing_snapshot,
                existing_snapshot.instance.is_alive,
                invalid=False,
            ):
                return existing_snapshot.instance
            await self.close_snapshot(existing_snapshot)

        # Lock-free section: ``create_terminal_instance`` and
        # ``launch`` may take real time (tmux spawn). Holding the
        # registry lock across them would serialize all conversations'
        # terminal spawns globally. Instead we re-check after the
        # spawn completes.
        created = create_terminal_instance(
            terminal_name,
            session_key,
            spec,
            parent_os_env_spec=parent_os_env,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            conversation_link=self.conversation_link_for_id(conversation_id),
        )
        await created.instance.launch(cwd=created.cwd)
        if not await created.instance.is_alive():
            try:
                await asyncio.wait_for(created.instance.close(), timeout=_CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "Newly launched terminal close timed out for %s:%s in conv %s",
                    terminal_name,
                    session_key,
                    conversation_id,
                )
            raise RuntimeError(
                f"terminal {terminal_name}:{session_key} exited before it became available"
            )

        registration_error: RuntimeError | None = None
        with self._lock:
            if self._shutting_down:
                shutdown_started = True
                instance_to_close = created.instance
                winning_instance = created.instance
            else:
                shutdown_started = False
                try:
                    self._assert_resource_id_available_locked(
                        conversation_id,
                        terminal_name,
                        session_key,
                    )
                except RuntimeError as exc:
                    registration_error = exc
                    instance_to_close = created.instance
                    winning_instance = created.instance
                else:
                    slot = self._by_conversation.setdefault(conversation_id, {})
                    # Re-check: another concurrent launch for the same key may
                    # have raced ours. Take the second-arrival policy: close
                    # ours and return the racer's.
                    racer = slot.get(key)
                    if racer is not None and racer.running:
                        instance_to_close = created.instance
                        winning_instance = racer
                    else:
                        slot[key] = created.instance
                        instance_to_close = None
                        winning_instance = created.instance

        if instance_to_close is not None:
            try:
                await asyncio.wait_for(instance_to_close.close(), timeout=_CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "Race-loser terminal close timed out for %s:%s in conv %s",
                    terminal_name,
                    session_key,
                    conversation_id,
                )
        if shutdown_started:
            raise RuntimeError("terminal registry is shutting down")
        if registration_error is not None:
            raise registration_error
        return winning_instance

    def _assert_resource_id_available_locked(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> None:
        """Reject a sanitized ``(name, key)`` collision while ``_lock`` is held."""
        from omnigent.entities.session_resources import terminal_resource_id

        requested_id = terminal_resource_id(terminal_name, session_key)
        slot = self._by_conversation.get(conversation_id, {})
        collisions = [
            (name, key)
            for name, key in slot
            if terminal_resource_id(name, key) == requested_id
            and (name, key) != (terminal_name, session_key)
        ]
        if collisions:
            raise RuntimeError(
                f"terminal resource id {requested_id!r} collides with registered "
                f"terminal {collisions[0][0]!r}:{collisions[0][1]!r}"
            )

    def resolve_snapshot(
        self,
        session_id: str,
        terminal_id: str,
    ) -> ResolveResult:
        """Resolve one public id and capture ownership atomically under ``_lock``."""
        from omnigent.entities.session_resources import terminal_resource_id

        with self._lock:
            matches = [
                (name, key, instance)
                for (name, key), instance in self._by_conversation.get(session_id, {}).items()
                if terminal_resource_id(name, key) == terminal_id
            ]
            if len(matches) > 1:
                return AmbiguousResourceId(session_id=session_id, terminal_id=terminal_id)
            if not matches:
                return None
            name, key, instance = matches[0]
            entry = TerminalListEntry(
                terminal_name=name,
                session_key=key,
                instance=instance,
            )
            return ResolveSnapshot(
                entry=entry,
                instance=instance,
                op_lock=instance.op_lock,
                owner_tuple=(session_id, name, key),
                owner_generation=instance.owner_generation,
            )

    def resolve_key_snapshot(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
    ) -> ResolveResult:
        """Resolve a tool's name/key arguments through the public-id resolver."""
        from omnigent.entities.session_resources import terminal_resource_id

        return self.resolve_snapshot(
            session_id,
            terminal_resource_id(terminal_name, session_key),
        )

    def snapshot_error_code(self, snapshot: ResolveSnapshot) -> str | None:
        """Return why a captured owner can no longer operate, or ``None``."""
        with self._lock:
            if not self._snapshot_mapping_matches_locked(snapshot):
                return "terminal_moved"
            if snapshot.instance.retired or not snapshot.instance.running:
                return "terminal_not_running"
            return None

    def run_fenced(
        self,
        snapshot: ResolveSnapshot,
        async_fn: Callable[
            [],
            Coroutine[Any, Any, _FencedResultT] | _FencedResultT,
        ],
        *,
        invalid: _FencedInvalidT | Callable[[str], _FencedInvalidT],
    ) -> _FencedResultT | _FencedInvalidT:
        """Run one operation while its captured terminal owner stays valid."""
        with snapshot.op_lock:
            error_code = self.snapshot_error_code(snapshot)
            if error_code is not None:
                return invalid(error_code) if callable(invalid) else invalid
            result = async_fn()
            if asyncio.iscoroutine(result):
                return asyncio.run(result)
            return result

    def _snapshot_mapping_matches_locked(self, snapshot: ResolveSnapshot) -> bool:
        session_id, terminal_name, session_key = snapshot.owner_tuple
        current = self._by_conversation.get(session_id, {}).get((terminal_name, session_key))
        return (
            current is snapshot.instance
            and snapshot.instance.owner_generation == snapshot.owner_generation
        )

    def get(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> TerminalInstance | None:
        """Look up a registered instance.

        Sync because it doesn't touch tmux — just reads the map.
        Returns ``None`` if no instance was ever launched for this
        triple, or if it was already closed.

        :param conversation_id: Owning conversation id.
        :param terminal_name: Terminal spec name.
        :param session_key: Session key from launch.
        :returns: The :class:`TerminalInstance` or ``None``.
        """
        with self._lock:
            return self._by_conversation.get(conversation_id, {}).get((terminal_name, session_key))

    def list_for_conversation(
        self,
        conversation_id: str,
    ) -> list[TerminalListEntry]:
        """Return all terminals owned by *conversation_id*.

        Snapshot semantics — the list reflects state at call time.
        Sync because it just reads the map. The caller (the
        ``sys_terminal_list`` tool) can inspect each
        ``TerminalListEntry.instance.running`` to filter alive vs
        defunct.

        :param conversation_id: Owning conversation id.
        :returns: List of :class:`TerminalListEntry`. Empty list when
            the conversation has no terminals.
        """
        with self._lock:
            slot = dict(self._by_conversation.get(conversation_id, {}))
        return [
            TerminalListEntry(
                terminal_name=name,
                session_key=key,
                instance=instance,
            )
            for (name, key), instance in slot.items()
        ]

    def native_panes(self) -> list[tuple[str, str, Path]]:
        """Return live native-harness CLI panes as ``(conversation_id, name, socket_path)``.

        A "native pane" is a terminal whose name is a native harness short name
        (``claude`` / ``codex`` / ``cursor`` / ...) with session key ``"main"``.
        This is a cheap NAME pre-filter for the native idle reaper
        (:mod:`omnigent.terminals.pane_reaper`); the reaper's wiring additionally
        confirms the resource ROLE is a native harness before reaping, so a user
        terminal that merely shares the name is never reclaimed. Snapshot
        semantics; sync (map read only, no tmux I/O).

        :returns: ``(conversation_id, terminal_name, tmux_socket_path)`` per live
            name-matching native pane.
        """
        from omnigent.terminals.pane_reaper import NATIVE_PANE_TERMINAL_NAMES

        out: list[tuple[str, str, Path]] = []
        with self._lock:
            for conv_id, slot in self._by_conversation.items():
                for (name, key), instance in slot.items():
                    if key == "main" and name in NATIVE_PANE_TERMINAL_NAMES:
                        out.append((conv_id, name, instance.socket_path))
        return out

    def transfer(
        self,
        source_conversation_id: str,
        target_conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> bool:
        """Move one terminal registry entry without closing tmux.

        This is used by native Claude ``/clear`` rotation: the Claude
        process and tmux pane keep running, but the Omnigent session that owns
        the terminal resource changes. No tmux I/O occurs in this method.

        :param source_conversation_id: Current owning conversation id,
            e.g. ``"conv_old"``.
        :param target_conversation_id: New owning conversation id,
            e.g. ``"conv_new"``.
        :param terminal_name: Terminal spec name, e.g. ``"claude"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :returns: ``True`` when an entry was moved, ``False`` when the
            source entry did not exist.
        :raises RuntimeError: If the target conversation already has an
            entry with the same terminal name and session key.
        """
        resolved = self.resolve_key_snapshot(
            source_conversation_id,
            terminal_name,
            session_key,
        )
        if isinstance(resolved, AmbiguousResourceId):
            raise AmbiguousResourceIdError(
                f"Terminal resource {resolved.terminal_id!r} is ambiguous"
            )
        if resolved is None:
            return False
        with resolved.op_lock:
            return self.transfer_snapshot(resolved, target_conversation_id)

    def transfer_snapshot(
        self,
        snapshot: ResolveSnapshot,
        target_conversation_id: str,
    ) -> bool:
        """Move a captured owner while its stable ``op_lock`` is held."""
        if not snapshot.op_lock.locked():
            raise RuntimeError("transfer_snapshot requires the instance operation lock")
        source_conversation_id, terminal_name, session_key = snapshot.owner_tuple
        key = (terminal_name, session_key)
        with self._lock:
            if self._shutting_down or not self._snapshot_mapping_matches_locked(snapshot):
                return False
            if snapshot.instance.retired or not snapshot.instance.running:
                return False
            if source_conversation_id == target_conversation_id:
                return True
            self._assert_resource_id_available_locked(
                target_conversation_id,
                terminal_name,
                session_key,
            )
            target_slot = self._by_conversation.setdefault(target_conversation_id, {})
            if key in target_slot:
                raise RuntimeError(
                    f"Terminal {terminal_name!r}:{session_key!r} already exists for "
                    f"conversation {target_conversation_id!r}"
                )

            snapshot.instance.owner_generation += 1
            source_slot = self._by_conversation[source_conversation_id]
            source_slot.pop(key)
            if not source_slot:
                self._by_conversation.pop(source_conversation_id, None)
            target_slot[key] = snapshot.instance
        return True

    async def close(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> bool:
        """Close one terminal and remove it from the registry.

        Idempotent: closing a non-existent or already-closed terminal
        returns ``False`` without raising. This matches the
        ``sys_terminal_close`` tool's contract — the LLM may close
        the same terminal twice without seeing an error.

        :param conversation_id: Owning conversation id.
        :param terminal_name: Terminal spec name.
        :param session_key: Session key.
        :returns: ``True`` if a live instance was closed, ``False``
            if no live instance was found (already-closed or
            never-launched).
        """
        resolved = self.resolve_key_snapshot(
            conversation_id,
            terminal_name,
            session_key,
        )
        if isinstance(resolved, AmbiguousResourceId):
            raise AmbiguousResourceIdError(
                f"Terminal resource {resolved.terminal_id!r} is ambiguous"
            )
        if resolved is None:
            return False
        return await self.close_snapshot(resolved)

    async def close_snapshot(self, snapshot: ResolveSnapshot) -> bool:
        """Retire and close one captured terminal without blocking the event loop."""
        return await asyncio.to_thread(self._retire_and_close, snapshot, "Terminal close")

    def _retire_and_close(
        self,
        snapshot: ResolveSnapshot,
        log_prefix: str,
    ) -> bool:
        """Linearize retirement, then run tmux teardown under ``op_lock``."""
        conversation_id, terminal_name, session_key = snapshot.owner_tuple
        with snapshot.op_lock:
            with self._lock:
                if not self._snapshot_mapping_matches_locked(snapshot):
                    return False
                if snapshot.instance.retired:
                    return False
                snapshot.instance.retired = True
                snapshot.instance.owner_generation += 1
                slot = self._by_conversation[conversation_id]
                slot.pop((terminal_name, session_key))
                if not slot:
                    self._by_conversation.pop(conversation_id, None)

            async def _close_with_timeout() -> None:
                await asyncio.wait_for(
                    snapshot.instance.close(),
                    timeout=_CLOSE_TIMEOUT_S,
                )

            try:
                asyncio.run(_close_with_timeout())
            except asyncio.TimeoutError:
                logger.warning(
                    "%s timed out for %s:%s in conv %s",
                    log_prefix,
                    terminal_name,
                    session_key,
                    conversation_id,
                )
        return True

    def _snapshots_for_conversation_locked(
        self,
        conversation_id: str,
    ) -> list[ResolveSnapshot]:
        """Capture every exact owner in a conversation while ``_lock`` is held."""
        return [
            ResolveSnapshot(
                entry=TerminalListEntry(name, key, instance),
                instance=instance,
                op_lock=instance.op_lock,
                owner_tuple=(conversation_id, name, key),
                owner_generation=instance.owner_generation,
            )
            for (name, key), instance in self._by_conversation.get(
                conversation_id,
                {},
            ).items()
        ]

    async def cleanup_conversation(self, conversation_id: str) -> None:
        """Close every terminal owned by *conversation_id*.

        Called from the workflow's ``finally:`` block at workflow
        exit (any of completed / failed / cancelled). Idempotent:
        no-op when the conversation has no terminals.

        Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.4, this is **not**
        checkpointed: tmux kills are idempotent external side
        effects, the workflow doesn't read from terminals after
        exit, and step wrappers exist to checkpoint results the
        workflow consumes — not for fire-and-forget cleanup.

        Each ``instance.close()`` is bounded by ``_CLOSE_TIMEOUT_S``
        via ``asyncio.wait_for`` so a wedged tmux session can't stall
        cleanup. Timeouts and exceptions are logged and swallowed; the
        rest of the cleanup proceeds.

        :param conversation_id: The conversation being torn down.
        """
        with self._lock:
            snapshots = self._snapshots_for_conversation_locked(conversation_id)
        if not snapshots:
            return
        for snapshot in snapshots:
            _, name, key = snapshot.owner_tuple
            try:
                await asyncio.to_thread(
                    self._retire_and_close,
                    snapshot,
                    "cleanup_conversation: close",
                )
            except Exception:
                # We're in a workflow finally block; raising here would
                # mask the original workflow result. Log and move on.
                logger.exception(
                    "cleanup_conversation: close failed for %s:%s in conv %s",
                    name,
                    key,
                    conversation_id,
                )

    async def shutdown(self) -> None:
        """Tear down every registered terminal across all conversations.

        Called from the FastAPI server's lifespan shutdown handler.
        Iterates every conversation slot and closes each instance,
        bounded by ``_CLOSE_TIMEOUT_S`` per instance. Best-effort —
        a stuck instance shouldn't block the rest of Omnigent shutdown.
        """
        with self._lock:
            self._shutting_down = True
            snapshots = [
                snapshot
                for conversation_id in tuple(self._by_conversation)
                for snapshot in self._snapshots_for_conversation_locked(conversation_id)
            ]
        for snapshot in snapshots:
            conversation_id, name, key = snapshot.owner_tuple
            try:
                await asyncio.to_thread(
                    self._retire_and_close,
                    snapshot,
                    "shutdown: close",
                )
            except Exception:
                logger.exception(
                    "shutdown: close failed for %s:%s in conv %s",
                    name,
                    key,
                    conversation_id,
                )

    def active_conversation_ids(self) -> list[str]:
        """Return ids of conversations with at least one registered terminal.

        Used by tests. Snapshot semantics — the list reflects state
        at call time.

        :returns: List of conversation ids currently in the registry.
        """
        with self._lock:
            return list(self._by_conversation.keys())
