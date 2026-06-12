"""
Sandbox primitive — wraps the existing inner implementation.

This module is the canonical sandbox surface for new code (per
``designs/SERVER_HARNESS_CONTRACT.md`` §Sandbox primitives). It
re-exports the resolution + activation API from
``omnigent.inner.sandbox`` without copying or re-implementing any
logic. The implementation continues to live under ``inner/`` and
legacy consumers there keep importing from ``inner.sandbox``
directly.

The expected migration is for new consumers (the harness scaffold,
``sys_os_*`` builtins, the unified subprocess primitive) to target
this module instead of reaching into ``inner.sandbox``. Once every
consumer has flipped, the inner module becomes unreachable on the
new path; until then, both import locations remain valid.

The ``SandboxPolicy`` / ``SandboxBackend`` / ``resolve_sandbox`` /
``activate_sandbox`` quartet is the canonical surface called out in
the contract; the ``with_additional_*`` and tmpdir helpers are
included because they are part of the same logical API and removing
them would force consumers to reach back into ``inner.sandbox``.
"""

from __future__ import annotations

from omnigent.inner.sandbox import (
    SandboxBackend,
    SandboxPolicy,
    activate_sandbox,
    cleanup_private_tmpdir,
    create_exec_launcher,
    create_private_tmpdir,
    get_backend,
    register_backend,
    resolve_sandbox,
    run_launcher,
    set_temp_env,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
    with_denied_unix_sockets,
    with_spawn_env_allowlist,
)

__all__ = [
    "SandboxBackend",
    "SandboxPolicy",
    "activate_sandbox",
    "cleanup_private_tmpdir",
    "create_exec_launcher",
    "create_private_tmpdir",
    "get_backend",
    "register_backend",
    "resolve_sandbox",
    "run_launcher",
    "set_temp_env",
    "with_additional_read_roots",
    "with_additional_write_files",
    "with_additional_write_roots",
    "with_denied_unix_sockets",
    "with_spawn_env_allowlist",
]
