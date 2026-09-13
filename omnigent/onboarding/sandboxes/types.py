"""Public types for the sandbox launcher surface.

These dataclasses and exceptions are the vocabulary used by both the
existing :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
interface and the newer pluggable surface in
:mod:`omnigent.onboarding.sandboxes.registry`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class SandboxError(Exception):
    """Base for all sandbox-provider errors."""


class SandboxConfigError(SandboxError):
    """Sandbox provider configuration is malformed or unavailable."""


class SandboxAuthError(SandboxError):
    """Provider credentials or local tooling are missing/invalid."""


class SandboxCommandError(SandboxError):
    """A command executed inside a sandbox failed.

    :param message: Human-readable reason.
    :param command: The remote command that failed.
    :param returncode: Remote exit code.
    :param stdout: Captured standard output.
    :param stderr: Captured standard error.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class SandboxCapabilities:
    """Feature flags declared by a sandbox provider.

    Providers advertise which primitives they support so callers can fail
    fast and surface actionable messages.

    :param cli_bootstrap: Provider supports ``omnigent sandbox create`` /
        ``connect`` (``put`` / ``stream_exec`` / ``exec_foreground`` /
        ``wheel_install_command``).
    :param managed_launch: Provider supports server-managed
        ``host_type="managed"`` sessions (``prepare`` / ``provision`` /
        ``start_host``).
    :param local_port_forward: Provider can bridge a local port into the
        sandbox for the App OAuth callback flow.
    :param resume_stopped: Provider can resume a stopped sandbox in place
        with its persistent volume.
    :param programmatic_terminate: Provider can terminate a sandbox
        programmatically.
    :param file_copy: Provider supports copying files into the sandbox.
    :param streaming_exec: Provider supports streaming process execution
        inside the sandbox.
    :param foreground_exec: Provider supports a foreground exec that
        inherits local stdio.
    :param classifies_runner_by_agent: Provider stamps the session's
        resolved built-in agent onto the managed runner as platform
        metadata a policy can select on (the Kubernetes runner Pod's
        ``omnigent.ai/agent`` label). When set, the managed launch path
        threads ``agent_name`` into ``start_host``; providers that leave
        it ``False`` never receive the keyword.
    :param snapshot_restore: Resuming a stopped sandbox restores a
        suspend-time snapshot (dependencies installed, caches warm)
        rather than cold-starting it. Only meaningful alongside
        ``resume_stopped``.
    :param multi_repo: Provider can clone more than one repository into a
        single session's workspace. Off by default, so a provider that
        clones only one repo (and every out-of-tree provider) is never
        handed a multi-repo request; providers opt in explicitly.
    """

    cli_bootstrap: bool = False
    managed_launch: bool = False
    local_port_forward: bool = False
    resume_stopped: bool = False
    programmatic_terminate: bool = False
    file_copy: bool = False
    streaming_exec: bool = False
    foreground_exec: bool = False
    classifies_runner_by_agent: bool = False
    # New fields append at the end to preserve positional-constructor
    # compatibility for out-of-tree providers.
    snapshot_restore: bool = False
    multi_repo: bool = False


@dataclass(frozen=True)
class SandboxSpec:
    """Provider-agnostic description of a sandbox to provision."""

    name: str
    image: str | None = None
    cpu: float | None = None
    memory_mib: int | None = None
    disk_gb: int | None = None
    lifetime_s: int | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxInfo:
    """Result of a successful provision or attach."""

    sandbox_id: str
    workspace_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepoWorkspace:
    """
    A single repository to materialize in a managed sandbox's workspace.

    Server code builds these via ``parse_repo_workspace`` (which validates
    the URL and branch); the launcher surface only reads the fields. Lives
    here rather than in the server package so a launcher can accept it
    without importing ``omnigent.server`` (an onboarding→server dependency
    the entrypoint-as-host launchers deliberately avoid).

    :param url: The clone URL with any ``#<branch>`` fragment stripped,
        e.g. ``"https://github.com/org/repo.git"`` or
        ``"git@github.com:org/repo.git"``.
    :param branch: Branch to clone (``--branch … --single-branch``), or
        ``None`` for the default branch.
    :param repo_name: Directory the clone lands in under the sandbox
        workspace, derived from the URL's last path segment, e.g.
        ``"repo"``.
    """

    url: str
    branch: str | None
    repo_name: str


def _owner_segment(url: str) -> str:
    """
    The owner/org segment of a repo URL (second-to-last path segment), used to
    disambiguate repos whose ``repo_name`` collides. Best-effort: ``""`` when
    the URL has no owner segment.

    ``https://github.com/org-a/api`` → ``"org-a"``;
    ``git@github.com:org-b/api.git`` → ``"org-b"``.
    """
    parts = [p for p in re.split(r"[/:]", url.rstrip("/")) if p]
    return parts[-2] if len(parts) >= 2 else ""


def clone_dir_names(repos: Sequence[RepoWorkspace]) -> list[str]:
    """
    Unique clone-directory names for a list of repos, positionally aligned.

    Each repo clones into ``<workspace>/<name>``. Distinct URLs can derive the
    same ``repo_name`` (e.g. ``org-a/api`` and ``org-b/api`` → ``api``), which
    would collide into one directory and fail the clone; those are qualified by
    owner (``org-a__api``), with a numeric suffix as a last resort. A repo whose
    name is already unique keeps the plain name, so the single-repo working
    directory is unchanged.

    :param repos: The repositories to clone, in order.
    :returns: A directory name per repo, in the same order.
    """
    from collections import Counter

    counts = Counter(r.repo_name for r in repos)
    used: set[str] = set()
    names: list[str] = []
    for repo in repos:
        if counts[repo.repo_name] > 1:
            owner = _owner_segment(repo.url)
            base = f"{owner}__{repo.repo_name}" if owner else repo.repo_name
        else:
            base = repo.repo_name
        name, n = base, 2
        while name in used:
            name = f"{base}-{n}"
            n += 1
        used.add(name)
        names.append(name)
    return names


@dataclass
class HostContext:
    """Context handed to ``start_host`` when launching a managed host."""

    token: str
    host_id: str
    host_name: str
    server_url: str
    repos: list[RepoWorkspace] = field(default_factory=list)
    host_config: dict[str, object] = field(default_factory=dict)
    on_stage: Callable[[str], None] | None = None
