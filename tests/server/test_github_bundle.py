"""
Tests for public-GitHub agent-source fetching
(``omnigent/server/github_bundle.py``).

Covers URL resolution (three accepted shapes + bad input), tarball
repackaging (GitHub root-strip, optional subdir, junk filtering), the
download-size guard, and the missing/private-repo 404 path. The repackaged
bytes are asserted to pass the same ``validate_agent_bundle`` a hand-upload
goes through, since structure parity is the whole point of the feature.
"""

from __future__ import annotations

import io
import tarfile

import httpx
import pytest
import respx

from omnigent.errors import OmnigentError
from omnigent.server.bundles import validate_agent_bundle
from omnigent.server.github_bundle import (
    fetch_github_bundle,
    parse_github_source,
)

# Minimal valid omnigent config.yaml (directory shape).
_CONFIG = (
    "spec_version: 1\n"
    "name: gh-agent\n"
    "executor:\n"
    "  type: omnigent\n"
    "  model: claude-sonnet-4-20250514\n"
    "  config:\n"
    "    harness: claude\n"
)


def _make_codeload(files: dict[str, str]) -> bytes:
    """Build a codeload-style ``.tar.gz`` from ``{archive_path: content}``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _entry_names(bundle: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:*") as tf:
        return sorted(tf.getnames())


# ── URL parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "owner", "repo", "ref", "subdir"),
    [
        ("github.com/acme/bot", "acme", "bot", None, None),
        ("https://github.com/acme/bot", "acme", "bot", None, None),
        ("https://github.com/acme/bot.git", "acme", "bot", None, None),
        ("https://github.com/acme/bot/", "acme", "bot", None, None),
        ("acme/bot@v1.2.0", "acme", "bot", "v1.2.0", None),
        (
            "https://github.com/acme/bot/tree/main/agents/foo",
            "acme",
            "bot",
            "main",
            "agents/foo",
        ),
        ("https://github.com/acme/bot/tree/v1.0", "acme", "bot", "v1.0", None),
    ],
)
def test_parse_github_source_shapes(url, owner, repo, ref, subdir):
    src = parse_github_source(url)
    assert (src.owner, src.repo, src.ref, src.subdir) == (owner, repo, ref, subdir)


def test_parse_github_source_arg_overrides_url():
    src = parse_github_source("acme/bot", ref="abc123", subdir="sub")
    assert src.ref == "abc123"
    assert src.subdir == "sub"
    assert src.codeload_url == "https://codeload.github.com/acme/bot/tar.gz/abc123"


def test_parse_github_source_defaults_to_head():
    assert parse_github_source("acme/bot").codeload_url.endswith("/tar.gz/HEAD")


@pytest.mark.parametrize("bad", ["", "not a url", "acme/bot with space", "acme"])
def test_parse_github_source_rejects_bad_input(bad):
    with pytest.raises(OmnigentError):
        parse_github_source(bad)


def test_parse_github_source_rejects_subdir_traversal():
    with pytest.raises(OmnigentError):
        parse_github_source("acme/bot", subdir="../evil")


# ── Repackage (via fetch_github_bundle + respx) ──────────────────────


@respx.mock
def test_fetch_repackages_and_strips_root_and_junk():
    raw = _make_codeload(
        {
            "acme-bot-sha/config.yaml": _CONFIG,
            "acme-bot-sha/AGENTS.md": "hello",
            "acme-bot-sha/.git/HEAD": "ref: refs/heads/main",
            "acme-bot-sha/node_modules/x/y.js": "junk",
            "acme-bot-sha/.DS_Store": "junk",
        }
    )
    src = parse_github_source("acme/bot")
    respx.get(src.codeload_url).mock(return_value=httpx.Response(200, content=raw))

    bundle = fetch_github_bundle(src)
    assert _entry_names(bundle) == ["AGENTS.md", "config.yaml"]
    # Structure parity: the repackaged bytes validate exactly like an upload.
    assert validate_agent_bundle(bundle, enforce_handler_allowlist=True).name == "gh-agent"


@respx.mock
def test_fetch_honors_subdir():
    raw = _make_codeload(
        {
            "acme-bot-sha/agents/foo/config.yaml": _CONFIG,
            "acme-bot-sha/agents/foo/AGENTS.md": "x",
            "acme-bot-sha/README.md": "excluded by subdir",
        }
    )
    src = parse_github_source("https://github.com/acme/bot/tree/main/agents/foo")
    respx.get(src.codeload_url).mock(return_value=httpx.Response(200, content=raw))

    bundle = fetch_github_bundle(src)
    assert _entry_names(bundle) == ["AGENTS.md", "config.yaml"]


@respx.mock
def test_fetch_missing_subdir_errors():
    raw = _make_codeload({"acme-bot-sha/config.yaml": _CONFIG})
    src = parse_github_source("acme/bot", subdir="nope")
    respx.get(src.codeload_url).mock(return_value=httpx.Response(200, content=raw))
    with pytest.raises(OmnigentError, match="subdir not found"):
        fetch_github_bundle(src)


@respx.mock
def test_fetch_404_is_invalid_input():
    src = parse_github_source("acme/missing")
    respx.get(src.codeload_url).mock(return_value=httpx.Response(404))
    with pytest.raises(OmnigentError, match="not found"):
        fetch_github_bundle(src)


@respx.mock
def test_fetch_rejects_oversize_download(monkeypatch):
    # Shrink the cap so a tiny fixture trips it deterministically.
    monkeypatch.setattr("omnigent.server.github_bundle._MAX_DOWNLOAD_BYTES", 16, raising=True)
    raw = _make_codeload({"acme-bot-sha/config.yaml": _CONFIG})
    assert len(raw) > 16
    src = parse_github_source("acme/bot")
    respx.get(src.codeload_url).mock(return_value=httpx.Response(200, content=raw))
    with pytest.raises(OmnigentError, match="maximum bundle size"):
        fetch_github_bundle(src)
