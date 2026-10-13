"""Remote sandbox launcher: omnigent's exec-model provider over a sandbox-control API."""

from __future__ import annotations

import contextlib
import logging
import posixpath
import shlex
import time
from collections.abc import Callable, Iterator, Sequence
from typing import ClassVar

import click

from omnigent.community.sandbox.remote.client import (
    RemoteSandboxClient,
    RemoteSandboxError,
    traced,
)
from omnigent.community.sandbox.remote.config import RemoteSandboxConfig, api_key_from_env
from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import (
    ExecModelHostLauncher,
    RemoteCommandResult,
    SandboxGoneError,
    StaticSandboxProvider,
    render_host_config_write_command,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.types import (
    GitCloneOptions,
    RepoWorkspace,
    SandboxCapabilities,
    clone_dir_names,
)

_logger = logging.getLogger(__name__)

# The state vocabulary the API publishes, mapped to the tri-state
# ``is_running`` answers with. A state this provider does not know reads as
# unknown for the same reason ``transitioning`` does.
_IS_RUNNING = {"running": True, "stopped": False, "absent": False, "transitioning": None}
_DEFAULT_GIT_CLONE_OPTIONS = GitCloneOptions()

# What the provider declares when the service cannot be asked. Narrow on
# purpose: the wake path reads ``capabilities`` without calling ``prepare``
# first, and answering "no resume" there costs a fresh relaunch, where
# answering "resume" against an unreachable service strands the session.
_UNKNOWN_SERVICE = SandboxCapabilities(
    managed_launch=True, programmatic_terminate=True, provisions_repos=True
)

# Waits between attempts of a start that failed transiently. Every attempt
# targets the same sandbox id, so a retry reuses whatever the service already
# built for it rather than starting over.
_START_RETRY_DELAYS_S = (2.0, 5.0)

_HOST_LOG_PATH = "/tmp/omnigent-host.log"
# Prefixes the line the startup script prints with the sandbox's $HOME.
_HOME_MARKER = "OMNIGENT_SANDBOX_HOME="

# Stops any ``omnigent host`` (and its supervisor) left running from an earlier
# generation in the same sandbox, so its revoked token does not race the new
# host. The script's own ancestors carry the startup script in their argv, so
# they are excluded.
_STOP_PREVIOUS_HOST_SCRIPT = """\
import os, signal, subprocess, time

try:
    rows = subprocess.check_output(["ps", "-eo", "pid=,ppid=,args="], text=True)
except Exception:
    raise SystemExit(0)
parents, args = {}, {}
for row in rows.splitlines():
    parts = row.split(None, 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        parents[int(parts[0])] = int(parts[1])
        args[int(parts[0])] = parts[2]
ancestors, pid = set(), os.getpid()
while pid > 1 and pid not in ancestors:
    ancestors.add(pid)
    pid = parents.get(pid, 0)
targets = [p for p, a in args.items() if p not in ancestors and "omnigent host" in a]
for sig in (signal.SIGTERM, signal.SIGKILL):
    for target in targets:
        try:
            os.kill(target, sig)
        except (ProcessLookupError, PermissionError):
            pass
    if targets and sig == signal.SIGTERM:
        time.sleep(0.5)
"""


@contextlib.contextmanager
def _as_click_exception() -> Iterator[None]:
    """Translate a client error into the exception omnigent's callers handle."""
    try:
        yield
    except RemoteSandboxError as exc:
        raise click.ClickException(str(exc)) from exc


class RemoteSandboxLauncher(ExecModelHostLauncher, StaticSandboxProvider):
    """Managed-launch provider that runs ``omnigent host`` in a remote sandbox.

    ``provision`` reserves an id, then materializes the sandbox: it is the
    call carrying the repository, and it blocks until the service reports
    the sandbox usable. ``start_host`` is the inherited exec bootstrap and
    assumes a live sandbox, so it must never start one itself.
    """

    provider: ClassVar[str] = "remote"
    # A host name maps to one stable sandbox id whose workspace survives a
    # stop, so a relaunch restarts the host in place instead of deleting it.
    keeps_workspace_on_relaunch: ClassVar[bool] = True
    # The legacy class var, kept in step with the property below. Unlike
    # resume, this one is a fact about the provider rather than the service:
    # put/stream_exec/exec_foreground are unimplemented.
    supports_cli_bootstrap: ClassVar[bool] = False

    def __init__(
        self,
        config: RemoteSandboxConfig | None = None,
        *,
        client: RemoteSandboxClient | None = None,
        principal: str | None = None,
    ) -> None:
        self._config = config or RemoteSandboxConfig()
        self._client = client
        self._client_injected = client is not None
        self._principal = principal.strip() if principal is not None else None
        if self._principal == "":
            raise RemoteSandboxError("remote sandbox requests require an authenticated principal")
        if client is not None:
            if self._principal is not None and client.principal != self._principal:
                raise RemoteSandboxError("remote sandbox client principal does not match launcher")
            self._principal = client.principal
        self._capabilities: SandboxCapabilities | None = None

    def bind_principal(self, principal: str) -> None:
        """Bind the authenticated account username to control-plane calls."""
        principal = principal.strip()
        if not principal:
            raise RemoteSandboxError("remote sandbox requests require an authenticated principal")
        if self._client is not None and self._client.principal != principal:
            if self._client_injected:
                raise RemoteSandboxError(
                    "remote sandbox client principal does not match host owner"
                )
            self._client.close()
            self._client = None
        self._principal = principal

    @traced
    def principal_is_eligible(self, principal: str) -> bool:
        """Ask the API-key-protected control plane whether *principal* may register."""
        self.bind_principal(principal)
        return self._api().check_access()

    @traced
    def list_repos(self, principal: str) -> tuple[list[dict[str, object]], bool] | None:
        """Return the service's repo-picker list for *principal*, or ``None``.

        ``None`` means the service has no listing route; errors raise
        :class:`RemoteSandboxError` so the caller can fail visibly.
        """
        self.bind_principal(principal)
        return self._api().list_repos()

    # -- static sandboxes ----------------------------------------------------
    # These raise RemoteSandboxError (not click) so callers can map the
    # service's status code (404 unknown VM, 400 refused name, 503 unprovisioned).

    @traced
    def list_available_sandboxes(self) -> list[dict[str, object]]:
        """Every VM the bound principal could add as a static sandbox."""
        return self._api().list_available_vms()

    @traced
    def list_static_sandboxes(self) -> list[dict[str, object]]:
        """The bound principal's static sandboxes, with live status."""
        return self._api().list_static_sandboxes()

    @traced
    def get_static_sandbox(self, sandbox_id: str) -> dict[str, object] | None:
        """The bound principal's entry for *sandbox_id*, or ``None``."""
        return self._api().get_static_sandbox(sandbox_id)

    @traced
    def resume_static_sandbox(self, sandbox_id: str) -> dict[str, object]:
        """Wake a paused static sandbox; returns its entry once it is running.

        :raises SandboxGoneError: The VM no longer exists.
        """
        try:
            return self._api().resume_static_sandbox(sandbox_id)
        except RemoteSandboxError as exc:
            if exc.code == "SANDBOX_GONE":
                raise SandboxGoneError(f"VM {sandbox_id} no longer exists") from exc
            raise

    @traced
    def static_checkout_path(self, sandbox_id: str, repo_url: str) -> str:
        """Where the static sandbox keeps its checkout of *repo_url*."""
        return self._api().static_checkout_path(sandbox_id, repo_url)

    @traced
    def diagnose_host(self, sandbox_id: str, server_url: str) -> dict[str, object]:
        """The service's structured report on the static sandbox's ``omnigent host``."""
        return self._api().diagnose_static_host(sandbox_id, server_url)

    @traced
    def put_static_sandbox(self, sandbox_id: str, label: str) -> dict[str, object]:
        """Add or rename one of the bound principal's static sandboxes."""
        return self._api().put_static_sandbox(sandbox_id, label)

    @traced
    def delete_static_sandbox(self, sandbox_id: str) -> bool:
        """Remove a static-sandbox entry; the VM is left alone."""
        return self._api().delete_static_sandbox(sandbox_id)

    @traced
    def link_static_host(self, sandbox_id: str, host_id: str) -> dict[str, object]:
        """Link *host_id* to a static sandbox unless one is linked; returns the entry."""
        return self._api().link_static_host(sandbox_id, host_id)

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Feature flags the service declares, fetched once and cached.

        A backend-agnostic provider cannot know any backend's capabilities
        statically, so they are read rather than declared. ``prepare``
        primes the cache; the fetch is lazy as well because the wake path
        builds a fresh launcher and reads this without preparing it.
        """
        if self._capabilities is None:
            try:
                self._capabilities = self._service_capabilities()
            except RemoteSandboxError:
                _logger.warning(
                    "could not read sandbox capabilities from the service; declaring "
                    "the managed-launch baseline only",
                    exc_info=True,
                )
                return _UNKNOWN_SERVICE
        return self._capabilities

    def _service_capabilities(self) -> SandboxCapabilities:
        """Read the capabilities the service owns; hardcode the rest.

        ``resume_stopped`` (whether a stopped sandbox comes back with its
        workspace intact), ``multi_repo`` (whether start clones several
        repos), and ``static_sandboxes`` (whether the service keeps the
        user's own VMs) are backend facts, so only the service can answer them. The
        rest are facts about this class: whether it has
        a ``put`` / ``stream_exec`` / ``exec_foreground`` /
        ``forward_local_port`` method at all, and whether its ``provision``
        takes ``repos``, none of which the service can see. Taking them from
        the response would let it turn on a method that does not exist, and
        the server would call it and raise mid-session. Any other field the
        response carries is ignored.
        """
        if self._client is not None or self._principal is not None:
            declared = self._api().capabilities()
        else:
            # /v1/info reads this before anyone signs in; the route needs only the key.
            with contextlib.closing(self._unbound_client()) as client:
                declared = client.capabilities()
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            programmatic_terminate=True,
            resume_stopped=declared.get("resume_stopped") is True,
            multi_repo=declared.get("multi_repo") is True,
            file_copy=False,
            streaming_exec=False,
            foreground_exec=False,
            local_port_forward=False,
            provisions_repos=True,
            static_sandboxes=declared.get("static_sandboxes") is True,
        )

    def _unbound_client(self) -> RemoteSandboxClient:
        """A client acting for no principal, for the key-only routes."""
        return RemoteSandboxClient(
            self._config, api_key_from_env(self._config.api_key_env_var), None
        )

    def _api(self) -> RemoteSandboxClient:
        if self._principal is None:
            raise RemoteSandboxError("remote sandbox requests require an authenticated principal")
        if self._client is None:
            self._client = RemoteSandboxClient(
                self._config,
                api_key_from_env(self._config.api_key_env_var),
                self._principal,
            )
        return self._client

    # -- lifecycle -----------------------------------------------------------

    @traced
    def prepare(self) -> None:
        try:
            self._api().health()
            self._capabilities = self._service_capabilities()
        except RemoteSandboxError as exc:
            raise click.ClickException(f"sandbox API unreachable: {exc}") from exc

    @traced
    def provision(self, name: str, *, repos: Sequence[RepoWorkspace] = ()) -> str:
        """Create the sandbox and block until the service reports it usable.

        The service clones during sandbox start, so the repositories ride the
        start call rather than a later exec. The server only sends more than
        one when the service declares ``multi_repo``.

        A transient start failure is retried against the same sandbox id, so
        the retry picks up the workspace the service already has for it. A
        sandbox that existed before this call holds the user's workspace and
        is never deleted here; one this call created is removed on failure.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
        :param repos: Repositories for the sandbox; empty for an empty
            workspace.
        :returns: The service-derived sandbox id.
        """
        with _as_click_exception():
            sandbox_id = self._api().reserve(name)
        existed = self._existed(sandbox_id)
        try:
            with _as_click_exception():
                self._start_with_retries(sandbox_id, repos)
        except BaseException:
            # The caller learns the id only on return, so a sandbox left
            # half-built here is ours to remove. Delete tolerates an
            # already-absent sandbox.
            if not existed:
                with contextlib.suppress(RemoteSandboxError):
                    self._api().delete(sandbox_id)
            raise
        return sandbox_id

    def _existed(self, sandbox_id: str) -> bool:
        """Whether the service already has a sandbox under *sandbox_id*.

        An unreadable state reads as existing, so a failed start never deletes
        a workspace this call cannot prove it created.
        """
        try:
            return self._api().state(sandbox_id) != "absent"
        except RemoteSandboxError:
            _logger.warning(
                "could not read sandbox %s state before start", sandbox_id, exc_info=True
            )
            return True

    def _start_with_retries(self, sandbox_id: str, repos: Sequence[RepoWorkspace]) -> None:
        for attempt, delay in enumerate((*_START_RETRY_DELAYS_S, None), start=1):
            try:
                self._api().start(
                    sandbox_id,
                    repo_url=repos[0].url if repos else None,
                    repo_branch=repos[0].branch if repos else None,
                    repos=[(repo.url, repo.branch) for repo in repos],
                )
                return
            except RemoteSandboxError as exc:
                if delay is None or not exc.retryable:
                    raise
                _logger.warning(
                    "sandbox %s start attempt %d failed; retrying in %.0fs: %s",
                    sandbox_id,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)

    @traced
    def terminate(self, sandbox_id: str) -> None:
        with _as_click_exception():
            self._api().delete(sandbox_id)

    @traced
    def terminate_with_failure_log(self, sandbox_id: str) -> None:
        """Delete a sandbox while asking the service to capture its failure log."""
        with _as_click_exception():
            self._api().delete(sandbox_id, capture_failure_log=True)

    @traced
    def resume(self, sandbox_id: str) -> None:
        with _as_click_exception():
            self._api().resume(sandbox_id)

    @traced
    def is_running(self, sandbox_id: str) -> bool | None:
        try:
            state = self._api().state(sandbox_id)
        except RemoteSandboxError:
            return None
        # ``False`` is the destructive answer: the caller reads it as evidence
        # a wake is needed and tears the host's tunnels down first. So a state
        # that is neither definitely up nor definitely down reads as unknown.
        return _IS_RUNNING.get(state)

    # -- exec transport ------------------------------------------------------

    @traced
    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        with _as_click_exception():
            returncode, stdout, stderr = self._api().run_script(sandbox_id, command)
        if check and returncode != 0:
            # The service's explanation when it has one, else the tail of the
            # command's own combined output.
            detail = stderr or stdout[-2000:] or "no output"
            raise click.ClickException(
                f"remote command failed in sandbox '{sandbox_id}': {command}\n{detail}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    @traced
    def stop_host(self, sandbox_id: str) -> None:
        """Stop every ``omnigent host`` in the sandbox, leaving the sandbox running."""
        self.run(sandbox_id, f"python3 -c {shlex.quote(_STOP_PREVIOUS_HOST_SCRIPT)} || true")

    @traced
    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Start ``omnigent host`` in the sandbox with a single exec.

        Each exec is a full workflow on the service, so this composes the base
        bootstrap's steps (workspace directory, host config, stopping a
        previous host, the supervised launch) into one script. The service
        already cloned every repository during ``provision``; the workspace
        call only locates each clone and checks out its branch.

        :returns: The single clone directory, the directory parenting several
            clones, or ``$HOME/workspace`` when no repos were requested.
        """
        if any(repo.git_clone != _DEFAULT_GIT_CLONE_OPTIONS for repo in repos):
            raise click.ClickException(
                f"sandbox provider '{self.provider}' does not support sandbox.git_clone options"
            )
        steps = ['test -n "${HOME:-}" || { echo "sandbox has no usable $HOME" >&2; exit 1; }']
        workspace: str | None = None
        if repos:
            clone_dirs = [
                self.materialize_workspace(
                    sandbox_id,
                    workspace="",
                    repo_url=repo.url,
                    repo_branch=repo.branch,
                    repo_name=dirname,
                    on_stage=on_stage,
                )
                for repo, dirname in zip(repos, clone_dir_names(repos), strict=True)
            ]
            workspace = clone_dirs[0] if len(clone_dirs) == 1 else posixpath.commonpath(clone_dirs)
        if on_stage is not None:
            on_stage("starting")
        steps.append('mkdir -p "$HOME/workspace"')
        if host_config is not None or self.capabilities.resume_stopped:
            steps.append(render_host_config_write_command(host_config or {}))
        steps.append(f"python3 -c {shlex.quote(_STOP_PREVIOUS_HOST_SCRIPT)} || true")
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in (
                (HOST_TOKEN_ENV_VAR, token),
                (HOST_ID_ENV_VAR, host_id),
                (HOST_NAME_ENV_VAR, host_name),
            )
        )
        command = f"{env_prefix} omnigent host --server {shlex.quote(server_url)}"
        steps.append(
            f"setsid nohup sh -c {shlex.quote(supervise_host_command(command))} "
            f">> {_HOST_LOG_PATH} 2>&1 < /dev/null &"
        )
        steps.append(f'printf "%s%s\\n" {shlex.quote(_HOME_MARKER)} "$HOME"')
        result = self.run(sandbox_id, "set -eu\n" + "\n".join(steps))
        if workspace is not None:
            return workspace
        home = next(
            (
                line[len(_HOME_MARKER) :]
                for line in reversed(result.stdout.splitlines())
                if line.startswith(_HOME_MARKER)
            ),
            "",
        )
        if not home:
            raise click.ClickException(
                f"could not resolve $HOME inside sandbox '{sandbox_id}': {result.stdout[-2000:]}"
            )
        return f"{home}/workspace"

    @traced
    def materialize_workspace(
        self,
        sandbox_id: str,
        *,
        workspace: str,
        repo_url: str,
        repo_branch: str | None,
        repo_name: str | None,
        on_stage: Callable[[str], None] | None = None,
        git_clone: GitCloneOptions = _DEFAULT_GIT_CLONE_OPTIONS,
    ) -> str:
        """Ask the service for the repository and return where it put it.

        The service owns the checkout, so *workspace* — the directory the
        inherited bootstrap created — is not where the repository lands, and
        the returned path is the service's answer rather than a path this
        provider composed.
        """
        if git_clone != _DEFAULT_GIT_CLONE_OPTIONS:
            raise click.ClickException(
                f"sandbox provider '{self.provider}' does not support sandbox.git_clone options"
            )
        del workspace
        if on_stage is not None:
            on_stage("cloning")
        with _as_click_exception():
            return self._api().workspace(
                sandbox_id, repo_url=repo_url, repo_branch=repo_branch, repo_name=repo_name
            )
