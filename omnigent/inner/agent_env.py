"""Deny-by-default environment filtering for agent-CLI subprocesses.

Every harness spawns a vendor CLI as a child process. Handing it
``os.environ`` hands it every secret on the host — cloud tokens, other
providers' API keys — whether or not a sandbox wraps the process afterwards.

The pi and codex executors already filtered; five siblings did not
(omnigent-ai/omnigent#3445). This is the shared implementation so the next
harness added is filtered by construction rather than by remembering.

The model is not "no credentials ever". It is:

    shared safe base  +  this harness's own config/provider families
                      +  whatever the spec declared in
                         ``os_env.sandbox.env_passthrough``

so a harness still sees the variables it legitimately authenticates with,
and stops seeing every *other* provider's.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from omnigent._platform import WINDOWS_ENV_PASSTHROUGH
from omnigent.runner.identity import OMNIGENT_SESSION_ENV_VAR

# Desktop access requires an explicit grant outside the sandbox.
DESKTOP_SESSION_ENV_VARS: frozenset[str] = frozenset(
    {"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}
)

# Categories every POSIX CLI needs regardless of vendor: where the user's
# config lives, how to reach the network, how to format output, where to put
# temp files, and the "you are inside Omnigent" marker.
#
# Deliberately NOT here: USER / LOGNAME / SHELL / TZ. pi passes those and
# codex does not, so they stay per-harness rather than silently widening
# codex's set while this refactor claims to preserve it.
BASE_ALLOW_PREFIXES: tuple[str, ...] = (
    "HTTP_",
    "HTTPS_",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_",
    "XDG_",
    "LANG",
    "LC_",
)

BASE_ALLOW_EXACT: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        # Node's equivalent of SSL_CERT_FILE, so it belongs in the same
        # reach-the-network category as the proxy and SSL_ entries. Most agent
        # CLIs are Node programs that honour this and ignore SSL_CERT_FILE;
        # without it a corporate-CA user upgrading would hit TLS failures from
        # every harness that does not happen to own a NODE_ prefix of its own.
        "NODE_EXTRA_CA_CERTS",
        # ssh-agent socket path, so an agent's git-over-SSH and SSH-cert
        # tooling authenticates. A path to a unix socket, not a bearer token:
        # reaching the agent still requires the user's own ssh-agent to be
        # running and to hold the key. Shared here because every harness runs
        # git, not just the one whose bug surfaced it.
        "SSH_AUTH_SOCK",
        OMNIGENT_SESSION_ENV_VAR,
        # Windows system / profile constants (SYSTEMROOT is mandatory for
        # Winsock init, USERPROFILE for Path.home(), etc.); no-ops on POSIX
        # because these names don't exist there. See omnigent._platform.
        *WINDOWS_ENV_PASSTHROUGH,
    }
)


def clean_agent_env(
    *,
    allow_prefixes: Iterable[str] = (),
    allow_exact: Iterable[str] = (),
    deny_exact: Iterable[str] = (),
    extra_allowed: Iterable[str] = (),
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Filter an environment down to the base plus this harness's own families.

    :param allow_prefixes: Prefix families this harness owns, e.g. ``("QWEN_",)``.
        Added to :data:`BASE_ALLOW_PREFIXES`.
    :param allow_exact: Extra exact names this harness needs. Added to
        :data:`BASE_ALLOW_EXACT`.
    :param deny_exact: Names excluded even when a prefix matches. Codex uses
        this for ``OPENAI_API_KEY`` so the CLI falls back to subscription auth
        instead of billing a developer key — a cost rule, not a security one.
    :param extra_allowed: Spec-declared names from
        ``os_env.sandbox.env_passthrough``. This is the documented escape hatch
        for an agent that authenticates from a variable outside its own family.
    :param source: Environment to filter. Defaults to ``os.environ``; injectable
        for tests.
    :returns: A filtered copy; desktop-session variables require ``extra_allowed``.
    """
    env_source = os.environ if source is None else source
    prefixes = BASE_ALLOW_PREFIXES + tuple(allow_prefixes)
    extra = set(extra_allowed)
    exact = BASE_ALLOW_EXACT | set(allow_exact) | extra
    denied = set(deny_exact) | (DESKTOP_SESSION_ENV_VARS - extra)
    return {
        key: value
        for key, value in env_source.items()
        if key not in denied and (key in exact or key.startswith(prefixes))
    }


def strip_desktop_session_env(env: Mapping[str, str]) -> dict[str, str]:
    """Remove host desktop locators without mutating the source environment."""
    return {key: value for key, value in env.items() if key not in DESKTOP_SESSION_ENV_VARS}


def desktop_session_passthrough(os_env: object | None) -> dict[str, str]:
    """Forward declared desktop variables only for explicitly unsandboxed specs."""
    sandbox = getattr(os_env, "sandbox", None)
    if getattr(sandbox, "type", None) != "none":
        return {}
    allowed = DESKTOP_SESSION_ENV_VARS.intersection(declared_passthrough(os_env))
    return {name: os.environ[name] for name in allowed if name in os.environ}


def declared_passthrough(os_env: object | None) -> tuple[str, ...]:
    """Env-var names the spec declared for passthrough.

    Lives on ``os_env.sandbox.env_passthrough``. Returns an empty tuple when
    any link in that chain is absent.

    Lives here rather than in any one executor so no harness has to import a
    sibling harness's module to read a field that belongs to the sandbox spec.
    Duck-typed on purpose: the only contract is the ``sandbox.env_passthrough``
    chain, so a caller holding a partially-built or stubbed spec is fine.
    """
    sandbox = getattr(os_env, "sandbox", None) if os_env is not None else None
    names = getattr(sandbox, "env_passthrough", None) if sandbox is not None else None
    if not names:
        return ()
    if getattr(sandbox, "type", None) == "none":
        return tuple(names)
    return tuple(name for name in names if name not in DESKTOP_SESSION_ENV_VARS)
