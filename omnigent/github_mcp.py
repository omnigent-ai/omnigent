"""Per-launch GitHub MCP server, available to any harness.

When a managed sandbox is launched for a user who has connected GitHub, this
module turns that connection into a ``github`` MCP server declaration so the
agent can interact with GitHub (open PRs, read issues, search code, …) through
MCP tools — no ``gh`` / ``git`` CLI required in the host image.

It uses GitHub's *hosted* MCP server over HTTP, so nothing needs to be baked
into the image and it works with any harness that speaks MCP.

Token handling (executor-agnostic broker): the connected user's token is *not*
injected into the sandbox environment. Instead the sandbox fetches it on demand
from the server's host-facing credential endpoint over the existing
host<->server channel (:mod:`omnigent.server.routes.host_github`), using the
broker coordinates every managed host already has in its environment
(:data:`RUNNER_SERVER_URL`, :data:`~omnigent.host.identity.HOST_ID_ENV_VAR`,
:data:`~omnigent.host.identity.HOST_TOKEN_ENV_VAR`). The GitHub token therefore
never lands in the harness's on-disk MCP config: only the lesser, expiring
launch token does, exactly like the git credential helper. The proxy re-fetches
the token per connection.
"""

from __future__ import annotations

import os
import sys

from omnigent.git_credential_github import fetch_broker_token
from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.spec.types import MCPServerConfig

#: Branded button image for PR bodies (the Omnigent star + "Open in Omnigent").
#: GitHub renders PR-body images through its camo proxy, which fetches
#: server-side and cannot reach a deployment behind Cloudflare Access / bot
#: protection — so the image is NOT served from the deployment's own origin.
#: Instead it points at a fixed, publicly camo-reachable URL (GitHub's own CDN
#: by default), like Cursor's PR-footer button: the image is the same for every
#: deployment, only the anchor ``href`` is the per-session URL. Override per
#: deployment with :data:`BUTTON_IMAGE_URL_ENV_VAR`; set it empty to fall back
#: to a plain markdown link.
_DEFAULT_BUTTON_IMAGE_URL = (
    "https://raw.githubusercontent.com/omnigent-ai/omnigent/HEAD/web/public/open-in-omnigent.svg"
)
BUTTON_IMAGE_URL_ENV_VAR = "OMNIGENT_PR_BUTTON_IMAGE_URL"

#: GitHub's hosted (remote) MCP server. Reachable over the sandbox's outbound
#: network; needs only an ``Authorization: Bearer <token>`` header.
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"

#: Server name surfaced to the harness (its tools appear under ``github``).
GITHUB_MCP_NAME = "github"

#: Server URL the sandbox uses to reach the credential broker (set on every
#: managed host/runner).
_SERVER_URL_ENV_VAR = "RUNNER_SERVER_URL"


