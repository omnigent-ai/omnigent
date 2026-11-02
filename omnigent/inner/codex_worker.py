"""Fail-closed launch preparation for the stdio Codex worker."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .datamodel import OSEnvSpec
from .egress.controller import EgressProxyHandle, apply_egress_env, start_egress_proxy
from .model_signer import SignerReadiness
from .sandbox import (
    SandboxPolicy,
    cleanup_private_tmpdir,
    create_exec_launcher,
    create_private_tmpdir,
    get_backend,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
    with_spawn_env_allowlist,
)

logger = logging.getLogger(__name__)

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
    native_tools_allowed: bool = True
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


def _host_link_target(link: Path) -> Path | None:
    """Return the regular host file a private-home link names, if any."""
    if not link.is_symlink():
        return None
    target = link.parent / link.readlink()
    target = target.parent.resolve() / target.name
    # A sandboxed session may have replaced the host file with a link; never follow it.
    if target.is_symlink() or not target.is_file():
        return None
    return target


def _login_rotates(login: Path) -> bool:
    """Return whether Codex may refresh this login; only an API-key login never does."""
    try:
        document = json.loads(login.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(document, dict):
        return True
    return not document.get("OPENAI_API_KEY") or bool(document.get("tokens"))


def _snapshot_sqlite(source: Path, target: Path) -> None:
    try:
        with (
            contextlib.closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as host,
            contextlib.closing(sqlite3.connect(target)) as private,
        ):
            host.backup(private)
    except sqlite3.Error:
        # Hand Codex the same bytes the link would have shown it.
        try:
            shutil.copyfile(source, target)
        except OSError:
            target.unlink(missing_ok=True)
            logger.warning(
                "Starting Codex without memories: cannot copy %s", source, exc_info=True
            )


def _contain_host_codex_home_links(
    policy: SandboxPolicy, codex_home: Path, *, share_host_login: bool
) -> SandboxPolicy:
    """Repair private-home links into the host Codex home, which the sandbox does not mount."""
    # SQLite needs writable sidecars beside the database, so start from a private copy.
    memories = codex_home / "memories_1.sqlite"
    host_memories = _host_link_target(memories)
    if memories.is_symlink():
        memories.unlink()
    if host_memories is not None:
        _snapshot_sqlite(host_memories, memories)
    login = codex_home / "auth.json"
    host_login = _host_link_target(login)
    if host_login is None:
        return policy
    if not share_host_login:
        # An API-key login never rotates, so a private copy keeps the host file out of reach.
        login.unlink()
        shutil.copy(host_login, login)
        return policy
    granted = with_additional_write_files(policy, [host_login])
    # The grant resolves the path again; drop it if the login was swapped for a link meanwhile.
    if not set(granted.write_files) <= {*policy.write_files, host_login}:
        return policy
    # Codex refreshes the login in place; link straight to the granted file.
    login.unlink()
    login.symlink_to(host_login)
    return granted


def prepare_codex_catalog_probe(
    *,
    codex_path: str,
    cwd: Path,
    codex_home: Path,
    os_env: OSEnvSpec | None,
    spawn_env_names: Sequence[str],
) -> CodexWorkerLaunch:
    """Prepare a network-denied launcher for ``codex debug models --bundled``."""
    if os_env is None or (os_env.sandbox is not None and os_env.sandbox.type == "none"):
        raise OSError("brokered Codex catalog probe requires an active sandbox")
    policy = resolve_sandbox(os_env, cwd)
    if not policy.active:
        raise OSError("brokered Codex catalog probe requires an active sandbox")

    codex_dir = Path(codex_path).resolve(strict=False).parent
    policy = with_additional_read_roots(policy, [codex_dir, _FRAMEWORK_PACKAGE_ROOT])
    policy = with_additional_write_roots(policy, [codex_home])
    policy = replace(
        policy,
        allow_network=False,
        egress_relay_port=None,
        egress_socket_path=None,
    )
    policy = with_spawn_env_allowlist(policy, spawn_env_names)
    backend = get_backend(policy.backend_type)
    probe_argv = [codex_path, "debug", "models", "--bundled"]
    if backend.wrap_launcher_argv(probe_argv, policy, cwd, target=codex_path) == probe_argv:
        raise OSError(
            f"Sandbox backend {policy.backend_type!r} cannot contain the Codex catalog probe"
        )
    launcher = Path(create_exec_launcher(codex_path, policy))
    return CodexWorkerLaunch(
        launch_path=str(launcher),
        sandboxed=True,
        _owned_launcher=launcher,
    )


def prepare_codex_worker(
    *,
    codex_path: str,
    cwd: Path,
    codex_home: Path,
    skills_dir: Path | None = None,
    os_env: OSEnvSpec | None,
    spawn_env_names: Sequence[str],
    signer_readiness: SignerReadiness | None = None,
    worker_env: MutableMapping[str, str] | None = None,
) -> CodexWorkerLaunch:
    """Prepare a contained worker or propagate the containment failure.

    A network-denying sandbox with no signer and no ``egress_rules`` gives the
    app-server no route to the model. The worker then keeps its filesystem
    sandbox but shares the host network, with Codex's native tools disabled.
    A worker given the host's refreshable ChatGPT login also runs without
    native tools, because Codex must rotate that login in place.
    """
    if os_env is None or (os_env.sandbox is not None and os_env.sandbox.type == "none"):
        if signer_readiness is not None:
            raise OSError("signer-backed Codex worker requires an active sandbox")
        return CodexWorkerLaunch(launch_path=codex_path, sandboxed=False)
    sandbox_spec = os_env.sandbox
    if signer_readiness is not None and sandbox_spec is not None and sandbox_spec.egress_rules:
        raise ValueError(
            "signer-backed Codex does not support os_env.sandbox.egress_rules; "
            "brokered sessions are model-only"
        )

    policy = resolve_sandbox(os_env, cwd)
    if not policy.active:
        if signer_readiness is not None:
            raise OSError("signer-backed Codex worker requires an active sandbox")
        return CodexWorkerLaunch(launch_path=codex_path, sandboxed=False)
    if signer_readiness is not None and worker_env is None:
        raise ValueError("signer-backed Codex worker requires an owned worker environment")
    egress_rules = (
        list(sandbox_spec.egress_rules or [])
        if signer_readiness is None and sandbox_spec is not None
        else []
    )
    if egress_rules and worker_env is None:
        raise ValueError("egress-filtered Codex worker requires an owned worker environment")
    native_tools_allowed = True
    if signer_readiness is None and not egress_rules and not policy.allow_network:
        # Tools then run only through the separately sandboxed sys_os_* tools.
        logger.warning(
            "os_env.sandbox denies network and gives the Codex app-server no model "
            "route; sharing the host network with the worker and disabling Codex's "
            "native tools. Add egress_rules for the model host to filter its traffic."
        )
        policy = replace(policy, allow_network=True)
        native_tools_allowed = False
    share_host_login = False
    if signer_readiness is None:
        host_login = _host_link_target(codex_home / "auth.json")
        share_host_login = host_login is not None and _login_rotates(host_login)
        if share_host_login and native_tools_allowed:
            logger.warning(
                "Sharing the host's Codex login with the sandboxed worker so token "
                "refreshes reach it; disabling Codex's native tools and keeping its file "
                "edits in the workspace so they cannot reach it."
            )
            native_tools_allowed = False

    codex_dir = Path(codex_path).resolve(strict=False).parent
    policy = with_additional_read_roots(policy, [codex_dir, _FRAMEWORK_PACKAGE_ROOT])
    if skills_dir is not None:
        policy = with_additional_read_roots(policy, [skills_dir])
    policy = with_additional_write_roots(policy, [codex_home])
    if signer_readiness is not None:
        if signer_readiness.ca_bundle_path.parent != signer_readiness.socket_path.parent:
            raise ValueError("signer relay and public CA must share one public directory")
        policy = with_additional_read_roots(policy, [signer_readiness.ca_bundle_path.parent])
    else:
        policy = _contain_host_codex_home_links(
            policy, codex_home, share_host_login=share_host_login
        )
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
            native_tools_allowed=native_tools_allowed,
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
