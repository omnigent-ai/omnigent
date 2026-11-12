"""Which host Claude MCP servers signed in after a claude-native pane started.

Claude Code connects MCP servers only when it starts, and a claude-native pane
has no control channel to reconnect one. A server that needed sign-in when the
pane started stays failed after the host's sign-in manager reports it
``connected``, until Claude Code restarts. This tracks those servers so the
runner can restart the pane, resuming the same conversation, while it is idle.
"""

from __future__ import annotations

import threading
import weakref

from omnigent.host.mcp_inventory import harness_sign_in_by_live_name


class ClaudeMcpSignInTracker:
    """Remember, per session, which Claude servers needed sign-in when its pane started.

    The runner records the state just before it starts a pane, for itself or
    for a CLI client (:meth:`note_launching`), hands it to the pane the launch
    produced (:meth:`note_launched`), and moves it with the pane on ``/clear``
    or fork (:meth:`transfer`). A pane with no record, such as one a runner
    restart left running, is recorded when first checked. Servers are named as
    Claude Code reports them, e.g. ``plugin:<plugin>:<server>``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # A launch in progress: the servers that needed sign-in as it began.
        self._launching: dict[str, frozenset[str]] = {}
        # The session's pane and the servers that needed sign-in as it started.
        self._panes: dict[str, tuple[weakref.ref[object], frozenset[str]]] = {}

    def note_launching(self, session_id: str) -> None:
        """Record which Claude servers need sign-in as the runner starts *session_id*'s pane.

        :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
        """
        signed_out = _signed_out_claude_servers()
        with self._lock:
            self._launching[session_id] = signed_out

    def note_launched(
        self, session_id: str, pane: object | None, *, previous: object | None = None
    ) -> None:
        """Give the state recorded as the launch began to the pane it produced.

        Call it however the launch ends, so a failed or cancelled one leaves no
        state behind for a later launch to hand out.

        :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
        :param pane: The session's terminal instance after the launch, or ``None``
            when the launch produced none. A no-op when no launch was recorded.
        :param previous: The pane running before the launch; a launch that found
            it running leaves that pane its own record.
        """
        with self._lock:
            signed_out = self._launching.pop(session_id, None)
            if signed_out is not None and pane is not None and pane is not previous:
                self._panes[session_id] = (weakref.ref(pane), signed_out)

    def signed_in_since_launch(self, session_id: str, pane: object) -> frozenset[str]:
        """Name the Claude servers that needed sign-in when *pane* started and are signed in now.

        :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
        :param pane: The session's running terminal instance.
        :returns: Server names, e.g. ``{"linear"}``; empty when nothing changed.
        """
        with self._lock:
            recorded = self._panes.get(session_id)
        if recorded is not None and recorded[0]() is pane:
            signed_out = recorded[1]
        else:
            signed_out = _signed_out_claude_servers()
            with self._lock:
                recorded = self._panes.get(session_id)
                if recorded is None or recorded[0]() is not pane:
                    self._panes[session_id] = (weakref.ref(pane), signed_out)
        if not signed_out:
            return frozenset()
        statuses = claude_sign_in_by_live_name()
        return frozenset(name for name in signed_out if statuses.get(name) == "connected")

    def transfer(self, source_session_id: str, target_session_id: str) -> None:
        """Hand *source_session_id*'s pane record to the session its pane moved to.

        ``/clear`` and fork move the running pane to a successor session; its
        record must follow, or a sign-in finished before the move is lost.

        :param source_session_id: The session the pane left, e.g. ``"conv_old"``.
        :param target_session_id: The session that now owns it, e.g. ``"conv_new"``.
        """
        with self._lock:
            recorded = self._panes.pop(source_session_id, None)
            if recorded is not None:
                self._panes[target_session_id] = recorded

    def forget(self, session_id: str) -> None:
        """Drop everything recorded for *session_id*, e.g. once the session is deleted."""
        with self._lock:
            self._launching.pop(session_id, None)
            self._panes.pop(session_id, None)


def claude_sign_in_by_live_name() -> dict[str, str]:
    """The host's sign-in state for each Claude server, keyed as Claude Code names it.

    :returns: E.g. ``{"linear": "connected", "plugin:docs:notion": "sign_in_required"}``.
    """
    return harness_sign_in_by_live_name("claude")


def _signed_out_claude_servers() -> frozenset[str]:
    """Name the Claude servers the host's sign-in manager says need sign-in now."""
    return frozenset(
        name
        for name, status in claude_sign_in_by_live_name().items()
        if status == "sign_in_required"
    )
