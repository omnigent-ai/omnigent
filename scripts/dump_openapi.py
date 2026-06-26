"""Generate and post-process the omnigent OpenAPI 3.2 document.

The omnigent server runs on FastAPI 0.135.x, which emits OpenAPI
3.1. OpenAPI 3.2 (released September 2025) introduced first-class
support for sequential media types — specifically, the
``itemSchema`` keyword for describing each item in a streaming
response on a ``text/event-stream`` content entry. We need 3.2's
``itemSchema`` so the SSE routes describe their per-event shape
correctly to consuming SDK / docs tooling.

This script:

1. Imports :func:`omnigent.server.app.create_app` and instantiates
   it against in-memory store stubs (no DB needed).
2. Calls ``app.openapi()`` to get the FastAPI-generated 3.1 dict.
3. Bumps the top-level ``openapi`` field to ``"3.2.0"``.
4. Materializes the :data:`ServerStreamEvent` discriminated union as
   a top-level entry under ``components.schemas`` so SSE responses
   can ``$ref`` it.
5. Rewrites the ``text/event-stream`` content entries on the SSE
   routes to use the OAS 3.2 ``itemSchema`` keyword in place of
   FastAPI's 3.1 ``schema`` keyword.
6. Writes the result to ``openapi.json`` at the repo root.

Run with no arguments to (re)generate the file. Pass ``--check``
in CI to verify the on-disk file is up to date — non-zero exit
means a developer changed the spec without regenerating.

Usage::

    python scripts/dump_openapi.py             # write openapi.json
    python scripts/dump_openapi.py --check     # exit 1 if drifted
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# DBOS's ``compute_app_version`` calls ``hashlib.md5()`` without
# ``usedforsecurity=False`` for a non-security content hash, which
# raises ``ValueError`` on FIPS-enabled hosts. Patch md5 here BEFORE
# any DBOS import so both this script and the drift test
# (``tests/server/test_openapi_drift.py``, which imports
# ``generate_spec`` from this module) work on FIPS hosts. The flag is
# the standard Python 3.9+ way to opt non-security md5 calls out of
# the FIPS gate; on non-FIPS hosts it's a harmless no-op.
_orig_md5 = hashlib.md5


def _fips_safe_md5(*args: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("usedforsecurity", False)
    return _orig_md5(*args, **kwargs)


hashlib.md5 = _fips_safe_md5

from pydantic import TypeAdapter  # noqa: E402 — must follow md5 patch

# ── Module-level constants (rule 34) ──────────────────────────────

# Output path. The spec lives at the repo root so external tooling
# (Stoplight, openapi-generator, redocly, …) can pick it up via a
# stable URL relative to the project. Pinned absolute via
# ``Path(__file__).resolve().parent.parent`` so the script works
# regardless of CWD.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_OPENAPI_OUT: Path = _REPO_ROOT / "openapi.json"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# OpenAPI 3.2.0 release: 2025-09-23. We pin the patch version so
# the post-processed doc declares its target spec unambiguously.
_OPENAPI_VERSION: str = "3.2.0"

# Routes that emit Server-Sent Events. Each tuple is
# ``(path, method)`` keyed exactly as the OpenAPI ``paths`` map
# stores them. If the route inventory grows (e.g. a new SSE
# endpoint), add the entry here so post-processing rewrites it.
_SSE_ROUTES: list[tuple[str, str]] = [
    ("/v1/responses", "post"),
    ("/v1/sessions/{session_id}/stream", "get"),
]

# ── Document-level enrichment ─────────────────────────────────────
#
# FastAPI emits accurate per-operation schemas but none of the
# document-level metadata an integrator needs: no ``servers``, no auth
# description, no ``info.description``, and only bare snake_case tags.
# We inject that connective tissue here so the published reference
# (rendered by Scalar on the omnigent website) is usable for building
# an integration. Keeping it in this script — rather than scattering it
# across the route decorators — confines presentation concerns to the
# spec-generation layer, and the drift test
# (``tests/server/test_openapi_drift.py``) guards the result.

# Self-hosted base URL. ``omnigent server`` binds 127.0.0.1:6767 by
# default (see ``_DEFAULT_LOCAL_PORT`` in
# ``omnigent/host/local_server.py``).
_SERVERS: list[dict[str, str]] = [
    {
        "url": "http://127.0.0.1:6767",
        "description": "Self-hosted Omnigent server (default local port).",
    },
]

# Markdown prose shown at the top of the rendered reference. Covers
# what the API is, the self-hosted base URL, and the deployment-driven
# auth model (there is no bearer/API-key scheme — see
# ``omnigent/server/auth.py``).
_INFO_DESCRIPTION: str = """\
Omnigent is an open-source meta-harness for building and running AI \
agents. This is the REST API exposed by the Omnigent server: use it to \
create and drive **sessions**, manage **agents**, **hosts**, and \
**runners**, attach **contextual policies**, post **comments**, and work \
with session **resources** — files, terminals, and sandboxed \
environments.

