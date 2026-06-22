"""Tests for the claude-native client-side launch-state store.

The state module persists per-conversation launch metadata (today
just the cwd a session was created in) under
``~/.omnigent/claude-native/<hash>/launch.json`` so the resume
path can detect cwd mismatches that would otherwise make
``claude --resume`` exit immediately on launch.

The test session's :func:`_isolate_claude_native_state` autouse
fixture (defined in ``tests/conftest.py``) redirects the state
root to a per-session ``tmp_path`` via
:data:`OMNIGENT_CLAUDE_NATIVE_STATE_DIR`, so these tests never
touch the developer's real ``~/.omnigent``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.claude_native_state import (
    _state_dir_for_conversation_id,
    read_launch_state,
    redirect_launch_state,
    write_launch_state,
)

# ── Roundtrip and idempotence ─────────────────────────────────────


def test_write_then_read_returns_recorded_path() -> None:
    """
    Round-trip happy path: writing a cwd and reading it back
    returns the same string verbatim.

    The state module deliberately stores the path as-is rather
    than resolving / normalizing it on write. Callers
    (:func:`_record_launch_for_fresh_session`) resolve through
    ``Path.cwd().resolve()`` BEFORE writing; the module trusts
    that and doesn't re-resolve on either end. If a future
    refactor accidentally adds normalization here it would
    confuse downstream comparisons in
    :func:`_align_working_directory_with_session`.
    """
    write_launch_state("conv_roundtrip", "/home/me/repo")
    state = read_launch_state("conv_roundtrip")
    assert state is not None, (
        "read_launch_state returned None immediately after a successful "
        "write -- the file is missing, malformed, or the conv-id hashing "
        "diverged between write and read."
    )
    assert state.working_directory == "/home/me/repo", (
        f"working_directory round-tripped to {state.working_directory!r}; "
        f"expected the literal string we wrote."
    )


def test_read_missing_state_returns_none() -> None:
    """
    A conversation that was never written has no state.

    ``None`` (not an exception) so the resume path treats it as a
    legacy/foreign-machine session and silently proceeds without
    prompting.
    """
    assert read_launch_state("conv_never_written") is None


def test_write_same_value_is_idempotent() -> None:
    """
    Re-writing the same cwd is a no-op (does not raise, does not
    rewrite).

    This protects against a wrapper that calls
    :func:`_record_launch_for_fresh_session` more than once for the
    same conv id (e.g. a future code path that retries the create
    after a transient failure).
    """
    write_launch_state("conv_idempotent", "/home/me/repo")
    # Second call with the same value: no exception, no change.
    write_launch_state("conv_idempotent", "/home/me/repo")
    state = read_launch_state("conv_idempotent")
    assert state is not None
    assert state.working_directory == "/home/me/repo"


def test_write_different_value_keeps_existing(caplog: pytest.LogCaptureFixture) -> None:
    """
    Overwrite with a different cwd is rejected (warning logged,
    file untouched).

    The launch cwd is a fact about the original session. A wrapper
    calling write_launch_state with a different value for the same
    conv id signals a bug -- e.g. resume from a different cwd
    incorrectly hitting the fresh-session write path. We log
    loudly and keep the original value so resume-time mismatch
    detection stays correct on the next launch.
    """
    import logging

    # Force the `omnigent` package logger to propagate so caplog's
    # root-attached handler captures warnings. Defensive: pollution
    # from a sibling test that runs ``setup_cli_logging`` (which sets
    # ``omnigent.propagate = False``) can leak into this xdist
    # worker if its cleanup fixture didn't run.
    logging.getLogger("omnigent").propagate = True
    write_launch_state("conv_overwrite", "/home/me/repo")
    with caplog.at_level(logging.WARNING):
        write_launch_state("conv_overwrite", "/elsewhere")
    state = read_launch_state("conv_overwrite")
    assert state is not None
    # First-writer wins. If this fails, the protection against
    # silent overwrites is broken and resume-time mismatch
    # detection would be wrong on the next resume.
    assert state.working_directory == "/home/me/repo", (
        f"expected first-writer-wins, but got {state.working_directory!r}"
    )
    assert any("mismatch" in r.message for r in caplog.records), (
        f"a divergent write must surface a warning; "
        f"captured records: {[r.message for r in caplog.records]!r}"
    )


def test_redirect_launch_state_overwrites_existing_cwd() -> None:
    """
    User-approved redirect can replace the persisted launch cwd.

    ``write_launch_state`` is deliberately first-writer-wins because
    fresh-session writes must not silently mutate the original cwd.
    Redirect is the explicit exception: after the user asks to make a
    Claude transcript belong to a new directory, future resumes must
    compare against that new cwd.
    """
    write_launch_state("conv_redirect_state", "/home/me/old-repo")

    redirect_launch_state("conv_redirect_state", "/home/me/new-repo")

    state = read_launch_state("conv_redirect_state")
    assert state is not None
    assert state.working_directory == "/home/me/new-repo"


def test_write_rejects_empty_string() -> None:
    """
    Writing an empty string is a programmer error -- caller must
    pass a real absolute path.

    Silently accepting ``\"\"`` would later make ``read_launch_state``
    return state whose comparison to a real cwd always mismatches,
    triggering spurious chdir prompts. Fail loud at the write site
    instead so the bug is obvious at the call site.
    """
    with pytest.raises(ValueError, match="non-empty absolute path"):
        write_launch_state("conv_empty", "")


# ── Path-traversal safety ─────────────────────────────────────────


def test_state_dir_for_conversation_id_is_under_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Even an attacker-chosen conversation id resolves to a single
    child of the state root.

    The directory name is a sha256 of the conv id, so a malicious
    id like ``\"../etc\"`` cannot escape the state root via path
    traversal. The hash is a deterministic single-segment hex
    string; we assert that the resolved path has the state root
    as a parent (not just as a prefix string-match, which would
    miss a ``..`` escape).
    """
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(tmp_path))

    for evil_id in [
        "../../../etc/passwd",
        "/absolute/path",
        "..",
        ".",
        "conv_normal",
    ]:
        state_dir = _state_dir_for_conversation_id(evil_id)
        # The resolved state dir MUST be under the configured root.
        # Without this check a conv id of ``\"../foo\"`` would escape.
        assert tmp_path.resolve() in state_dir.resolve().parents or (
            state_dir.resolve() == tmp_path.resolve()
        ), (
            f"conv id {evil_id!r} resolved to {state_dir!r}, which is not "
            f"under the state root {tmp_path!r}. Path-traversal protection "
            f"is broken."
        )


