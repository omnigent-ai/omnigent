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
different threads. The threading lock is held only for short map
mutations (no tmux I/O underneath); slow tmux subprocess calls happen
outside the lock.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
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
    :param instance: The live :class:`TerminalInstance`. Callers can
        read ``.running``, ``.command``, ``.socket_path``, etc., or
        invoke ``send`` / ``read`` directly when wrapping in tools.
    """

    terminal_name: str
    session_key: str
    instance: TerminalInstance


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
        # Per-instance locks keyed by the full (conv_id, name, session_key)
        # triple. Created at launch, removed at close /
        # cleanup_conversation. Sender / reader / closer tools acquire
        # this around the ``asyncio.run(instance.X())`` call to serialize
        # tmux subprocess invocations on the same instance — without it,
        # two concurrent ``sys_terminal_send`` calls can interleave their
        # per-keystroke tmux commands (a ``send(text=X, keys="Enter")``
        # decomposes to ~2 subprocess calls with a 50ms ``asyncio.sleep``
        # between them — plenty of room for another send to slip in).
        # See ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §9.1.
        self._instance_locks: dict[tuple[str, str, str], threading.Lock] = {}
        # Threading lock — see module docstring for the rationale.
        # Protects both ``_by_conversation`` and ``_instance_locks``.
        self._lock = threading.Lock()

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
        with self._lock:
            existing = self._by_conversation.get(conversation_id, {}).get(key)
            if existing is not None and existing.running:
                return existing

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

        with self._lock:
            slot = self._by_conversation.setdefault(conversation_id, {})
            # Re-check: another concurrent launch for the same key may
            # have raced ours. Take the second-arrival policy: close
            # ours and return the racer's. Avoids two live tmux
            # sessions for the same key.
            racer = slot.get(key)
            if racer is not None and racer.running:
                # Close ours outside the lock; racer wins.
                instance_to_close: TerminalInstance | None = created.instance
                winning_instance = racer
            else:
                slot[key] = created.instance
                instance_to_close = None
                winning_instance = created.instance
                # Allocate a per-instance lock alongside the
                # registration. Tools fetch it via
                # :meth:`get_instance_lock` to serialize concurrent
                # tmux ops on this instance.
                self._instance_locks[(conversation_id, terminal_name, session_key)] = (
                    threading.Lock()
                )

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
        return winning_instance

    def get_instance_lock(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> threading.Lock | None:
        """Return the per-instance lock for a registered terminal.

        Used by the ``sys_terminal_send`` / ``read`` / ``close``
        tools to serialize tmux subprocess invocations against the
        same instance. The lock exists from registration (in
        :meth:`launch`) until the instance is closed (in
        :meth:`close` or :meth:`cleanup_conversation`).

        :param conversation_id: Owning conversation id.
        :param terminal_name: Terminal spec name.
        :param session_key: Session key from launch.
        :returns: A :class:`threading.Lock` to acquire around
            per-instance ops, or ``None`` if the instance was
            never registered (or has been closed). Callers should
            handle ``None`` by surfacing a "not running" error to
            the LLM rather than skipping the lock — the
            instance-not-found path means the operation can't
            proceed at all.
        """
        with self._lock:
            return self._instance_locks.get((conversation_id, terminal_name, session_key))

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
        key = (terminal_name, session_key)
        source_lock_key = (source_conversation_id, terminal_name, session_key)
        target_lock_key = (target_conversation_id, terminal_name, session_key)
        with self._lock:
            source_slot = self._by_conversation.get(source_conversation_id)
            if source_slot is None:
                return False
            instance = source_slot.get(key)
            if instance is None:
                return False
            target_slot = self._by_conversation.setdefault(target_conversation_id, {})
            if key in target_slot:
                raise RuntimeError(
                    f"Terminal {terminal_name!r}:{session_key!r} already exists for "
                    f"conversation {target_conversation_id!r}"
                )

            source_slot.pop(key)
            if not source_slot:
                self._by_conversation.pop(source_conversation_id, None)
            target_slot[key] = instance

            lock = self._instance_locks.pop(source_lock_key, None)
            self._instance_locks[target_lock_key] = lock or threading.Lock()
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
        key = (terminal_name, session_key)
        with self._lock:
            slot = self._by_conversation.get(conversation_id)
            if slot is None:
                return False
            instance = slot.pop(key, None)
            if not slot:
                # Drop the empty per-conversation dict so memory
                # doesn't grow with stale conversation ids.
                self._by_conversation.pop(conversation_id, None)
            # Drop the per-instance lock too so subsequent
            # ``get_instance_lock`` calls return ``None`` for this
            # closed instance (callers surface a "not running"
            # error to the LLM).
            self._instance_locks.pop((conversation_id, terminal_name, session_key), None)
        if instance is None:
            return False
        try:
            await asyncio.wait_for(instance.close(), timeout=_CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Terminal close timed out for %s:%s in conv %s",
                terminal_name,
                session_key,
                conversation_id,
            )
        return True

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
            slot = self._by_conversation.pop(conversation_id, None)
            # Drop every per-instance lock owned by this conversation.
            # Iterate over `slot` (the just-popped per-conv map) for the
            # canonical (name, key) pairs.
            if slot:
                for name, sess in slot:
                    self._instance_locks.pop((conversation_id, name, sess), None)
        if not slot:
            return
        for (name, key), instance in slot.items():
            try:
                await asyncio.wait_for(instance.close(), timeout=_CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "cleanup_conversation: close timed out for %s:%s in conv %s",
                    name,
                    key,
                    conversation_id,
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
            slots = list(self._by_conversation.items())
            self._by_conversation.clear()
            # AP-shutdown clears all instance locks too — every
            # conversation's terminals are being torn down.
            self._instance_locks.clear()
        for conversation_id, slot in slots:
            for (name, key), instance in slot.items():
                try:
                    await asyncio.wait_for(instance.close(), timeout=_CLOSE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logger.warning(
                        "shutdown: close timed out for %s:%s in conv %s",
                        name,
                        key,
                        conversation_id,
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
