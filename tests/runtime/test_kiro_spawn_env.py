"""Unit tests for the kiro-native spawn-env / terminal-env builders.

The kiro-native harness is terminal-first: the runner spawns ``kiro-cli`` in a
tmux pane and the executor reads ``HARNESS_KIRO_NATIVE_BRIDGE_DIR`` to find the
per-session bridge directory. Two builders in ``omnigent.kiro_native_bridge``
produce that env:

* :func:`build_kiro_native_spawn_env` — the minimal env handed to the harness
  executor: *only* the bridge-dir pointer (kiro has no provider/model/theme env,
  unlike goose-native's ``build_goose_native_spawn_env``).
* :func:`build_kiro_native_terminal_env` — the allowlisted child env for the
  ``kiro-cli`` process itself: the bridge-dir pointer and the ACP-record path,
  plus a fixed allowlist of terminal/locale vars, with everything else (ambient
  provider keys, arbitrary exports) dropped.

This is the kiro sibling of ``tests/runtime/test_goose_spawn_env.py``. Like that
suite it does not isolate ``TMPDIR`` — ``_BRIDGE_ROOT`` is resolved at import
time, so the builders write their per-session dir under the real
``/tmp/omnigent-<uid>/kiro-native/`` and the assertions key off the path shape,
determinism, and permissions rather than an injected root.
"""

from __future__ import annotations

import stat

from omnigent.kiro_native_bridge import (
    KIRO_ACP_RECORD_PATH_ENV_VAR,
    KIRO_NATIVE_BRIDGE_DIR_ENV_VAR,
    acp_record_path,
    bridge_dir_for_session_id,
    build_kiro_native_spawn_env,
    build_kiro_native_terminal_env,
)


def test_spawn_env_sets_only_the_bridge_dir() -> None:
    """The spawn env carries the bridge-dir pointer and nothing else.

    kiro-native's executor reads the session bridge dir from this single var; the
    harness owns no provider/model/theme env (the sibling goose builder adds
    ``GOOSE_*``). Asserting an exact one-key set pins that minimality — a refactor
    that leaked an extra var here would surface.
    """
    env = build_kiro_native_spawn_env("sess-1")
    assert set(env) == {KIRO_NATIVE_BRIDGE_DIR_ENV_VAR}
    bridge_dir = env[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR]
    # It is ``<root>/kiro-native/<hash>`` — the segment is present, but the path
    # does not *end* at the harness root (that would mean no per-session hash).
    assert "kiro-native/" in bridge_dir
    assert not bridge_dir.endswith("kiro-native")
    assert bridge_dir == str(bridge_dir_for_session_id("sess-1"))


def test_spawn_env_bridge_dir_is_deterministic_per_session() -> None:
    """Same session id → same bridge dir; a different id → a different dir.

    The dir is keyed by a hash of the session id, so resume/fork of the same
    session must resolve to the same bridge dir, and two sessions must never
    collide. Mirrors ``test_goose_spawn_env``'s determinism check.
    """
    a = build_kiro_native_spawn_env("same")[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR]
    b = build_kiro_native_spawn_env("same")[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR]
    c = build_kiro_native_spawn_env("other")[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR]
    assert a == b
    assert a != c


def test_spawn_env_creates_bridge_dir_with_0700() -> None:
    """Building the spawn env materializes the bridge dir, private to the owner.

    The bridge dir holds the tmux target + forwarder handshake files, so it is
    created eagerly with ``0o700`` (``prepare_bridge_dir`` mkdirs then chmods).
    """
    bridge_dir = bridge_dir_for_session_id("sess-perm")
    build_kiro_native_spawn_env("sess-perm")
    assert bridge_dir.is_dir()
    assert stat.S_IMODE(bridge_dir.stat().st_mode) == 0o700


def test_terminal_env_keeps_allowlisted_vars_and_adds_bridge_dir() -> None:
    """The child env forwards allowlisted terminal/locale vars + the bridge dir.

    ``kiro-cli`` needs the terminal/locale context (``TERM``/``LANG``/…) and its
    config-home vars to render and locate state, plus the bridge-dir pointer so
    the in-pane process and the forwarder agree on the handshake directory.
    """
    source_env = {
        "HOME": "/home/agent",
        "PATH": "/usr/bin",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "KIRO_CONFIG_HOME": "/home/agent/.kiro",
    }
    env = build_kiro_native_terminal_env("sess-2", source_env=source_env)
    for key, value in source_env.items():
        assert env[key] == value
    bridge_dir = bridge_dir_for_session_id("sess-2")
    assert env[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR] == str(bridge_dir)
    # The ACP transcript-record path is injected too, under the bridge dir.
    assert env[KIRO_ACP_RECORD_PATH_ENV_VAR] == str(acp_record_path(bridge_dir))


def test_terminal_env_drops_non_allowlisted_and_ambient_secrets() -> None:
    """Anything off the allowlist — arbitrary exports and provider keys — is dropped.

    The builder is an allowlist, not a denylist, so a leaked ``ANTHROPIC_API_KEY``
    (kiro authenticates against its own backend and must not inherit ambient
    provider creds) and a junk ``RANDOM_EXPORT`` both fall away. Only the harness's
    own injected vars (bridge dir + ACP record path) survive from an otherwise-
    disallowed source env.
    """
    source_env = {
        "ANTHROPIC_API_KEY": "sk-should-not-leak",
        "AWS_SECRET_ACCESS_KEY": "should-not-leak",
        "RANDOM_EXPORT": "nope",
    }
    env = build_kiro_native_terminal_env("sess-3", source_env=source_env)
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "RANDOM_EXPORT" not in env
    # Nothing from the (all-disallowed) source env survives — only the harness's
    # own injected vars remain.
    assert set(env) == {KIRO_NATIVE_BRIDGE_DIR_ENV_VAR, KIRO_ACP_RECORD_PATH_ENV_VAR}


def test_terminal_env_drops_empty_allowlisted_var() -> None:
    """An allowlisted var present-but-empty is omitted, not forwarded blank.

    The builder keeps a key only when ``env.get(key)`` is truthy, so an exported
    ``HOME=""`` resolves to *absent* rather than an empty string that would
    mislead ``kiro-cli`` into a bogus home.
    """
    env = build_kiro_native_terminal_env("sess-4", source_env={"HOME": "", "TERM": "xterm"})
    assert "HOME" not in env
    assert env["TERM"] == "xterm"
