"""Host tunnel frame schema for ``omnigent host``.

Host-specific frame kinds, all JSON (see :class:`HostFrameKind`),
plus reuse of ``PingFrame``/``PongFrame`` from the runner tunnel
for keepalive.

Host frames carry only control messages (launch/stop runner
requests and their results). They do NOT carry HTTP
request/response traffic — runners connect directly to the server
with their own tunnels.

This module is intentionally separate from the runner tunnel's
``frames.py`` to keep the two protocols partitioned. The runner
module has a closed ``FrameKind`` enum and ``decode_frame`` match
statement that handles all runner frame kinds. Adding host kinds
there would force runner-side decoders to handle frames they never
see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omnigent.harness_availability import HarnessAvailability, is_harness_availability

# Structured error code carried in ``HostLaunchRunnerResultFrame.error_code``
# when the host refuses a launch because the session's harness is not
# configured on that machine (CLI missing or no default credential). Shared
# by the daemon (producer), server (maps it to
# ``ErrorCode.HARNESS_NOT_CONFIGURED``), and tests.
HARNESS_NOT_CONFIGURED_ERROR_CODE = "harness_not_configured"


class HostFrameKind(str, Enum):
    """All host frame kinds; the value is the JSON wire string."""

    HELLO = "host.hello"
    HARNESS_READINESS = "host.harness_readiness"
    LAUNCH_RUNNER = "host.launch_runner"
    LAUNCH_RUNNER_RESULT = "host.launch_runner_result"
    STOP_RUNNER = "host.stop_runner"
    STOP_RUNNER_RESULT = "host.stop_runner_result"
    RUNNER_EXITED = "host.runner_exited"
    RUNNER_STATUS = "host.runner_status"
    RUNNER_STATUS_RESULT = "host.runner_status_result"
    STAT = "host.stat"
    STAT_RESULT = "host.stat_result"
    LIST_DIR = "host.list_dir"
    LIST_DIR_RESULT = "host.list_dir_result"
    CREATE_WORKTREE = "host.create_worktree"
    CREATE_WORKTREE_RESULT = "host.create_worktree_result"
    REMOVE_WORKTREE = "host.remove_worktree"
    REMOVE_WORKTREE_RESULT = "host.remove_worktree_result"
    LIST_WORKTREES = "host.list_worktrees"
    LIST_WORKTREES_RESULT = "host.list_worktrees_result"
    CREATE_DIR = "host.create_dir"
    CREATE_DIR_RESULT = "host.create_dir_result"
    INSTALL_HARNESS = "host.install_harness"
    INSTALL_HARNESS_RESULT = "host.install_harness_result"
    FS_REQUEST = "host.fs_request"
    FS_RESULT = "host.fs_result"
    MODEL_OPTIONS = "host.model_options"
    MODEL_OPTIONS_RESULT = "host.model_options_result"


# ── Frame dataclasses ────────────────────────────────────


@dataclass
class HostHelloFrame:
    """Host's first frame on a fresh tunnel.

    :param version: Host software version, e.g. ``"0.1.0"``.
    :param frame_protocol_version: Wire-protocol major. Server
        refuses on major mismatch.
    :param name: Human-readable host name from ``config.yaml``,
        e.g. ``"corey-laptop"``.
    :param runners: Runner IDs currently alive on this host.
        Enables state reconciliation on reconnect — the server
        diffs this against sessions in the DB.
    :param configured_harnesses: Per-harness readiness on this
        machine, e.g. ``{"claude-sdk": True, "codex": False}``
        (see ``omnigent.onboarding.harness_readiness``). Keys
        cover every accepted harness spelling. ``None`` means
        unknown (an older host that doesn't report it) — never
        treat ``None`` as "nothing is configured". Changes arrive in
        :class:`HostHarnessReadinessFrame`; launch-time checks remain
        authoritative.
    """

    version: str
    frame_protocol_version: int
    name: str
    runners: list[str] = field(default_factory=list)
    configured_harnesses: dict[str, HarnessAvailability] | None = None
    telemetry_opt_out: bool = False
    installation_id: str | None = None


@dataclass
class HostHarnessReadinessFrame:
    """Host's refreshed per-harness readiness while the tunnel stays open.

    :param configured_harnesses: Current launch readiness keyed by every
        accepted harness spelling. Sent only when the map changes.
    """

    configured_harnesses: dict[str, HarnessAvailability]


@dataclass
class HostLaunchRunnerFrame:
    """Server → host: spawn a new runner process.

    :param request_id: Unique ID for correlating the result,
        e.g. ``"req_abc123"``.
    :param binding_token: Secret token the runner must present
        when connecting. The server derives ``runner_id`` from
        this via ``token_bound_runner_id()``.
    :param workspace: Absolute path on the host machine to use
        as the runner's working directory, e.g.
        ``"/Users/corey/projects/frontend"``.
    :param session_id: Conversation/session ID the runner is being
        launched for, e.g. ``"conv_abc123"``. ``None`` means an older
        server did not include it.
    :param harness: Canonical harness the session will run, e.g.
        ``"claude-sdk"``. The host checks it is configured before
        spawning and refuses with
        :data:`HARNESS_NOT_CONFIGURED_ERROR_CODE` when not.
        ``None`` (older server, or no resolvable harness) skips
        the check — fail open.
    """

    request_id: str
    binding_token: str
    workspace: str
    session_id: str | None = None
    harness: str | None = None


@dataclass
class HostLaunchRunnerResultFrame:
    """Host → server: outcome of a launch request.

    :param request_id: Correlates to the
        :class:`HostLaunchRunnerFrame`, e.g. ``"req_abc123"``.
    :param status: ``"launched"`` or ``"failed"``.
    :param runner_id: Runner ID derived from the binding token.
        Confirms the host spawned the expected runner. ``None``
        when ``status`` is ``"failed"``.
    :param error: Error message when ``status`` is ``"failed"``,
        e.g. ``"workspace path does not exist"``. ``None`` on
        success.
    :param error_code: Machine-readable failure category when
        ``status`` is ``"failed"``, e.g.
        :data:`HARNESS_NOT_CONFIGURED_ERROR_CODE`. ``None`` for
        uncategorized failures and on success (and always from
        older hosts that don't send it).
    """

    request_id: str
    status: str
    runner_id: str | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass
class HostStopRunnerFrame:
    """Server → host: terminate a runner process.

    :param request_id: Unique ID for correlating the result,
        e.g. ``"req_def456"``.
    :param runner_id: Runner to stop, e.g.
        ``"runner_token_abc123..."``.
    """

    request_id: str
    runner_id: str


@dataclass
class HostStopRunnerResultFrame:
    """Host → server: outcome of a stop request.

    :param request_id: Correlates to the
        :class:`HostStopRunnerFrame`, e.g. ``"req_def456"``.
    :param status: ``"stopped"`` or ``"failed"``.
    :param error: Error message when ``status`` is ``"failed"``.
        ``None`` on success.
    """

    request_id: str
    status: str
    error: str | None = None


@dataclass
class HostRunnerExitedFrame:
    """Host → server: a spawned runner process died unexpectedly.

    One-way report (no result frame). The host daemon watches every
    runner it spawns; when one exits without a ``host.stop_runner``
    request, the daemon composes a human-readable error — exit code
    plus the tail of the runner's captured log — and reports it here.
    The server stashes it so the runner status endpoint can answer
    "offline, and here is why" — a client waiting for the runner to
    connect fails fast with the actual cause instead of polling to a
    timeout and pointing the user at a log directory on the host.

    :param runner_id: The runner that died, e.g.
        ``"runner_abc123..."``.
    :param error: Human-readable cause, e.g.
        ``"runner process exited with code 1 (log: ~/...) ..."``,
        including the trailing lines of the runner's log.
    """

    runner_id: str
    error: str


@dataclass
class HostRunnerStatusFrame:
    """Server → host: is this runner's process alive, dead, or unknown?

    The host is the authoritative owner of runner-process liveness — it
    holds each runner's :class:`subprocess.Popen`. The runner tunnel
    only tells the server "connected right now"; it cannot distinguish a
    runner that is still booting (will connect) from one that was stopped
    or died when the host restarted (never will). The message-dispatch
    path asks this before its connect grace so it waits for a runner that
    is coming and relaunches immediately for one that is not.

    :param request_id: Unique id for correlating the result, e.g.
        ``"req_rs_1"``.
    :param runner_id: Runner to query, e.g. ``"runner_abc123..."``.
    """

    request_id: str
    runner_id: str


@dataclass
class HostRunnerStatusResultFrame:
    """Host → server: liveness of a queried runner.

    :param request_id: Correlates to the :class:`HostRunnerStatusFrame`,
        e.g. ``"req_rs_1"``.
    :param status: One of:

        * ``"alive"`` — the host has this runner and its process is
          running (booting or serving). The runner is coming; wait.
        * ``"dead"`` — the host has this runner but its process has
          exited. It will never connect; relaunch now.
        * ``"unknown"`` — the host has no record of this runner (it was
          stopped, or a fresh post-restart host never spawned it).
          Relaunch now.
    """

    request_id: str
    status: str


@dataclass
class HostStatFrame:
    """Server → host: stat a path on the host's filesystem.

    Used by session-create validation to verify that a workspace
    path (or an agent's ``os_env.cwd`` boundary) exists and is a
    directory before storing the session row. Single round-trip;
    no directory walking.

    :param request_id: Unique ID for correlating the result,
        e.g. ``"req_stat_1"``.
    :param path: Absolute path on the host (e.g.
        ``"/Users/corey/universe"``) OR a tilde-prefixed path
        (``"~/foo"``). The host expands ``~`` against its own
        process owner's home directory before stating. Only the
        host knows its own ``HOME`` — the server never expands
        tildes itself.
    """

    request_id: str
    path: str


@dataclass
class HostStatResultFrame:
    """Host → server: outcome of a stat request.

    :param request_id: Correlates to the :class:`HostStatFrame`.
    :param status: ``"ok"`` or ``"failed"``. ``"failed"`` is
        reserved for unexpected I/O errors (e.g. EIO); EACCES and
        ENOENT both produce ``status: "ok", exists: false`` so the
        caller can treat them uniformly. Validation messages
        distinguishing missing-vs-unreadable can be added later if
        users find the collapse confusing — see
        designs/SESSION_WORKSPACE_SELECTION.md.
    :param exists: ``True`` when the path exists, is accessible to
        the host process, and (for symlinks) the target also
        exists. ``False`` for non-existent paths, dangling
        symlinks, and permission-denied paths.
    :param type: ``"directory"``, ``"file"``, or ``"other"``.
        Reflects the **target's** type after symlink resolution —
        a symlink to a directory returns ``"directory"``, never
        ``"symlink"``. ``None`` when ``exists`` is ``False``.
    :param canonical_path: Absolute, normalized realpath, e.g.
        ``"/Users/corey/universe"``. ``None`` when ``exists`` is
        ``False``. The server stores this on the session row
        instead of the user's input so symlinks cannot smuggle a
        workspace out of an agent's ``os_env.cwd`` boundary.
    :param error: Filesystem error message when ``status`` is
        ``"failed"``. ``None`` on success (including the
        ``exists: false`` case).
    """

    request_id: str
    status: str
    exists: bool = False
    type: str | None = None
    canonical_path: str | None = None
    error: str | None = None


@dataclass
class HostListDirEntry:
    """A single entry in a host.list_dir result.

    Mirrors the runner's ``FilesystemEntry`` shape so the Web UI's
    existing tree component can consume host browse results without
    a different mapping.

    :param name: Basename of the entry, e.g. ``"src"``.
    :param path: Absolute path on the host, e.g.
        ``"/Users/corey/universe/src"``. The host returns absolute
        paths so the Web UI can address each entry directly via
        the same REST endpoint without re-resolving relatives.
    :param type: ``"directory"``, ``"file"``, or ``"other"``.
        Reflects the target type after symlink resolution; symlinks
        themselves are not surfaced (consistent with
        ``host.stat_result``).
    :param bytes: File size for regular files; ``None`` for
        directories and other types. Lets the UI render sizes
        without an extra stat per entry.
    :param modified_at: Unix epoch seconds of last modification,
        e.g. ``1779980000``. Drives "modified" timestamps in the
        directory tree.
    """

    name: str
    path: str
    type: str
    bytes: int | None
    modified_at: int


@dataclass
class HostListDirFrame:
    """Server → host: list contents of a directory on the host.

    Used by ``GET /v1/hosts/{id}/filesystem/{path}`` to render the
    directory picker before any runner exists. The host owns ``~``
    resolution; the server passes whatever the user supplied (or
    ``~`` when the REST path is empty).

    :param request_id: Unique ID for correlating the result, e.g.
        ``"req_list_1"``.
    :param path: Absolute or tilde-prefixed directory path, e.g.
        ``"/Users/corey/projects"`` or ``"~/projects"``. Same rules
        as ``host.stat`` — the host expands ``~`` against its own
        process owner's home.
    :param limit: Maximum entries to return per page,
        e.g. ``20``. Pagination is in-memory at the host since
        most directories fit easily in one page.
    :param after: Optional cursor (entry ``path``) for forward
        pagination. ``None`` returns the first page.
    :param before: Optional cursor for backward pagination.
        ``None`` paginates forward only.
    """

    request_id: str
    path: str
    limit: int = 20
    after: str | None = None
    before: str | None = None


@dataclass
class HostListDirResultFrame:
    """Host → server: outcome of a list_dir request.

    :param request_id: Correlates to the
        :class:`HostListDirFrame`, e.g. ``"req_list_1"``.
    :param status: ``"ok"`` or ``"failed"``. ``"failed"`` is
        reserved for unexpected I/O errors; missing path collapses
        to a normal ``"ok"`` with an empty entries list and a
        descriptive error (the route layer maps these into 404).
    :param entries: Directory contents, possibly paginated. Empty
        list when the directory is empty or when the path doesn't
        exist (callers should check ``error`` to distinguish).
    :param has_more: ``True`` when more pages exist; ``False`` for
        the last page (or when entries are empty).
    :param error: Filesystem error, e.g.
        ``"path does not exist"`` or
        ``"permission denied"``. ``None`` on success. Populated
        even when ``status`` is ``"ok"`` so a missing path still
        carries a useful message into the REST response.
    """

    request_id: str
    status: str
    entries: list[HostListDirEntry] = field(default_factory=list)
    has_more: bool = False
    error: str | None = None


@dataclass
class HostCreateWorktreeFrame:
    """Server → host: create a git worktree for a new branch.

    See designs/SESSION_GIT_WORKTREE.md.

    :param request_id: Correlates the result, e.g. ``"req_wt_1"``.
    :param repo_path: Absolute path inside the source repo (the
        picked dir or a subdir), e.g. ``"/Users/alice/myrepo"``.
    :param branch_name: New branch to create, e.g. ``"feature/login"``.
    :param base_branch: Optional base ref, e.g. ``"main"``. ``None``
        branches from ``HEAD``.
    """

    request_id: str
    repo_path: str
    branch_name: str
    base_branch: str | None = None


@dataclass
class HostCreateWorktreeResultFrame:
    """Host → server: outcome of a create-worktree request.

    :param request_id: Correlates to the
        :class:`HostCreateWorktreeFrame`, e.g. ``"req_wt_1"``.
    :param status: ``"ok"`` or ``"failed"``.
    :param worktree_path: Created worktree directory (stored as the
        session ``workspace``), e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``. ``None``
        on failure.
    :param branch: Branch checked out, e.g. ``"feature/login"``.
        ``None`` on failure.
    :param error: Error message when ``status`` is ``"failed"``,
        e.g. ``"not a git repository"``. ``None`` on success.
    """

    request_id: str
    status: str
    worktree_path: str | None = None
    branch: str | None = None
    error: str | None = None


@dataclass
class HostRemoveWorktreeFrame:
    """Server → host: remove a git worktree (opt-in session cleanup).

    The host derives the main repo from ``worktree_path`` itself, so
    no repo path is carried. See designs/SESSION_GIT_WORKTREE.md.

    :param request_id: Correlates the result, e.g. ``"req_wt_rm_1"``.
    :param worktree_path: Worktree directory to remove (the stored
        session ``workspace``), e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips deletion.
    :param delete_branch: When ``True``, ``git branch -D`` after
        removing the directory; when ``False``, remove only the
        directory.
    """

    request_id: str
    worktree_path: str
    branch: str | None = None
    delete_branch: bool = False


@dataclass
class HostRemoveWorktreeResultFrame:
    """Host → server: outcome of a remove-worktree request.

    :param request_id: Correlates to the
        :class:`HostRemoveWorktreeFrame`, e.g. ``"req_wt_rm_1"``.
    :param status: ``"ok"`` or ``"failed"``.
    :param error: Error message when ``status`` is ``"failed"``.
        ``None`` on success.
    """

    request_id: str
    status: str
    error: str | None = None


@dataclass
class HostListWorktreesFrame:
    """Server → host: list the git worktrees of a repository.

    Backs ``GET /v1/hosts/{id}/worktrees``, used by the Web UI's
    new-session worktree picker to show worktrees a session can start
    in directly. Read-only; the host derives the main work tree from
    ``repo_path`` (so a linked worktree resolves the same list).

    :param request_id: Correlates the result, e.g. ``"req_wt_ls_1"``.
    :param repo_path: Absolute path inside the repo (the picked dir or
        a subdir), e.g. ``"/Users/alice/myrepo"``.
    """

    request_id: str
    repo_path: str


@dataclass
class HostListWorktreesResultFrame:
    """Host → server: outcome of a list-worktrees request.

    :param request_id: Correlates to the
        :class:`HostListWorktreesFrame`, e.g. ``"req_wt_ls_1"``.
    :param status: ``"ok"`` or ``"failed"``.
    :param worktrees: One dict per worktree with keys ``path`` (str),
        ``branch`` (str | None), ``is_main`` (bool), ``detached``
        (bool), main first. ``None`` on failure.
    :param error: Error message when ``status`` is ``"failed"``, e.g.
        ``"not a git repository"``. ``None`` on success.
    """

    request_id: str
    status: str
    worktrees: list[dict[str, Any]] | None = None
    error: str | None = None


@dataclass
class HostCreateDirFrame:
    """Server → host: create a new directory on the host.

    Backs ``POST /v1/hosts/{id}/directories``, used by the Web UI's
    workspace picker so a user can make a fresh folder to start a
    session in without dropping to a terminal. The host owns ``~``
    resolution, same rules as ``host.list_dir`` / ``host.stat``.

    :param request_id: Correlates the result, e.g. ``"req_mkdir_1"``.
    :param path: Absolute or tilde-prefixed directory path to create,
        e.g. ``"/Users/corey/projects/new-app"`` or ``"~/scratch"``.
        Missing parent directories are created (``os.makedirs``).
    """

    request_id: str
    path: str


@dataclass
class HostCreateDirResultFrame:
    """Host → server: outcome of a create-dir request.

    :param request_id: Correlates to the
        :class:`HostCreateDirFrame`, e.g. ``"req_mkdir_1"``.
    :param status: ``"ok"`` or ``"failed"``. ``"failed"`` is reserved
        for unexpected I/O errors; an expected filesystem error (the
        directory already exists, permission denied, a parent path
        component is a file) collapses to ``"ok"`` with a descriptive
        ``error`` so the route layer can map it to a 409 rather than a
        500 — same posture as ``host.list_dir`` for a missing path.
    :param path: Absolute path of the created directory, e.g.
        ``"/Users/corey/projects/new-app"``. ``None`` when the
        directory was not created.
    :param error: Filesystem error, e.g. ``"directory already
        exists"`` or ``"permission denied"``. ``None`` on success.
    """

    request_id: str
    status: str
    path: str | None = None
    error: str | None = None


@dataclass
class HostInstallHarnessFrame:
    """Server → host: install a harness CLI on the host.

    Backs ``POST /v1/hosts/{id}/harnesses/{harness}/install``, used by
    the Web UI's New Chat dialog so a user can install a missing,
    npm-installable harness onto a connected host without dropping to a
    terminal. The host runs the same :func:`install_harness_cli` the
    ``omnigent setup`` wizard uses. Only allowlisted, npm-installable
    harnesses reach this frame — the server rejects curl/brew and
    interactive-auth harnesses before sending it.

    :param request_id: Correlates the result, e.g. ``"req_install_1"``.
    :param harness: Harness identifier to install, e.g. ``"claude"`` or
        ``"codex"``. The host maps it to its install-spec key.
    """

    request_id: str
    harness: str


@dataclass
class HostInstallHarnessResultFrame:
    """Host → server: outcome of an install request.

    Carries the freshly-recomputed readiness map so the server can
    update its view and the UI can flip the harness badge without
    waiting for a reconnect (the ``host.hello`` handshake is the only
    other readiness carrier, sent once per connect).

    :param request_id: Correlates to the
        :class:`HostInstallHarnessFrame`, e.g. ``"req_install_1"``.
    :param status: ``"ok"`` when the installer ran and the binary landed
        on ``PATH``, ``"failed"`` otherwise. A ``"failed"`` status pairs
        with a human-readable ``error`` (e.g. ``"npm not found"``).
    :param configured_harnesses: The host's readiness map recomputed
        after the install attempt, e.g. ``{"claude-native": True,
        "codex-native": "needs-auth"}``. ``None`` when the install could
        not run (the server keeps its prior readiness view).
    :param error: Why the install failed, e.g. ``"npm not found"`` or
        ``"install timed out"``. ``None`` on success.
    """

    request_id: str
    status: str
    configured_harnesses: dict[str, HarnessAvailability] | None = None
    error: str | None = None


@dataclass
class HostFsRequestFrame:
    """Server → host: read-only workspace filesystem request.

    Serves the web UI's file panel (directory browse, changed files,
    diffs, search, file content) from the host when the session's runner
    is offline but the host still holds the workspace on disk. The host
    runs :class:`omnigent.workspace_fs.WorkspaceReader` against
    ``workspace`` and returns the same JSON the runner's filesystem
    endpoints would.

    :param request_id: Correlates the result, e.g. ``"req_fs_1"``.
    :param op: Operation name — one of ``"list_or_read"``, ``"changes"``,
        ``"diff"``, ``"search"``.
    :param workspace: Absolute path to the session's workspace on the
        host, e.g. ``"/Users/alice/project"``.
    :param session_id: Session id, forwarded to the change registry.
    :param params: Operation-specific arguments (relative path, glob
        filters, pagination cursors), e.g.
        ``{"path": "src", "limit": 100, "order": "asc"}``.
    """

    request_id: str
    op: str
    workspace: str
    session_id: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class HostFsResultFrame:
    """Host → server: outcome of a workspace filesystem request.

    :param request_id: Correlates to the :class:`HostFsRequestFrame`.
    :param status: ``"ok"`` when ``payload`` carries the runner-shaped
        result, or ``"error"`` when the read failed.
    :param payload: The runner-shaped JSON result on success, ``None`` on
        error.
    :param error_status: HTTP status the runner would have returned on
        failure (e.g. ``404``), or ``None`` on success.
    :param error_code: Machine-readable error code on failure (e.g.
        ``"not_found"``), or ``None`` on success.
    :param error: Human-readable error detail on failure, or ``None``.
    """

    request_id: str
    status: str
    payload: dict[str, Any] | None = None
    error_status: int | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass
class HostModelOptionsFrame:
    """Server → host: resolve pre-launch model choices for a harness."""

    request_id: str
    harness: str


@dataclass
class HostModelOptionsResultFrame:
    """Host → server: pre-launch model choices resolved on that machine."""

    request_id: str
    status: str
    models: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


HostFrame = (
    HostHelloFrame
    | HostHarnessReadinessFrame
    | HostLaunchRunnerFrame
    | HostLaunchRunnerResultFrame
    | HostStopRunnerFrame
    | HostStopRunnerResultFrame
    | HostRunnerExitedFrame
    | HostRunnerStatusFrame
    | HostRunnerStatusResultFrame
    | HostStatFrame
    | HostStatResultFrame
    | HostListDirFrame
    | HostListDirResultFrame
    | HostCreateWorktreeFrame
    | HostCreateWorktreeResultFrame
    | HostRemoveWorktreeFrame
    | HostRemoveWorktreeResultFrame
    | HostListWorktreesFrame
    | HostListWorktreesResultFrame
    | HostCreateDirFrame
    | HostCreateDirResultFrame
    | HostFsRequestFrame
    | HostFsResultFrame
    | HostModelOptionsFrame
    | HostModelOptionsResultFrame
)


# ── Encode / decode ──────────────────────────────────────


def _encode_payload(payload: dict[str, Any]) -> str:
    """Serialize a frame payload, injecting the active trace context.

    Centralized so every host frame carries a W3C ``traceparent`` (and
    ``tracestate`` when set) whenever it is encoded inside an active
    span — the host tunnel is a JSON-frame transport no OTel
    auto-instrumentor can see, so this is how the Host Daemon ↔ Server
    boundary joins the distributed trace. When no span is active the
    payload is unchanged. Decoders ignore the extra envelope keys, so
    this stays wire-compatible with peers that do not read them.

    :param payload: The frame fields about to be serialized.
    :returns: The JSON wire string.
    """
    from omnigent.runtime import telemetry

    # Record the outbound body on the active span (redacted, gated by
    # content capture) before injecting propagation keys, so the span
    # shows exactly what this side sent.
    telemetry.record_message_payload(payload)
    telemetry.inject_trace_context(payload)
    return json.dumps(payload)


def encode_host_frame(frame: HostFrame) -> str:
    """Serialize a host frame to its JSON wire form.

    :param frame: The host frame dataclass to encode.
    :returns: JSON string for the WebSocket text message.
    :raises TypeError: If ``frame`` is not a known host frame type.
    """
    if isinstance(frame, HostHelloFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.HELLO.value,
                "version": frame.version,
                "frame_protocol_version": frame.frame_protocol_version,
                "name": frame.name,
                "runners": list(frame.runners),
                "configured_harnesses": frame.configured_harnesses,
                "telemetry_opt_out": frame.telemetry_opt_out,
                "installation_id": frame.installation_id,
            }
        )
    if isinstance(frame, HostHarnessReadinessFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.HARNESS_READINESS.value,
                "configured_harnesses": frame.configured_harnesses,
            }
        )
    if isinstance(frame, HostLaunchRunnerFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.LAUNCH_RUNNER.value,
                "request_id": frame.request_id,
                "binding_token": frame.binding_token,
                "workspace": frame.workspace,
                "session_id": frame.session_id,
                "harness": frame.harness,
            }
        )
    if isinstance(frame, HostLaunchRunnerResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.LAUNCH_RUNNER_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "runner_id": frame.runner_id,
                "error": frame.error,
                "error_code": frame.error_code,
            }
        )
    if isinstance(frame, HostStopRunnerFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.STOP_RUNNER.value,
                "request_id": frame.request_id,
                "runner_id": frame.runner_id,
            }
        )
    if isinstance(frame, HostStopRunnerResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.STOP_RUNNER_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostRunnerExitedFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.RUNNER_EXITED.value,
                "runner_id": frame.runner_id,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostRunnerStatusFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.RUNNER_STATUS.value,
                "request_id": frame.request_id,
                "runner_id": frame.runner_id,
            }
        )
    if isinstance(frame, HostRunnerStatusResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.RUNNER_STATUS_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
            }
        )
    if isinstance(frame, HostStatFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.STAT.value,
                "request_id": frame.request_id,
                "path": frame.path,
            }
        )
    if isinstance(frame, HostStatResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.STAT_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "exists": frame.exists,
                "type": frame.type,
                "canonical_path": frame.canonical_path,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostListDirFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.LIST_DIR.value,
                "request_id": frame.request_id,
                "path": frame.path,
                "limit": frame.limit,
                "after": frame.after,
                "before": frame.before,
            }
        )
    if isinstance(frame, HostListDirResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.LIST_DIR_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "entries": [
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "type": entry.type,
                        "bytes": entry.bytes,
                        "modified_at": entry.modified_at,
                    }
                    for entry in frame.entries
                ],
                "has_more": frame.has_more,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostCreateWorktreeFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.CREATE_WORKTREE.value,
                "request_id": frame.request_id,
                "repo_path": frame.repo_path,
                "branch_name": frame.branch_name,
                "base_branch": frame.base_branch,
            }
        )
    if isinstance(frame, HostCreateWorktreeResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.CREATE_WORKTREE_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "worktree_path": frame.worktree_path,
                "branch": frame.branch,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostRemoveWorktreeFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.REMOVE_WORKTREE.value,
                "request_id": frame.request_id,
                "worktree_path": frame.worktree_path,
                "branch": frame.branch,
                "delete_branch": frame.delete_branch,
            }
        )
    if isinstance(frame, HostRemoveWorktreeResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.REMOVE_WORKTREE_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostListWorktreesFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.LIST_WORKTREES.value,
                "request_id": frame.request_id,
                "repo_path": frame.repo_path,
            }
        )
    if isinstance(frame, HostListWorktreesResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.LIST_WORKTREES_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "worktrees": frame.worktrees,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostCreateDirFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.CREATE_DIR.value,
                "request_id": frame.request_id,
                "path": frame.path,
            }
        )
    if isinstance(frame, HostCreateDirResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.CREATE_DIR_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "path": frame.path,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostInstallHarnessFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.INSTALL_HARNESS.value,
                "request_id": frame.request_id,
                "harness": frame.harness,
            }
        )
    if isinstance(frame, HostInstallHarnessResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.INSTALL_HARNESS_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "configured_harnesses": frame.configured_harnesses,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostFsRequestFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.FS_REQUEST.value,
                "request_id": frame.request_id,
                "op": frame.op,
                "workspace": frame.workspace,
                "session_id": frame.session_id,
                "params": frame.params,
            }
        )
    if isinstance(frame, HostFsResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.FS_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "payload": frame.payload,
                "error_status": frame.error_status,
                "error_code": frame.error_code,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostModelOptionsFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.MODEL_OPTIONS.value,
                "request_id": frame.request_id,
                "harness": frame.harness,
            }
        )
    if isinstance(frame, HostModelOptionsResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.MODEL_OPTIONS_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "models": frame.models,
                "error": frame.error,
            }
        )
    raise TypeError(f"unknown host frame type: {type(frame).__name__}")


def decode_host_frame(text: str) -> HostFrame:
    """Parse a JSON wire frame back into its host frame dataclass.

    :param text: Raw JSON frame text from the WebSocket.
    :returns: The typed host frame dataclass.
    :raises ValueError: On malformed JSON, missing ``kind``, unknown
        kind, or missing required fields.
    """
    msg = _parse_frame_object(text)
    kind = _parse_host_frame_kind(msg)
    return _decode_known_host_frame(kind, msg)


def _parse_frame_object(text: str) -> dict[str, Any]:
    """Parse a JSON frame object.

    :param text: Raw JSON frame text.
    :returns: Decoded frame object.
    :raises ValueError: If the payload is not a JSON object.
    """
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ValueError(f"frame must be a JSON object, got {type(msg).__name__}")
    return msg


def _parse_host_frame_kind(msg: dict[str, Any]) -> HostFrameKind:
    """Parse the host frame kind discriminator.

    :param msg: Decoded frame object.
    :returns: Host frame kind enum.
    :raises ValueError: If ``kind`` is missing or unknown.
    """
    kind = msg.get("kind")
    if not isinstance(kind, str):
        raise ValueError("frame missing 'kind' field")
    try:
        return HostFrameKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown host frame kind: {kind!r}") from exc


def _decode_known_host_frame(
    kind: HostFrameKind,
    msg: dict[str, Any],
) -> HostFrame:
    """Decode a host frame with a validated kind.

    :param kind: Parsed host frame kind.
    :param msg: Decoded frame object.
    :returns: The typed host frame dataclass.
    :raises ValueError: If the kind is unexpectedly unhandled.
    """
    match kind:
        case HostFrameKind.HELLO:
            return _decode_host_hello(msg)
        case HostFrameKind.HARNESS_READINESS:
            return _decode_harness_readiness(msg)
        case HostFrameKind.LAUNCH_RUNNER:
            return _decode_launch_runner(msg)
        case HostFrameKind.LAUNCH_RUNNER_RESULT:
            return _decode_launch_runner_result(msg)
        case HostFrameKind.STOP_RUNNER:
            return _decode_stop_runner(msg)
        case HostFrameKind.STOP_RUNNER_RESULT:
            return _decode_stop_runner_result(msg)
        case HostFrameKind.RUNNER_EXITED:
            return _decode_runner_exited(msg)
        case HostFrameKind.RUNNER_STATUS:
            return _decode_runner_status(msg)
        case HostFrameKind.RUNNER_STATUS_RESULT:
            return _decode_runner_status_result(msg)
        case HostFrameKind.STAT:
            return _decode_stat(msg)
        case HostFrameKind.STAT_RESULT:
            return _decode_stat_result(msg)
        case HostFrameKind.LIST_DIR:
            return _decode_list_dir(msg)
        case HostFrameKind.LIST_DIR_RESULT:
            return _decode_list_dir_result(msg)
        case HostFrameKind.CREATE_WORKTREE:
            return _decode_create_worktree(msg)
        case HostFrameKind.CREATE_WORKTREE_RESULT:
            return _decode_create_worktree_result(msg)
        case HostFrameKind.REMOVE_WORKTREE:
            return _decode_remove_worktree(msg)
        case HostFrameKind.REMOVE_WORKTREE_RESULT:
            return _decode_remove_worktree_result(msg)
        case HostFrameKind.LIST_WORKTREES:
            return _decode_list_worktrees(msg)
        case HostFrameKind.LIST_WORKTREES_RESULT:
            return _decode_list_worktrees_result(msg)
        case HostFrameKind.CREATE_DIR:
            return _decode_create_dir(msg)
        case HostFrameKind.CREATE_DIR_RESULT:
            return _decode_create_dir_result(msg)
        case HostFrameKind.INSTALL_HARNESS:
            return _decode_install_harness(msg)
        case HostFrameKind.INSTALL_HARNESS_RESULT:
            return _decode_install_harness_result(msg)
        case HostFrameKind.FS_REQUEST:
            return _decode_fs_request(msg)
        case HostFrameKind.FS_RESULT:
            return _decode_fs_result(msg)
        case HostFrameKind.MODEL_OPTIONS:
            return _decode_model_options(msg)
        case HostFrameKind.MODEL_OPTIONS_RESULT:
            return _decode_model_options_result(msg)
    raise ValueError(f"unhandled host frame kind: {kind.value!r}")  # pragma: no cover


def _decode_host_hello(msg: dict[str, Any]) -> HostHelloFrame:
    """Decode a host hello frame.

    :param msg: Decoded frame object.
    :returns: Typed host hello frame.
    """
    return HostHelloFrame(
        version=_required_str(msg, "version"),
        frame_protocol_version=_required_int(msg, "frame_protocol_version"),
        name=_required_str(msg, "name"),
        runners=_optional_str_list(msg, "runners"),
        configured_harnesses=_optional_str_availability_map(msg, "configured_harnesses"),
        telemetry_opt_out=bool(msg.get("telemetry_opt_out", False)),
        installation_id=_optional_nullable_str(msg, "installation_id"),
    )


def _decode_harness_readiness(msg: dict[str, Any]) -> HostHarnessReadinessFrame:
    """Decode a live harness-readiness refresh frame."""
    configured_harnesses = _optional_str_availability_map(msg, "configured_harnesses")
    if configured_harnesses is None:
        raise ValueError("harness readiness frame requires a configured_harnesses object")
    raw = msg["configured_harnesses"]
    if len(configured_harnesses) != len(raw):
        raise ValueError("harness readiness frame contains an unsupported availability state")
    if not configured_harnesses:
        raise ValueError("harness readiness frame requires a non-empty configured_harnesses map")
    return HostHarnessReadinessFrame(configured_harnesses=configured_harnesses)


def _decode_launch_runner(msg: dict[str, Any]) -> HostLaunchRunnerFrame:
    """Decode a launch-runner frame.

    :param msg: Decoded frame object.
    :returns: Typed launch-runner frame.
    """
    return HostLaunchRunnerFrame(
        request_id=_required_str(msg, "request_id"),
        binding_token=_required_str(msg, "binding_token"),
        workspace=_required_str(msg, "workspace"),
        session_id=_optional_nullable_str(msg, "session_id"),
        harness=_optional_nullable_str(msg, "harness"),
    )


def _decode_launch_runner_result(
    msg: dict[str, Any],
) -> HostLaunchRunnerResultFrame:
    """Decode a launch-runner-result frame.

    :param msg: Decoded frame object.
    :returns: Typed launch-runner-result frame.
    """
    return HostLaunchRunnerResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        runner_id=_optional_nullable_str(msg, "runner_id"),
        error=_optional_nullable_str(msg, "error"),
        error_code=_optional_nullable_str(msg, "error_code"),
    )


def _decode_stop_runner(msg: dict[str, Any]) -> HostStopRunnerFrame:
    """Decode a stop-runner frame.

    :param msg: Decoded frame object.
    :returns: Typed stop-runner frame.
    """
    return HostStopRunnerFrame(
        request_id=_required_str(msg, "request_id"),
        runner_id=_required_str(msg, "runner_id"),
    )


def _decode_stop_runner_result(
    msg: dict[str, Any],
) -> HostStopRunnerResultFrame:
    """Decode a stop-runner-result frame.

    :param msg: Decoded frame object.
    :returns: Typed stop-runner-result frame.
    """
    return HostStopRunnerResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_runner_exited(msg: dict[str, Any]) -> HostRunnerExitedFrame:
    """Decode a host.runner_exited report frame.

    :param msg: Decoded frame object.
    :returns: Typed host.runner_exited frame.
    """
    return HostRunnerExitedFrame(
        runner_id=_required_str(msg, "runner_id"),
        error=_required_str(msg, "error"),
    )


def _decode_runner_status(msg: dict[str, Any]) -> HostRunnerStatusFrame:
    """Decode a host.runner_status request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.runner_status frame.
    """
    return HostRunnerStatusFrame(
        request_id=_required_str(msg, "request_id"),
        runner_id=_required_str(msg, "runner_id"),
    )


def _decode_runner_status_result(
    msg: dict[str, Any],
) -> HostRunnerStatusResultFrame:
    """Decode a host.runner_status_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.runner_status_result frame.
    """
    return HostRunnerStatusResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
    )


def _decode_stat(msg: dict[str, Any]) -> HostStatFrame:
    """Decode a host.stat request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.stat frame.
    """
    return HostStatFrame(
        request_id=_required_str(msg, "request_id"),
        path=_required_str(msg, "path"),
    )


def _decode_stat_result(msg: dict[str, Any]) -> HostStatResultFrame:
    """Decode a host.stat_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.stat_result frame.
    """
    return HostStatResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        exists=_required_bool(msg, "exists"),
        type=_optional_nullable_str(msg, "type"),
        canonical_path=_optional_nullable_str(msg, "canonical_path"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_list_dir(msg: dict[str, Any]) -> HostListDirFrame:
    """Decode a host.list_dir request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.list_dir frame.
    """
    limit_value = msg.get("limit", 20)
    if not isinstance(limit_value, int) or isinstance(limit_value, bool):
        raise ValueError("frame field must be an int: 'limit'")
    return HostListDirFrame(
        request_id=_required_str(msg, "request_id"),
        path=_required_str(msg, "path"),
        limit=limit_value,
        after=_optional_nullable_str(msg, "after"),
        before=_optional_nullable_str(msg, "before"),
    )


def _decode_list_dir_result(msg: dict[str, Any]) -> HostListDirResultFrame:
    """Decode a host.list_dir_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.list_dir_result frame.
    """
    raw_entries = msg.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError("frame field must be a list: 'entries'")
    entries: list[HostListDirEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("each entry in 'entries' must be a JSON object")
        entries.append(_decode_list_dir_entry(raw))
    has_more = msg.get("has_more", False)
    if not isinstance(has_more, bool):
        raise ValueError("frame field must be a bool: 'has_more'")
    return HostListDirResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        entries=entries,
        has_more=has_more,
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_list_dir_entry(msg: dict[str, Any]) -> HostListDirEntry:
    """Decode a single entry in a host.list_dir_result.

    :param msg: Decoded entry object.
    :returns: Typed entry.
    :raises ValueError: When required fields are missing or
        wrong type.
    """
    bytes_val = msg.get("bytes")
    if bytes_val is not None and (not isinstance(bytes_val, int) or isinstance(bytes_val, bool)):
        raise ValueError("entry field must be int or null: 'bytes'")
    modified_at = msg.get("modified_at")
    if not isinstance(modified_at, int) or isinstance(modified_at, bool):
        raise ValueError("entry field must be an int: 'modified_at'")
    return HostListDirEntry(
        name=_required_str(msg, "name"),
        path=_required_str(msg, "path"),
        type=_required_str(msg, "type"),
        bytes=bytes_val,
        modified_at=modified_at,
    )


def _decode_create_worktree(msg: dict[str, Any]) -> HostCreateWorktreeFrame:
    """Decode a host.create_worktree request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.create_worktree frame.
    """
    return HostCreateWorktreeFrame(
        request_id=_required_str(msg, "request_id"),
        repo_path=_required_str(msg, "repo_path"),
        branch_name=_required_str(msg, "branch_name"),
        base_branch=_optional_nullable_str(msg, "base_branch"),
    )


def _decode_create_worktree_result(
    msg: dict[str, Any],
) -> HostCreateWorktreeResultFrame:
    """Decode a host.create_worktree_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.create_worktree_result frame.
    """
    return HostCreateWorktreeResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        worktree_path=_optional_nullable_str(msg, "worktree_path"),
        branch=_optional_nullable_str(msg, "branch"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_remove_worktree(msg: dict[str, Any]) -> HostRemoveWorktreeFrame:
    """Decode a host.remove_worktree request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.remove_worktree frame.
    """
    delete_branch = msg.get("delete_branch", False)
    if not isinstance(delete_branch, bool):
        raise ValueError("frame field must be a bool: 'delete_branch'")
    return HostRemoveWorktreeFrame(
        request_id=_required_str(msg, "request_id"),
        worktree_path=_required_str(msg, "worktree_path"),
        branch=_optional_nullable_str(msg, "branch"),
        delete_branch=delete_branch,
    )


def _decode_remove_worktree_result(
    msg: dict[str, Any],
) -> HostRemoveWorktreeResultFrame:
    """Decode a host.remove_worktree_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.remove_worktree_result frame.
    """
    return HostRemoveWorktreeResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_list_worktrees(msg: dict[str, Any]) -> HostListWorktreesFrame:
    """Decode a host.list_worktrees request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.list_worktrees frame.
    """
    return HostListWorktreesFrame(
        request_id=_required_str(msg, "request_id"),
        repo_path=_required_str(msg, "repo_path"),
    )


def _decode_list_worktrees_result(
    msg: dict[str, Any],
) -> HostListWorktreesResultFrame:
    """Decode a host.list_worktrees_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.list_worktrees_result frame.
    """
    raw = msg.get("worktrees")
    if raw is not None:
        if not isinstance(raw, list):
            raise ValueError("frame field must be a list or null: 'worktrees'")
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError("each entry in 'worktrees' must be a JSON object")
    return HostListWorktreesResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        worktrees=raw,
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_create_dir(msg: dict[str, Any]) -> HostCreateDirFrame:
    """Decode a host.create_dir request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.create_dir frame.
    """
    return HostCreateDirFrame(
        request_id=_required_str(msg, "request_id"),
        path=_required_str(msg, "path"),
    )


def _decode_create_dir_result(msg: dict[str, Any]) -> HostCreateDirResultFrame:
    """Decode a host.create_dir_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.create_dir_result frame.
    """
    return HostCreateDirResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        path=_optional_nullable_str(msg, "path"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_install_harness(msg: dict[str, Any]) -> HostInstallHarnessFrame:
    """Decode a host.install_harness request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.install_harness frame.
    """
    return HostInstallHarnessFrame(
        request_id=_required_str(msg, "request_id"),
        harness=_required_str(msg, "harness"),
    )


def _decode_install_harness_result(msg: dict[str, Any]) -> HostInstallHarnessResultFrame:
    """Decode a host.install_harness_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.install_harness_result frame.
    """
    return HostInstallHarnessResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        configured_harnesses=_optional_str_availability_map(msg, "configured_harnesses"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_fs_request(msg: dict[str, Any]) -> HostFsRequestFrame:
    """Decode a host.fs_request request frame.

    :param msg: Decoded frame object.
    :returns: Typed host.fs_request frame.
    """
    params = msg.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("frame field must be a JSON object: 'params'")
    return HostFsRequestFrame(
        request_id=_required_str(msg, "request_id"),
        op=_required_str(msg, "op"),
        workspace=_required_str(msg, "workspace"),
        session_id=_required_str(msg, "session_id"),
        params=params,
    )


def _decode_fs_result(msg: dict[str, Any]) -> HostFsResultFrame:
    """Decode a host.fs_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.fs_result frame.
    """
    payload = msg.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("frame field must be a JSON object or null: 'payload'")
    error_status = msg.get("error_status")
    if error_status is not None and (
        not isinstance(error_status, int) or isinstance(error_status, bool)
    ):
        raise ValueError("frame field must be an int or null: 'error_status'")
    return HostFsResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        payload=payload,
        error_status=error_status,
        error_code=_optional_nullable_str(msg, "error_code"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_model_options(msg: dict[str, Any]) -> HostModelOptionsFrame:
    """Decode a host.model_options request frame."""
    return HostModelOptionsFrame(
        request_id=_required_str(msg, "request_id"),
        harness=_required_str(msg, "harness"),
    )


def _decode_model_options_result(msg: dict[str, Any]) -> HostModelOptionsResultFrame:
    """Decode a host.model_options_result frame."""
    models = msg.get("models", [])
    if not isinstance(models, list) or not all(isinstance(model, dict) for model in models):
        raise ValueError("frame field must be a list of JSON objects: 'models'")
    return HostModelOptionsResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        models=models,
        error=_optional_nullable_str(msg, "error"),
    )


# ── Field validators ─────────────────────────────────────


def _required_str(msg: dict[str, Any], key: str) -> str:
    """Return a required string field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"version"``.
    :returns: The string value.
    :raises ValueError: If the field is missing or not a string.
    """
    val = msg.get(key)
    if not isinstance(val, str):
        raise ValueError(f"frame missing required string field: {key!r}")
    return val


def _required_int(msg: dict[str, Any], key: str) -> int:
    """Return a required integer field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"frame_protocol_version"``.
    :returns: The integer value.
    :raises ValueError: If the field is missing or not an integer.
    """
    val = msg.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"frame missing required int field: {key!r}")
    return val


def _required_bool(msg: dict[str, Any], key: str) -> bool:
    """Return a required boolean field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"exists"``.
    :returns: The boolean value.
    :raises ValueError: If the field is missing or not a bool.
    """
    val = msg.get(key)
    if not isinstance(val, bool):
        raise ValueError(f"frame missing required bool field: {key!r}")
    return val


def _optional_str_list(msg: dict[str, Any], key: str) -> list[str]:
    """Return an optional list of strings.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"runners"``.
    :returns: A list of strings, empty when absent.
    :raises ValueError: If the field is not a string list.
    """
    val = msg.get(key, [])
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise ValueError(f"frame field must be a list of strings: {key!r}")
    return list(val)


def _optional_str_availability_map(
    msg: dict[str, Any], key: str
) -> dict[str, HarnessAvailability] | None:
    """Return an optional string→availability mapping field.

    Tolerant by design: absent, null, or non-mapping values all decode
    to ``None`` ("unknown") rather than raising, so an older or newer
    peer's hello never breaks the tunnel handshake. Entries with a
    non-string key or unsupported readiness value are dropped for the same reason.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"configured_harnesses"``.
    :returns: The mapping, e.g. ``{"claude-sdk": True, "codex": "needs-auth"}``, or ``None``
        when absent / null / not a JSON object.
    """
    val = msg.get(key)
    if not isinstance(val, dict):
        return None
    return {k: v for k, v in val.items() if isinstance(k, str) and is_harness_availability(v)}


def _optional_nullable_str(msg: dict[str, Any], key: str) -> str | None:
    """Return an optional nullable string field.

    :param msg: Decoded frame object.
    :param key: Field name, e.g. ``"error"``.
    :returns: The string value, or ``None`` when absent or null.
    :raises ValueError: If the field is present and not a string or
        null.
    """
    val = msg.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"frame field must be a string or null: {key!r}")
    return val
