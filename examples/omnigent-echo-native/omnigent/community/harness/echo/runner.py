"""Runner terminal-builder hook for the echo-native example harness.

``launch_echo`` is the ``auto_create_terminal`` hook the launch/attach seam
resolves and calls with a :class:`~omnigent.runner.native.NativeLaunchContext`.
It runs on the RUNNER, so (unlike ``plugin.py``) it may import the runner stack
— the resolver only imports this module at dispatch time, never during
entry-point discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.entities.session_resources import SessionResourceView
    from omnigent.runner.native import NativeLaunchContext


async def launch_echo(ctx: NativeLaunchContext) -> SessionResourceView:
    """Build the echo-native terminal from a launch context (``auto_create_terminal``).

    A real adapter launches the vendor CLI as a runner-owned terminal resource
    and returns its :class:`SessionResourceView` (see
    ``omnigent.runner.native.orchestration._launch_pi`` →
    ``_auto_create_pi_terminal`` for the full pattern: spawn the TUI, register
    the streamable terminal, start the transcript forwarder).

    :param ctx: Launch inputs (session id, resource registry, event publisher,
        server client, resolved agent spec, …).
    :returns: The created terminal resource view.
    """
    # TODO(real-harness): call ctx.resource_registry.launch_required_terminal(...)
    # with the vendor command, register the native terminal role, and start the
    # forwarder. The stub documents the adapter's return contract.
    raise NotImplementedError(
        "echo-native is an example harness stub; launch_echo has no live vendor terminal. "
        "See omnigent.runner.native.orchestration._auto_create_pi_terminal for a real one."
    )
