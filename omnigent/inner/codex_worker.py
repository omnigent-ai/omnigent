"""Fail-closed launch preparation for the stdio Codex worker."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .datamodel import OSEnvSpec
from .sandbox import (
    create_exec_launcher,
    get_backend,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_roots,
    with_spawn_env_allowlist,
)

_FRAMEWORK_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class CodexWorkerLaunch:
    """Prepared worker executable and its owned launcher file."""

    launch_path: str
    sandboxed: bool
    _owned_launcher: Path | None = None

    def close(self) -> None:
        """Delete the generated launcher, if any."""
        launcher = self._owned_launcher
        self._owned_launcher = None
        if launcher is not None:
            with contextlib.suppress(OSError):
                launcher.unlink()


def prepare_codex_worker(
    *,
    codex_path: str,
    cwd: Path,
    codex_home: Path,
    os_env: OSEnvSpec | None,
    spawn_env_names: Sequence[str],
) -> CodexWorkerLaunch:
    """Prepare a contained worker or propagate the containment failure."""
    if os_env is None or (os_env.sandbox is not None and os_env.sandbox.type == "none"):
        return CodexWorkerLaunch(launch_path=codex_path, sandboxed=False)

    policy = resolve_sandbox(os_env, cwd)
    if not policy.active:
        return CodexWorkerLaunch(launch_path=codex_path, sandboxed=False)

    codex_dir = Path(codex_path).resolve(strict=False).parent
    policy = with_additional_read_roots(policy, [codex_dir, _FRAMEWORK_PACKAGE_ROOT])
    policy = with_additional_write_roots(policy, [codex_home])
    policy = with_spawn_env_allowlist(policy, spawn_env_names)
    # Model traffic is enabled later only through the signer relay.
    policy = replace(policy, allow_network=False)

    backend = get_backend(policy.backend_type)
    probe_argv = [codex_path, "app-server"]
    wrapped_argv = backend.wrap_launcher_argv(
        probe_argv,
        policy,
        cwd,
        target=codex_path,
    )
    if wrapped_argv == probe_argv:
        raise OSError(
            f"Sandbox backend {policy.backend_type!r} cannot contain the Codex worker at spawn"
        )

    launcher = Path(create_exec_launcher(codex_path, policy))
    return CodexWorkerLaunch(
        launch_path=str(launcher),
        sandboxed=True,
        _owned_launcher=launcher,
    )
