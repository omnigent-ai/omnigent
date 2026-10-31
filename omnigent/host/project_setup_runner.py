"""Run a project's setup commands on the host before a runner launches.

Two phases, both best-effort (a failure becomes a warning, never a failed launch):

1. ``env_commands`` run in the user's interactive login shell, so rc-file tool
   init (``conda init``, ``nvm``) is loaded. The environment is dumped before
   and after the commands; only the *delta* is applied to the runner, so the
   runner env allowlist still keeps the rest of the user's shell env out.
2. ``setup_commands`` run once per linked git worktree, recorded by a marker
   in the worktree's private git dir. Re-running takes an explicit request.

Warnings name the failing step, exit code and host log; command output stays in
the host log, since warnings are shown in a session others may read.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from omnigent._platform import IS_WINDOWS, _resolve_interactive_shell, default_shell_argv
from omnigent.host.git_worktree import WorktreeError, _main_work_tree, _run_git
from omnigent.process_logging import display_log_path, open_process_log_file
from omnigent.project_setup import SETUP_BUDGET_S, ProjectSetup, commands_digest

_logger = logging.getLogger(__name__)

#: Bound for the env probe: rc files plus ``conda activate`` take a few seconds.
#: Kept under the server's 10s relaunch acknowledgement wait so a timeout's
#: warning still reaches the session.
ENV_PROBE_TIMEOUT_S = 7.0
#: A probe slower than this is logged so users can trim their rc files.
ENV_PROBE_SLOW_S = 2.0
#: How long a successful probe is reused for the same workspace and commands.
ENV_PROBE_CACHE_TTL_S = 600.0
#: Output lines copied to the host log for a failure.
_LOG_TAIL_LINES = 40

SETUP_MARKER_NAME = "omnigent-setup.json"

#: Shells whose ``-c`` syntax accepts the POSIX script the probe builds.
_POSIX_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh"})

#: Variables that differ between any two shells and say nothing about the project.
_VOLATILE_ENV_KEYS = frozenset(
    {"PWD", "OLDPWD", "SHLVL", "_", "PS1", "PS2", "PS4", "COLUMNS", "LINES"}
)
_VOLATILE_ENV_PREFIXES = ("TERM", "TMUX", "SHELL_SESSION_", "ITERM_", "__CF")
#: Runner wiring that project commands must never override.
_PROTECTED_ENV_PREFIXES = ("RUNNER_", "OMNIGENT_")

#: Prints the exported environment NUL-separated, so values may hold newlines.
_ENV_DUMP = "/usr/bin/env -0"


@dataclass(frozen=True)
class EnvDelta:
    """
    Environment changes produced by a project's ``env_commands``.

    :param set_vars: Variables added or changed, e.g. ``{"CONDA_DEFAULT_ENV": "proj"}``.
    :param unset_vars: Variables the commands removed, e.g. ``frozenset({"VIRTUAL_ENV"})``.
    """

    set_vars: dict[str, str] = field(default_factory=dict)
    unset_vars: frozenset[str] = frozenset()


@dataclass
class ProjectSetupOutcome:
    """
    Result of preparing a launch.

    :param env_delta: Changes to layer onto the runner env.
    :param warnings: Human-readable failures to show on the session, e.g.
        ``["env command failed (exit 1): ..."]``.
    """

    env_delta: EnvDelta = field(default_factory=EnvDelta)
    warnings: list[str] = field(default_factory=list)


def _is_volatile(key: str) -> bool:
    return key in _VOLATILE_ENV_KEYS or key.startswith(_VOLATILE_ENV_PREFIXES)


def compute_env_delta(before: dict[str, str], after: dict[str, str]) -> EnvDelta:
    """
    Diff two environment snapshots, ignoring volatile and protected keys.

    :param before: Environment of the shell before the commands ran.
    :param after: Environment after the commands ran.
    :returns: Added/changed and removed variables.
    """
    set_vars = {
        key: value
        for key, value in after.items()
        if before.get(key) != value
        and not _is_volatile(key)
        and not key.startswith(_PROTECTED_ENV_PREFIXES)
    }
    unset_vars = frozenset(
        key
        for key in before
        if key not in after
        and not _is_volatile(key)
        and not key.startswith(_PROTECTED_ENV_PREFIXES)
    )
    return EnvDelta(set_vars=set_vars, unset_vars=unset_vars)


def apply_env_delta(env: dict[str, str], delta: EnvDelta) -> None:
    """
    Layer an env delta onto a runner environment in place.

    Protected wiring keys are skipped even if a delta carries them.

    :param env: Runner environment from ``_build_runner_env``.
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
    """
    Keep the project's ``PATH`` order first, then base entries it lacks.

    Activation puts its bin dir first; base entries the runner was started
    with (e.g. a pinned tool dir) stay reachable instead of being dropped.
    """
    entries = [entry for entry in project_path.split(os.pathsep) if entry]
    seen = set(entries)
    entries += [entry for entry in base_path.split(os.pathsep) if entry and entry not in seen]
    return os.pathsep.join(entries)