## Base URL

Omnigent is self-hosted. The server binds `http://127.0.0.1:6767` by \
default (`omnigent server`); point the base URL at your own deployment.

## Authentication

There is no API-key or bearer-token scheme. Identity is supplied by the \
deployment's configured auth provider (`OMNIGENT_AUTH_PROVIDER`):

- **Trusted proxy header** (default) — an upstream proxy injects an \
identity header (`X-Forwarded-Email`, configurable). Single-user local \
runtimes fall back to a reserved `local` user.
- **Session cookie** — a signed session cookie minted after an \
interactive OIDC or accounts login. It is named `ap_session` over HTTP \
(the advertised local default) and `__Host-ap_session` under HTTPS, where \
the `__Host-` prefix guards against subdomain cookie-tossing.

Auth is configured server-side; clients send the cookie or proxy header \
according to your deployment.

## Streaming

`GET /v1/sessions/{session_id}/stream` streams Server-Sent Events \
(`text/event-stream`). Each event conforms to the `ServerStreamEvent` \
schema documented below.
"""

# Auth representations. Omnigent has no bearer/API-key scheme — identity
# arrives via a trusted-proxy header or a signed session cookie,
# selected by ``OMNIGENT_AUTH_PROVIDER``. We model both as OpenAPI
# ``apiKey`` schemes so SDK generators and the reference can surface
# them. We deliberately do NOT assert a top-level ``security``
# requirement: the active scheme is deployment-specific, and public
# endpoints (``/health``, ``/api/version``) require none — the prose in
# :data:`_INFO_DESCRIPTION` carries the human-facing explanation.
_SECURITY_SCHEMES: dict[str, dict[str, str]] = {
    "proxyHeaderAuth": {
        "type": "apiKey",
        "in": "header",
        "name": "X-Forwarded-Email",
        "description": (
            "Trusted-proxy identity header (header-auth mode, the "
            "default). The header name is configurable via "
            "``OMNIGENT_AUTH_HEADER``."
        ),
    },
    "sessionCookieAuth": {
        "type": "apiKey",
        "in": "cookie",
        # Named to match the advertised HTTP server. The ``__Host-``
        # prefix requires HTTPS (browsers drop it on plain HTTP), so the
        # cookie is ``ap_session`` for the default local deployment and
        # ``__Host-ap_session`` only under HTTPS — see ``secure_cookies``
        # in ``accounts_config.py`` / ``oidc.py``.
        "name": "ap_session",
        "description": (
            "Signed session cookie minted after an interactive OIDC or "
            "accounts login (oidc / accounts auth modes). Named "
            "``ap_session`` over HTTP (the advertised local default); "
            "under HTTPS the secure ``__Host-ap_session`` prefixed form "
            "is used instead."
        ),
    },
}

# Tag display metadata: human descriptions + sidebar order. Each
# ``name`` MUST match the tag FastAPI puts on operations (the route
# decorators use these snake_case values). ``x-displayName`` gives docs
# tooling a readable label in place of the raw tag. Order here is the
# order tags render in the reference sidebar.
#
# This intentionally covers only the stub-build surface that
# ``generate_spec()`` emits: the ``terminals`` router is WebSocket-only
# (no HTTP operations in the spec) and the ``auth`` router is mounted
# only when an auth provider with a ``login_url`` is configured (absent
# in the stub build). If either ever surfaces HTTP operations here, add
# its tag below so the operation doesn't render without a description.
_TAGS: list[dict[str, str]] = [
    {
        "name": "sessions",
        "x-displayName": "Sessions",
        "description": (
            "Create, inspect, fork, and drive agent sessions — the core "
            "unit of work. Covers session items and events, agent "
            "binding, permissions, labels, and child sessions. The "
            "files, terminals, and sandboxed environments attached to a "
            "session live under Session Resources."
        ),
    },
    {
        "name": "session_resources",
        "x-displayName": "Session Resources",
        "description": (
            "Files, terminals, and sandboxed environments attached to a "
            "session: upload and read files, create and manage "
            "terminals, and read, write, edit, and search the "
            "environment filesystem."
        ),
    },
    {
        "name": "session_mcp_servers",
        "x-displayName": "Session MCP Servers",
        "description": (
            "Manage the MCP server declarations on a session's bound "
            "agent: list the configured servers and create, update, and "
            "remove them on session-scoped agents."
        ),
    },
    {
        "name": "agents",
        "x-displayName": "Agents",
        "description": "Discover the built-in agents available to bind to a session.",
    },
    {
        "name": "hosts",
        "x-displayName": "Hosts",
        "description": (
            "Hosts that can launch runners. Browse the host filesystem and create directories."
        ),
    },
    {
        "name": "runners",
        "x-displayName": "Runners",
        "description": "Launch runners on a host and check their status.",
    },
    {
        "name": "session_policies",
        "x-displayName": "Session Policies",
        "description": (
            "Contextual policies scoped to a single session — list, create, update, and remove."
        ),
    },
    {
        "name": "default_policies",
        "x-displayName": "Default Policies",
        "description": "Server-level default policies applied to new sessions.",
    },
    {
        "name": "policy_registry",
        "x-displayName": "Policy Registry",
        "description": "The catalog of policy types available to instantiate.",
    },
    {
        "name": "comments",
        "x-displayName": "Comments",
        "description": (
            "Threaded comments on a session, including sending a comment to the agent."
        ),
    },
    {
        "name": "system",
        "x-displayName": "System",
        "description": "Health, version, and identity endpoints for the running server.",
    },
]

# Utility endpoints FastAPI leaves untagged. We assign them a synthetic
# ``system`` tag so they group cleanly in the reference instead of
# floating in an unlabeled "default" bucket. Keyed ``(path, method)``
# like :data:`_SSE_ROUTES`; keep accurate if the route inventory grows.
_SYSTEM_ROUTES: list[tuple[str, str]] = [
    ("/health", "get"),
    ("/api/version", "get"),
    ("/v1/info", "get"),
    ("/v1/me", "get"),
]

# HTTP methods that denote an operation object inside a path item
# (everything else under a path — ``parameters``, ``servers``, … — is
# not an operation and must be skipped when retagging).
_HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "put", "post", "delete", "patch", "options", "head", "trace"},
)

# Path prefix whose operations form the dedicated "Session Resources"
# group. The sessions router is mounted with ``tags=["sessions"]`` in
# app.py, so every session route — including the resource subtree —
# inherits that single tag. We split this subtree (files, terminals,
# sandboxed environments) into its own section in the published
# reference rather than fracturing the router.
_SESSION_RESOURCES_PREFIX: str = "/v1/sessions/{session_id}/resources"


def _build_app_with_stub_stores() -> Any:
    """
    Build a FastAPI app with stub stores sufficient for OpenAPI generation.

    ``app.openapi()`` walks the route table and Pydantic models — it
    does not call any store methods. We use the SQLite-backed
    implementations against an on-disk temporary database. The temp
    file is best-effort cleaned up by the caller's filesystem.

    :returns: A configured :class:`fastapi.FastAPI` app.
    """
    import tempfile

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    # On-disk SQLite (mkdtemp ensures uniqueness so concurrent
    # invocations don't collide).
    workdir = Path(tempfile.mkdtemp(prefix="oa-openapi-"))
    db_path = workdir / "spec.sqlite"
    db_uri = f"sqlite:///{db_path}"
    artifact_store = LocalArtifactStore(str(workdir / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        comment_store=SqlAlchemyCommentStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=workdir / "cache",
        ),
        # Pass stores so conditionally-mounted routes stay in the spec.
        host_store=HostStore(db_uri),
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )


def _server_stream_event_schema() -> dict[str, Any]:
    """
    Return the JSON-Schema dict for the ``ServerStreamEvent`` union.

    Pydantic's ``TypeAdapter.json_schema(ref_template=...)`` emits a
    schema with internal ``$ref`` pointers in OpenAPI's expected
    ``#/components/schemas/<name>`` form. We then split out the
    union-root schema and inline the variant definitions into the
    components map so each per-event class appears as a top-level
    component schema.

    :returns: A dict with two keys:

        * ``"root"`` — the discriminated-union schema (the value
          assigned to ``components.schemas.ServerStreamEvent``).
        * ``"definitions"`` — the per-variant component schemas
          (merged into ``components.schemas``).
    """
    from omnigent.server.schemas import ServerStreamEvent

    adapter: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)
    schema = adapter.json_schema(ref_template="#/components/schemas/{model}")
    # Pydantic returns ``{"oneOf": [...], "discriminator": {...},
    # "$defs": {...}}``. We hoist ``$defs`` to top-level component
    # schemas and keep the rest as the union root.
    definitions = schema.pop("$defs", {})
    return {"root": schema, "definitions": definitions}


def _rewrite_sse_route(
    paths: dict[str, Any],
    path: str,
    method: str,
) -> None:
    """
    Rewrite one SSE route's ``text/event-stream`` content for OAS 3.2.

    FastAPI emits ``content: {text/event-stream: {schema: <ref>}}``;
    OAS 3.2 uses ``itemSchema`` for sequential media types so each
    event in the stream is described as one item. We rename the key.

    No-op if the route doesn't exist (e.g. a renamed endpoint that
    fell off the inventory) — the caller's job is to keep
    :data:`_SSE_ROUTES` accurate.

    :param paths: The OpenAPI ``paths`` map; mutated in place.
    :param path: Route path, e.g. ``"/v1/responses"``.
    :param method: HTTP method (lowercase), e.g. ``"post"``.
    """
    op = paths.get(path, {}).get(method)
    if op is None:
        return
    ok_response = op.get("responses", {}).get("200", {})
    content = ok_response.get("content", {})
    sse_entry = content.get("text/event-stream")
    if sse_entry is None:
        return
    # Rename ``schema`` → ``itemSchema``. The value (a ``$ref``) is
    # untouched because the union schema applies to each event
    # equally — itemSchema is "validate this against every item
    # in the stream" per the 3.2 spec.
    if "schema" in sse_entry:
        sse_entry["itemSchema"] = sse_entry.pop("schema")


def _tag_system_routes(paths: dict[str, Any]) -> None:
    """
    Assign the synthetic ``system`` tag to untagged utility routes.

    FastAPI leaves ``/health``, ``/api/version``, ``/v1/info``, and
    ``/v1/me`` untagged. Without a tag they render in an unlabeled
    "default" bucket in the reference; tagging them groups the lot
    under "System". Only fills in a tag where none exists — never
    overrides one FastAPI already set.

    No-op for any ``(path, method)`` not present, so
    :data:`_SYSTEM_ROUTES` stays resilient to inventory changes.

    :param paths: The OpenAPI ``paths`` map; mutated in place.
    """
    for path, method in _SYSTEM_ROUTES:
        op = paths.get(path, {}).get(method)
        if op is None:
            continue
        if not op.get("tags"):
            op["tags"] = ["system"]


def _retag_session_resources(paths: dict[str, Any]) -> None:
    """
    Move the session-resource subtree into its own ``session_resources`` tag.

    Every operation whose path starts with
    :data:`_SESSION_RESOURCES_PREFIX` has its tag list *replaced* (not
    appended) with ``["session_resources"]`` so it renders as a
    dedicated section instead of inheriting the broad ``sessions`` tag.
    Prefix-based so newly added resource endpoints group automatically.

    :param paths: The OpenAPI ``paths`` map; mutated in place.
    """
    for path, methods in paths.items():
        if not path.startswith(_SESSION_RESOURCES_PREFIX):
            continue
        for method, op in methods.items():
            if method in _HTTP_METHODS and isinstance(op, dict):
                op["tags"] = ["session_resources"]


# ── reStructuredText docstring → Markdown ─────────────────────────
#
# FastAPI uses each route handler's docstring verbatim as the OpenAPI
# operation ``description``. Our docstrings are Sphinx/reST: ``:param
# name:`` / ``:returns:`` / ``:raises Exc:`` field lists and inline
# ``:class:`Foo``` cross-reference roles. Docs renderers (Scalar) treat
# the description as Markdown, so reST field lists collapse into one
# unreadable run of literal text. We convert that markup to Markdown:
#
#   * each ``:param name:`` whose name matches a real query/path
#     parameter is moved onto that parameter's ``description`` (so it
#     renders inline in the parameter table, not in the prose blob);
#   * request-body / form ``:param`` entries that have no matching
#     parameter become a Markdown ``**Parameters**`` bullet list;
#   * ``:returns:`` becomes a ``**Returns:**`` line, ``:raises:`` a
#     ``**Raises**`` bullet list;
#   * framework-internal params (``request``/``response``/…) are dropped;
#   * inline ``:role:`X``` roles collapse to `` `X` `` and reST double
#     backticks (`` ``X`` ``) normalize to Markdown single backticks.

# Field-list line markers (matched at column 0; continuation lines are
# indented and accumulate onto the field opened above them).
_RST_PARAM = re.compile(r"^:(?:param|parameter|arg|argument|keyword|kwarg)\s+(\S+)\s*:\s*(.*)$")
_RST_RETURNS = re.compile(r"^:returns?\s*:\s*(.*)$")
_RST_RAISES = re.compile(r"^:raises?\s+([^:]+?)\s*:\s*(.*)$")
# Any other reST field marker (``:rtype:``, ``:type x:``, …) — dropped.
_RST_OTHER_FIELD = re.compile(r"^:[a-zA-Z][\w ]*:")
# Inline cross-reference role, e.g. ``:class:`Foo``` → `` `Foo` ``.
_RST_ROLE = re.compile(r":[a-zA-Z]+:`([^`]+)`")
# reST inline literal (double backtick) → Markdown code span (single).
# Non-greedy + DOTALL so a literal may span lines and contain nested
# single backticks (e.g. a role left inside it); the replacement flattens
# those so the resulting code span is valid.
_RST_DOUBLE_BACKTICK = re.compile(r"``(.+?)``", re.DOTALL)

# Handler parameters that are FastAPI plumbing, not API inputs.
_INTERNAL_PARAMS = frozenset(
    {"request", "response", "websocket", "ws", "background_tasks", "bg", "_", "args", "kwargs"},
)


def _rst_double_backtick_to_code(match: re.Match[str]) -> str:
    """Flatten a reST ``literal`` into a single-line Markdown code span."""
    inner = re.sub(r"\s+", " ", match.group(1).replace("`", "")).strip()
    return f"`{inner}`"


def _rst_inline_to_md(text: str) -> str:
    """Convert inline reST roles / literals in *text* to Markdown."""
    text = _RST_ROLE.sub(r"`\1`", text)
    return _RST_DOUBLE_BACKTICK.sub(_rst_double_backtick_to_code, text)


def _rst_field_text(lines: list[str]) -> str:
    """Join a field's (possibly multi-line) body into one Markdown string."""
    joined = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return _rst_inline_to_md(joined)


