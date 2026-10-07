"""Pytest version-gate fixtures shared by UI tests and infrastructure checks."""

import os

import pytest
from packaging.version import InvalidVersion, Version

from tests._helpers.compat import (
    COMPAT_SERVER_VERSION_ENV,
    meets_min_server_version,
    resolve_server_version,
)


@pytest.fixture(scope="session")
def server_version(live_server: str) -> str:
    """Read the live version once and cross-check any pinned compat server."""
    return resolve_server_version(live_server)


@pytest.fixture(autouse=True)
def _enforce_min_server_version(request: pytest.FixtureRequest) -> None:
    """Skip unsupported features without hiding a shadowed compat server."""
    marker = request.node.get_closest_marker("min_server_version")
    if marker is None and not os.environ.get(COMPAT_SERVER_VERSION_ENV):
        return

    required = None
    if marker is not None:
        if len(marker.args) != 1 or marker.kwargs or not isinstance(marker.args[0], str):
            raise pytest.UsageError("min_server_version marker requires one version string")
        required = marker.args[0]
        try:
            Version(required)
        except InvalidVersion as exc:
            raise pytest.UsageError(f"invalid min_server_version marker: {required!r}") from exc

    # Resolve even unmarked compat tests so a worktree-shadowed server fails.
    server_ver = request.getfixturevalue("server_version")
    if required is not None and not meets_min_server_version(server_ver, required):
        pytest.skip(f"requires server >= {required}; running {server_ver}")
