"""Tests for the OS environment fork (copy-on-write) mode."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import (
    _copy_tree,
    create_os_environment,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Unit tests for copy tree helper
# ---------------------------------------------------------------------------


class TestCopyTree(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = Path(self.tmpdir) / "src"
        self.dst = Path(self.tmpdir) / "dst"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_files_are_copied(self):
        self.src.mkdir()
        (self.src / "a.txt").write_text("aaa")
        (self.src / "b.txt").write_text("bbb")

        _copy_tree(self.src, self.dst)
        self.assertEqual((self.dst / "a.txt").read_text(), "aaa")
        self.assertEqual((self.dst / "b.txt").read_text(), "bbb")

        # Different inodes (independent copies)
        self.assertNotEqual(
            (self.src / "a.txt").stat().st_ino,
            (self.dst / "a.txt").stat().st_ino,
        )

    def test_subdirectories_are_recreated(self):
        (self.src / "sub" / "deep").mkdir(parents=True)
        (self.src / "sub" / "deep" / "file.txt").write_text("deep content")

        _copy_tree(self.src, self.dst)
        self.assertEqual((self.dst / "sub" / "deep" / "file.txt").read_text(), "deep content")

    def test_symlinks_are_copied_as_symlinks(self):
        self.src.mkdir()
        (self.src / "target.txt").write_text("target")
        (self.src / "link.txt").symlink_to("target.txt")

        _copy_tree(self.src, self.dst)
        self.assertTrue((self.dst / "link.txt").is_symlink())
        self.assertEqual(os.readlink(str(self.dst / "link.txt")), "target.txt")

    def test_empty_directory(self):
        self.src.mkdir()
        _copy_tree(self.src, self.dst)
        self.assertTrue(self.dst.is_dir())

    def test_writes_to_copy_dont_affect_original(self):
        self.src.mkdir()
        (self.src / "file.txt").write_text("original")
        _copy_tree(self.src, self.dst)

        (self.dst / "file.txt").write_text("modified")
        self.assertEqual((self.src / "file.txt").read_text(), "original")
        self.assertEqual((self.dst / "file.txt").read_text(), "modified")


# ---------------------------------------------------------------------------
# Integration tests: forked CallerProcessOSEnvironment
# ---------------------------------------------------------------------------


class TestForkedOSEnvironment(unittest.TestCase):
    """Test the fork mode end-to-end through the helper process."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = Path(self.tmpdir) / "project"
        self.src.mkdir()
        (self.src / "readme.txt").write_text("original readme\n")
        (self.src / "data.txt").write_text("original data\n")
        (self.src / "sub").mkdir()
        (self.src / "sub" / "nested.txt").write_text("nested content\n")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_env(self):
        spec = OSEnvSpec(
            type="caller_process",
            cwd=str(self.src),
            fork=True,
            sandbox=OSEnvSandboxSpec(type="none"),
        )
        return create_os_environment(spec)

    def test_read_sees_original_content(self):
        env = self._make_env()
        try:
            result = _run(env.read("readme.txt"))
            self.assertIn("original readme", result["content"])
        finally:
            env.close()

    def test_write_does_not_modify_original(self):
        env = self._make_env()
        try:
            _run(env.write("readme.txt", "modified readme\n"))

            # Forked env sees the modification
            result = _run(env.read("readme.txt"))
            self.assertIn("modified readme", result["content"])

            # Original is untouched
            self.assertEqual((self.src / "readme.txt").read_text(), "original readme\n")
        finally:
            env.close()

    def test_edit_does_not_modify_original(self):
        env = self._make_env()
        try:
            _run(env.edit("data.txt", old_text="original", new_text="edited"))

            # Forked env sees the edit
            result = _run(env.read("data.txt"))
            self.assertIn("edited data", result["content"])

            # Original is untouched
            self.assertEqual((self.src / "data.txt").read_text(), "original data\n")
        finally:
            env.close()

    def test_write_new_file_does_not_appear_in_original(self):
        env = self._make_env()
        try:
            _run(env.write("new_file.txt", "brand new\n"))
            result = _run(env.read("new_file.txt"))
            self.assertIn("brand new", result["content"])

            # Original directory does not have the new file
            self.assertFalse((self.src / "new_file.txt").exists())
        finally:
            env.close()

    def test_shell_does_not_modify_original(self):
        env = self._make_env()
        try:
            # Shell write via redirect
            _run(env.shell("echo 'shell wrote this' > readme.txt"))

            # Forked env sees it
            result = _run(env.read("readme.txt"))
            self.assertIn("shell wrote this", result["content"])

            # Original is untouched (hardlinks were broken before shell ran)
            self.assertEqual((self.src / "readme.txt").read_text(), "original readme\n")
        finally:
            env.close()

    def test_nested_file_isolation(self):
        env = self._make_env()
        try:
            _run(env.write("sub/nested.txt", "modified nested\n"))

            result = _run(env.read("sub/nested.txt"))
            self.assertIn("modified nested", result["content"])

            self.assertEqual(
                (self.src / "sub" / "nested.txt").read_text(),
                "nested content\n",
            )
        finally:
            env.close()

    def test_cleanup_removes_fork_dir(self):
        env = self._make_env()
        fork_dir = env._fork_dir
        self.assertIsNotNone(fork_dir)
        self.assertTrue(fork_dir.exists())

        env.close()
        self.assertFalse(fork_dir.exists())

    def test_multiple_writes_to_same_file(self):
        env = self._make_env()
        try:
            _run(env.write("readme.txt", "first write\n"))
            _run(env.write("readme.txt", "second write\n"))

            result = _run(env.read("readme.txt"))
            self.assertIn("second write", result["content"])

            self.assertEqual((self.src / "readme.txt").read_text(), "original readme\n")
        finally:
            env.close()

    def test_shell_absolute_path_reads_fork_not_original(self):
        """Shell commands using relative paths operate on the fork tree."""
        env = self._make_env()
        try:
            # Write a marker into the fork via the helper
            _run(env.write("marker.txt", "fork marker\n"))
            # Shell reads it
            result = _run(env.shell("cat marker.txt"))
            self.assertIn("fork marker", result["stdout"])
            # Original should not have marker.txt
            self.assertFalse((self.src / "marker.txt").exists())
        finally:
            env.close()

    def test_edit_after_write_in_fork(self):
        env = self._make_env()
        try:
            _run(env.write("data.txt", "line one\nline two\n"))
            _run(env.edit("data.txt", old_text="line two", new_text="line TWO"))

            result = _run(env.read("data.txt"))
            self.assertIn("line TWO", result["content"])

            self.assertEqual((self.src / "data.txt").read_text(), "original data\n")
        finally:
            env.close()


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


class TestForkYAMLLoading(unittest.TestCase):
    def test_loader_parses_fork_field(self):
        from omnigent.inner.loader import load_agent_def

        agent = load_agent_def(
            {
                "name": "test",
                "os_env": {
                    "type": "caller_process",
                    "fork": True,
                },
            }
        )
        self.assertTrue(agent.os_env.fork)

    def test_loader_default_fork_false(self):
        from omnigent.inner.loader import load_agent_def

        agent = load_agent_def(
            {
                "name": "test",
                "os_env": {
                    "type": "caller_process",
                },
            }
        )
        self.assertFalse(agent.os_env.fork)


if __name__ == "__main__":
    unittest.main()