def _user_shell() -> str | None:
    """
    Resolve the user's POSIX login shell, or ``None`` when it is not POSIX.

    ``$SHELL`` first; a host started by launchd or systemd may lack it, so fall
    back to the account's login shell rather than a bash that skips ``.zshrc``.

    :returns: Absolute shell path, e.g. ``"/bin/zsh"``.
    """
    shell = os.environ.get("SHELL", "").strip()
    if not shell:
        with contextlib.suppress(KeyError, ImportError):
            import pwd

            shell = pwd.getpwuid(os.getuid()).pw_shell
    name = os.path.basename(shell) or "bash"
    if name not in _POSIX_SHELLS:
        return None
    if os.path.isabs(shell) and os.access(shell, os.X_OK):
        return shell
    return _resolve_interactive_shell(name)


def _tail(text: str, lines: int = _LOG_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill a command and anything it started (it runs in its own session)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


#: Project command processes alive now, so host shutdown can stop them.
_live_processes: set[subprocess.Popen[bytes]] = set()
_live_processes_lock = threading.Lock()
#: Commands killed by host shutdown; their setup records no marker, so it re-runs.
_interrupted_processes: set[subprocess.Popen[bytes]] = set()


class InterruptedByShutdownError(Exception):
    """A project command was killed because the host is shutting down."""


def kill_running_project_commands() -> None:
    """Kill every running project command and its children (host shutdown).

    Each command runs in its own session, so it would otherwise outlive the host.
    """
    with _live_processes_lock:
        processes = list(_live_processes)
        _interrupted_processes.update(processes)
    for process in processes:
        _kill_group(process)


def _run_bounded(
    argv: list[str], *, cwd: str, env: dict[str, str], timeout: float, output_path: Path
) -> int | None:
    """
    Run a command in its own process group, appending its output to a file.

    :returns: The exit code, or ``None`` when it timed out and was killed.
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
            start_new_session=True,
        )
        with _live_processes_lock:
            _live_processes.add(process)
        try:
            exit_code: int | None = process.wait(timeout=max(timeout, 0.1))
        except subprocess.TimeoutExpired:
            _kill_group(process)
            exit_code = None
        finally:
            with _live_processes_lock:
                _live_processes.discard(process)
                interrupted = process in _interrupted_processes
                _interrupted_processes.discard(process)
        if interrupted:
            raise InterruptedByShutdownError
        return exit_code


def _probe_script(commands: tuple[str, ...], before_path: str, after_path: str) -> str:
    # Each command runs in a brace group so its own ';' or '||' stays inside it.
    chained = " && ".join(f"{{ {command}\n}}" for command in commands)
    return (
        f"{_ENV_DUMP} > {shlex.quote(before_path)} || exit 97\n"
        f"{chained} && {_ENV_DUMP} > {shlex.quote(after_path)}\n"
    )


def _read_env_dump(path: str) -> dict[str, str]:
    """Parse ``env -0`` output into a mapping."""
    with open(path, "rb") as dump:
        entries = dump.read().decode("utf-8", errors="surrogateescape").split("\0")
    return dict(entry.split("=", 1) for entry in entries if "=" in entry)


def _keep_failure_output(output_path: Path, prefix: str) -> str:
    """Copy a failed probe's output into a host log and return its display path."""
    log_path, log_file = open_process_log_file("project-setup", prefix=prefix)
    with log_file:
        if output_path.exists():
            log_file.write(output_path.read_bytes())
    output = output_path.read_text(errors="replace") if output_path.exists() else ""
    _logger.warning("Project env commands failed; output tail:\n%s", _tail(output))
    return display_log_path(log_path)


def probe_env_delta(
    commands: tuple[str, ...], *, workspace: str, timeout: float = ENV_PROBE_TIMEOUT_S
) -> tuple[EnvDelta | None, str | None]:
    """
    Run ``env_commands`` in the user's interactive login shell and diff the env.

    :param commands: Commands in order, e.g. ``("conda activate proj",)``.
    :param workspace: Directory to run them in.
    :param timeout: Seconds before the shell is killed.
    :returns: ``(delta, None)`` on success, or ``(None, warning)`` on failure.
    """
    if IS_WINDOWS:
        return None, "project env commands need a POSIX shell; skipped on Windows"
    shell = _user_shell()
    if shell is None:
        return None, (
            f"project env commands need bash or zsh; $SHELL is "
            f"{os.environ.get('SHELL') or 'unset'}, so they were skipped"
        )
    with tempfile.TemporaryDirectory(prefix="omnigent-env-probe-") as scratch:
        before_path = os.path.join(scratch, "before.env")
        after_path = os.path.join(scratch, "after.env")
        output_path = Path(scratch) / "output.log"
        started = time.monotonic()
        exit_code = _run_bounded(
            [shell, "-i", "-l", "-c", _probe_script(commands, before_path, after_path)],
            cwd=workspace,
            env=os.environ.copy(),
            timeout=timeout,
            output_path=output_path,
        )
        elapsed = time.monotonic() - started
        if elapsed > ENV_PROBE_SLOW_S:
            _logger.warning("Project env commands took %.1fs in %s", elapsed, workspace)
        if exit_code is None or exit_code != 0 or not os.path.exists(after_path):
            outcome = (
                f"timed out after {timeout:.0f}s" if exit_code is None else f"exit {exit_code}"
            )
            log = _keep_failure_output(output_path, "project-env-")
            return None, f"project env commands failed ({outcome}); log: {log}"
        try:
            before, after = _read_env_dump(before_path), _read_env_dump(after_path)
        except OSError as exc:
            return None, f"could not read the environment from project env commands: {exc}"
    return compute_env_delta(before, after), None


class EnvProbeCache:
    """Reuses a successful env probe for the same workspace and commands."""

    def __init__(self, ttl_s: float = ENV_PROBE_CACHE_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[tuple[str, str, str], tuple[float, EnvDelta]] = {}
        self._lock = threading.Lock()

    def get(self, workspace: str, commands: tuple[str, ...]) -> EnvDelta | None:
        key = (workspace, os.environ.get("SHELL", ""), commands_digest(commands))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] < time.monotonic():
                self._entries.pop(key, None)
                return None
            return entry[1]

    def put(self, workspace: str, commands: tuple[str, ...], delta: EnvDelta) -> None:
        key = (workspace, os.environ.get("SHELL", ""), commands_digest(commands))
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl_s, delta)


