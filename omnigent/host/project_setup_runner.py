"""Run a project's setup scripts on the host around a runner launch.

Two phases, both best-effort (a failure becomes a warning, never a failed launch):

1. ``env_commands`` are sourced in the user's interactive login shell, so
   rc-file tool init (``conda init``, ``nvm``) is loaded. The environment is
   dumped before and after; only the *delta* is applied to the runner, so the
   runner env allowlist still keeps the rest of the user's shell env out.
2. ``setup_commands`` run once per linked git worktree as one script that stops
   at the first failing command, recorded by a marker in the worktree's private
   git dir. Re-running takes an explicit request.

Warnings name the failure and the host log; command output stays in host logs,
since warnings are shown in a session others may read.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import cachetools

from omnigent._platform import posix_login_shell
from omnigent.host.git_worktree import linked_worktree
from omnigent.inner import _proc
from omnigent.install_ledger import atomic_write_json
from omnigent.process_logging import (
    display_log_path,
    open_process_log_file,
    process_log_dir,
    read_log_tail,
    redact_log_text,
)
from omnigent.project_setup import SETUP_BUDGET_S, ProjectSetup, commands_digest

_logger = logging.getLogger(__name__)

#: Bound for the env probe: rc files plus ``conda activate`` take a few seconds.
#: Kept under the server's 10s relaunch acknowledgement wait so a timeout's
#: warning still reaches the session.
ENV_PROBE_TIMEOUT_S = 7.0
#: A probe slower than this is logged so users can trim their rc files.
ENV_PROBE_SLOW_S = 2.0
#: How long a successful probe is reused for the same workspace and commands.
#: Every probe runs the user's rc files, which may start agents or print prompts.
ENV_PROBE_CACHE_TTL_S = 3600.0
#: How long a failed probe is remembered, so a slow or broken rc file does not
#: cost every relaunch the full timeout.
ENV_PROBE_FAILURE_TTL_S = 60.0
#: Grace a timed-out setup gets to clean up before it is killed.
SETUP_TERMINATE_GRACE_S = 5.0
#: Most of a setup log returned to a reader, from its end.
SETUP_LOG_READ_LIMIT_BYTES = 256 * 1024
#: Output lines copied to the host's own log for a failure.
_LOG_TAIL_LINES = 40

SETUP_MARKER_NAME = "omnigent-setup.json"
_LOG_DESTINATION = "project-setup"

#: Variables that differ between any two shells and say nothing about the project.
_VOLATILE_ENV_KEYS = frozenset(
    {
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "PS1",
        "PS2",
        "PS4",
        "COLUMNS",
        "LINES",
        # An rc that starts ssh-agent or gpg-agent makes a new one per probe;
        # forwarding its socket would point the agent at a process that may go away.
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GPG_AGENT_INFO",
        # Set by the probe itself.
        "DISABLE_AUTO_UPDATE",
        "ZSH_TMUX_AUTOSTART",
        "ZSH_TMUX_AUTOSTARTED",
    }
)
_VOLATILE_ENV_PREFIXES = ("TERM", "TMUX", "SHELL_SESSION_", "ITERM_", "__CF")
#: Runner wiring that project commands must never override.
_PROTECTED_ENV_PREFIXES = ("RUNNER_", "OMNIGENT_")
#: Variables that change how a process loads code or libraries. The runner and
#: its harness are Python processes started with this env, so a project's
#: ``PYTHONPATH`` or ``LD_LIBRARY_PATH`` could break them; they are withheld.
_RUNTIME_ENV_PREFIXES = ("PYTHON", "LD_", "DYLD_")

#: Prints the exported environment NUL-separated, so values may hold newlines.
_ENV_DUMP = "/usr/bin/env -0"
#: Tells rc files a non-interactive probe is running: oh-my-zsh skips its
#: update prompt and tmux autostart, which would otherwise hang the probe.
_PROBE_ENV = {
    "OMNIGENT_ENV_PROBE": "1",
    "DISABLE_AUTO_UPDATE": "true",
    "ZSH_TMUX_AUTOSTART": "false",
    "ZSH_TMUX_AUTOSTARTED": "true",
}
#: Keeps git from waiting on a credential prompt nobody can answer.
_NON_INTERACTIVE_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


@dataclass(frozen=True)
class EnvDelta:
    """
    Environment changes produced by a project's ``env_commands``.

    :param set_vars: Variables added or changed, e.g. ``{"CONDA_DEFAULT_ENV": "proj"}``.
    :param unset_vars: Variables the commands removed, e.g. ``frozenset({"VIRTUAL_ENV"})``.
    :param path_prepend: ``PATH`` entries the commands added, in order, e.g.
        ``("/opt/conda/envs/proj/bin",)``. Re-asserted after a login shell's
        profile, which would otherwise move system dirs ahead of them.
    :param withheld: Runtime variables the commands changed that are not
        applied, e.g. ``frozenset({"PYTHONPATH"})``.
    """

    set_vars: dict[str, str] = field(default_factory=dict)
    unset_vars: frozenset[str] = frozenset()
    path_prepend: tuple[str, ...] = ()
    withheld: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        """Whether the commands changed nothing that is applied."""
        return not self.set_vars and not self.unset_vars


@dataclass
class ProjectSetupOutcome:
    """
    Result of preparing a launch's project env.

    :param env_delta: Changes to layer onto the runner env.
    :param warnings: Failures to show on the session, e.g.
        ``["project env commands failed (exit 1); log: ~/..."]``.
    """

    env_delta: EnvDelta = field(default_factory=EnvDelta)
    warnings: list[str] = field(default_factory=list)


def _is_ignored(key: str) -> bool:
    return (
        key in _VOLATILE_ENV_KEYS
        or key.startswith(_VOLATILE_ENV_PREFIXES)
        or key.startswith(_PROTECTED_ENV_PREFIXES)
    )


def _path_entries(path: str) -> list[str]:
    return [entry for entry in path.split(os.pathsep) if entry]


def compute_env_delta(before: dict[str, str], after: dict[str, str]) -> EnvDelta:
    """
    Diff two environment snapshots, ignoring volatile, protected and runtime keys.

    :param before: Environment of the shell before the commands ran.
    :param after: Environment after the commands ran.
    :returns: The changes to apply, and the runtime keys withheld.
    """
    changed = {
        key: value
        for key, value in after.items()
        if before.get(key) != value and not _is_ignored(key)
    }
    removed = {key for key in before if key not in after and not _is_ignored(key)}
    withheld = {key for key in (*changed, *removed) if key.startswith(_RUNTIME_ENV_PREFIXES)}
    before_path = set(_path_entries(before.get("PATH", "")))
    return EnvDelta(
        set_vars={key: value for key, value in changed.items() if key not in withheld},
        unset_vars=frozenset(removed - withheld),
        path_prepend=tuple(
            entry for entry in _path_entries(after.get("PATH", "")) if entry not in before_path
        ),
        withheld=frozenset(withheld),
    )


def apply_env_delta(env: dict[str, str], delta: EnvDelta) -> None:
    """
    Layer an env delta onto an environment in place.

    ``PATH`` is merged: the project's order first, then base entries it lacks,
    so a runner's pinned tool dir stays reachable. Protected wiring keys are
    skipped even if a delta carries them.

    :param env: Environment to update, e.g. a runner env from ``_build_runner_env``.
    :param delta: Changes from :func:`compute_env_delta`.
    """
    for key in delta.unset_vars:
        if not key.startswith(_PROTECTED_ENV_PREFIXES) and key != "PATH":
            env.pop(key, None)
    for key, value in delta.set_vars.items():
        if key.startswith(_PROTECTED_ENV_PREFIXES):
            continue
        env[key] = _merge_paths(value, env.get("PATH", "")) if key == "PATH" else value


def _merge_paths(project_path: str, base_path: str) -> str:
    entries = _path_entries(project_path)
    seen = set(entries)
    entries += [entry for entry in _path_entries(base_path) if entry not in seen]
    return os.pathsep.join(entries)


def _tail(text: str, lines: int = _LOG_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


#: Project command processes alive now, so host shutdown can stop them.
_live_processes: set[subprocess.Popen[bytes]] = set()
#: Commands killed by host shutdown; their setup records no marker, so it re-runs.
_interrupted_processes: set[subprocess.Popen[bytes]] = set()
_live_processes_lock = threading.Lock()


class InterruptedByShutdownError(Exception):
    """A project command was killed because the host is shutting down."""


def kill_running_project_commands() -> None:
    """
    Kill every running project command and its children (host shutdown).

    Each command runs in its own process group, so it would otherwise outlive the host.
    """
    with _live_processes_lock:
        processes = list(_live_processes)
        _interrupted_processes.update(processes)
    for process in processes:
        _proc.kill_tree(process)


def _run_bounded(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    output_path: Path,
    terminate_grace_s: float = 0.0,
) -> int | None:
    """
    Run a command in its own process group, appending its output to a file.

    :param terminate_grace_s: On timeout, seconds to let it stop gracefully
        before killing it, so an install can release its lock files.
    :returns: The exit code, or ``None`` when it timed out and was stopped.
    :raises InterruptedByShutdownError: If host shutdown killed it.
    """
    with open(output_path, "ab") as output:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            **_proc.spawn_kwargs(),
        )
        with _live_processes_lock:
            _live_processes.add(process)
        try:
            exit_code: int | None = process.wait(timeout=max(timeout, 0.1))
        except subprocess.TimeoutExpired:
            _proc.terminate_tree(process, grace=terminate_grace_s)
            _proc.kill_tree(process)
            exit_code = None
        finally:
            with _live_processes_lock:
                _live_processes.discard(process)
                interrupted = process in _interrupted_processes
                _interrupted_processes.discard(process)
        if interrupted:
            raise InterruptedByShutdownError
        return exit_code


def _script_text(lines: Iterable[str], *, prelude: Iterable[str] = ()) -> str:
    return "\n".join((*prelude, *lines)) + "\n"


def _probe_script(script_path: str, before_path: str, after_path: str) -> str:
    return (
        f"{_ENV_DUMP} > {shlex.quote(before_path)} || exit 97\n"
        f". {shlex.quote(script_path)} && {_ENV_DUMP} > {shlex.quote(after_path)}\n"
    )


def _read_env_dump(path: str) -> dict[str, str]:
    """Parse ``env -0`` output into a mapping."""
    with open(path, "rb") as dump:
        entries = dump.read().decode("utf-8", errors="surrogateescape").split("\0")
    return dict(entry.split("=", 1) for entry in entries if "=" in entry)


def _keep_probe_failure_output(output_path: Path, digest: str) -> str:
    """
    Save a failed probe's output to one host log per command list.

    One file per command list (overwritten), so repeated failures neither pile
    up logs nor change the warning text the server deduplicates on.

    :returns: The log's display path.
    """
    log_dir = process_log_dir(_LOG_DESTINATION)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"project-env-{digest[:12]}.log"
    output = output_path.read_bytes() if output_path.exists() else b""
    log_path.write_bytes(output)
    _logger.warning(
        "Project env commands failed; output tail:\n%s",
        redact_log_text(_tail(output.decode("utf-8", errors="replace"))),
    )
    return display_log_path(log_path)


def probe_env_delta(
    commands: tuple[str, ...], *, workspace: str, timeout: float = ENV_PROBE_TIMEOUT_S
) -> tuple[EnvDelta | None, str | None]:
    """
    Source ``env_commands`` in the user's interactive login shell and diff the env.

    The commands are one script, as if sourced from a terminal; it succeeds when
    its last command does.

    :param commands: Script lines, e.g. ``("conda activate proj",)``.
    :param workspace: Directory to run them in.
    :param timeout: Seconds before the shell is killed.
    :returns: ``(delta, None)`` on success, or ``(None, warning)`` on failure.
    """
    shell = posix_login_shell()
    if shell is None:
        return None, "project env commands need a POSIX shell; skipped on this host"
    with tempfile.TemporaryDirectory(prefix="omnigent-env-probe-") as scratch:
        before_path = os.path.join(scratch, "before.env")
        after_path = os.path.join(scratch, "after.env")
        script_path = os.path.join(scratch, "env_commands.sh")
        Path(script_path).write_text(_script_text(commands))
        output_path = Path(scratch) / "output.log"
        started = time.monotonic()
        exit_code = _run_bounded(
            [shell, "-i", "-l", "-c", _probe_script(script_path, before_path, after_path)],
            cwd=workspace,
            env={**os.environ, **_PROBE_ENV},
            timeout=timeout,
            output_path=output_path,
        )
        elapsed = time.monotonic() - started
        if elapsed > ENV_PROBE_SLOW_S:
            _logger.warning("Project env commands took %.1fs in %s", elapsed, workspace)
        if exit_code != 0 or not os.path.exists(after_path):
            outcome = (
                f"timed out after {timeout:.0f}s" if exit_code is None else f"exit {exit_code}"
            )
            log = _keep_probe_failure_output(output_path, commands_digest(commands))
            return None, f"project env commands failed ({outcome}); log: {log}"
        try:
            before, after = _read_env_dump(before_path), _read_env_dump(after_path)
        except OSError as exc:
            return None, f"could not read the environment from project env commands: {exc}"
    return compute_env_delta(before, after), None


_ProbeResult = tuple[EnvDelta | None, str | None]
_ProbeKey = tuple[str, str, str]


class EnvProbeCache:
    """
    Remembers probes per workspace and commands: successes long, failures briefly.

    Concurrent launches for the same key share one probe, so a host restart
    relaunching many sessions does not start a login shell per session.
    """

    def __init__(
        self,
        ttl_s: float = ENV_PROBE_CACHE_TTL_S,
        failure_ttl_s: float = ENV_PROBE_FAILURE_TTL_S,
        maxsize: int = 256,
    ) -> None:
        def time_to_use(_key: _ProbeKey, result: _ProbeResult, now: float) -> float:
            return now + (ttl_s if result[0] is not None else failure_ttl_s)

        self._entries: cachetools.TLRUCache[_ProbeKey, _ProbeResult] = cachetools.TLRUCache(
            maxsize=maxsize, ttu=time_to_use, timer=time.monotonic
        )
        self._lock = threading.Lock()
        self._key_locks: dict[_ProbeKey, threading.Lock] = {}

    @staticmethod
    def _key(workspace: str, commands: tuple[str, ...]) -> _ProbeKey:
        return (workspace, os.environ.get("SHELL", ""), commands_digest(commands))

    def probe(self, workspace: str, commands: tuple[str, ...]) -> _ProbeResult:
        """
        Return the remembered probe result, probing once if none is current.

        :returns: ``(delta, warning)`` as from :func:`probe_env_delta`.
        """
        key = self._key(workspace, commands)
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                cached = self._entries.get(key)
            if cached is not None:
                return cached
            result = probe_env_delta(commands, workspace=workspace)
            with self._lock:
                self._entries[key] = result
            return result

    def invalidate(self, workspace: str) -> None:
        """Forget every probe for *workspace*, e.g. before an explicit re-run."""
        with self._lock:
            for key in [key for key in self._entries if key[0] == workspace]:
                self._entries.pop(key, None)


def prepare_project_env(
    setup: ProjectSetup, *, workspace: str, cache: EnvProbeCache
) -> ProjectSetupOutcome:
    """
    Resolve the env delta for one runner launch (blocking; call off the loop).

    :param setup: The project's commands.
    :param workspace: Session workspace on this host.
    :param cache: Host-lifetime env probe cache.
    :returns: The env delta for the runner and any warnings.
    """
    outcome = ProjectSetupOutcome()
    if not setup.env_commands:
        return outcome
    delta, warning = cache.probe(workspace, setup.env_commands)
    if warning:
        outcome.warnings.append(warning)
    if delta is not None:
        outcome.env_delta = delta
        if delta.withheld:
            names = ", ".join(sorted(delta.withheld))
            outcome.warnings.append(
                f"project env commands set {names}, which would change how Omnigent "
                "itself starts; not applied to the agent"
            )
    return outcome


#: Worktree git dirs whose setup is running now, so two sessions starting in
#: the same worktree don't run the installs twice; maps to its log once known.
_running_setups: dict[Path, Path | None] = {}
_running_setups_lock = threading.Lock()


def pending_setup_marker(workspace: str) -> Path | None:
    """
    Return the marker a worktree's setup will write, or ``None`` if none is due.

    A runner given this path waits for it before its first turn.

    :param workspace: Session workspace.
    :returns: The marker path for a linked worktree whose setup has not run yet.
    """
    worktree = linked_worktree(workspace)
    if worktree is None or (worktree.git_dir / SETUP_MARKER_NAME).exists():
        return None
    return worktree.git_dir / SETUP_MARKER_NAME


def clear_setup_marker(workspace: str) -> bool:
    """
    Forget that a worktree's setup ran, so the next request runs it again.

    :param workspace: Directory inside the linked worktree.
    :returns: Whether a marker was removed.
    """
    worktree = linked_worktree(workspace)
    if worktree is None:
        return False
    try:
        (worktree.git_dir / SETUP_MARKER_NAME).unlink()
    except FileNotFoundError:
        return False
    return True


def claim_worktree_setup(*, workspace: str) -> Path | None:
    """
    Decide whether this launch should run the worktree's setup, and claim it.

    Setup runs once per worktree: editing the commands does not re-run it in
    a worktree that already ran them, where an agent may be working.

    :param workspace: Session workspace.
    :returns: The claimed worktree git dir, to pass to :func:`run_worktree_setup`;
        ``None`` when the workspace is not a linked worktree, its setup already
        ran, or another launch is running it.
    """
    marker = pending_setup_marker(workspace)
    if marker is None:
        return None
    git_dir = marker.parent
    with _running_setups_lock:
        if git_dir in _running_setups:
            return None
        _running_setups[git_dir] = None
    return git_dir


def release_worktree_claim(git_dir: Path) -> None:
    """Give up a claim from :func:`claim_worktree_setup` without running the setup."""
    with _running_setups_lock:
        _running_setups.pop(git_dir, None)


def release_unclaimed_gate(workspace: str) -> None:
    """
    Unblock a runner waiting on a setup that will not run.

    When a launch held its runner but could not start the setup, and no other
    launch is running it, a "skipped" marker releases the runner at once
    instead of after the full wait.

    :param workspace: Session workspace.
    """
    marker = pending_setup_marker(workspace)
    if marker is None:
        return
    with _running_setups_lock:
        if marker.parent not in _running_setups:
            _write_marker_record(
                marker.parent, {"status": "skipped", "finished_at": int(time.time())}
            )


@dataclass(frozen=True)
class WorktreeSetupResult:
    """
    Outcome of a worktree's setup run.

    :param warning: What failed, or ``None`` on success.
    :param log_path: Full output log on the host, if one was opened.
    """

    warning: str | None
    log_path: Path | None


def run_worktree_setup(
    commands: tuple[str, ...],
    *,
    git_dir: Path,
    workspace: str,
    session_id: str | None,
    env: dict[str, str],
    path_prepend: tuple[str, ...] = (),
    budget_s: float = SETUP_BUDGET_S,
) -> WorktreeSetupResult:
    """
    Run a claimed worktree's ``setup_commands`` and record the marker.

    Blocking for up to ``budget_s``; the host runs it in the background after
    the runner starts. The marker is written whatever the outcome, since a
    waiting runner proceeds on it; only host shutdown leaves it unwritten, so
    the next launch runs setup again.

    :param commands: Script lines.
    :param git_dir: Worktree git dir from :func:`claim_worktree_setup`.
    :param workspace: Session workspace inside the worktree.
    :param session_id: Session the setup runs for, exposed as ``OMNIGENT_SESSION_ID``.
    :param env: Base environment, already carrying the project env delta.
    :param path_prepend: The project env's ``PATH`` entries, re-asserted after
        the login shell's profile runs.
    :param budget_s: Total seconds for the script.
    :returns: The outcome and log path.
    """
    try:
        result = _run_claimed_setup(
            commands, git_dir, workspace, session_id, env, path_prepend, budget_s
        )
    except InterruptedByShutdownError:
        with _running_setups_lock:
            _running_setups.pop(git_dir, None)
        return WorktreeSetupResult(warning="interrupted by host shutdown", log_path=None)
    except Exception as exc:
        _logger.exception("Project setup crashed in %s", workspace)
        result = WorktreeSetupResult(
            warning=f"project setup crashed on the host ({type(exc).__name__})", log_path=None
        )
    # Recorded before the claim is released, so no launch sees neither.
    with _running_setups_lock:
        _write_marker_record(
            git_dir,
            {
                "commands_sha256": commands_digest(commands),
                "status": "failed" if result.warning else "succeeded",
                "finished_at": int(time.time()),
                "log_path": str(result.log_path) if result.log_path else None,
            },
        )
        _running_setups.pop(git_dir, None)
    return result


def _write_marker_record(git_dir: Path, record: dict[str, object]) -> None:
    try:
        atomic_write_json(git_dir / SETUP_MARKER_NAME, record)
    except OSError:
        _logger.warning("Could not record project setup marker in %s", git_dir, exc_info=True)


def _run_claimed_setup(
    commands: tuple[str, ...],
    git_dir: Path,
    workspace: str,
    session_id: str | None,
    env: dict[str, str],
    path_prepend: tuple[str, ...],
    budget_s: float,
) -> WorktreeSetupResult:
    log_path, log_file = open_process_log_file(_LOG_DESTINATION, prefix="project-setup-")
    log_file.close()
    with _running_setups_lock:
        _running_setups[git_dir] = log_path
    shell = posix_login_shell()
    if shell is None:
        return WorktreeSetupResult(
            warning="project setup needs a POSIX shell; skipped on this host", log_path=log_path
        )
    worktree = linked_worktree(workspace)
    command_env = {
        **env,
        **_NON_INTERACTIVE_GIT_ENV,
        "OMNIGENT_ROOT_PATH": str(worktree.main_work_tree) if worktree else workspace,
        "OMNIGENT_WORKSPACE": workspace,
        "OMNIGENT_WORKSPACE_NAME": os.path.basename(os.path.normpath(workspace)),
    }
    if session_id:
        command_env["OMNIGENT_SESSION_ID"] = session_id
    # A login shell restores the user's PATH (e.g. Homebrew from .zprofile),
    # which a daemon started by launchd or systemd often lacks. Its profile also
    # moves system dirs ahead of an activated env's bin, so those go back in front.
    prelude = ["set -e"]
    if path_prepend:
        joined = shlex.quote(os.pathsep.join(path_prepend))
        prelude.insert(0, f'export PATH={joined}{os.pathsep}"$PATH"')
    with tempfile.TemporaryDirectory(prefix="omnigent-setup-") as scratch:
        script_path = os.path.join(scratch, "setup_commands.sh")
        Path(script_path).write_text(_script_text(commands, prelude=prelude))
        exit_code = _run_bounded(
            [shell, "-l", script_path],
            cwd=workspace,
            env=command_env,
            timeout=budget_s,
            output_path=log_path,
            terminate_grace_s=SETUP_TERMINATE_GRACE_S,
        )
    if exit_code == 0:
        return WorktreeSetupResult(warning=None, log_path=log_path)
    _logger.warning(
        "Project setup failed in %s; output tail:\n%s",
        workspace,
        redact_log_text(_tail(log_path.read_text(errors="replace"))),
    )
    outcome = f"timed out after {budget_s:.0f}s" if exit_code is None else f"exit {exit_code}"
    return WorktreeSetupResult(warning=f"setup commands failed ({outcome})", log_path=log_path)


def read_setup_log(workspace: str) -> str | None:
    """
    Return the end of a worktree's latest setup log, running or finished, redacted.

    Only a regular file in the host's own project-setup log directory is read:
    the marker naming it sits in the worktree, where the agent can write.

    :param workspace: Session workspace inside the worktree.
    :returns: Up to :data:`SETUP_LOG_READ_LIMIT_BYTES` of log text, or ``None``
        when this worktree has no readable setup log.
    """
    worktree = linked_worktree(workspace)
    if worktree is None:
        return None
    with _running_setups_lock:
        log_path = _running_setups.get(worktree.git_dir)
    if log_path is None:
        try:
            recorded = json.loads((worktree.git_dir / SETUP_MARKER_NAME).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(recorded, dict) or not isinstance(recorded.get("log_path"), str):
            return None
        log_path = Path(recorded["log_path"])
    resolved = log_path.resolve()
    if resolved.parent != process_log_dir(_LOG_DESTINATION).resolve() or not resolved.is_file():
        return None
    return redact_log_text(
        read_log_tail(resolved, SETUP_LOG_READ_LIMIT_BYTES), include_whitespace_credentials=True
    )
