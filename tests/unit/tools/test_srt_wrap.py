"""Unit tests for :mod:`omnigent.tools._srt`.

The wrap helper is the shared sandbox-on-or-off contract used by
both :class:`LocalPythonTool` and the MCP stdio transport. These
tests pin the four-corner truth table and the settings-file branch
used by stateful local tools.

What breaks if these fail:

- Either subprocess caller silently skips srt wrapping (agents run
  unsandboxed) or silently wraps when srt isn't installed (``srt``
  exec fails with FileNotFoundError at subprocess spawn).
- Stateful tools lose their ``-s <settings_file>`` whitelist and
  their per-invocation ToolState writes fail inside the sandbox.
"""

from __future__ import annotations

import shlex

import pytest

from omnigent.tools._srt import is_srt_available, wrap_with_srt


def test_wrap_with_srt_passthrough_when_disabled() -> None:
    """
    ``sandbox_enabled=False`` passes the command through unchanged,
    regardless of whether srt is available. Callers get a plain
    subprocess — no srt prefix.

    What breaks if this fails: authors who set
    ``SandboxConfig(enabled=False)`` for ``LocalPythonTool``
    would still run inside srt's sandbox, violating their
    explicit opt-out. (Stdio MCPs no longer go through this
    helper post-step-7 — see ``omnigent/tools/mcp.py`` for
    the rationale.)
    """
    cmd = ["python", "/tmp/foo.py", "--flag"]
    assert wrap_with_srt(cmd, sandbox_enabled=False, srt_available=True) is cmd
    assert wrap_with_srt(cmd, sandbox_enabled=False, srt_available=False) is cmd


def test_wrap_with_srt_passthrough_when_unavailable() -> None:
    """
    ``srt_available=False`` passes the command through regardless
    of the caller's sandbox preference. On machines without srt
    installed, the subprocess runs unsandboxed — silently, by
    design, so dev boxes that haven't installed the sandbox
    runtime still function.

    What breaks if this fails: CI environments and fresh dev
    machines without srt installed would crash at subprocess
    spawn with FileNotFoundError on ``srt``.
    """
    cmd = ["python", "/tmp/foo.py"]
    assert wrap_with_srt(cmd, sandbox_enabled=True, srt_available=False) is cmd


def test_wrap_with_srt_wraps_when_enabled_and_available() -> None:
    """
    ``sandbox_enabled=True`` AND ``srt_available=True`` produces the
    wrapped form ``srt -c <shlex.join(cmd)>``. The joined form
    preserves embedded spaces and quotes — ``shlex.join`` is the
    project's standard because ``srt -c`` takes a single shell
    string (like ``bash -c``).

    What breaks if this fails: agents either bypass the sandbox
    silently (if the function returns cmd unchanged) or lose
    argument boundaries when srt's shell re-splits the joined
    string (if shlex.join isn't used).
    """
    cmd = ["python", "/tmp/my tool.py", "--flag=value with spaces"]
    wrapped = wrap_with_srt(cmd, sandbox_enabled=True, srt_available=True)
    # Structure: ``srt -c <single-string>``.
    assert wrapped[:2] == ["srt", "-c"]
    assert len(wrapped) == 3
    # The third arg is the shell-quoted form of the original argv.
    # shlex.split(wrapped[2]) must round-trip back to cmd — that's
    # the invariant srt -c relies on.
    assert shlex.split(wrapped[2]) == cmd


def test_wrap_with_srt_includes_settings_file_when_provided() -> None:
    """
    When *settings_file* is supplied, the wrapped form includes
    ``-s <path>`` between ``srt`` and ``-c`` — matching the
    :class:`LocalPythonTool` stateful-tool contract (each tool
    invocation writes a per-call settings JSON that whitelists
    the tool's ToolState directory for writes).

    What breaks if this fails: stateful tools either can't write
    to their ToolState dir at all (the sandbox denies the write and
    the subprocess exits) or use a stale / shared settings file
    (cross-invocation contamination of whitelisted paths).
    """
    cmd = ["python", "/tmp/stateful_tool.py"]
    wrapped = wrap_with_srt(
        cmd,
        sandbox_enabled=True,
        srt_available=True,
        settings_file="/tmp/srt-12345.json",
    )
    # Structure: ``srt -s <path> -c <quoted>``.
    assert wrapped[:4] == ["srt", "-s", "/tmp/srt-12345.json", "-c"]
    assert len(wrapped) == 5
    assert shlex.split(wrapped[4]) == cmd


def test_wrap_with_srt_ignores_settings_file_when_passthrough() -> None:
    """
    ``settings_file`` has no effect when the function passes
    through (either sandbox disabled or srt unavailable). The
    returned command must be exactly the input — the helper
    shouldn't silently insert ``-s`` flags when it's otherwise
    declining to wrap.

    What breaks if this fails: a caller that speculatively
    computes a settings_file path before checking availability
    would end up with a hybrid command
    (``["-s", ..., original...]``) that fails at spawn time
    with "no such file or directory: -s".
    """
    cmd = ["python", "/tmp/foo.py"]
    result = wrap_with_srt(
        cmd,
        sandbox_enabled=True,
        srt_available=False,
        settings_file="/tmp/srt-ignored.json",
    )
    assert result is cmd


def test_is_srt_available_returns_bool() -> None:
    """
    :func:`is_srt_available` returns a concrete ``bool`` — not
    a truthy Path, not None when absent. Callers cache it in
    a ``bool`` field (``self._srt_available``) and rely on
    the exact type for mypy strict mode.

    What breaks if this fails: callers storing the result in
    a ``bool``-annotated attribute would trip mypy or, at
    runtime, ``bool(Path)`` would behave correctly but any
    ``is True`` / ``is False`` comparisons (e.g. in tests)
    would miss.
    """
    result = is_srt_available()
    assert isinstance(result, bool)


@pytest.mark.parametrize(
    "sandbox_enabled,srt_available,expected_wrapped",
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_wrap_with_srt_truth_table(
    sandbox_enabled: bool,
    srt_available: bool,
    expected_wrapped: bool,
) -> None:
    """
    Exhaustive 2x2 of sandbox_enabled × srt_available. Only the
    (True, True) cell wraps; every other cell passes through.

    This is the explicit truth table form of the individual tests
    above — present so future changes to the on/off logic flip a
    single parametrize row rather than editing separate test
    functions in two places.

    :param sandbox_enabled: The sandbox-enabled flag.
    :param srt_available: The srt-available flag.
    :param expected_wrapped: Whether the output should be the
        wrapped form (``["srt", "-c", ...]``) vs the passthrough.
    """
    cmd = ["echo", "hello"]
    result = wrap_with_srt(cmd, sandbox_enabled=sandbox_enabled, srt_available=srt_available)
    if expected_wrapped:
        assert result[:2] == ["srt", "-c"]
        assert shlex.split(result[2]) == cmd
    else:
        assert result is cmd
