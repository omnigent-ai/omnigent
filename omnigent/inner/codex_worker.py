"""Fail-closed launch preparation for the stdio Codex worker."""

from __future__ import annotations

import contextlib
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .datamodel import OSEnvSpec
from .egress.controller import apply_egress_env
from .model_signer import SignerReadiness
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
    signer_readiness: SignerReadiness | None = None,
    worker_env: MutableMapping[str, str] | None = None,
) -> CodexWorkerLaunch:
    """Prepare a contained worker or propagate the containment failure."""
    if os_env is None or (os_env.sandbox is not None and os_env.sandbox.type == "none"):
        if signer_readiness is not None:
            raise OSError("signer-backed Codex worker requires an active sandbox")
        return CodexWorkerLaunch(launch_path=codex_path, sandboxed=False)

    policy = resolve_sandbox(os_env, cwd)
    if not policy.active:
        if signer_readiness is not None:
            raise OSError("signer-backed Codex worker requires an active sandbox")
        return CodexWorkerLaunch(launch_path=codex_path, sandboxed=False)
    if signer_readiness is not None and worker_env is None:
        raise ValueError("signer-backed Codex worker requires an owned worker environment")

    codex_dir = Path(codex_path).resolve(strict=False).parent
    policy = with_additional_read_roots(policy, [codex_dir, _FRAMEWORK_PACKAGE_ROOT])
    policy = with_additional_write_roots(policy, [codex_home])
    if signer_readiness is not None:
        if signer_readiness.ca_bundle_path.parent != signer_readiness.socket_path.parent:
            raise ValueError("signer relay and public CA must share one public directory")
        policy = with_additional_read_roots(policy, [signer_readiness.ca_bundle_path.parent])
    staged_env = dict(worker_env) if worker_env is not None else None
    if signer_readiness is not None:
        assert staged_env is not None
        for key in ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
            staged_env.pop(key, None)
        apply_egress_env(
            staged_env,
            relay_port=signer_readiness.relay_port,
            ca_bundle_path=signer_readiness.ca_bundle_path,
            auth_token=None,
        )
        staged_env["OPENAI_API_KEY"] = signer_readiness.placeholder
    policy = with_spawn_env_allowlist(
        policy,
        list(staged_env) if staged_env is not None else spawn_env_names,
    )
    # Model traffic is enabled later only through the signer relay.
    policy = replace(
        policy,
        allow_network=False,
        egress_relay_port=(signer_readiness.relay_port if signer_readiness is not None else None),
        egress_socket_path=(
            str(signer_readiness.socket_path) if signer_readiness is not None else None
        ),
    )

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
    if staged_env is not None and worker_env is not None:
        worker_env.clear()
        worker_env.update(staged_env)
    return CodexWorkerLaunch(
        launch_path=str(launcher),
        sandboxed=True,
        _owned_launcher=launcher,
    )
