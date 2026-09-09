"""Fail-closed launch preparation for the stdio Codex worker."""

from __future__ import annotations

import contextlib
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .datamodel import OSEnvSpec
from .egress.controller import EgressProxyHandle, apply_egress_env, start_egress_proxy
from .model_signer import SignerReadiness
from .sandbox import (
    cleanup_private_tmpdir,
    create_exec_launcher,
    create_private_tmpdir,
    get_backend,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_roots,
    with_spawn_env_allowlist,
)

_FRAMEWORK_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_BROKERED_AUTH_SECRET_ENV = frozenset(
    {
        "DATABRICKS_BEARER",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CODEX_TOKEN",
        "DATABRICKS_TOKEN",
        "OPENAI_API_KEY",
    }
)


@dataclass
class CodexWorkerLaunch:
    """Prepared worker executable and its owned launcher file."""

    launch_path: str
    sandboxed: bool
    _owned_launcher: Path | None = None
    _egress_handle: EgressProxyHandle | None = None
    _egress_tmpdir: Path | None = None

    def close(self) -> None:
        """Release the generated launcher and generic egress relay."""
        launcher = self._owned_launcher
        self._owned_launcher = None
        if launcher is not None:
            with contextlib.suppress(OSError):
                launcher.unlink()
        handle = self._egress_handle
        self._egress_handle = None
        if handle is not None:
            handle.stop()
        tmpdir = self._egress_tmpdir
        self._egress_tmpdir = None
        cleanup_private_tmpdir(tmpdir)


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
    sandbox_spec = os_env.sandbox
    egress_rules = (
        list(sandbox_spec.egress_rules or [])
        if signer_readiness is None and sandbox_spec is not None
        else []
    )
    if egress_rules and worker_env is None:
        raise ValueError("egress-filtered Codex worker requires an owned worker environment")

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
        for key in _BROKERED_AUTH_SECRET_ENV:
            staged_env.pop(key, None)
        for key in ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
            staged_env.pop(key, None)
        apply_egress_env(
            staged_env,
            relay_port=signer_readiness.relay_port,
            ca_bundle_path=signer_readiness.ca_bundle_path,
            auth_token=None,
        )
        staged_env["OPENAI_API_KEY"] = signer_readiness.placeholder
        # Model traffic is enabled only through the trusted signer relay.
        policy = replace(
            policy,
            allow_network=False,
            egress_relay_port=signer_readiness.relay_port,
            egress_socket_path=str(signer_readiness.socket_path),
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

    egress_handle: EgressProxyHandle | None = None
    egress_tmpdir: Path | None = None
    launcher: Path | None = None
    try:
        if egress_rules:
            assert staged_env is not None
            assert sandbox_spec is not None
            for key in ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
                staged_env.pop(key, None)
            egress_tmpdir = create_private_tmpdir()
            policy = with_additional_write_roots(policy, [egress_tmpdir])
            egress_handle = start_egress_proxy(
                rules=egress_rules,
                tmpdir=egress_tmpdir,
                allow_private_destinations=sandbox_spec.egress_allow_private_destinations,
                # Codex has no helper config channel that keeps this value out
                # of its environment. Match the terminal path: relay access is
                # tokenless, while destination/method/path policy remains strict.
                require_auth=False,
            )
            apply_egress_env(
                staged_env,
                relay_port=egress_handle.relay_port,
                ca_bundle_path=egress_handle.ca_bundle_path,
                auth_token=None,
            )
            policy = replace(
                policy,
                allow_network=False,
                egress_relay_port=egress_handle.relay_port,
                egress_socket_path=str(egress_handle.socket_path),
            )

        # Compute this after proxy variables are staged so the sandbox does
        # not prune HTTP_PROXY or the CA bundle variables at exec time.
        policy = with_spawn_env_allowlist(
            policy,
            list(staged_env) if staged_env is not None else spawn_env_names,
        )
        launcher = Path(create_exec_launcher(codex_path, policy))
        if staged_env is not None and worker_env is not None:
            worker_env.clear()
            worker_env.update(staged_env)
        return CodexWorkerLaunch(
            launch_path=str(launcher),
            sandboxed=True,
            _owned_launcher=launcher,
            _egress_handle=egress_handle,
            _egress_tmpdir=egress_tmpdir,
        )
    except BaseException:
        if launcher is not None:
            with contextlib.suppress(OSError):
                launcher.unlink()
        if egress_handle is not None:
            egress_handle.stop()
        cleanup_private_tmpdir(egress_tmpdir)
        raise
