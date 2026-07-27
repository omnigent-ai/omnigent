"""Tests for workspace validation pure helpers.

The async ``validate_workspace`` function requires a live host
connection, so we test only the synchronous helpers here.
"""

from __future__ import annotations

import pytest

from omnigent.server.routes._workspace_validation import (
    _is_relative_cwd,
    _is_subpath_of,
    is_absolute_host_path,
)


class TestIsRelativeCwd:
    """Tests for the spec cwd classification helper."""

    def test_none_is_relative(self) -> None:
        assert _is_relative_cwd(None) is True

    def test_dot_is_relative(self) -> None:
        assert _is_relative_cwd(".") is True

    def test_dot_slash_is_relative(self) -> None:
        assert _is_relative_cwd("./") is True

    def test_empty_is_relative(self) -> None:
        assert _is_relative_cwd("") is True

    def test_dot_slash_subdir_is_relative(self) -> None:
        assert _is_relative_cwd("./src") is True

    def test_absolute_is_not_relative(self) -> None:
        assert _is_relative_cwd("/Users/alice/project") is False

    def test_tilde_is_not_relative(self) -> None:
        assert _is_relative_cwd("~/project") is False


class TestIsSubpathOf:
    """Tests for the canonicalized path containment check."""

    def test_same_path(self) -> None:
        assert _is_subpath_of("/a/b", "/a/b") is True

    def test_child_path(self) -> None:
        assert _is_subpath_of("/a/b/c", "/a/b") is True

    def test_not_a_subpath(self) -> None:
        assert _is_subpath_of("/a/b", "/a/b/c") is False

    def test_prefix_collision(self) -> None:
        """``/a/foo`` must NOT be treated as a subpath of ``/a/fo``."""
        assert _is_subpath_of("/a/foo", "/a/fo") is False

    def test_root_boundary(self) -> None:
        assert _is_subpath_of("/Users/corey/x", "/") is True

    def test_trailing_slash_boundary(self) -> None:
        assert _is_subpath_of("/a/b/c", "/a/b/") is True


class TestIsAbsoluteHostPath:
    """Tests for the cross-platform absolute-path check.

    The server and the connected host can run different OSes, so a
    Windows-style ``C:\\project`` must be recognized as absolute even
    when the server is POSIX (where ``os.path.isabs`` would say no).
    """

    @pytest.mark.parametrize(
        "workspace",
        ["/home/user/project", "C:\\project", "C:/project", "\\\\unc\\share"],
    )
    def test_absolute_paths_accepted(self, workspace: str) -> None:
        assert is_absolute_host_path(workspace) is True

    @pytest.mark.parametrize("workspace", ["some/relative", "~/projects", "project"])
    def test_relative_paths_rejected(self, workspace: str) -> None:
        assert is_absolute_host_path(workspace) is False
