"""Tests for HostProcess._handle_clone_and_bundle."""

from __future__ import annotations

import base64
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from omnigent.git_source import clone_and_bundle as real_clone_and_bundle
from omnigent.host.connect import (
    _HOST_CLONE_MAX_BUNDLE_BYTES,
    HostProcess,
)
from omnigent.host.frames import HostCloneAndBundleFrame
from omnigent.host.identity import HostIdentity

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────


def _make_host_process() -> HostProcess:
    """Instantiate a HostProcess with a minimal test identity."""
    identity = HostIdentity(
        host_id="host_test_clone",
        name="test-laptop",
    )
    return HostProcess(identity=identity, server_url="http://localhost:8000")


def _make_local_repo(tmp_path: Path, files: dict[str, str], branch: str = "main") -> str:
    """Create a bare-ish local git repo and return its path as a string.

    clone_and_bundle is called via monkeypatch with _allow_local=True, so
    the URL guard doesn't reject the local path.
    """
    repo = tmp_path / "origin"
    repo.mkdir()

    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    run("init", "-b", branch)
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "t")
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run("add", "-A")
    run("commit", "-m", "init")
    return str(repo)


def _tar_entries(bundle_b64: str) -> dict[str, str]:
    """Decode a base64 bundle and return {member_name: content} for files."""
    raw = base64.b64decode(bundle_b64)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        return {m.name: tf.extractfile(m).read().decode() for m in tf.getmembers() if m.isfile()}


_VALID_CONFIG = (
    "spec_version: 1\nname: git-agent\n"
    "executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
)


# ── Tests ────────────────────────────────────────────────


async def test_handle_clone_and_bundle_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: status ok, bundle decodes to tar containing config.yaml,
    commit_sha is 40 hex chars, resolved_ref matches the branch.
    """
    repo_path = _make_local_repo(tmp_path, {"config.yaml": _VALID_CONFIG})

    monkeypatch.setattr(
        "omnigent.host.connect.clone_and_bundle",
        lambda url, ref, sub: real_clone_and_bundle(url, ref, sub, _allow_local=True),
    )

    host = _make_host_process()
    frame = HostCloneAndBundleFrame(
        request_id="req_clone_1",
        git_url=repo_path,
        git_ref="main",
        git_subpath=None,
    )
    result = await host._handle_clone_and_bundle(frame)

    assert result.request_id == "req_clone_1"
    assert result.status == "ok"
    assert result.error is None
    assert result.bundle_b64 is not None

    entries = _tar_entries(result.bundle_b64)
    assert "config.yaml" in entries
    assert not any(n.startswith(".git/") for n in entries)

    assert result.commit_sha is not None
    assert len(result.commit_sha) == 40
    assert all(c in "0123456789abcdef" for c in result.commit_sha)

    assert result.resolved_ref == "main"


async def test_handle_clone_and_bundle_subpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subpath bundling only includes files under the given subdir."""
    repo_path = _make_local_repo(
        tmp_path,
        {
            "agents/reviewer/config.yaml": _VALID_CONFIG,
            "agents/reviewer/AGENTS.md": "# hi",
            "README.md": "top-level",
        },
    )

    monkeypatch.setattr(
        "omnigent.host.connect.clone_and_bundle",
        lambda url, ref, sub: real_clone_and_bundle(url, ref, sub, _allow_local=True),
    )

    host = _make_host_process()
    frame = HostCloneAndBundleFrame(
        request_id="req_clone_sub",
        git_url=repo_path,
        git_ref="main",
        git_subpath="agents/reviewer",
    )
    result = await host._handle_clone_and_bundle(frame)

    assert result.status == "ok"
    entries = _tar_entries(result.bundle_b64)
    assert set(entries) == {"config.yaml", "AGENTS.md"}


async def test_handle_clone_and_bundle_bad_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected URL (file://) from the URL validator → status failed."""
    # No monkeypatch needed: real clone_and_bundle will reject file:// urls.
    host = _make_host_process()
    frame = HostCloneAndBundleFrame(
        request_id="req_clone_bad",
        git_url="file:///etc/passwd",
        git_ref=None,
        git_subpath=None,
    )
    result = await host._handle_clone_and_bundle(frame)

    assert result.status == "failed"
    assert result.error is not None
    assert result.bundle_b64 is None
    assert result.commit_sha is None


async def test_handle_clone_and_bundle_missing_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requesting a branch that doesn't exist → status failed with error."""
    repo_path = _make_local_repo(tmp_path, {"config.yaml": _VALID_CONFIG})

    monkeypatch.setattr(
        "omnigent.host.connect.clone_and_bundle",
        lambda url, ref, sub: real_clone_and_bundle(url, ref, sub, _allow_local=True),
    )

    host = _make_host_process()
    frame = HostCloneAndBundleFrame(
        request_id="req_clone_nobranch",
        git_url=repo_path,
        git_ref="branch-does-not-exist",
        git_subpath=None,
    )
    result = await host._handle_clone_and_bundle(frame)

    assert result.status == "failed"
    assert result.error is not None
    assert result.bundle_b64 is None


async def test_handle_clone_and_bundle_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bundles that exceed _HOST_CLONE_MAX_BUNDLE_BYTES → status failed with size message."""
    oversized = b"x" * (_HOST_CLONE_MAX_BUNDLE_BYTES + 1)

    monkeypatch.setattr(
        "omnigent.host.connect.clone_and_bundle",
        lambda url, ref, sub: (oversized, "a" * 40, "main"),
    )

    host = _make_host_process()
    frame = HostCloneAndBundleFrame(
        request_id="req_clone_big",
        git_url="https://example.com/repo.git",
        git_ref="main",
        git_subpath=None,
    )
    result = await host._handle_clone_and_bundle(frame)

    assert result.status == "failed"
    assert result.bundle_b64 is None
    assert result.error is not None
    assert "too large" in result.error
    assert str(_HOST_CLONE_MAX_BUNDLE_BYTES) in result.error


async def test_handle_clone_and_bundle_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected non-OmnigentError exception → status failed, no hang."""

    def _boom(url, ref, sub):
        raise RuntimeError("something unexpected exploded")

    monkeypatch.setattr("omnigent.host.connect.clone_and_bundle", _boom)

    host = _make_host_process()
    frame = HostCloneAndBundleFrame(
        request_id="req_clone_exc",
        git_url="https://example.com/repo.git",
        git_ref=None,
        git_subpath=None,
    )
    result = await host._handle_clone_and_bundle(frame)

    assert result.status == "failed"
    assert result.bundle_b64 is None
    assert result.error is not None
    assert "internal error" in result.error