def _parse_rst_doc(desc: str) -> tuple[str, list[tuple[str, str | None, str]]]:
    """
    Split a reST docstring into Markdown prose and parsed fields.

    Lines before the first reST field marker are prose; ``:param:`` /
    ``:returns:`` / ``:raises:`` open a field that subsequent indented
    continuation lines accumulate onto. Unknown field markers (e.g.
    ``:rtype:``) are discarded.

    :param desc: The raw (reST) description text.
    :returns: ``(prose_markdown, fields)`` where ``fields`` is a list of
        ``(kind, name, text)`` triples (``kind`` in ``param`` /
        ``returns`` / ``raises``) with ``text`` already Markdown.
    """
    prose: list[str] = []
    fields: list[tuple[str, str | None, list[str]]] = []
    cur: tuple[str, str | None, list[str]] | None = None
    in_fields = False
    for line in desc.split("\n"):
        param_m = _RST_PARAM.match(line)
        if param_m:
            in_fields = True
            cur = ("param", param_m.group(1).strip().lstrip("*"), [param_m.group(2)])
            fields.append(cur)
            continue
        returns_m = _RST_RETURNS.match(line)
        if returns_m:
            in_fields = True
            cur = ("returns", None, [returns_m.group(1)])
            fields.append(cur)
            continue
        raises_m = _RST_RAISES.match(line)
        if raises_m:
            in_fields = True
            cur = ("raises", raises_m.group(1).strip(), [raises_m.group(2)])
            fields.append(cur)
            continue
        if in_fields and _RST_OTHER_FIELD.match(line):
            cur = ("drop", None, [])  # unknown field (e.g. :rtype:) — discard
            fields.append(cur)
            continue
        if in_fields:
            if cur is not None:
                cur[2].append(line)
        else:
            prose.append(line)

    prose_md = _rst_inline_to_md("\n".join(prose).strip())
    parsed = [(kind, name, _rst_field_text(body)) for kind, name, body in fields if kind != "drop"]
    return prose_md, parsed


