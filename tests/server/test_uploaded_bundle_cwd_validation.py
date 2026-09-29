"""Regression tests for cwd containment on uploaded agent bundles."""

from __future__ import annotations

import io
import json
import tarfile
import unittest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.bundles import validate_agent_bundle

_LOCATIONS = ("root", "terminal", "grandchild", "grandchild_terminal")
_BAD_CWDS = (
    "/etc/passwd",
    "../outside",
    r"C:\Windows",
    r"\\server\share",
    r"..\outside",
)
_VALID_CWDS = (".", "./", "subdir", "nested/subdir", r"nested\subdir")


def _config(name: str) -> dict[str, object]:
    return {
        "spec_version": 1,
        "name": name,
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        "prompt": "hello",
    }


def _os_env(cwd: str) -> dict[str, object]:
    return {
        "type": "caller_process",
        "cwd": cwd,
        "sandbox": {"type": "none"},
    }


def _set_cwd(config: dict[str, object], cwd: str, *, terminal: bool) -> None:
    if terminal:
        config["terminals"] = {
            "shell": {"command": "sh", "os_env": _os_env(cwd)},
        }
    else:
        config["os_env"] = _os_env(cwd)


def _bundle_with_cwd(location: str, cwd: str) -> bytes:
    root = _config("root")
    files = {"config.yaml": root}

    if location in ("root", "terminal"):
        _set_cwd(root, cwd, terminal=location == "terminal")
    else:
        child = _config("child")
        grandchild = _config("grandchild")
        root["tools"] = {"agents": ["child"]}
        child["tools"] = {"agents": ["grandchild"]}
        _set_cwd(
            grandchild,
            cwd,
            terminal=location == "grandchild_terminal",
        )
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


class UploadedBundleCwdValidationTest(unittest.TestCase):
    def test_rejects_escaping_cwd(self) -> None:
        for location in _LOCATIONS:
            for bad_cwd in _BAD_CWDS:
                with self.subTest(location=location, cwd=bad_cwd):
                    with self.assertRaisesRegex(
                        OmnigentError,
                        r"os_env\.cwd must be a relative path within the workspace",
                    ) as raised:
                        validate_agent_bundle(_bundle_with_cwd(location, bad_cwd))
                    self.assertEqual(raised.exception.code, ErrorCode.INVALID_INPUT)

    def test_accepts_contained_cwd(self) -> None:
        for location in _LOCATIONS:
            for valid_cwd in _VALID_CWDS:
                with self.subTest(location=location, cwd=valid_cwd):
                    spec = validate_agent_bundle(_bundle_with_cwd(location, valid_cwd))
                    self.assertEqual(spec.name, "root")

    def test_trusted_local_bundle_allows_absolute_cwd(self) -> None:
        for location in _LOCATIONS:
            with self.subTest(location=location):
                spec = validate_agent_bundle(
                    _bundle_with_cwd(location, "/operator/workspace"),
                    enforce_handler_allowlist=False,
                )
                self.assertEqual(spec.name, "root")


if __name__ == "__main__":
    unittest.main()
