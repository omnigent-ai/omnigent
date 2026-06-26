"""Shared helpers for uploaded agent bundles."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec import AgentSpec, ExtractionError, load


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
        registered policy handler, before the inner loader
        can resolve and call it. Callers pass ``False`` only for a
        trusted single-user/local server, where ``omnigent run`` uploads
        the operator's own bundle through this same path and custom
        handlers must keep working (the operator already has code
        execution, so the restriction would add no security). See the
        call sites in ``omnigent/server/routes/sessions.py``, which gate
        this on :func:`omnigent.server.auth.local_single_user_enabled`.
    :returns: The validated :class:`AgentSpec`.
    :raises OmnigentError: If the bundle is invalid, the spec is
        missing a name, or (when *enforce_handler_allowlist*) a policy
        names an unregistered handler or ``os_env.cwd`` is an absolute
        or escaping path.
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

    # Untrusted uploads may not pin an absolute or escaping os_env.cwd
    # (GHSA-p8rw-8qj3-hf33). This is gated on the same trust signal as the
    # handler allowlist: a trusted single-user/local server
    # (enforce_handler_allowlist=False) uploads the operator's OWN bundle, and
    # the operator legitimately controls cwd — matching the documented
    # absolute-cwd behavior for direct/local runs in
    # designs/SESSION_WORKSPACE_SELECTION.md.
    if enforce_handler_allowlist:
        os_env = getattr(spec, "os_env", None)
        spec_cwd = getattr(os_env, "cwd", None) if os_env is not None else None
        if (
            spec_cwd is not None
            and spec_cwd not in (".", "./")
            and _cwd_escapes_workspace(spec_cwd)
        ):
            raise OmnigentError(
                "agent os_env.cwd must be a relative path within the workspace "
                f"(no absolute paths or '..'); got {spec_cwd!r}",
                code=ErrorCode.INVALID_INPUT,
            )

    return spec


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
