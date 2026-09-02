"""Tests for workspace validation pure helpers.

The async ``validate_workspace`` function requires a live host
connection, so we test only the synchronous helpers here.
"""

from __future__ import annotations

from omnigent.server.routes._workspace_validation import (
    _is_relative_cwd,
    _is_subpath_of,
    restore_host_filesystem_url_path,
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

    def test_windows_backslash_child(self) -> None:
        assert _is_subpath_of("C:\\a\\b", "C:\\a") is True

    def test_windows_drive_case_insensitive(self) -> None:
        assert _is_subpath_of("C:\\Users\\me\\work", "c:\\Users\\me") is True

    def test_windows_prefix_collision(self) -> None:
        assert _is_subpath_of("C:\\a\\foo", "C:\\a\\fo") is False

    def test_posix_backslash_is_not_a_separator(self) -> None:
        """A POSIX filename may contain a backslash; it is not a separator."""
        assert _is_subpath_of("/allowed\\escape/project", "/allowed") is False

    def test_windows_mixed_separators(self) -> None:
        assert _is_subpath_of("C:\\Users\\alice\\work", "C:/Users/alice") is True


class TestRestoreHostFilesystemUrlPath:
    """FastAPI :path capture restoration for POSIX vs Windows paths."""

    def test_posix_stripped_slash_is_restored(self) -> None:
        assert restore_host_filesystem_url_path("Users/corey/proj") == "/Users/corey/proj"

    def test_posix_already_absolute(self) -> None:
        assert restore_host_filesystem_url_path("/Users/corey/proj") == "/Users/corey/proj"

    def test_tilde_is_unchanged(self) -> None:
        assert restore_host_filesystem_url_path("~/proj") == "~/proj"

    def test_windows_drive_forward_slash_is_not_prefixed(self) -> None:
        assert restore_host_filesystem_url_path("C:/Users/alice/work") == "C:/Users/alice/work"

    def test_windows_drive_backslash_is_not_prefixed(self) -> None:
        assert restore_host_filesystem_url_path(r"C:\Users\alice\work") == r"C:\Users\alice\work"

    def test_unc_is_not_prefixed(self) -> None:
        assert restore_host_filesystem_url_path("//server/share/proj") == "//server/share/proj"