def _reformat_doc(
    desc: str | None,
    targets: dict[str, Any],
    internal: frozenset[str] | None = None,
) -> str | None:
    """
    Convert one reST ``description`` to Markdown.

    Each ``:param name:`` whose ``name`` is a key in *targets* (a
    parameter or property object) is moved onto that object's own
    ``description``; entries with no matching target become a Markdown
    ``**Parameters**`` list. ``:returns:`` / ``:raises:`` become
    ``**Returns:**`` / ``**Raises**`` sections. Names in *internal*
    (FastAPI plumbing) are dropped.

    :param desc: The raw description, or ``None``.
    :param targets: Map of name -> object that may receive a moved
        ``description`` (empty when there are no field targets).
    :param internal: Parameter names to drop entirely (``None`` = drop
        none, used for schema fields).
    :returns: The rebuilt Markdown description, or the original falsy
        value when *desc* is empty.
    """
    if not desc:
        return desc
    skip = internal or frozenset()
    prose_md, fields = _parse_rst_doc(desc)
    body_params: list[tuple[str, str]] = []
    raises: list[tuple[str, str]] = []
    returns: str | None = None
    for kind, name, text in fields:
        if kind == "param":
            if not text or name in skip:
                continue
            target = targets.get(name) if name else None
            if isinstance(target, dict):
                # Move onto the matching field; don't clobber an explicit
                # Field/Query description if one already exists.
                if not target.get("description"):
                    target["description"] = text
            else:
                body_params.append((name or "", text))
        elif kind == "raises" and text:
            raises.append((name or "", text))
        elif kind == "returns" and text:
            returns = text

    sections: list[str] = []
    if prose_md:
        sections.append(prose_md)
    if body_params:
        sections.append("**Parameters**\n\n" + "\n".join(f"- `{n}` — {t}" for n, t in body_params))
    if returns:
        sections.append(f"**Returns:** {returns}")
    if raises:
        sections.append("**Raises**\n\n" + "\n".join(f"- `{e}` — {t}" for e, t in raises))
    return "\n\n".join(sections)


