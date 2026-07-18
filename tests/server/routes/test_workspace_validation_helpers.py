"""Tests for workspace validation pure helpers.

The async ``validate_workspace`` function requires a live host
connection, so we test only the synchronous helpers here.
"""

from __future__ import annotations

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

    # --- Windows path flavour (native-Windows host) ---

    def test_windows_child_path(self) -> None:
        assert _is_subpath_of(r"C:\repo\child", r"C:\repo") is True

    def test_windows_not_a_subpath(self) -> None:
        assert _is_subpath_of(r"C:\repo", r"C:\other") is False

    def test_windows_case_insensitive(self) -> None:
        assert _is_subpath_of(r"c:\repo\child", r"C:\Repo") is True

    def test_windows_mixed_separators(self) -> None:
        assert _is_subpath_of("C:/repo/child", r"C:\repo") is True

    def test_windows_prefix_collision(self) -> None:
        """``C:\\repo2`` must NOT be treated as a subpath of ``C:\\repo``."""
        assert _is_subpath_of(r"C:\repo2\x", r"C:\repo") is False

    def test_windows_unc_child(self) -> None:
        assert _is_subpath_of(r"\\srv\share\proj\src", r"\\srv\share\proj") is True


class TestIsAbsoluteHostPath:
    """Tests for the POSIX-or-Windows absolute-path gate."""

    def test_posix_absolute(self) -> None:
        assert is_absolute_host_path("/Users/alice/project") is True

    def test_windows_drive_backslash(self) -> None:
        assert is_absolute_host_path(r"C:\Users\alice") is True

    def test_windows_drive_forward_slash(self) -> None:
        assert is_absolute_host_path("C:/Users/alice") is True

    def test_unc_path(self) -> None:
        assert is_absolute_host_path(r"\\server\share") is True

    def test_relative_rejected(self) -> None:
        assert is_absolute_host_path("project/src") is False

    def test_dot_relative_rejected(self) -> None:
        assert is_absolute_host_path("./src") is False

    def test_bare_name_rejected(self) -> None:
        assert is_absolute_host_path("project") is False

    def test_empty_rejected(self) -> None:
        assert is_absolute_host_path("") is False
