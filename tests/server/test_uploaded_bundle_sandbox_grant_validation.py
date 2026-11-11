"""Tests for containment of os_env.sandbox path grants on uploaded agent bundles."""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.bwrap_sandbox import _resolve_grant
from omnigent.inner.sandbox import resolve_sandbox
from omnigent.server.bundles import validate_agent_bundle

_LOCATIONS = ("root", "terminal", "grandchild", "grandchild_terminal")
_GRANT_FIELDS = ("read_paths", "write_paths", "write_files")
_BAD_GRANTS = (
    "/etc",
    "/root/.ssh",
    "/root/.ssh/authorized_keys",
    "../outside",
    "~",
    "~/.ssh",
    r"C:\Windows",
    r"\\server\share",
    r"..\outside",
)
_VALID_GRANTS = (".", "subdir", "nested/subdir")


def _config(name: str) -> dict[str, object]:
    return {
        "spec_version": 1,
        "name": name,
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        "prompt": "hello",
    }


def _os_env(field: str, grant: object) -> dict[str, object]:
    return {
        "type": "caller_process",
        "cwd": ".",
        "sandbox": {"type": "none", field: [grant]},
    }


def _set_grant(config: dict[str, object], field: str, grant: object, *, terminal: bool) -> None:
    if terminal:
        config["terminals"] = {"shell": {"command": "sh", "os_env": _os_env(field, grant)}}
    else:
        config["os_env"] = _os_env(field, grant)


def _bundle(location: str, field: str, grant: object) -> bytes:
    root = _config("root")
    files = {"config.yaml": root}

    if location in ("root", "terminal"):
        _set_grant(root, field, grant, terminal=location == "terminal")
    else:
        child = _config("child")
        grandchild = _config("grandchild")
        root["tools"] = {"agents": ["child"]}
        child["tools"] = {"agents": ["grandchild"]}
        _set_grant(grandchild, field, grant, terminal=location == "grandchild_terminal")
        files["agents/child/config.yaml"] = child
        files["agents/child/agents/grandchild/config.yaml"] = grandchild

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, config in files.items():
            content = json.dumps(config).encode()
            info = tarfile.TarInfo(path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class UploadedBundleSandboxGrantValidationTest(unittest.TestCase):
    def test_rejects_escaping_grant(self) -> None:
        for location in _LOCATIONS:
            for field in _GRANT_FIELDS:
                for bad in _BAD_GRANTS:
                    with self.subTest(location=location, field=field, grant=bad):
                        with self.assertRaisesRegex(
                            OmnigentError,
                            r"os_env\.sandbox\." + field + r" entries must be relative paths",
                        ) as raised:
                            validate_agent_bundle(_bundle(location, field, bad))
                        self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)

    def test_accepts_contained_grant(self) -> None:
        for location in _LOCATIONS:
            for field in _GRANT_FIELDS:
                for good in _VALID_GRANTS:
                    with self.subTest(location=location, field=field, grant=good):
                        spec = validate_agent_bundle(_bundle(location, field, good))
                        self.assertEqual(spec.name, "root")

    def test_rejects_structured_write_path_grant(self) -> None:
        # write_paths also accepts the {path, copy_on_write} mapping form; the
        # path inside it must be validated like the string shorthand.
        for bad in ("/root/.ssh", "~/.ssh", "../outside"):
            with self.subTest(grant=bad):
                with self.assertRaisesRegex(
                    OmnigentError,
                    r"os_env\.sandbox\.write_paths entries must be relative paths",
                ):
                    validate_agent_bundle(_bundle("root", "write_paths", {"path": bad}))

    def test_direct_trusted_caller_allows_absolute_grant(self) -> None:
        for field in _GRANT_FIELDS:
            with self.subTest(field=field):
                spec = validate_agent_bundle(
                    _bundle("root", field, "/operator/workspace"),
                    enforce_handler_allowlist=False,
                )
                self.assertEqual(spec.name, "root")

    def test_relative_grant_cannot_follow_workspace_symlink_after_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            outside = Path(tempdir) / "outside"
            workspace.mkdir()
            outside.mkdir()

            for field in _GRANT_FIELDS:
                with self.subTest(field=field):
                    spec = validate_agent_bundle(_bundle("root", field, "linked/file"))
                    assert spec.os_env is not None
                    (workspace / "linked").symlink_to(outside, target_is_directory=True)
                    with patch.dict(os.environ, {"OMNIGENT_RUNNER_WORKSPACE": str(workspace)}):
                        with self.assertRaisesRegex(ValueError, "outside its working directory"):
                            resolve_sandbox(spec.os_env, workspace)
                        with self.assertRaisesRegex(ValueError, "outside its working directory"):
                            _resolve_grant(workspace, "linked/file")
                    (workspace / "linked").unlink()

    def test_contained_symlink_and_trusted_absolute_grant_remain_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            target = workspace / "target"
            outside = Path(tempdir) / "outside"
            target.mkdir(parents=True)
            outside.mkdir()
            (workspace / "linked").symlink_to(target, target_is_directory=True)
            with patch.dict(os.environ, {"OMNIGENT_RUNNER_WORKSPACE": str(workspace)}):
                spec = validate_agent_bundle(_bundle("root", "read_paths", "linked/file"))
                assert spec.os_env is not None
                policy = resolve_sandbox(spec.os_env, workspace)
                self.assertEqual(policy.read_roots, [target / "file"])

                trusted = validate_agent_bundle(
                    _bundle("root", "read_paths", str(outside)),
                    enforce_handler_allowlist=False,
                )
                assert trusted.os_env is not None
                self.assertEqual(resolve_sandbox(trusted.os_env, workspace).read_roots, [outside])


if __name__ == "__main__":
    unittest.main()
