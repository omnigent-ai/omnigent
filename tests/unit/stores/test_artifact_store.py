"""Tests for LocalArtifactStore."""

from __future__ import annotations

import pytest

from omnigent.stores.artifact_store.local import LocalArtifactStore


@pytest.fixture()
def store(tmp_path):
    return LocalArtifactStore(str(tmp_path / "artifacts"))


# ── put / get round-trip ────────────────────────────────────


def test_put_and_get(store):
    store.put("abc123", b"hello world")
    assert store.get("abc123") == b"hello world"


def test_put_overwrites(store):
    store.put("k", b"first")
    store.put("k", b"second")
    assert store.get("k") == b"second"


def test_put_empty_bytes(store):
    store.put("empty", b"")
    assert store.get("empty") == b""


def test_put_binary_data(store):
    data = bytes(range(256))
    store.put("bin", data)
    assert store.get("bin") == data


# ── nested keys ─────────────────────────────────────────────


def test_nested_key_put_get(store):
    store.put("agents/abc/bundle.tar", b"bundle-data")
    assert store.get("agents/abc/bundle.tar") == b"bundle-data"


def test_nested_key_exists(store):
    store.put("a/b/c", b"deep")
    assert store.exists("a/b/c")
    assert not store.exists("a/b/d")


def test_nested_key_delete(store):
    store.put("x/y", b"data")
    store.delete("x/y")
    assert not store.exists("x/y")


# ── get errors ──────────────────────────────────────────────


def test_get_missing_raises_key_error(store):
    with pytest.raises(KeyError, match=r"no-such-key"):
        store.get("no-such-key")


# ── delete ──────────────────────────────────────────────────


def test_delete_removes_blob(store):
    store.put("to-delete", b"data")
    store.delete("to-delete")
    assert not store.exists("to-delete")


def test_delete_missing_is_noop(store):
    store.delete("nonexistent")


# ── exists ──────────────────────────────────────────────────


def test_exists_true(store):
    store.put("present", b"x")
    assert store.exists("present")


def test_exists_false(store):
    assert not store.exists("absent")


# ── root directory creation ─────────────────────────────────


def test_creates_root_on_init(tmp_path):
    root = tmp_path / "deep" / "nested" / "dir"
    assert not root.exists()
    LocalArtifactStore(str(root))
    assert root.is_dir()


# ── key validation ──────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "..",
        "../etc/passwd",
        "foo\\bar",
        "a/../b",
        "valid/../../escape",
        "/absolute/path",
        "C:/windows/drive",
    ],
)
def test_rejects_invalid_keys(store, bad_key):
    with pytest.raises(ValueError, match=r"invalid artifact key|escapes root"):
        store.put(bad_key, b"x")


def test_all_methods_validate_keys(store):
    """Every public method rejects bad keys (all go through _resolve)."""
    for method, args in [
        (store.get, ("",)),
        (store.delete, ("..",)),
        (store.exists, ("a/../b",)),
    ]:
        with pytest.raises(ValueError):
            method(*args)


# ── symlink traversal ──────────────────────────────────────


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(str(root))

    # Create a symlink inside root that points outside
    escape_target = tmp_path / "secret"
    escape_target.write_bytes(b"sensitive")
    (root / "evil-link").symlink_to(escape_target)

    with pytest.raises(ValueError, match=r"escapes root directory"):
        store.get("evil-link")