# ── Malformed-state resilience ─────────────────────────────────────


def test_read_malformed_json_returns_none(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A corrupted state file returns ``None`` and logs, not raise.

    The launch state is a UX nicety; a corrupted file (truncated
    by a crash mid-write, or hand-edited) shouldn't block resume.
    Falling through to ``None`` makes the resume path act as if
    the session were a legacy session -- the user can still
    proceed and chdir manually if Claude exits.
    """
    import logging

    # See test_write_different_value_keeps_existing — defensive
    # restore of ``omnigent`` logger propagation in case a sibling
    # test leaked ``propagate = False`` into this xdist worker.
    logging.getLogger("omnigent").propagate = True

    # Point the state root at a fresh tmp dir so we don't read a
    # malformed file from another test.
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(tmp_path))

    state_dir = _state_dir_for_conversation_id("conv_malformed")
    state_dir.mkdir(parents=True)
    (state_dir / "launch.json").write_text("not json {")

    with caplog.at_level(logging.WARNING):
        result = read_launch_state("conv_malformed")
    assert result is None
    assert any("malformed" in r.message.lower() for r in caplog.records), (
        f"a malformed-JSON read must log a warning; "
        f"got records: {[r.message for r in caplog.records]!r}"
    )


def test_read_missing_working_directory_field_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A state file missing the ``working_directory`` field returns
    ``None``.

    Counter-test to the happy path: extraction must not invent a
    value when the key isn't present. Would surface as a forward-
    compatibility hazard if a future writer drops the field by
    accident -- we want the resume path to treat it as legacy,
    not crash and not falsely prompt.
    """
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(tmp_path))

    state_dir = _state_dir_for_conversation_id("conv_no_wd")
    state_dir.mkdir(parents=True)
    (state_dir / "launch.json").write_text(json.dumps({"conversation_id": "conv_no_wd"}))

    assert read_launch_state("conv_no_wd") is None


def test_read_empty_working_directory_field_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    An empty-string ``working_directory`` is treated as missing.

    Empty strings are not legitimate cwds. Treating them as
    ``None`` aligns with the write-site guard (which raises on
    empty input) and prevents downstream code from comparing
    ``"".resolve()`` against a real cwd.
    """
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(tmp_path))

    state_dir = _state_dir_for_conversation_id("conv_empty_wd")
    state_dir.mkdir(parents=True)
    (state_dir / "launch.json").write_text(
        json.dumps({"conversation_id": "conv_empty_wd", "working_directory": ""})
    )

    assert read_launch_state("conv_empty_wd") is None


# ── State root override -------------------------------------------


def test_state_root_env_var_redirects_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``OMNIGENT_CLAUDE_NATIVE_STATE_DIR`` redirects the state
    tree.

    Tests rely on this for isolation (the autouse
    ``_isolate_claude_native_state`` fixture in ``tests/conftest.py``).
    If the override stopped working, every test that drives the
    wrapper would silently write to the developer's real
    ``~/.omnigent/``.
    """
    redirect = tmp_path / "alt-state"
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(redirect))

    write_launch_state("conv_redirect", "/some/path")
    # File must exist under the redirected root, not under any
    # default home-relative path.
    state_files = list(redirect.rglob("launch.json"))
    assert len(state_files) == 1, (
        f"expected exactly one launch.json under {redirect!r}, got {state_files!r}. "
        f"The env override either isn't being honored or it's leaking writes "
        f"to a different root."
    )
    # And the read path also honors the override.
    state = read_launch_state("conv_redirect")
    assert state is not None
    assert state.working_directory == "/some/path"
