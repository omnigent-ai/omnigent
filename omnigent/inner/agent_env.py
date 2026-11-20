"""Deny-by-default environment filtering for agent-CLI subprocesses.

Every harness spawns a vendor CLI as a child process. Handing it
``os.environ`` hands it every secret on the host — cloud tokens, other
providers' API keys — whether or not a sandbox wraps the process afterwards.

The pi and codex executors already filtered; five siblings did not
(omnigent-ai/omnigent#3445). This is the shared implementation so the next
harness added is filtered by construction rather than by remembering.

The model is not "no credentials ever". It is:

    shared safe base  +  this harness's own config/provider families
                      +  exact names the host owner forwarded through
                         ``OMNIGENT_RUNNER_ENV_PASSTHROUGH``
                      +  whatever the spec declared in
                         ``os_env.sandbox.env_passthrough``

so a harness still sees the variables it legitimately authenticates with,
and stops seeing every *other* provider's.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from omnigent._platform import WINDOWS_ENV_PASSTHROUGH
from omnigent.databricks_ai_gateway import CI_CONTEXT_ENV, ci_request_tags
from omnigent.inner.env_template import (
    RESOLVED_TEMPLATE_NAMES_ENV_VAR,
    SESSION_WORKSPACE_TEMPLATE_VAR,
    declared_env_template,
    resolve_env_template,
    resolved_template_names,
    workspace_template_context,
)
from omnigent.runner.identity import (
    OMNIGENT_SESSION_ENV_VAR,
    RUNNER_AUTH_SECRET_ENV_VARS,
    RUNNER_SESSION_ENV_NAMES_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
)

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
        # git's CA paths, for git builds that ignore SSL_CERT_FILE.
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        # ssh-agent socket path, so an agent's git-over-SSH and SSH-cert
        # tooling authenticates. A path to a unix socket, not a bearer token:
        # reaching the agent still requires the user's own ssh-agent to be
        # running and to hold the key. Shared here because every harness runs
        # git, not just the one whose bug surfaced it.
        "SSH_AUTH_SOCK",
        # The user's URL-opener command, so an agent's CLI logins open URLs the
        # way the user configured (e.g. forwarded to a laptop from a remote box).
        "BROWSER",
        # Config locations and hosts let agent tools use the host's CLI login.
        "GH_CONFIG_DIR",
        "GLAB_CONFIG_DIR",
        "GITLAB_HOST",
        "GLAB_HOST",
        OMNIGENT_SESSION_ENV_VAR,
        # Windows system / profile constants (SYSTEMROOT is mandatory for
        # Winsock init, USERPROFILE for Path.home(), etc.); no-ops on POSIX
        # because these names don't exist there. See omnigent._platform.
        *WINDOWS_ENV_PASSTHROUGH,
    }
)


def agent_env_name_filter(
    *,
    allow_prefixes: Iterable[str] = (),
    allow_exact: Iterable[str] = (),
    deny_exact: Iterable[str] = (),
    extra_allowed: Iterable[str] = (),
    source: Mapping[str, str] | None = None,
) -> Callable[[str], bool]:
    """Return the name test :func:`clean_agent_env` applies; takes the same arguments."""
    env_source = os.environ if source is None else source
    prefixes = BASE_ALLOW_PREFIXES + tuple(allow_prefixes)
    operator_names = {
        name.strip()
        for name in env_source.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "").split(",")
        if name.strip()
    }
    extra = set(extra_allowed)
    exact = (
        BASE_ALLOW_EXACT
        | set(allow_exact)
        | extra
        | operator_names
        | forwarded_session_env_names(env_source)
        | resolved_template_names(env_source)
    )
    denied = set(deny_exact) | RUNNER_AUTH_SECRET_ENV_VARS | (DESKTOP_SESSION_ENV_VARS - extra)

    def is_allowed(name: str) -> bool:
        return name not in denied and (name in exact or name.startswith(prefixes))

    return is_allowed


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
        for tests. Its ``OMNIGENT_RUNNER_ENV_PASSTHROUGH`` names are also
        allowed, subject to the deny rules.
    :returns: A filtered copy without runner-auth secrets that also keeps the
        variables the launching CLI forwarded for this session; desktop-session
        variables require ``extra_allowed``.
    """
    env_source = os.environ if source is None else source
    denied = frozenset(deny_exact)
    is_allowed = agent_env_name_filter(
        allow_prefixes=allow_prefixes,
        allow_exact=allow_exact,
        deny_exact=denied,
        extra_allowed=extra_allowed,
        source=env_source,
    )
    filtered = {key: value for key, value in env_source.items() if is_allowed(key)}
    # The CI context is synthesized, not inherited, so only an explicit deny drops it.
    if CI_CONTEXT_ENV not in denied and (tags := ci_request_tags(env_source)):
        filtered[CI_CONTEXT_ENV] = json.dumps(tags, separators=(",", ":"))
    return filtered


def forwarded_session_env_names(source: Mapping[str, str] | None = None) -> frozenset[str]:
    """Names of the variables the launching CLI forwarded to this runner.

    :param source: Runner environment to read. Defaults to ``os.environ``.
    :returns: Forwarded names, never including Omnigent's own variables.
    """
    env = os.environ if source is None else source
    names = env.get(RUNNER_SESSION_ENV_NAMES_ENV_VAR, "").split(",")
    return frozenset(name for name in names if name and not name.upper().startswith("OMNIGENT"))


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


def resolve_agent_env_template(
    os_env: object | None,
    *,
    session_workspace: str | Path | None,
    for_harness_child: bool = False,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve ``os_env.sandbox.env_template`` with the :func:`clean_agent_env` filter.

    :param os_env: The agent's ``OSEnvSpec``, or ``None``.
    :param session_workspace: The session's workspace; ``None`` uses the runner's.
    :param for_harness_child: Also export the session workspace and the resolved
        names, which the child's CLI filter and ``sys_os_*`` helper read.
    :param source: Environment to read. Defaults to ``os.environ``.
    :returns: Resolved variables, or ``{}`` when the spec declares no template.
    """
    template = declared_env_template(os_env)
    if template is None:
        return {}
    env_source = os.environ if source is None else source
    context = workspace_template_context(
        session_workspace=session_workspace,
        runner_workspace=env_source.get(RUNNER_WORKSPACE_ENV_VAR),
    )
    resolved = resolve_env_template(
        template,
        source_env=env_source,
        is_allowed=agent_env_name_filter(
            extra_allowed=declared_passthrough(os_env), source=env_source
        ),
        context=context,
    )
    if for_harness_child:
        if resolved:
            resolved[RESOLVED_TEMPLATE_NAMES_ENV_VAR] = ",".join(sorted(resolved))
        if SESSION_WORKSPACE_TEMPLATE_VAR in context:
            resolved[SESSION_WORKSPACE_TEMPLATE_VAR] = context[SESSION_WORKSPACE_TEMPLATE_VAR]
    return resolved