def linked_worktree_git_dir(workspace: str) -> Path | None:
    """
    Return the private git dir of a linked worktree, or ``None`` otherwise.

    :param workspace: Any directory inside the worktree.
    :returns: e.g. ``Path("/repo/.git/worktrees/feature")``; ``None`` for a main
        checkout, a non-repo directory, or when git fails.
    """
    try:
        result = _run_git(["rev-parse", "--absolute-git-dir", "--git-common-dir"], cwd=workspace)
    except WorktreeError:
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    git_dir = Path(lines[0]).resolve()
    common_dir = (Path(workspace) / lines[1]).resolve()
    return None if git_dir == common_dir else git_dir


def clear_setup_marker(workspace: str) -> bool:
    """
    Forget that a worktree's setup ran, so the next launch runs it again.

    :param workspace: Directory inside the linked worktree.
    :returns: Whether a marker was removed.
    """
    git_dir = linked_worktree_git_dir(workspace)
    if git_dir is None:
        return False
    try:
        (git_dir / SETUP_MARKER_NAME).unlink()
    except FileNotFoundError:
        return False
    return True


def pending_setup_marker(workspace: str) -> Path | None:
    """
    Return the marker a worktree's setup will write, or ``None`` if none is due.

    A runner given this path waits for it before its first turn.

    :param workspace: Session workspace.
    :returns: The marker path for a linked worktree whose setup has not run yet.
    """
    git_dir = linked_worktree_git_dir(workspace)
    if git_dir is None or (git_dir / SETUP_MARKER_NAME).exists():
        return None
    return git_dir / SETUP_MARKER_NAME


#: Worktree git dirs whose setup is running now, so two sessions starting in
#: the same worktree don't run the installs twice.
_running_setups: set[Path] = set()
_running_setups_lock = threading.Lock()


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
        _running_setups.add(git_dir)
    return git_dir


