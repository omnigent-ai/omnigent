"""Offline coverage of release selection, artifact verification and real Dockerfile edits."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "update_gh_cli", ROOT / ".github/scripts/update_gh_cli.py"
)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    for name in (*updater.DOCKERFILES, ".github/pull_request_template.md"):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((ROOT / name).read_text())
    return tmp_path


def release(version: str = "9.1.0", **fields) -> dict:
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-01T00:00:00Z",
        "assets": [
            {
                "name": f"gh_{version}_linux_{arch}.tar.gz",
                "size": len(arch),
                "digest": f"sha256:{hashlib.sha256(arch.encode()).hexdigest()}",
                "browser_download_url": (
                    f"https://github.com/cli/cli/releases/download/v{version}/"
                    f"gh_{version}_linux_{arch}.tar.gz"
                ),
            }
            for arch in updater.ARCHITECTURES
        ],
        **fields,
    }


@pytest.mark.parametrize(
    "excluded",
    [
        release("9.3.0", draft=True),
        release("9.3.0", prerelease=True),
        release("9.3.0", tag_name="v9.3.0-rc1"),
        release("9.3.0", published_at="2026-10-20T00:00:00Z"),
        release("9.3.0", published_at="2026-11-01T00:00:00Z"),
        release("9.0.0"),
        release("8.9.9"),
    ],
    ids=["draft", "prerelease", "rc-tag", "young", "future", "same", "older"],
)
def test_selects_highest_eligible_release_not_api_order(excluded: dict) -> None:
    assert updater.select_release([excluded], "9.0.0", now=NOW, min_age_days=7) is None
    winner = release("9.10.0", published_at="2026-10-19T00:00:00Z")
    assert (
        updater.select_release(
            [release("9.2.0"), excluded, winner], "9.0.0", now=NOW, min_age_days=7
        )
        == winner
    )


def test_manual_cooldown_bypass_and_no_update() -> None:
    young = release(published_at="2026-10-25T00:00:00Z")
    assert updater.select_release([young], "9.0.0", now=NOW, min_age_days=7) is None
    assert updater.select_release([young], "9.0.0", now=NOW, min_age_days=0) == young
    with pytest.raises(ValueError, match="nonnegative"):
        updater.select_release([young], "9.0.0", now=NOW, min_age_days=-1)


@pytest.mark.parametrize("version", ["v1.2.3", "1.2", "1.2.3-rc1", "1.2.3\nmalicious"])
def test_rejects_unstable_or_unsafe_version(version: str) -> None:
    with pytest.raises(ValueError, match="stable"):
        updater.version_tuple(version)


def test_hashes_both_archives(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = []

    def download(url, *, timeout):
        assert timeout == 60
        urls.append(url)
        return io.BytesIO(b"amd64" if "amd64" in url else b"arm64")

    monkeypatch.setattr(updater.urllib.request, "urlopen", download)
    digests = updater.verified_digests(release())
    assert digests == {
        arch: hashlib.sha256(arch.encode()).hexdigest() for arch in ("amd64", "arm64")
    }
    assert len(urls) == 2


@pytest.mark.parametrize("fault", ["missing", "duplicate", "digest", "url", "size", "bytes"])
def test_rejects_bad_artifact_without_writing(
    fault: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = release()
    asset = candidate["assets"][1]
    if fault == "missing":
        candidate["assets"].pop()
    elif fault == "duplicate":
        candidate["assets"].append(deepcopy(asset))
    elif fault == "digest":
        asset.pop("digest")
    elif fault == "url":
        asset["browser_download_url"] = "https://example.test/untrusted"
    elif fault == "size":
        asset["size"] += 1
    else:
        asset["digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda url, **kwargs: io.BytesIO(b"amd64" if "amd64" in url else b"arm64"),
    )
    before = {name: (repo_copy / name).read_text() for name in updater.DOCKERFILES}
    monkeypatch.setattr(sys, "argv", ["updater", "--root", str(repo_copy), "--write"])
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps([[candidate]])),
    )
    with pytest.raises(ValueError):
        updater.main()
    assert {name: (repo_copy / name).read_text() for name in updater.DOCKERFILES} == before


@pytest.mark.parametrize("write", [False, True])
def test_main_dry_run_or_write_both_real_dockerfiles(
    repo_copy: Path, monkeypatch: pytest.MonkeyPatch, write: bool
) -> None:
    candidate = release(published_at="2020-01-01T00:00:00Z")
    before = {name: (repo_copy / name).read_text() for name in updater.DOCKERFILES}
    body = repo_copy / "body.md"
    args = ["updater", "--root", str(repo_copy), "--body-file", str(body)]
    monkeypatch.setattr(sys, "argv", args + (["--write"] if write else []))

    def api(command, **kwargs):
        assert command == [
            "gh",
            "api",
            "repos/cli/cli/releases?per_page=100",
            "--paginate",
            "--slurp",
        ]
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(command, 0, json.dumps([[], [candidate]]))

    monkeypatch.setattr(updater.subprocess, "run", api)
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda url, **kwargs: io.BytesIO(b"amd64" if "amd64" in url else b"arm64"),
    )
    updater.main()
    for name, original in before.items():
        actual = (repo_copy / name).read_text()
        if write:
            assert updater.read_pins(actual)["GH_VERSION"] == "9.1.0"

            # Only the three ARG lines change, preserving download and install logic.
            def strip_pins(text):
                return [line for line in text.splitlines() if not line.startswith("ARG GH_")]

            assert strip_pins(actual) == strip_pins(original)
        else:
            assert actual == original
    assert body.exists() == write
    if write:
        text = body.read_text()
        assert "Closes #" not in text
        assert "## Changelog" not in text
        assert "<!--" not in text
        for heading in (
            "Related issue",
            "Summary",
            "Test Plan",
            "Demo",
            "Coverage notes",
            "Release notes",
        ):
            assert f"## {heading}" in text
        assert "- [x] Test / CI" in text
        assert "- [x] Existing tests cover this change" in text


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "empty-duplicate", "malformed", "drift", "downgrade"]
)
def test_rejects_pin_drift_before_edits(repo_copy: Path, fault: str) -> None:
    path = repo_copy / updater.DOCKERFILES[1]
    text = path.read_text()
    pins = updater.read_pins(text)
    if fault == "missing":
        text = text.replace(f"ARG GH_VERSION={pins['GH_VERSION']}\n", "")
    elif fault == "duplicate":
        text += f"\nARG GH_VERSION={pins['GH_VERSION']}\n"
    elif fault == "empty-duplicate":
        text += "\nARG GH_VERSION=\n"
    elif fault == "malformed":
        text = text.replace(pins["GH_SHA256_ARM64"], "invalid")
    elif fault == "drift":
        text = text.replace(pins["GH_VERSION"], "1.0.0")
    path.write_text(text)
    with pytest.raises(ValueError):
        updater.updated_files(
            repo_copy,
            release("1.0.0" if fault == "downgrade" else "9.1.0"),
            {"amd64": "a" * 64, "arm64": "b" * 64},
        )
    assert path.read_text() == text


def test_no_candidate_does_not_download_or_write(
    repo_copy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    body = repo_copy / "body.md"
    before = {name: (repo_copy / name).read_text() for name in updater.DOCKERFILES}
    monkeypatch.setattr(
        sys, "argv", ["updater", "--root", str(repo_copy), "--write", "--body-file", str(body)]
    )
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "[[]]"),
    )

    def unexpected_download(*args, **kwargs):
        pytest.fail("No archives should be requested without an eligible release")

    monkeypatch.setattr(updater.urllib.request, "urlopen", unexpected_download)
    updater.main()
    assert "No newer stable" in capsys.readouterr().out
    assert not body.exists()
    assert {name: (repo_copy / name).read_text() for name in updater.DOCKERFILES} == before


def test_workflow_preserves_ci_and_review_boundaries() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/update-gh-cli.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["update"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    steps = {step["name"]: step for step in job["steps"]}
    assert steps["Checkout trusted default branch"]["with"]["persist-credentials"] is False
    assert (
        steps["Verify official archives and prepare pins"]["if"]
        == "steps.existing.outputs.number == ''"
    )
    assert steps["Mint scoped bot token"]["with"]["repositories"] == "omnigent"
    assert (
        steps["Open normal dependency PR"]["env"]["GH_TOKEN"]
        == "${{ steps.app-token.outputs.token }}"
    )
    publish = steps["Open normal dependency PR"]["run"]
    assert "--force-with-lease=" in publish
    assert "git add -- deploy/docker/Dockerfile deploy/docker/Dockerfile.ubi" in publish
    assert "gh pr merge" not in publish
