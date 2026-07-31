"""``~/.omnigent/auth_tokens.json`` holds session JWTs and must never be readable.

``_store_entry``'s docstring already promised ``0o600``, but the implementation was
``path.write_text(...)`` followed by ``os.chmod``. ``write_text`` creates a missing file at
the process umask, so on first login — exactly when a JWT is first persisted — the token was
world-readable until the chmod landed. ``clear_token`` rewrote the file with no chmod at all.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from omnigent import cli_auth

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")

SERVER = "http://localhost:6767"


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Point the module's token path at a scratch dir with nothing pre-created."""
    target = tmp_path / ".omnigent" / "auth_tokens.json"
    monkeypatch.setattr(cli_auth, "_token_file_path", lambda: target)
    return target


@posix_only
def test_first_login_never_leaves_the_jwt_readable(state):
    """The regression: the file must be 0o600 from creation, not after a chmod."""
    cli_auth.store_token(SERVER, "eyJ-SESSION-JWT", "alice@example.com", 1.0)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


@posix_only
def test_state_directory_is_not_world_traversable(state):
    cli_auth.store_token(SERVER, "eyJ-JWT", "alice@example.com", 1.0)
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700


@posix_only
def test_the_jwt_is_never_on_disk_group_or_world_readable(state, monkeypatch):
    """The regression itself.

    Hooks ``os.chmod``, which both the old and new writer call, and samples the mode of the
    path being restricted at that moment. Old code chmod-ed the *final* file after
    ``write_text`` had already created it at the umask default, so this observes 0o644 with
    the JWT in it. New code chmods a temp that ``tempfile`` already created owner-only, so
    the same sample sees 0o600.
    """
    observed: dict[str, int] = {}
    real_chmod = cli_auth.os.chmod

    def spy(path, mode, *a, **kw):
        try:
            if os.path.isfile(path):
                observed[str(path)] = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            pass
        return real_chmod(path, mode, *a, **kw)

    monkeypatch.setattr(cli_auth.os, "chmod", spy)
    cli_auth.store_token(SERVER, "eyJ-SESSION-JWT", "alice@example.com", 1.0)

    assert observed, "expected the writer to restrict a file holding the token"
    for name, mode in observed.items():
        assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0, (
            f"{name} held the JWT at {oct(mode)} before being restricted"
        )


@posix_only
def test_clear_token_keeps_the_file_private(state):
    """clear_token rewrote the file without any chmod of its own."""
    cli_auth.store_token(SERVER, "eyJ-A", "alice@example.com", 1.0)
    cli_auth.store_token("http://other:1", "eyJ-B", "bob@example.com", 1.0)
    cli_auth.clear_token(SERVER)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


@posix_only
def test_clear_token_hardens_a_preexisting_world_traversable_dir(state):
    """A dir that predates this fix at 0o755 is tightened even on a clear-only path."""
    cli_auth.store_token(SERVER, "eyJ-A", "alice@example.com", 1.0)
    cli_auth.store_token("http://other:1", "eyJ-B", "bob@example.com", 1.0)
    os.chmod(state.parent, 0o755)
    cli_auth.clear_token(SERVER)
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700


def test_tokens_round_trip(state):
    cli_auth.store_token(SERVER, "eyJ-A", "alice@example.com", 9e9)
    cli_auth.store_token("http://other:1", "eyJ-B", "bob@example.com", 9e9)
    assert cli_auth.load_token(SERVER) == "eyJ-A"
    assert cli_auth.load_token("http://other:1") == "eyJ-B"


def test_trailing_slash_is_the_same_entry(state):
    cli_auth.store_token(SERVER + "/", "eyJ-A", "alice@example.com", 9e9)
    assert cli_auth.load_token(SERVER) == "eyJ-A"


def test_clear_token_removes_only_the_named_server(state):
    cli_auth.store_token(SERVER, "eyJ-A", "alice@example.com", 9e9)
    cli_auth.store_token("http://other:1", "eyJ-B", "bob@example.com", 9e9)
    cli_auth.clear_token(SERVER)
    assert cli_auth.load_token(SERVER) is None
    assert cli_auth.load_token("http://other:1") == "eyJ-B"


def test_a_failed_write_does_not_destroy_existing_tokens(state, monkeypatch):
    """write_text truncated in place, so a crash mid-write lost every token."""
    cli_auth.store_token(SERVER, "eyJ-ORIGINAL", "alice@example.com", 9e9)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cli_auth.os, "replace", boom)
    with pytest.raises(OSError):
        cli_auth.store_token(SERVER, "eyJ-REPLACEMENT", "alice@example.com", 9e9)

    assert json.loads(state.read_text())[SERVER]["token"] == "eyJ-ORIGINAL"
    leftovers = [p.name for p in state.parent.iterdir() if p.name != state.name]
    assert leftovers == [], f"temp not cleaned up: {leftovers}"