def _reformat_operation_doc(op: dict[str, Any]) -> None:
    """
    Rewrite an operation's (and its responses') reST docs as Markdown.

    Matched ``:param:`` entries move onto ``op['parameters']``; response
    descriptions are reformatted with no field targets.

    :param op: An OpenAPI operation object; mutated in place.
    """
    if op.get("description"):
        targets = {p.get("name"): p for p in op.get("parameters", []) if isinstance(p, dict)}
        op["description"] = _reformat_doc(op["description"], targets, _INTERNAL_PARAMS)
    for resp in (op.get("responses") or {}).values():
        if isinstance(resp, dict) and resp.get("description"):
            resp["description"] = _reformat_doc(resp["description"], {})


def _reformat_schema_node(node: Any) -> None:
    """
    Rewrite a JSON-Schema node's reST ``description`` as Markdown.

    A model's docstring becomes its schema ``description`` with
    ``:param name:`` entries describing its fields; each moves onto the
    matching ``properties[name]`` description. Recurses into nested
    schema positions so inline sub-objects are handled too.

    :param node: A JSON-Schema object (non-dicts are ignored); mutated
        in place.
    """
    if not isinstance(node, dict):
        return
    if node.get("description"):
        props = node.get("properties")
        node["description"] = _reformat_doc(
            node["description"],
            props if isinstance(props, dict) else {},
        )
    properties = node.get("properties")
    if isinstance(properties, dict):
        for sub in properties.values():
            _reformat_schema_node(sub)
    for defs_key in ("$defs", "definitions"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for sub in defs.values():
                _reformat_schema_node(sub)
    for child_key in ("items", "additionalProperties"):
        _reformat_schema_node(node.get(child_key))
    for combinator in ("allOf", "anyOf", "oneOf", "prefixItems"):
        members = node.get(combinator)
        if isinstance(members, list):
            for sub in members:
                _reformat_schema_node(sub)


def _reformat_descriptions(paths: dict[str, Any]) -> None:
    """Convert every operation's reST description to Markdown in place."""
    for methods in paths.values():
        for method, op in methods.items():
            if method in _HTTP_METHODS and isinstance(op, dict):
                _reformat_operation_doc(op)


def _reformat_component_schemas(components: dict[str, Any]) -> None:
    """Convert every component schema's reST description to Markdown."""
    schemas = components.get("schemas")
    if isinstance(schemas, dict):
        for schema in schemas.values():
            _reformat_schema_node(schema)


def _normalize_inline_descriptions(node: Any) -> None:
    """
    Final safety net: normalize inline reST in any remaining description.

    Walks the whole document and converts inline ``:role:`X``` roles and
    reST double-backtick literals to Markdown `` `X` `` in every
    ``description`` string — covering responses, ``info``, tags and
    security schemes that the structured passes don't rewrite.

    :param node: Any spec fragment; mutated in place.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                node[key] = _rst_inline_to_md(value)
            else:
                _normalize_inline_descriptions(value)
    elif isinstance(node, list):
        for value in node:
            _normalize_inline_descriptions(value)


def _enrich_spec(spec: dict[str, Any]) -> None:
    """
    Inject document-level metadata for docs / SDK tooling.

    Adds ``info.description``, ``servers``, top-level ``tags`` with
    human-readable descriptions, and ``components.securitySchemes`` —
    none of which FastAPI emits — tags the untagged utility routes, and
    rewrites reST docstrings (operations, parameters, and component
    schemas) as Markdown.
    Mutates ``spec`` in place. See the module-level enrichment
    constants for the rationale behind each value.

    :param spec: The generated OpenAPI dict; mutated in place.
    """
    info = spec.setdefault("info", {})
    info["description"] = _INFO_DESCRIPTION
    spec["servers"] = _SERVERS
    spec["tags"] = _TAGS

    components = spec.setdefault("components", {})
    components["securitySchemes"] = _SECURITY_SCHEMES

    paths = spec.setdefault("paths", {})
    _tag_system_routes(paths)
    _retag_session_resources(paths)
    _reformat_descriptions(paths)
    _reformat_component_schemas(components)
    _normalize_inline_descriptions(spec)


def generate_spec() -> dict[str, Any]:
    """
    Build, generate, and post-process the OpenAPI 3.2 spec.

    Encapsulates every step (app construction, generation, version
    bump, schema injection, SSE rewrite) so callers can compare the
    generated dict against ``openapi.json`` without writing to disk.

    :returns: The post-processed OpenAPI dict, ready to serialize.
    """
    app = _build_app_with_stub_stores()
    spec = app.openapi()
    # Bump the OpenAPI version literal — we don't change any
    # 3.1-only constructs because FastAPI's emitted shape is also
    # valid 3.2.x (3.2 is JSON-Schema-aligned and largely additive
    # over 3.1).
    spec["openapi"] = _OPENAPI_VERSION

    # Inject the ServerStreamEvent union + per-variant defs into
    # ``components.schemas`` so the SSE routes' $ref points resolve.
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    union = _server_stream_event_schema()
    schemas["ServerStreamEvent"] = union["root"]
    for name, definition in union["definitions"].items():
        # Don't clobber a same-named schema FastAPI already
        # synthesized — the union's per-variant defs include
        # ``ResponseObject`` (referenced from terminal events), and
        # FastAPI also emits one. Keep FastAPI's version; the
        # serialized shape is identical for our models.
        schemas.setdefault(name, definition)

    # Rewrite SSE routes' content entries to use ``itemSchema``.
    paths = spec.get("paths", {})
    for path, method in _SSE_ROUTES:
        _rewrite_sse_route(paths, path, method)

    # Inject document-level metadata (servers, auth, tags, prose) that
    # FastAPI doesn't emit but docs / SDK tooling needs.
    _enrich_spec(spec)

    return spec  # type: ignore[no-any-return]


def main() -> int:
    """
    CLI entry point.

    With no arguments, regenerates ``openapi.json``. With
    ``--check``, compares the generated spec to the on-disk file
    and exits 1 if they differ.

    :returns: 0 on success / no drift, 1 on drift in ``--check``
        mode.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "CI mode — exit 1 if the on-disk openapi.json differs from "
            "the generated spec. Use to fail PRs that change the spec "
            "without regenerating."
        ),
    )
    args = parser.parse_args()

    spec = generate_spec()
    serialized = json.dumps(spec, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not _OPENAPI_OUT.exists():
            sys.stderr.write(
                f"openapi.json not found at {_OPENAPI_OUT}; "
                "run `python scripts/dump_openapi.py` to generate it.\n",
            )
            return 1
        existing = _OPENAPI_OUT.read_text()
        if existing != serialized:
            sys.stderr.write(
                "openapi.json is out of sync with the generated spec.\n"
                "Run `python scripts/dump_openapi.py` to regenerate.\n",
            )
            return 1
        sys.stdout.write("openapi.json is up to date.\n")
        return 0

    _OPENAPI_OUT.write_text(serialized)
    sys.stdout.write(f"Wrote {_OPENAPI_OUT}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
