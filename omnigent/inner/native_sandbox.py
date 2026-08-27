"""Wrap a native (vendor) process in Omnigent's bwrap sandbox + optional egress.

Native harnesses (Codex, and the tmux terminal) spawn a vendor binary directly
rather than routing tool calls through the ``sys_os`` helper. That means the
vendor process — and every shell command it runs — does NOT participate in the
egress proxy the ``sys_os`` helper sets up, so ``os_env.sandbox.egress_rules``
never governs it.

This module holds the shared logic (previously inlined in
:mod:`omnigent.inner.terminal`) for bootstrapping a parent-side L7 egress proxy
for such a process and baking its relay port / socket path / CA bundle into the
:class:`SandboxPolicy` + spawn env, so the process can be wrapped with
:func:`create_exec_launcher` and reach the network only through the allow-list.

``require_auth=False`` is used deliberately: a native vendor process has no
out-of-band FD channel to receive the Proxy-Authorization token (embedding it in
``HTTP_PROXY`` would leak it via ``ps -E`` to every shell child). The relay's
other defenses — random ephemeral port, default-deny on private destinations,
and the ``egress_rules`` allow-list — still apply. See the egress controller's
docstring for the full trade-off discussion.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from dataclasses import replace

from .egress import EgressProxyHandle, apply_egress_env, start_egress_proxy
from .sandbox import (
    SandboxPolicy,
    create_private_tmpdir,
    with_additional_write_roots,
)


def bootstrap_native_egress(
    sandbox: SandboxPolicy,
    env: MutableMapping[str, str],
    *,
    egress_rules: Sequence[str],
    allow_private_destinations: bool = False,
) -> tuple[SandboxPolicy, EgressProxyHandle]:
    """Start a parent-side MITM egress proxy for a native process.

    Mints a private scratch tmpdir (added to ``write_roots`` so bwrap
    bind-mounts it into the namespace — the CA bundle and egress socket live
    there), starts the proxy, injects ``HTTP_PROXY`` / ``HTTPS_PROXY`` / CA env
    vars into ``env``, and returns a policy whose ``egress_relay_port`` /
    ``egress_socket_path`` are populated for :func:`create_exec_launcher`.

    The caller MUST use the returned policy for the launcher and is responsible
    for the returned handle's lifecycle: call ``handle.stop()`` and
    ``cleanup_private_tmpdir(handle.socket_path.parent)`` on teardown.

    :param sandbox: Active sandbox policy (caller verified ``sandbox.active``).
    :param env: Mutable spawn env; mutated in place with proxy + CA vars.
    :param egress_rules: Non-empty allow-list of ``"METHODS host/path"`` rules.
    :param allow_private_destinations: Passed through to the proxy; default
        ``False`` blocks RFC1918 / loopback upstream (anti DNS-rebinding).
    :returns: ``(updated_policy, egress_handle)``.
    """
    tmpdir = create_private_tmpdir()
    # Add the scratch tmpdir to write_roots BEFORE encoding the policy into the
    # launcher; otherwise bwrap won't bind it and the CA bundle / egress socket
    # the launcher needs at activate time would be invisible in the namespace.
    sandbox = with_additional_write_roots(sandbox, [tmpdir])
    handle = start_egress_proxy(
        rules=list(egress_rules),
        tmpdir=tmpdir,
        allow_private_destinations=allow_private_destinations,
        require_auth=False,
    )
    apply_egress_env(
        env,
        relay_port=handle.relay_port,
        ca_bundle_path=handle.ca_bundle_path,
        auth_token=None,
    )
    return (
        replace(
            sandbox,
            egress_relay_port=handle.relay_port,
            egress_socket_path=str(handle.socket_path),
        ),
        handle,
    )