def _broker_coords() -> tuple[str, str, str] | None:
    """The ``(server, host_id, host_token)`` broker triple, or ``None``.

    Present in every managed sandbox (host and runner); absent in a plain local
    CLI run, where there is no server-vended credential to fetch.
    """
    server = (os.environ.get(_SERVER_URL_ENV_VAR) or "").strip()
    host_id = (os.environ.get(HOST_ID_ENV_VAR) or "").strip()
    host_token = (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()
    if server and host_id and host_token:
        return server, host_id, host_token
    return None


def github_mcp_token() -> str | None:
    """The session owner's GitHub token, fetched from the broker, or ``None``."""
    coords = _broker_coords()
    if coords is None:
        return None
    return fetch_broker_token(*coords)


def github_mcp_available() -> bool:
    """Whether the session owner has connected GitHub (fetches from the broker)."""
    return github_mcp_token() is not None


def _button_image_url() -> str | None:
    """The configured button image URL, or ``None`` to use a plain link.

    Defaults to the Omnigent logo on GitHub's CDN (camo-reachable everywhere);
    :data:`BUTTON_IMAGE_URL_ENV_VAR` overrides it, and an explicit empty value
    disables the image.
    """
    override = os.environ.get(BUTTON_IMAGE_URL_ENV_VAR)
    if override is not None:
        override = override.strip()
        return override or None
    return _DEFAULT_BUTTON_IMAGE_URL


def open_in_omnigent_link(session_url: str) -> str:
    """A branded 'Open in Omnigent' button for a PR body.

    Renders like Cursor's PR-footer button: a fixed Omnigent star image (from a
    camo-reachable CDN, not the deployment's own origin) linking back to the
    session. The session URL is kept verbatim in the anchor ``href`` so the
    session-PR panel still associates the PR by substring match. Falls back to a
    plain markdown link when no image URL is configured.
    """
    image_url = _button_image_url()
    if image_url is None:
        return f"[Open in Omnigent]({session_url})"
    return (
        f'<a href="{session_url}"><img alt="Open in Omnigent" src="{image_url}" height="28"></a>'
    )


def inject_session_link(arguments: dict, session_url: str | None) -> dict:
    """Return *arguments* with the Open-in-Omnigent link appended to ``body``.

    Idempotent and safe: no-op when *session_url* is falsy or already present.
    Used by the GitHub MCP proxy to stamp ``create_pull_request`` bodies so the
    session-PR panel can associate the PR without relying on the model.
    """
    if not session_url:
        return arguments
    args = dict(arguments or {})
    body = str(args.get("body") or "")
    if session_url in body:
        return args
    link = open_in_omnigent_link(session_url)
    args["body"] = f"{body}\n\n{link}".strip() if body else link
    return args


def github_session_url() -> str | None:
    """The public Open-in-Omnigent session URL for this launch, or ``None``.

    Set by the launcher into the runner env (``OMNIGENT_SESSION_URL``) when the
    public base URL and session id are both known.
    """
    return (os.environ.get("OMNIGENT_SESSION_URL") or "").strip() or None


def github_mcp_server_config(
    *, session_url: str | None = None, python_executable: str | None = None
) -> MCPServerConfig | None:
    """The ``github`` MCP server to inject, or ``None`` when GitHub isn't connected.

    A local **stdio** server running :mod:`omnigent.github_mcp_proxy`, which
    forwards to GitHub's hosted MCP and stamps the Open-in-Omnigent link onto
    ``create_pull_request`` bodies. The proxy fetches the GitHub token itself,
    from the broker, per connection — so only the broker coordinates (server
    URL, host id, and the lesser, expiring launch token) plus the session URL
    are handed to the proxy subprocess via ``env``; the GitHub token is never
    written to the harness's on-disk MCP config.

    :param python_executable: Python that has ``omnigent`` importable; defaults
        to the current interpreter (the runner's).
    :returns: A stdio :class:`~omnigent.spec.types.MCPServerConfig`, or ``None``
        when there is no broker to fetch from (a plain local run).
    """
    # Gate on the broker coordinates only — never a network call here, so this
    # stays safe to call on the event loop. The proxy fetches the token at
    # connect time and degrades to an empty tool set if the owner hasn't
    # connected GitHub, so we don't probe connectivity synchronously at build.
    coords = _broker_coords()
    if coords is None:
        return None
    server, host_id, host_token = coords
    env = {
        _SERVER_URL_ENV_VAR: server,
        HOST_ID_ENV_VAR: host_id,
        HOST_TOKEN_ENV_VAR: host_token,
    }
    resolved_session_url = session_url or github_session_url()
    if resolved_session_url:
        env["OMNIGENT_SESSION_URL"] = resolved_session_url
    # Forward a deployment's button-image override so the proxy stamps the same
    # image the deployment configured (the proxy process, not the runner, builds
    # the PR body).
    button_override = os.environ.get(BUTTON_IMAGE_URL_ENV_VAR)
    if button_override is not None:
        env[BUTTON_IMAGE_URL_ENV_VAR] = button_override
    return MCPServerConfig(
        name=GITHUB_MCP_NAME,
        transport="stdio",
        command=python_executable or sys.executable,
        args=["-m", "omnigent.github_mcp_proxy"],
        env=env,
    )
