"""Shared inert doctor responses and explicit validator policy inputs."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from omnigent.inner.sandbox import SandboxPolicy

CAPTURED_DOCTOR = json.loads(
    (Path(__file__).with_name("fixtures") / "claude-doctor-2.1.323.json").read_text(
        encoding="utf-8"
    )
)
DOCTOR_HELP = CAPTURED_DOCTOR["runs"]["help"]["stdout"]
DOCTOR_HEADER = (
    "Claude Code doctor\n\nRunning: inert fixture\nCommit: fixture-build\nPath: /fixture/claude\n"
)
DOCTOR_COMPLETION = (
    "\nNo installation issues found.\n\n"
    "For a full setup checkup that can also fix issues, run /doctor in a Claude Code session.\n"
)


def validator_policy(
    workspace: Path,
    private_dir: Path,
    socket_path: Path,
    *,
    read_roots: Sequence[Path],
    spawn_env_allowlist: Sequence[str],
) -> SandboxPolicy:
    """Build the shared synthetic policy without hiding case-specific faults."""
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=list(read_roots),
        write_roots=[workspace, private_dir],
        write_files=[workspace / "write-file"],
        allow_network=True,
        mask_paths=[workspace / "secret", private_dir / "covered"],
        deny_unix_socket_paths=[socket_path, workspace / "other.sock"],
        credential_source_paths=[workspace / "credential-file"],
        copy_on_write_roots=[workspace],
        copy_on_write_namespace=(123, 456, 789),
        spawn_env_allowlist=list(spawn_env_allowlist),
    )
