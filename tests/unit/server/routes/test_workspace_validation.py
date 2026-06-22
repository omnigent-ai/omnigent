"""
Tests for ``omnigent.server.routes._workspace_validation``.

Drives the validator with a fake host that auto-replies to
``host.stat`` frames with controlled outcomes — covers each of the
seven validation steps from
``designs/SESSION_WORKSPACE_SELECTION.md`` without spinning up a
live host process.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.host.frames import (
    HostHelloFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    validate_workspace,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_ws_test"


class _FakeWebSocket:
    """
    Minimal stand-in for a WebSocket connection.

    The validator only calls ``send_text`` (via the registry); the
    fake captures sent frames so we can pull out the ``request_id``
    and reply via the registry's ``pending_stats`` future.
    """

    def __init__(self) -> None:
        """Initialize with an empty outbound capture."""
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        """Capture an outbound frame.

        :param data: JSON-encoded frame text.
        """
        self.sent.append(data)


def _hello_frame() -> HostHelloFrame:
    """Construct a host hello frame for registry registration.

    :returns: Hello frame with default version + empty runners.
    """
    return HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="ws-test-host",
    )


@pytest.fixture()
async def host_setup() -> AsyncIterator[tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]]]:
    """
    Build a registered host plus a background task that auto-replies
    to ``host.stat`` frames.

    Tests drive replies by setting ``stat_replies`` (a
    ``dict[path, dict]``) before the validator runs. The auto-replier
    consumes the host's outbound queue and pushes the matching reply
    via the registry's pending_stats future, mimicking what
    ``host_tunnel.py``'s receive loop does in production.

    :returns: Async iterator yielding (registry, ws, drain_task).
    """
    registry = HostRegistry()
    ws = _FakeWebSocket()
    conn = registry.register(
        host_id=_HOST_ID,
        ws=ws,  # type: ignore[arg-type] — duck-typed
        hello=_hello_frame(),
        owner=None,
    )

    # Per-test reply table; mutate from the test before calling
    # validate_workspace.
    stat_replies: dict[str, dict[str, Any]] = {}

    async def _drain() -> None:
        """Consume frames from the host's outbound queue and reply.

        Mimics the host tunnel route's receive loop: read each
        outbound frame, find the corresponding pending_stats
        future, and resolve it with the configured fake reply.
        """
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if not isinstance(frame, HostStatFrame):
                continue
            reply = stat_replies.get(frame.path)
            if reply is None:
                # Default: path doesn't exist. Tests must register
                # a reply for every path they expect the validator
                # to stat, otherwise this default surfaces.
                reply = {
                    "status": "ok",
                    "exists": False,
                    "type": None,
                    "canonical_path": None,
                    "error": None,
                }
            future = conn.pending_stats.pop(frame.request_id, None)
            if future is not None and not future.done():
                future.set_result(reply)

    drain_task = asyncio.create_task(_drain())
    # Stash the reply table on the registry so tests can mutate it
    # without further plumbing.
    registry._stat_replies_for_test = stat_replies  # type: ignore[attr-defined]

    try:
        yield registry, ws, drain_task
    finally:
        # Poison the queue so the drain task exits cleanly.
        conn.outbound_queue.put_nowait(None)
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


def _set_stat(
    registry: HostRegistry,
    path: str,
    *,
    exists: bool = True,
    type_: str | None = "directory",
    canonical: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    """
    Register a fake stat reply for the given path.

    :param registry: Registry returned by the ``host_setup`` fixture.
    :param path: Input path the validator will send (matches the
        ``HostStatFrame.path`` field exactly — the validator never
        rewrites paths before sending).
    :param exists: Value for the reply's ``exists`` field.
    :param type_: Value for ``type``. Defaults to ``"directory"``.
    :param canonical: Value for ``canonical_path``. ``None`` falls
        back to ``path`` itself when ``exists`` is ``True``.
    :param status: ``"ok"`` (default) or ``"failed"``.
    :param error: Error message when ``status == "failed"``.
    """
    if exists and canonical is None:
        canonical = path
    registry._stat_replies_for_test[path] = {  # type: ignore[attr-defined]
        "status": status,
        "exists": exists,
        "type": type_ if exists else None,
        "canonical_path": canonical if exists else None,
        "error": error,
    }


# ── Step 0: host online check ────────────────────────────


async def test_offline_host_rejected() -> None:
    """
    Verify validation fails fast when the host isn't in the registry.

    The check runs before any stat call so an offline host can't
    produce a hung session-create. If this test fails, callers can
    create sessions that will never be launchable.
    """
    registry = HostRegistry()  # nothing registered

    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id="host_does_not_exist",
            workspace="/Users/corey/foo",
            spec_cwd=".",
        )
    assert "is offline" in exc_info.value.message


# ── Step 4: workspace stat ──────────────────────────────


async def test_workspace_must_exist(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a non-existent workspace path is rejected with a clear error.

    Without this check, the host's launch would later fail with the
    same root cause but at a worse moment (post-create, on first
    message). The session-create rejection moves the failure to a
    UI surface where the user can correct it.
    """
    registry, _, _ = host_setup
    # No stat reply registered → fixture's default returns exists:false.
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/Users/corey/missing",
            spec_cwd=".",
        )
    assert "does not exist" in exc_info.value.message


