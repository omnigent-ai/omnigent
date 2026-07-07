"""Tests for workspace validation pure helpers.

The async ``validate_workspace`` function requires a live host
connection, so we test only the synchronous helpers here.
"""

from __future__ import annotations

from omnigent.server.routes._host_paths import (
    is_host_absolute_path,
    normalize_host_filesystem_route_path,
)
from omnigent.server.routes._workspace_validation import (
    _is_relative_cwd,
    _is_subpath_of,
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

    def test_windows_drive_absolute_is_not_relative(self) -> None:
        assert _is_relative_cwd("C:/Users/alice/project") is False

    def test_tilde_is_not_relative(self) -> None:
        assert _is_relative_cwd("~/project") is False


class TestHostAbsolutePath:
    """Tests for host absolute-path classification across OS families."""

    def test_posix_absolute(self) -> None:
        assert is_host_absolute_path("/Users/alice/project") is True

    def test_windows_drive_forward_slashes(self) -> None:
        assert is_host_absolute_path("C:/Users/alice/project") is True

    def test_windows_drive_backslashes(self) -> None:
        assert is_host_absolute_path("C:\\Users\\alice\\project") is True

    def test_relative_path(self) -> None:
        assert is_host_absolute_path("Users/alice/project") is False


class TestNormalizeHostFilesystemRoutePath:
    """Tests for FastAPI path captures sent to host filesystem browsing."""

    def test_posix_capture_gets_leading_slash(self) -> None:
        assert normalize_host_filesystem_route_path("Users/alice/project") == (
            "/Users/alice/project"
        )

    def test_windows_drive_forward_slashes_is_preserved(self) -> None:
        assert normalize_host_filesystem_route_path("C:/Users/alice/project") == (
            "C:/Users/alice/project"
        )

    def test_windows_drive_backslashes_is_preserved(self) -> None:
        assert normalize_host_filesystem_route_path("C:\\Users\\alice\\project") == (
            "C:\\Users\\alice\\project"
        )

    def test_tilde_is_preserved(self) -> None:
        assert normalize_host_filesystem_route_path("~/project") == "~/project"


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

    def test_windows_child_path_forward_slashes(self) -> None:
        assert _is_subpath_of("C:/Users/alice/project/src", "C:/Users/alice/project") is True

    def test_windows_child_path_backslashes(self) -> None:
        assert (
            _is_subpath_of(
                "C:\\Users\\alice\\project\\src",
                "C:\\Users\\alice\\project",
            )
            is True
        )

    def test_windows_case_insensitive(self) -> None:
        assert (
            _is_subpath_of(
                "c:\\users\\alice\\project\\src",
                "C:\\Users\\Alice\\Project",
            )
            is True
        )

    def test_windows_prefix_collision(self) -> None:
        assert (
            _is_subpath_of(
                "C:\\Users\\alice\\project-other",
                "C:\\Users\\alice\\project",
            )
            is False
        )
