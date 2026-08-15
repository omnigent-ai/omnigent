"""Unit tests for git_source: URL guard, clone, bundle, SHA resolution."""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.git_source import _inject_token, clone_and_bundle, validate_git_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo",
        "https://github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "ssh://git@github.com/org/repo.git",
    ],
)
def test_validate_git_url_accepts_remote(url):
    validate_git_url(url)  # no raise


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "/local/path/repo",
        "../repo",
        "ftp://example.com/repo",
        "",
        # CRITICAL 1 — file:// with user@host:path must NOT slip through the SCP regex
        "file://user@localhost:/tmp/repo",
        "file://x@y:z",
        # CRITICAL 1 (argument injection) — a leading-dash "URL" must be rejected so
        # ``git clone`` can never parse it as an option (e.g. --upload-pack=<cmd>,
        # RCE on the host). These all previously slipped through the SCP regex.
        "--upload-pack=touchX@h:p",
        "-oProxyCommand=x@h:p",
        "-c@h:p",
        "--depth@h:p",
        "-",
        "--",
    ],
)
def test_validate_git_url_rejects_local_and_bad_schemes(url):
    with pytest.raises(OmnigentError) as exc:
        validate_git_url(url)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_clone_argv_uses_end_of_options_separator(monkeypatch):
    """The clone argv must place ``--`` before the URL so a leading-dash URL
    can never be parsed by git as an option (CRITICAL argument-injection fix)."""
    captured: list[list[str]] = []

    class _FakeCompleted:
        returncode = 1
        stderr = "boom"
        stdout = ""

    def _fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # _allow_local skips the URL guard, isolating the argv construction under test.
    with pytest.raises(OmnigentError):
        clone_and_bundle("https://github.com/org/repo", None, None, _allow_local=True)
    argv = captured[0]
    assert "--" in argv, argv
    # The URL must come immediately after the separator (as a positional).
    assert argv[argv.index("--") + 1] == "https://github.com/org/repo"
    # And nothing before '--' may be the URL.
    assert "https://github.com/org/repo" not in argv[: argv.index("--")]


def _make_repo(tmp_path: Path, files: dict[str, str], branch: str = "main") -> str:
    """Create a local git repo with the given files; return the on-disk path.

    Tests pass it via the ``_allow_local`` hook so the URL guard doesn't reject
    the local path.
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


_VALID_CONFIG = (
    "spec_version: 1\nname: git-agent\n"
    "executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
)


def _entries(bundle: bytes) -> dict[str, str]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
        return {m.name: tf.extractfile(m).read().decode() for m in tf.getmembers() if m.isfile()}


def test_clone_and_bundle_root_agent(tmp_path):
    repo = _make_repo(tmp_path, {"config.yaml": _VALID_CONFIG})
    bundle, sha, ref = clone_and_bundle(repo, "main", None, _allow_local=True)
    entries = _entries(bundle)
    assert "config.yaml" in entries
    assert not any(n.startswith(".git/") or n == ".git" for n in entries)
    assert len(sha) == 40
    assert ref == "main"


def test_clone_and_bundle_subpath_agent(tmp_path):
    repo = _make_repo(
        tmp_path,
        {
            "agents/reviewer/config.yaml": _VALID_CONFIG,
            "agents/reviewer/AGENTS.md": "# hi",
            "README.md": "top-level, must be excluded",
        },
    )
    bundle, _sha, _ref = clone_and_bundle(repo, "main", "agents/reviewer", _allow_local=True)
    entries = _entries(bundle)
    assert set(entries) == {"config.yaml", "AGENTS.md"}
    assert not any(n.startswith(".git/") or n == ".git" for n in entries)


def test_clone_and_bundle_default_branch_when_ref_none(tmp_path):
    repo = _make_repo(tmp_path, {"config.yaml": _VALID_CONFIG}, branch="develop")
    _, _, ref = clone_and_bundle(repo, None, None, _allow_local=True)
    assert ref == "develop"


def test_clone_and_bundle_branch_not_found(tmp_path):
    repo = _make_repo(tmp_path, {"config.yaml": _VALID_CONFIG})
    with pytest.raises(OmnigentError) as exc:
        clone_and_bundle(repo, "nope", None, _allow_local=True)
    assert "nope" in str(exc.value)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_clone_and_bundle_subpath_missing(tmp_path):
    repo = _make_repo(tmp_path, {"config.yaml": _VALID_CONFIG})
    with pytest.raises(OmnigentError) as exc:
        clone_and_bundle(repo, "main", "missing/dir", _allow_local=True)
    assert "missing/dir" in str(exc.value)
    assert exc.value.code == ErrorCode.INVALID_INPUT


# CRITICAL 2 — path traversal guard
def test_clone_and_bundle_path_traversal_rejected(tmp_path):
    repo = _make_repo(tmp_path, {"config.yaml": _VALID_CONFIG})
    with pytest.raises(OmnigentError) as exc:
        clone_and_bundle(repo, "main", "..", _allow_local=True)
    assert exc.value.code == ErrorCode.INVALID_INPUT


# MINOR 5 — token injection: _inject_token mutates URL; clone failure doesn't leak token
def test_inject_token_inserts_into_https_url():
    original = "https://github.com/org/repo.git"
    injected = _inject_token(original, "mysecret")
    assert "mysecret" in injected
    assert injected != original
    assert injected.startswith("https://x-access-token:mysecret@")


def test_inject_token_leaves_ssh_url_unchanged():
    url = "git@github.com:org/repo.git"
    assert _inject_token(url, "tok") == url


def test_clone_and_bundle_failure_message_uses_original_url_not_token(tmp_path):
    """A bad-branch error message must reference git_url, not the token-injected URL."""
    repo = _make_repo(tmp_path, {"config.yaml": _VALID_CONFIG})
    with pytest.raises(OmnigentError) as exc:
        clone_and_bundle(repo, "nosuchbranch", None, token="supersecret", _allow_local=True)
    msg = str(exc.value)
    assert "supersecret" not in msg
    assert "nosuchbranch" in msg
    assert exc.value.code == ErrorCode.INVALID_INPUT
