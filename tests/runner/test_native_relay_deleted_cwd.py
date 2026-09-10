"""The native relay tool surface survives a deleted working directory.

A long-lived runner can outlive the directory it was started in — the user
deletes the worktree the host was launched from. ``build_native_relay_tool_
schemas`` sits on both the comment-relay pre-start and turn-setup paths, so an
unhandled ``FileNotFoundError`` from reading the vanished cwd kills relay
pre-start ("Failed to pre-start comment relay") and the next turn ("turn setup
failed: [Errno 2] ..."). The schema build must not depend on the process cwd
existing, and the surface it relays — including the ``sys_os_*`` tools, whose
schema-extraction environment used to be rooted at ``Path.cwd()`` — must be
identical with and without a live cwd.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="a removed cwd makes os.getcwd() raise on Linux; other platforms may serve stale paths",
)

_OS_TOOLS = {"sys_os_read", "sys_os_write", "sys_os_edit", "sys_os_shell"}


def _native_spec() -> AgentSpec:
    """A resolved native-harness spec, as the runner hands to the relay build.

    :returns: A minimal non-``None`` native-harness :class:`AgentSpec`.
    """
    return AgentSpec(
        spec_version=1,
        name="deleted-cwd-relay-probe",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )


def test_relay_surface_is_identical_after_cwd_is_deleted(tmp_path: Path) -> None:
    """Deleting the process cwd must not shrink (or crash) the relay surface.

    The ``sys_os_*`` schemas are relayed unconditionally to override
    harness-static versions and centralize policy enforcement, so they must
    stay present even when the cwd is gone — the schema-extraction environment
    only needs *a* directory, not the launch directory.
    """
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    original_cwd = os.getcwd()
    spec = _native_spec()
    try:
        os.chdir(doomed)
        live_names = {s["name"] for s in build_native_relay_tool_schemas(spec)}

        doomed.rmdir()
        gone_names = {s["name"] for s in build_native_relay_tool_schemas(spec)}
    finally:
        os.chdir(original_cwd)

    assert live_names >= _OS_TOOLS, "precondition: sys_os_* ride the relay with a live cwd"
    assert gone_names == live_names, (
        "the native relay surface must not change when the process cwd is "
        f"deleted; lost {sorted(live_names - gone_names)}, "
        f"gained {sorted(gone_names - live_names)}"
    )