async def test_workspace_must_be_directory(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a workspace that exists but is a regular file is rejected.

    The runner ``cd``s into the workspace; an ``os.chdir`` on a
    file would surface a confusing OSError on first launch. Better
    to reject up front.
    """
    registry, _, _ = host_setup
    _set_stat(registry, "/Users/corey/README.md", type_="file")
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/Users/corey/README.md",
            spec_cwd=".",
        )
    assert "not a directory" in exc_info.value.message


async def test_relative_cwd_skips_boundary_check(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a relative ``os_env.cwd`` (``"."``) imposes no boundary.

    The validator must accept any existing workspace directory when
    the agent's cwd is relative — the picker UI is unrestricted in
    this case (designs/SESSION_WORKSPACE_SELECTION.md "Three path
    types, one picker"). A boundary check on relative cwds would
    reject every legitimate pick.
    """
    registry, _, _ = host_setup
    _set_stat(registry, "/tmp/scratch", canonical="/tmp/scratch")

    canonical = await validate_workspace(
        host_registry=registry,
        host_id=_HOST_ID,
        workspace="/tmp/scratch",
        spec_cwd=".",
    )
    assert canonical == "/tmp/scratch"


# ── Steps 2/3/5: boundary check ─────────────────────────


async def test_absolute_cwd_boundary_must_exist(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a missing agent boundary path is rejected.

    When the agent declares ``cwd: ~/universe`` and ``~/universe``
    isn't on this host, the user has no valid pick. Surfacing
    this at session-create lets the UI suggest "pick a different
    host" rather than failing on first launch.
    """
    registry, _, _ = host_setup
    _set_stat(registry, "/Users/corey/foo", canonical="/Users/corey/foo")
    # Boundary path NOT registered → exists:false.
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/Users/corey/foo",
            spec_cwd="~/universe",
        )
    assert "agent requires path" in exc_info.value.message
    assert "~/universe" in exc_info.value.message


async def test_workspace_outside_boundary_rejected(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a workspace outside the agent's boundary is rejected.

    User picks ``/tmp/scratch`` for an agent declaring
    ``cwd: /Users/corey/universe`` — runtime would let the agent
    operate outside its declared scope. Boundary check on
    canonicalized paths prevents this.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/tmp/scratch",
        canonical="/tmp/scratch",
    )
    _set_stat(
        registry,
        "/Users/corey/universe",
        canonical="/Users/corey/universe",
    )
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/tmp/scratch",
            spec_cwd="/Users/corey/universe",
        )
    assert "outside the agent's required path" in exc_info.value.message


async def test_workspace_inside_boundary_accepted(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a workspace inside the boundary returns the canonical path.

    Pairs with the rejection test above to pin both directions of
    the contract: outside → reject, inside → accept-with-canonical.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/Users/corey/universe/src/foo",
        canonical="/Users/corey/universe/src/foo",
    )
    _set_stat(
        registry,
        "/Users/corey/universe",
        canonical="/Users/corey/universe",
    )

    canonical = await validate_workspace(
        host_registry=registry,
        host_id=_HOST_ID,
        workspace="/Users/corey/universe/src/foo",
        spec_cwd="/Users/corey/universe",
    )
    assert canonical == "/Users/corey/universe/src/foo"


async def test_symlink_escape_rejected_via_canonical_paths(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a symlink that points outside the boundary is caught.

    Setup: agent boundary ``/Users/corey/foo``; user picks
    ``/Users/corey/foo/link``; the symlink resolves to ``/etc``.
    The host returns ``canonical_path: "/etc"`` for the workspace
    stat. Boundary check operates on canonicals, so ``/etc`` ⊄
    ``/Users/corey/foo`` → reject.

    Without this guarantee, a user could "smuggle" a workspace
    out of the boundary via a symlink — the agent would end up
    cd'd into a directory it wasn't supposed to operate in.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/Users/corey/foo/link",
        canonical="/etc",  # symlink target — escapes the boundary
    )
    _set_stat(
        registry,
        "/Users/corey/foo",
        canonical="/Users/corey/foo",
    )
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/Users/corey/foo/link",
            spec_cwd="/Users/corey/foo",
        )
    # Error message references the user's input path, not the
    # canonical — the user picked /link and that's what makes
    # sense to them.
    assert "/Users/corey/foo/link" in exc_info.value.message
    assert "outside the agent's required path" in exc_info.value.message


async def test_symlink_inside_boundary_accepted(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a symlink that resolves inside the boundary is accepted,
    with the canonical path returned.

    Pairs with the symlink-escape test: when the link target is
    inside the boundary, the canonical (target) is what gets
    stored — not the symlink path.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/Users/corey/foo/link",
        canonical="/Users/corey/foo/sub",  # inside the boundary
    )
    _set_stat(
        registry,
        "/Users/corey/foo",
        canonical="/Users/corey/foo",
    )

    canonical = await validate_workspace(
        host_registry=registry,
        host_id=_HOST_ID,
        workspace="/Users/corey/foo/link",
        spec_cwd="/Users/corey/foo",
    )
    # The stored value is the realpath the host returned, not the
    # symlinked input — see designs/... "Why this is better".
    assert canonical == "/Users/corey/foo/sub"


# ── Step 6: ./subdir presence ───────────────────────────


async def test_subdir_must_exist_under_workspace(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify ``cwd: ./subdir`` requires the subdir to exist under the picked workspace.

    Without this check, the agent's `os.chdir(spec_cwd)` would
    fail at first launch, surfacing a confusing error far from
    the session-create form.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/Users/corey/projects",
        canonical="/Users/corey/projects",
    )
    # Subdir not registered → exists:false.
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/Users/corey/projects",
            spec_cwd="./config",
        )
    assert "config" in exc_info.value.message
    assert "not present" in exc_info.value.message


async def test_subdir_present_accepted(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify ``cwd: ./subdir`` accepts a workspace that contains ``subdir``.

    The validator stats both the workspace and the joined path;
    only the workspace canonical_path is returned.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/Users/corey/projects",
        canonical="/Users/corey/projects",
    )
    _set_stat(
        registry,
        "/Users/corey/projects/config",
        canonical="/Users/corey/projects/config",
    )

    canonical = await validate_workspace(
        host_registry=registry,
        host_id=_HOST_ID,
        workspace="/Users/corey/projects",
        spec_cwd="./config",
    )
    assert canonical == "/Users/corey/projects"


# ── Input shape ────────────────────────────────────────


async def test_relative_workspace_rejected(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a relative workspace path is rejected up front.

    Defense-in-depth: the Pydantic schema layer should also reject
    this, but if a test or internal caller bypasses the schema,
    this guard catches them.
    """
    registry, _, _ = host_setup
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="some/relative",
            spec_cwd=".",
        )
    assert "absolute path" in exc_info.value.message


async def test_tilde_workspace_rejected(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a tilde-prefixed workspace is rejected.

    The server doesn't resolve ``~`` (only the host does, via
    host.stat). Allowing a tilde here would mean we'd ship it
    through to host.stat and store the host-resolved canonical
    path — but the absolute-path requirement is also a hard input
    contract from the API spec.
    """
    registry, _, _ = host_setup
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="~/projects",
            spec_cwd=".",
        )
    assert "absolute path" in exc_info.value.message


# ── Host failure handling ────────────────────────────────


async def test_host_stat_failure_surfaced(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify ``status: "failed"`` from host.stat is surfaced as an
    error rather than treated as success.

    A silent treat-failure-as-success would land sessions in the
    DB with no workspace — they'd fail on every launch.
    """
    registry, _, _ = host_setup
    registry._stat_replies_for_test["/Users/corey/foo"] = {  # type: ignore[attr-defined]
        "status": "failed",
        "exists": False,
        "type": None,
        "canonical_path": None,
        "error": "I/O error reading filesystem",
    }
    with pytest.raises(WorkspaceValidationError) as exc_info:
        await validate_workspace(
            host_registry=registry,
            host_id=_HOST_ID,
            workspace="/Users/corey/foo",
            spec_cwd=".",
        )
    assert "I/O error" in exc_info.value.message


# ── Tilde-prefixed boundary ──────────────────────────────


async def test_tilde_boundary_passed_through_to_host(
    host_setup: tuple[HostRegistry, _FakeWebSocket, asyncio.Task[None]],
) -> None:
    """
    Verify a ``~/foo`` boundary is sent verbatim to the host.

    Per the design: the host owns ``~`` resolution,
    not the server. The validator's stat call must therefore
    pass tildes through unmodified. The host returns the
    expanded canonical_path, which the validator then uses for
    the boundary-subset check.
    """
    registry, _, _ = host_setup
    _set_stat(
        registry,
        "/Users/corey/universe/src/foo",
        canonical="/Users/corey/universe/src/foo",
    )
    _set_stat(
        registry,
        "~/universe",  # validator sends this verbatim
        canonical="/Users/corey/universe",  # host expands it
    )

    canonical = await validate_workspace(
        host_registry=registry,
        host_id=_HOST_ID,
        workspace="/Users/corey/universe/src/foo",
        spec_cwd="~/universe",
    )
    assert canonical == "/Users/corey/universe/src/foo"
