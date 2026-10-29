"""Shared helpers for uploaded agent bundles."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import posixpath
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import installed_agent_id
from omnigent.errors import SESSION_AGENT_BUNDLE_MISSING_MESSAGE, ErrorCode, OmnigentError
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.spec import AgentSpec, ExtractionError, ToolRuntime, load

if TYPE_CHECKING:
    from omnigent.entities import Agent
    from omnigent.stores.agent_store import AgentStore
    from omnigent.stores.artifact_store import ArtifactStore

_logger = logging.getLogger(__name__)


def _is_dotted_callable_path(path: str) -> bool:
    """Distinguish dotted Python imports from bundle-relative tool files."""
    return "/" not in path and not path.endswith((".py", ".ts")) and "." in path


def _reject_uploaded_callable_tools(spec: AgentSpec) -> None:
    """Reject server-side callable and source-file tools in untrusted bundles."""
    for tool in spec.local_tools:
        if (
            tool.runtime == ToolRuntime.SERVER
            and tool.path is not None
            and _is_dotted_callable_path(tool.path)
        ):
            raise OmnigentError(
                "uploaded agent bundle may not declare a server-side Python "
                f"callable tool (tool {tool.name!r} -> {tool.path!r}); a "
                "'callable:' tool imports and runs operator-trusted code on "
                "the runner, so it is rejected from untrusted uploads",
                code=ErrorCode.INVALID_INPUT,
            )
        # Source-file tools also require operator-authored bundles.
        if (
            tool.runtime == ToolRuntime.SERVER
            and tool.path is not None
            and tool.path.endswith((".py", ".ts"))
        ):
            raise OmnigentError(
                "uploaded agent bundle may not ship a server-side Python/TS "
                f"tool file (tool {tool.name!r} -> {tool.path!r}); its module "
                "body is imported and executed in-process on the runner at tool "
                "discovery, so it is rejected from untrusted uploads",
                code=ErrorCode.INVALID_INPUT,
            )
    for sub in spec.sub_agents:
        _reject_uploaded_callable_tools(sub)


def _cwd_escapes_workspace(spec_cwd: str) -> bool:
    """Whether an agent-spec ``os_env.cwd`` would escape the session workspace.

    ``True`` for an absolute path or one containing a ``..`` segment, in
    either POSIX or Windows form (the runner is POSIX, but checking both
    avoids a separator-style bypass). Such a cwd must be rejected for
    untrusted uploads (GHSA-p8rw-8qj3-hf33): on a runner without
    ``OMNIGENT_RUNNER_WORKSPACE`` it becomes the agent environment root and
    ``copytree`` source, exposing the host filesystem.
    """
    posix, win = PurePosixPath(spec_cwd), PureWindowsPath(spec_cwd)
    return posix.is_absolute() or win.is_absolute() or ".." in posix.parts or ".." in win.parts


def _reject_escaping_cwd(cwd: str | None) -> None:
    """Reject an absolute or escaping working directory in an uploaded spec."""
    if cwd not in (None, ".", "./") and _cwd_escapes_workspace(cwd):
        raise OmnigentError(
            "agent os_env.cwd must be a relative path within the workspace "
            f"(no absolute paths or '..'); got {cwd!r}",
            code=ErrorCode.INVALID_INPUT,
        )


def _reject_uploaded_escaping_cwd(spec: AgentSpec) -> None:
    """Validate working directories in the agent, its terminals, and all sub-agents."""
    _reject_escaping_cwd(spec.os_env.cwd if spec.os_env is not None else None)
    for terminal in (spec.terminals or {}).values():
        if isinstance(terminal.os_env, OSEnvSpec):
            _reject_escaping_cwd(terminal.os_env.cwd)
    for sub in spec.sub_agents:
        _reject_uploaded_escaping_cwd(sub)


def validate_agent_bundle(
    bundle_bytes: bytes,
    *,
    enforce_handler_allowlist: bool = True,
) -> AgentSpec:
    """
    Validate an agent bundle and return the parsed spec.

    Extracts the tarball to a temp directory, parses the spec,
    and checks that a name is present.

    This validates bundles uploaded over HTTP, so it always parses with
    ``expand_env=False``: expanding a tenant-supplied ``${VAR}`` against
    the server process environment would leak server-side secrets.
    The author of an HTTP-uploaded spec is not the
    server operator, so the server must never resolve env vars on their
    behalf — operator-authored specs resolve env at the client /
    registration boundary instead (``omnigent.cli._resolve_bundle_env_vars``).

    :param bundle_bytes: Raw bytes of the ``.tar.gz`` bundle.
    :param enforce_handler_allowlist: When ``True`` (the default),
        reject any ``type: function`` policy whose handler is not a
        registered policy handler, before the inner loader can resolve and
        call it. HTTP entry points always enforce this, including local
        single-user servers. ``False`` is for direct trusted in-process callers.
    :returns: The validated :class:`AgentSpec`.
    :raises OmnigentError: If the bundle is invalid, the spec is
        missing a name, or (when *enforce_handler_allowlist*) a policy
        names an unregistered handler, ``os_env.cwd`` is an absolute or
        escaping path, or a tool declares a server-side callable or Python/TS source file.
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = load(
                bundle_bytes,
                dest=Path(tmpdir) / "agent",
                expand_env=False,
                enforce_handler_allowlist=enforce_handler_allowlist,
            )
    except OmnigentError:
        raise
    except ExtractionError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
    except OSError:
        raise OmnigentError(
            "Agent bundle validation failed", code=ErrorCode.INTERNAL_ERROR
        ) from None
    except Exception as exc:
        # Catch YAML parse errors and other unexpected failures
        # during spec loading so they surface as 400, not 500.
        raise OmnigentError(
            f"invalid agent bundle: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    if spec.name is None:
        raise OmnigentError(
            "agent spec must include a name",
            code=ErrorCode.INVALID_INPUT,
        )

    # HTTP entry points always enforce these uploaded-code boundaries.
    if enforce_handler_allowlist:
        # Terminals and nested agents can override the session working directory.
        _reject_uploaded_escaping_cwd(spec)

        # Server-side callables and source-file tools require operator-authored bundles.
        _reject_uploaded_callable_tools(spec)

    return spec


def agent_needs_own_copy(agent: Agent, user_id: str | None) -> bool:
    """Whether *user_id* must get their own copy of *agent* instead of sharing it.

    Server agents (no session, no owner) and the caller's own agents are shared.
    Another user's agent, or a user agent with no recorded owner, is copied so
    its owner can never change code that runs in the caller's sessions. Without
    auth (``user_id`` None) there is one user, so nothing is copied.
    """
    if user_id is None or agent.operator_authored:
        return False
    return agent.created_by != user_id


def copy_agent_bundle(artifact_store: ArtifactStore, location: str, new_agent_id: str) -> str:
    """Store a copy of the bundle at *location* under *new_agent_id*; return its location."""
    try:
        data = artifact_store.get(location)
    except KeyError as exc:
        _logger.warning(
            "agent bundle %s is missing from the artifact store; cannot copy it", location
        )
        raise OmnigentError(
            SESSION_AGENT_BUNDLE_MISSING_MESSAGE, code=ErrorCode.SESSION_AGENT_MISSING
        ) from exc
    new_location = bundle_location(new_agent_id, data)
    artifact_store.put(new_location, data)
    return new_location


def agent_for_user(
    agent_store: AgentStore,
    artifact_store: ArtifactStore | None,
    agent: Agent,
    user_id: str | None,
) -> Agent:
    """Return the agent a new session or schedule of *user_id* binds: *agent* itself,
    or a new copy *user_id* owns (see :func:`agent_needs_own_copy`).

    Without an artifact store or user agent support there is nowhere to put a
    copy, so *agent* is returned as is.
    """
    if (
        artifact_store is None
        or not agent_store.supports_user_agents
        or not agent_needs_own_copy(agent, user_id)
    ):
        return agent
    try:
        data = artifact_store.get(agent.bundle_location)
    except KeyError as exc:
        raise OmnigentError(
            SESSION_AGENT_BUNDLE_MISSING_MESSAGE, code=ErrorCode.SESSION_AGENT_MISSING
        ) from exc
    return save_user_agent(
        agent_store,
        artifact_store,
        owner=user_id,
        name=agent.name,
        bundle_bytes=data,
        description=agent.description,
        replace_existing=False,
    )


def bundle_location(agent_id: str, bundle_bytes: bytes) -> str:
    """
    Compute a content-addressed artifact key for a bundle.

    :param agent_id: The agent's unique identifier,
        e.g. ``"ag_abc123"``.
    :param bundle_bytes: Raw bytes of the bundle.
    :returns: Artifact store key in the form
        ``"{agent_id}/{sha256_hex}"``.
    """
    digest = hashlib.sha256(bundle_bytes).hexdigest()
    return f"{agent_id}/{digest}"


def bundle_content_digest(bundle_bytes: bytes) -> str | None:
    """
    SHA-256 of the files a bundle extracts to, ignoring archive metadata.

    Tarballs of the same files hash alike even when their timestamps, owners, or
    member order differ (the CLI re-tars on every run), so this identifies an
    upload's content where :func:`bundle_location`'s byte hash cannot. Covers
    each entry's path, type, executable bit, and content or link target; a
    later entry for a path replaces an earlier one, as extraction does.

    :param bundle_bytes: Tarball already checked by :func:`validate_agent_bundle`.
    :returns: A 64-char hex digest, or ``None`` when the archive can't be read
        or holds an entry other than a file, directory, or link.
    """
    entries: dict[str, tuple[str, str]] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:*") as tar:
            for member in tar:
                path = posixpath.normpath(member.name)
                if path == ".":
                    continue
                if member.isfile():
                    data = tar.extractfile(member)
                    if data is None:
                        return None
                    kind = "x" if member.mode & 0o111 else "f"
                    entries[path] = (kind, hashlib.sha256(data.read()).hexdigest())
                elif member.isdir():
                    entries[path] = ("d", "")
                elif member.issym() or member.islnk():
                    entries[path] = ("s" if member.issym() else "h", member.linkname)
                else:
                    return None
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        return None
    canonical = json.dumps(sorted(entries.items()), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def content_bundle_location(agent_id: str, bundle_bytes: bytes) -> str:
    """
    Artifact key for a bundle named by its content (:func:`bundle_content_digest`).

    A re-upload of the same files gets the same key, so comparing locations tells
    whether an agent's content changed. Falls back to :func:`bundle_location`.

    :param agent_id: The agent's id, e.g. ``"0f1a2b3c..."``.
    :param bundle_bytes: Tarball already checked by :func:`validate_agent_bundle`.
    :returns: ``"{agent_id}/{sha256_hex}"``.
    """
    digest = bundle_content_digest(bundle_bytes)
    if digest is None:
        return bundle_location(agent_id, bundle_bytes)
    return f"{agent_id}/{digest}"


def save_user_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    *,
    owner: str | None,
    name: str,
    bundle_bytes: bytes,
    description: str | None = None,
    replace_existing: bool = True,
) -> Agent:
    """Save one owner/name identity across installs, session uploads and copies.

    New identities share a deterministic primary key, including concurrent
    requests from different creation paths. Legacy upload IDs remain separate.
    Copying another user's agent may reuse identical files, but never replace
    the recipient's configuration. Explicit uploads replace the whole bundle.
    """
    agent_id = installed_agent_id(owner, name)
    existing = agent_store.get(agent_id)
    location = content_bundle_location(agent_id, bundle_bytes)
    for _ in range(3):
        if existing is None:
            artifact_store.put(location, bundle_bytes)
            try:
                return agent_store.create_user_agent(
                    agent_id, name, location, owner=owner, description=description
                )
            except IntegrityError:
                existing = agent_store.get(agent_id)
                if existing is None:
                    raise
        if existing.kind != "user" or existing.created_by != owner or existing.name != name:
            raise OmnigentError("Agent identity conflict", code=ErrorCode.CONFLICT)
        current_location = location
        if current_location != existing.bundle_location and artifact_store.exists(
            existing.bundle_location
        ):
            current = artifact_store.get(existing.bundle_location)
            if content_bundle_location(agent_id, current) == location:
                current_location = existing.bundle_location
        if not replace_existing and current_location != existing.bundle_location:
            raise OmnigentError(
                f"You already have an agent named {name!r}. Select your agent or rename "
                "the source before copying it.",
                code=ErrorCode.CONFLICT,
            )
        if current_location != existing.bundle_location or not artifact_store.exists(
            current_location
        ):
            artifact_store.put(current_location, bundle_bytes)
        if not replace_existing:
            return existing
        if agent_store.supports_config_edit:
            updated = agent_store.update_configuration(
                existing,
                current_location,
                description=(
                    None
                    if current_location == existing.bundle_location
                    and existing.description is None
                    else description
                ),
            )
        elif current_location == existing.bundle_location:
            return existing
        else:
            updated = agent_store.update(agent_id, current_location)
        if updated is not None:
            return updated
        existing = agent_store.get(agent_id)
        if existing is None:
            raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
    raise OmnigentError("Agent changed concurrently; retry the upload", code=ErrorCode.CONFLICT)


def uploaded_agent_for(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    *,
    owner: str | None,
    spec: AgentSpec,
    bundle_bytes: bytes,
) -> Agent | None:
    """Create or update the uploader's named agent, including child uploads."""
    if spec.name is None or not agent_store.supports_user_agents:
        return None
    return save_user_agent(
        agent_store,
        artifact_store,
        owner=owner,
        name=spec.name,
        bundle_bytes=bundle_bytes,
        description=spec.description,
    )
