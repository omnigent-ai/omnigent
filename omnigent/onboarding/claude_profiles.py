"""Claude Code per-session account profiles (issue #503).

The Claude Code CLI authenticates against a single Anthropic subscription
login at a time, stored under ``~/.claude``. Omnigent's claude-sdk harness
spawns that CLI, so without a way to choose the account, every Omnigent
session shares one login — a problem for anyone with more than one Claude
seat (private vs. work, consultants).

Claude Code honors the ``CLAUDE_CONFIG_DIR`` environment variable: pointing
the ``claude`` process at a per-profile directory isolates its
``.credentials.json``, settings, and session state. This module owns the
``claude_profiles:`` block of ``~/.omnigent/config.yaml`` that maps a
profile *name* (what the user picks) to a *config_dir* (what the harness
injects as ``CLAUDE_CONFIG_DIR`` on the spawned CLI subprocess):

.. code-block:: yaml

    claude_profiles:
      active_default: work
      profiles:
        - name: work
          display: "Work (Anthropic)"
          config_dir: ~/.omnigent/claude-profiles/work
        - name: personal
          display: "Personal"
          config_dir: ~/.omnigent/claude-profiles/personal
      # Optional: profiles the runner fans sub-agent work across
      # concurrently (issue #692). Each sub-agent spawn is assigned
      # one profile round-robin from this list, so N sub-agents run
      # across N budgets in parallel instead of all on active_default.
      # Names must match a profile above; unknown names are dropped.
      fanout_pool: [work, personal]

Modeled on the dedicated ``cursor:`` / ``antigravity:`` config blocks (see
:mod:`omnigent.onboarding.cursor_auth`): a per-feature top-level block
rather than the shared ``auth:`` gateway credential, because these are
Claude-Code-local login dirs, not harness API keys. The block holds **no
secrets** — credentials live inside each profile's ``config_dir`` (placed
there by ``claude auth login --claudeai`` run with ``CLAUDE_CONFIG_DIR``
set), never in the Omnigent config. The server's profile-list endpoint
exposes only ``name`` + ``display``.

A profile ``name`` must be 1-64 chars of ``[A-Za-z0-9_-]`` (the charset the
server validates a session-create pick against) and must not be the
reserved ``__default__``, which the account picker uses as its
"no per-session override" sentinel. A profile that breaks either rule
could never be selected, so it is dropped at load with a warning.

Profile resolution (name → ``config_dir``) happens on the *runner* (the
host where the Claude CLI is spawned), via :func:`load_config`, which
respects ``$OMNIGENT_CONFIG_HOME`` for test isolation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.provider_config import load_config

_logger = logging.getLogger(__name__)

# The dedicated top-level config block. Profiles are stored as a list of
# mappings under ``profiles:`` with an optional ``active_default:`` name.
CLAUDE_PROFILES_CONFIG_KEY = "claude_profiles"
_PROFILES_FIELD = "profiles"
_ACTIVE_DEFAULT_FIELD = "active_default"
_FANOUT_POOL_FIELD = "fanout_pool"
_NAME_FIELD = "name"
_DISPLAY_FIELD = "display"
_CONFIG_DIR_FIELD = "config_dir"

# Canonical profile-name charset. A name round-trips through the web picker
# and back as ``claude_profile`` on session create, where the server
# re-validates it against this same pattern — so it is the single source of
# truth for both ends (the server imports it rather than restating it).
CLAUDE_PROFILE_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_NAME_RE = re.compile(CLAUDE_PROFILE_NAME_PATTERN)

# Reserved name: the web account picker needs a non-empty value to mean
# "no per-session override" (a Radix Select cannot hold an empty string),
# and it uses this one. It matches the charset above, so an operator could
# otherwise declare a real profile that the picker can never select.
RESERVED_CLAUDE_PROFILE_NAME = "__default__"


@dataclass(frozen=True)
class ClaudeProfile:
    """A configured Claude Code account profile.

    :param name: The profile identifier the user picks (e.g. ``"work"``).
        Used as the lookup key and the value sent on session create.
        Validated against :data:`CLAUDE_PROFILE_NAME_PATTERN` at load.
    :param display: Optional human-readable label for UI pickers
        (e.g. ``"Work (Anthropic)"``). ``None`` falls back to ``name``.
    :param config_dir: The directory the spawned Claude CLI uses as its
        ``CLAUDE_CONFIG_DIR`` (isolates credentials / settings / session
        state). Already ``~``-expanded — :func:`load_claude_profiles`
        expands the raw YAML value when it builds the instance.
    """

    name: str
    config_dir: str
    display: str | None = None


def _expand_config_dir(raw: str) -> str:
    """Expand a leading ``~`` in a config_dir value.

    Only ``~`` is expanded — a relative value stays relative (the Claude CLI
    resolves ``CLAUDE_CONFIG_DIR`` against its own cwd, which the spawn env
    controls, so the loader does not second-guess an operator who wrote one).

    :param raw: The raw ``config_dir`` value from YAML, e.g.
        ``"~/.omnigent/claude-profiles/work"``.
    :returns: The path with ``~`` expanded, e.g.
        ``"/home/u/.omnigent/claude-profiles/work"``.
    """
    return str(Path(raw).expanduser())


def load_claude_profiles(
    config: dict[str, object] | None = None,
) -> list[ClaudeProfile]:
    """Load the configured Claude Code profiles.

    Entries whose ``name`` breaks :data:`CLAUDE_PROFILE_NAME_PATTERN` or
    equals the reserved :data:`RESERVED_CLAUDE_PROFILE_NAME` are skipped
    with a logged warning: neither could survive a round-trip through the
    picker and the server's create-time validator, so dropping them here
    surfaces the config error once instead of failing every spawn.

    :param config: A pre-loaded config mapping; ``None`` loads
        ``~/.omnigent/config.yaml`` via :func:`load_config`.
    :returns: The valid profiles declared under the ``claude_profiles:``
        block, in declared order. An empty list when the block is absent or
        malformed (a missing/malformed block is not fatal — the harness
        falls back to the CLI's default ``~/.claude``).
    """
    cfg = load_config() if config is None else config
    block = cfg.get(CLAUDE_PROFILES_CONFIG_KEY)
    if not isinstance(block, dict):
        return []
    raw_profiles = block.get(_PROFILES_FIELD)
    if not isinstance(raw_profiles, list):
        return []
    profiles: list[ClaudeProfile] = []
    for entry in raw_profiles:
        if not isinstance(entry, dict):
            continue
        name = entry.get(_NAME_FIELD)
        config_dir = entry.get(_CONFIG_DIR_FIELD)
        if not isinstance(name, str) or not name:
            continue
        if name == RESERVED_CLAUDE_PROFILE_NAME:
            _logger.warning(
                "claude_profiles: skipping profile %r — that name is reserved for "
                "the account picker's no-override option",
                name,
            )
            continue
        if not _NAME_RE.match(name):
            _logger.warning(
                "claude_profiles: skipping profile %r — name must match %s",
                name,
                CLAUDE_PROFILE_NAME_PATTERN,
            )
            continue
        if not isinstance(config_dir, str) or not config_dir:
            continue
        display = entry.get(_DISPLAY_FIELD)
        if display is not None and not isinstance(display, str):
            display = None
        profiles.append(
            ClaudeProfile(
                name=name,
                config_dir=_expand_config_dir(config_dir),
                display=display,
            )
        )
    return profiles


def active_default_claude_profile(
    config: dict[str, object] | None = None,
) -> str | None:
    """Return the configured ``active_default`` profile name, if any.

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: The ``active_default`` name, or ``None`` when unset/absent.
    """
    cfg = load_config() if config is None else config
    block = cfg.get(CLAUDE_PROFILES_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    default = block.get(_ACTIVE_DEFAULT_FIELD)
    return default if isinstance(default, str) and default else None


def claude_profile_by_name(
    name: str,
    config: dict[str, object] | None = None,
) -> ClaudeProfile | None:
    """Look up a single profile by name.

    :param name: The profile name to resolve.
    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: The matching :class:`ClaudeProfile`, or ``None`` when no
        profile with that name is configured.
    """
    for profile in load_claude_profiles(config):
        if profile.name == name:
            return profile
    return None


def resolve_claude_profile_config_dir(
    name: str | None,
    config: dict[str, object] | None = None,
) -> str | None:
    """Resolve a profile name to its expanded ``config_dir``.

    The runner / spawn-env builder calls this to translate the user's
    profile pick into the ``CLAUDE_CONFIG_DIR`` value injected on the
    spawned Claude CLI subprocess.

    :param name: The profile name (e.g. from ``spec.executor.config
        ["claude_profile"]`` or the per-session create override). ``None``
        resolves to the ``active_default`` profile, then to ``None``
        (fall back to the CLI's default ``~/.claude``).
    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: The expanded config_dir path, or ``None`` when the name is
        unknown / no profiles are configured (caller leaves
        ``CLAUDE_CONFIG_DIR`` unset so the CLI uses its default).
    """
    if name is None:
        name = active_default_claude_profile(config)
        if name is None:
            return None
    profile = claude_profile_by_name(name, config)
    return profile.config_dir if profile is not None else None


def claude_profiles_list(
    config: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    """Return profiles as a secret-free list for the server profile endpoint.

    Exposes only ``name`` + ``display`` (never ``config_dir`` or any
    credential) so the ap-web account picker can populate its dropdown
    without the server ever serving profile internals.

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: A list of ``{"name": ..., "display": ...}`` mappings in
        declared order. ``display`` falls back to ``name`` when unset.
    """
    return [{"name": p.name, "display": p.display or p.name} for p in load_claude_profiles(config)]


def load_claude_fanout_pool(
    config: dict[str, object] | None = None,
) -> list[str]:
    """Load the configured claude-profile fan-out pool (issue #692).

    The ``fanout_pool:`` entry under the ``claude_profiles:`` block names the
    profiles the runner fans sub-agent work across concurrently. When a parent
    agent spawns a sub-agent, the runner assigns one profile from this pool
    (round-robin per parent, see :mod:`omnigent.runner.tool_dispatch`) and
    injects it as the child session's ``claude_profile``, so N sub-agents run
    across N budgets in parallel instead of all on ``active_default``.

    Names must resolve to a configured :class:`ClaudeProfile` — which
    :func:`load_claude_profiles` has already validated against
    :data:`CLAUDE_PROFILE_NAME_PATTERN` and the reserved
    :data:`RESERVED_CLAUDE_PROFILE_NAME` — so a pool entry can never carry a
    name the server would reject when the spawn creates the child session.
    Anything else (a typo, a profile that failed validation, a non-string) is
    dropped with a warning rather than breaking every spawn; a partial pool
    still fans out. Declared order is preserved so round-robin is
    deterministic and testable.

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config via :func:`load_config`.
    :returns: The validated pool profile names in declared order. ``[]`` when
        the block or ``fanout_pool:`` entry is absent, not a list, or lists
        only unknown names — i.e. fan-out is simply disabled and sub-agents
        keep today's behavior (all on ``active_default``).
    """
    cfg = load_config() if config is None else config
    block = cfg.get(CLAUDE_PROFILES_CONFIG_KEY)
    if not isinstance(block, dict):
        return []
    raw_pool = block.get(_FANOUT_POOL_FIELD)
    if not isinstance(raw_pool, list):
        return []
    configured = {p.name for p in load_claude_profiles(cfg)}
    pool: list[str] = []
    for name in raw_pool:
        if not isinstance(name, str) or not name:
            _logger.warning("claude_profiles: dropping non-string fanout_pool entry %r", name)
            continue
        if name not in configured:
            _logger.warning(
                "claude_profiles: dropping fanout_pool entry %r — no valid profile with "
                "that name is configured",
                name,
            )
            continue
        if name not in pool:
            pool.append(name)
    return pool