@dataclass(frozen=True)
class WorktreeSetupResult:
    """
    Outcome of a worktree's setup run.

    :param warning: Failing step and exit code, or ``None`` on success.
    :param log_path: Full output log on the host.
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
    budget_s: float = SETUP_BUDGET_S,
) -> WorktreeSetupResult:
    """
    Run a claimed worktree's ``setup_commands`` and record the marker.

    Blocking for up to ``budget_s``; the host runs it in the background after
    the runner starts. Execution stops at the first failing command. The marker
    is written whatever the outcome, since a waiting runner proceeds on it; only
    host shutdown leaves it unwritten, so the next launch runs setup again.

    :param commands: Commands in order.
    :param git_dir: Worktree git dir from :func:`claim_worktree_setup`.
    :param workspace: Session workspace inside the worktree.
    :param session_id: Session the setup runs for, exposed as ``OMNIGENT_SESSION_ID``.
    :param env: Base environment, already carrying the project env delta.
    :param budget_s: Total seconds for all commands.
    :returns: The outcome and log path.
    """
    try:
        result = _run_claimed_setup(commands, workspace, session_id, env, budget_s)
    except InterruptedByShutdownError:
        return WorktreeSetupResult(warning="interrupted by host shutdown", log_path=None)
    except Exception as exc:
        _logger.exception("Project setup crashed in %s", workspace)
        result = WorktreeSetupResult(
            warning=f"project setup crashed on the host ({type(exc).__name__})", log_path=None
        )
    finally:
        with _running_setups_lock:
            _running_setups.discard(git_dir)
    _write_marker(git_dir, commands, result)
    return result


def _write_marker(git_dir: Path, commands: tuple[str, ...], result: WorktreeSetupResult) -> None:
    record = {
        "commands_sha256": commands_digest(commands),
        "status": "failed" if result.warning else "succeeded",
        "finished_at": int(time.time()),
        "log_path": str(result.log_path) if result.log_path else None,
    }
    try:
        (git_dir / SETUP_MARKER_NAME).write_text(json.dumps(record))
    except OSError:
        _logger.warning("Could not record project setup marker in %s", git_dir, exc_info=True)


def _run_claimed_setup(
    commands: tuple[str, ...],
    workspace: str,
    session_id: str | None,
    env: dict[str, str],
    budget_s: float,
) -> WorktreeSetupResult:
    try:
        root_path = _main_work_tree(workspace)
    except WorktreeError:
        root_path = workspace
    command_env = {
        **env,
        "OMNIGENT_ROOT_PATH": root_path,
        "OMNIGENT_WORKSPACE": workspace,
        "OMNIGENT_WORKSPACE_NAME": os.path.basename(os.path.normpath(workspace)),
    }
    if session_id:
        command_env["OMNIGENT_SESSION_ID"] = session_id
    log_path, log_file = open_process_log_file("project-setup", prefix="project-setup-")
    log_file.close()
    deadline = time.monotonic() + budget_s
    warning: str | None = None
    # A login shell restores the user's PATH (e.g. Homebrew from .zprofile),
    # which a daemon started by launchd or systemd often lacks.
    shell = None if IS_WINDOWS else _user_shell()
    for step, command in enumerate(commands, start=1):
        exit_code = _run_bounded(
            [shell, "-l", "-c", command] if shell else default_shell_argv(command),
            cwd=workspace,
            env=command_env,
            timeout=deadline - time.monotonic(),
            output_path=log_path,
        )
        if exit_code != 0:
            outcome = (
                f"timed out after {budget_s:.0f}s" if exit_code is None else f"exit {exit_code}"
            )
            _logger.warning(
                "Project setup step %d failed in %s; output tail:\n%s",
                step,
                workspace,
                _tail(log_path.read_text(errors="replace")),
            )
            warning = f"setup command {step} of {len(commands)} failed ({outcome})"
            break
    return WorktreeSetupResult(warning=warning, log_path=log_path)


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
    delta = cache.get(workspace, setup.env_commands)
    if delta is None:
        delta, warning = probe_env_delta(setup.env_commands, workspace=workspace)
        if warning:
            outcome.warnings.append(warning)
        if delta is not None:
            cache.put(workspace, setup.env_commands, delta)
    if delta is not None:
        outcome.env_delta = delta
    return outcome
