"""tmux hooks that let a start-on-attach pane follow its viewers.

The pane command waits on :data:`START_ON_ATTACH_CHANNEL` before starting, and
the hook table wakes :data:`CLIENTS_CHANGED_CHANNEL` only on the transitions
that change whether anyone is watching -- the first client attaching and the
last client detaching -- so the pane application can stop painting an unviewed
screen. Shared by :mod:`omnigent.inner.terminal` (launch) and
:mod:`omnigent.repl._tmux_pane`.
"""

from __future__ import annotations

START_ON_ATTACH_CHANNEL = "omnigent-start-on-attach"
CLIENTS_CHANGED_CHANNEL = "omnigent-clients-changed"


def client_hook_commands() -> list[list[str]]:
    """Return the ``set-hook`` commands for a start-on-attach tmux server.

    Both hooks signal :data:`CLIENTS_CHANGED_CHANNEL` only on the boundary
    between zero and one viewer: ``client-attached`` when ``#{session_attached}``
    becomes ``1`` and ``client-detached`` when it reaches ``0``. A viewer joining
    or leaving while others remain is silent.

    tmux cancels two signals delivered to a channel while nobody is waiting, so a
    later ``wait-for`` blocks instead of returning. Signalling only the zero/one
    boundary keeps paired reconnects inside the watcher's rearm gap from
    colliding; because the watcher also samples the current client count before
    each wait, a collapse that still happens (an attach then detach before it
    starts) only costs a redundant wake, never a stranded pause.

    ``client-attached`` still signals :data:`START_ON_ATTACH_CHANNEL` on every
    attach to release the pane's one-shot start gate.

    :returns: Argument lists for one tmux command sequence.
    """
    return [
        [
            "set-hook",
            "-g",
            "client-attached",
            f"wait-for -S {START_ON_ATTACH_CHANNEL} ; "
            f"if-shell -F '#{{==:#{{session_attached}},1}}' "
            f"'wait-for -S {CLIENTS_CHANGED_CHANNEL}'",
        ],
        [
            "set-hook",
            "-g",
            "client-detached",
            f"if-shell -F '#{{==:#{{session_attached}},0}}' "
            f"'wait-for -S {CLIENTS_CHANGED_CHANNEL}'",
        ],
    ]
