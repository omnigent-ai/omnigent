"""A project's worktree settings: where worktrees go, and what runs around them.

Three optional keys, all read by the SERVER out of a project's config:

- ``worktree_root`` — the directory new worktrees are created under, e.g.
  ``".worktrees"`` or ``"{repo}-worktrees"``. Unset keeps the host's built-in
  sibling layout. This is the one knob that stops worktrees from accumulating
  in a different place per tool.
- ``worktree_post_create_command`` — setup after Omnigent creates a worktree
  (dependency install, ``.env`` copy). The first turn waits for it.
- ``worktree_pre_delete_command`` — teardown before Omnigent removes a
  worktree (stop a dev server, drop a database).

Each command may be a single line or a multi-line script (optionally with a
shebang selecting its interpreter). Both are fail-open: a failing or timed-out
hook is surfaced but never blocks the session from becoming usable, nor the
worktree from being deleted.

The keys live in the opaque ``projects.config`` JSON column (no migration).
This module owns the vocabulary and the validation, so route code never
re-parses raw config keys.

Trust model: a hook runs as the host daemon's OS user, unsandboxed — the same
trust level as the agent itself. Anyone who can write a project's settings can
run arbitrary code on that project's hosts. Accepted; the settings UI says
so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from omnigent.entities.conversation import Conversation
from omnigent.host.git_worktree import WorktreeError, clamp_hook_timeout, validate_worktree_root
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.stores.project_store import ProjectStore

_logger = logging.getLogger(__name__)

#: Config key holding the directory new worktrees are created under.
ROOT_KEY = "worktree_root"
#: Config key holding the post-create setup script.
POST_CREATE_KEY = "worktree_post_create_command"
#: Config key holding the pre-delete teardown script.
PRE_DELETE_KEY = "worktree_pre_delete_command"
#: Config key holding the shared timeout for both hooks, in seconds.
TIMEOUT_KEY = "worktree_hook_timeout_seconds"

#: Lifecycle point names, exported to the hook as ``OMNIGENT_HOOK``.
POST_CREATE_HOOK = "post_create"
PRE_DELETE_HOOK = "pre_delete"


@dataclass(frozen=True)
class WorktreeHookConfig:
    """A project's validated worktree lifecycle scripts.

    :param post_create_command: Script to run after a worktree is
        created, e.g. ``"bun install"`` or a multi-line script. ``None``
        when unset (an absent key or a whitespace-only string).
    :param pre_delete_command: Script to run before a worktree is
        removed, e.g. ``"./scripts/teardown.sh"``. ``None`` when unset.
    :param timeout_seconds: Bound on either hook, clamped to 1–3600 s
        (default 300).
    """

    post_create_command: str | None
    pre_delete_command: str | None
    timeout_seconds: float

    @property
    def any_configured(self) -> bool:
        """
        Whether the project configures either hook.

        :returns: ``True`` when at least one command is set.
        """
        return self.post_create_command is not None or self.pre_delete_command is not None


#: The "nothing configured" config — what every project without hook keys
#: resolves to, so callers can short-circuit on ``any_configured``.
NO_HOOKS = WorktreeHookConfig(
    post_create_command=None,
    pre_delete_command=None,
    timeout_seconds=clamp_hook_timeout(None),
)


def _command_or_none(raw: Any) -> str | None:
    """Normalize a configured script value to a script or ``None``.

    Empty / whitespace-only means "unset" so clearing the field in the
    settings dialog turns the hook off rather than running a blank shell.
    Only the OUTER whitespace is trimmed — a multi-line script's internal
    newlines and indentation are part of the program and are preserved
    verbatim. Windows-style line endings are normalized so a script
    pasted from a CRLF editor doesn't reach a POSIX shell with stray
    carriage returns (which turn into ``\r``-suffixed command names).

    :param raw: The raw config value, e.g. ``"bun install"``, a
        multi-line script, ``""``, or a non-string a hand-edited config
        could hold.
    :returns: The trimmed script, or ``None`` when unset / not a string.
    """
    if not isinstance(raw, str):
        return None
    trimmed = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    return trimmed or None


def hook_config_from_project_config(config: dict[str, Any] | None) -> WorktreeHookConfig:
    """Read the hook settings out of a project's opaque config object.

    :param config: The project's stored ``config`` dict, or ``None``.
    :returns: The validated config; :data:`NO_HOOKS` when neither
        command is set.
    """
    if not config:
        return NO_HOOKS
    post = _command_or_none(config.get(POST_CREATE_KEY))
    pre = _command_or_none(config.get(PRE_DELETE_KEY))
    if post is None and pre is None:
        return NO_HOOKS
    raw_timeout = config.get(TIMEOUT_KEY)
    timeout = clamp_hook_timeout(raw_timeout if isinstance(raw_timeout, (int, float)) else None)
    return WorktreeHookConfig(
        post_create_command=post,
        pre_delete_command=pre,
        timeout_seconds=timeout,
    )


def worktree_root_from_project_config(config: dict[str, Any] | None) -> str | None:
    """Read the worktree root out of a project's opaque config object.

    An unusable value (a ``{branch}`` typo, a NUL) is dropped with a
    warning rather than raised: the caller is mid-create, and falling back
    to the built-in layout is better than failing the session. The
    settings dialog is where a bad value gets reported to the user.

    :param config: The project's stored ``config`` dict, or ``None``.
    :returns: The configured root, e.g. ``".worktrees"``, or ``None``
        when unset, blank, or unusable.
    """
    if not config:
        return None
    raw = config.get(ROOT_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = raw.strip()
    try:
        validate_worktree_root(root)
    except WorktreeError as exc:
        _logger.warning(
            "Ignoring unusable %s %r: %s",
            ROOT_KEY,
            root,
            exc.message,
        )
        return None
    return root


def _project_config_for_conversation(
    *,
    conv: Conversation,
    user_id: str | None,
    project_store: ProjectStore | None,
) -> dict[str, Any] | None:
    """Resolve the stored config of the project a session is filed under.

    A session reaches its project either through the first-class
    ``project_id`` or (for a just-created, "born filed" session) through
    the legacy ``omni_project`` label carrying the project NAME — the
    same dual-read the sidebar does. Projects are owner-private, so the
    lookup is scoped to ``user_id`` and a session filed under someone
    else's project resolves to ``None``.

    Never raises: worktree settings are an optional convenience, so a
    store hiccup degrades to "no project config" rather than failing the
    session create / delete the lookup hangs off.

    :param conv: The session row, e.g. the freshly created conversation.
    :param user_id: The requesting user, or ``None`` in single-user mode.
    :param project_store: Store for first-class projects, or ``None``
        when projects are not wired (then no project can be resolved).
    :returns: The project's ``config`` dict, or ``None``.
    """
    if project_store is None:
        return None
    try:
        if conv.project_id is not None:
            project = project_store.get(conv.project_id, user_id=user_id)
            return project.config if project else None
        label_name = (conv.labels or {}).get(PROJECT_LABEL_KEY, "").strip()
        if not label_name:
            return None
        for project in project_store.list(user_id=user_id):
            if project.name == label_name:
                return project.config
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Could not resolve project worktree settings for session %s",
            conv.id,
            exc_info=True,
        )
    return None


def project_config_for_name(
    *,
    project_name: str | None,
    user_id: str | None,
    project_store: ProjectStore | None,
) -> dict[str, Any] | None:
    """Resolve a project's stored config by NAME.

    The session-create path needs the project's settings BEFORE the
    conversation row exists (the worktree is created first), so it cannot
    go through a session row. It files by name: the JSON create body
    carries only the ``omni_project`` label, and the first-class
    ``project_id`` is written by a follow-up PATCH. Same owner-scoping and
    same never-raises contract as
    :func:`_project_config_for_conversation`.

    :param project_name: Project NAME from the request's
        ``omni_project`` label, or ``None`` for an unfiled session.
    :param user_id: The requesting user, or ``None`` in single-user mode.
    :param project_store: Store for first-class projects, or ``None``.
    :returns: The project's ``config`` dict, or ``None``.
    """
    if project_store is None:
        return None
    name = (project_name or "").strip()
    if not name:
        return None
    try:
        for project in project_store.list(user_id=user_id):
            if project.name == name:
                return project.config
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Could not resolve project worktree settings for project %r",
            name,
            exc_info=True,
        )
    return None


def hook_config_for_conversation(
    *,
    conv: Conversation,
    user_id: str | None,
    project_store: ProjectStore | None,
) -> WorktreeHookConfig:
    """Resolve the hook config for the project a session is filed under.

    :param conv: The session row, e.g. the freshly created conversation.
    :param user_id: The requesting user, or ``None`` in single-user mode.
    :param project_store: Store for first-class projects, or ``None``
        when projects are not wired (then no project can be resolved).
    :returns: The project's hook config, or :data:`NO_HOOKS`.
    """
    return hook_config_from_project_config(
        _project_config_for_conversation(
            conv=conv,
            user_id=user_id,
            project_store=project_store,
        )
    )


def worktree_root_for_conversation(
    *,
    conv: Conversation,
    user_id: str | None,
    project_store: ProjectStore | None,
) -> str | None:
    """Resolve the worktree root for the project a session is filed under.

    :param conv: The session row.
    :param user_id: The requesting user, or ``None`` in single-user mode.
    :param project_store: Store for first-class projects, or ``None``.
    :returns: The configured root, or ``None`` for the built-in layout.
    """
    return worktree_root_from_project_config(
        _project_config_for_conversation(
            conv=conv,
            user_id=user_id,
            project_store=project_store,
        )
    )
