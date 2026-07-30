"""Tests for omnigent.runtime.verify."""

from __future__ import annotations

import dataclasses

import pytest

from omnigent.runtime.verify import VerifyResult, run_verify
from omnigent.spec.types import VerifySpec


class _FakeOSEnv:
    """Test double exposing only the ``shell``/``read`` methods run_verify calls."""

    def __init__(self, *, shell: dict | None = None, read: dict | None = None) -> None:
        self._shell = shell or {}
        self._read = read or {}

    async def shell(
        self, command: str, timeout: int | None = None, max_output: int | None = None
    ) -> dict:
        return self._shell.get(
            command,
            {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False},
        )

    async def read(self, path: str, offset: int = 1, limit: int | None = None) -> dict:
        return self._read.get(path, {"error": f"not found: {path}"})


async def test_empty_spec_passes_vacuously() -> None:
    """A VerifySpec with no checks passes with an empty check list."""
    result = await run_verify(VerifySpec(), _FakeOSEnv())

    # No checks declared — nothing can fail, and downstream tooling sees no work.
    assert result == VerifyResult(passed=True, checks=())


async def test_command_exit_zero_passes() -> None:
    """A command exiting 0 is a passing check."""
    spec = VerifySpec(commands=("pytest",))
    os_env = _FakeOSEnv(
        shell={"pytest": {"exit_code": 0, "stdout": "3 passed", "stderr": "", "timed_out": False}},
    )

    result = await run_verify(spec, os_env)

    assert result.passed is True
    assert len(result.checks) == 1
    assert result.checks[0].passed is True


async def test_command_non_zero_fails() -> None:
    """A command exiting non-zero fails and surfaces stderr."""
    spec = VerifySpec(commands=("pytest",))
    os_env = _FakeOSEnv(
        shell={
            "pytest": {
                "exit_code": 1,
                "stdout": "",
                "stderr": "1 failed",
                "timed_out": False,
                "error": "Command exited with status 1: 1 failed",
            }
        },
    )

    result = await run_verify(spec, os_env)

    assert result.passed is False
    assert result.checks[0].passed is False
    assert "exit 1" in result.checks[0].detail
    assert "1 failed" in result.checks[0].detail


async def test_command_timeout_fails() -> None:
    """A timed-out command (exit_code None) fails as timed out."""
    spec = VerifySpec(commands=("slow",))
    os_env = _FakeOSEnv(
        shell={
            "slow": {
                "exit_code": None,
                "stdout": "partial output",
                "stderr": "",
                "timed_out": True,
                "error": "Command timed out after 120 seconds",
            }
        },
    )

    result = await run_verify(spec, os_env)

    assert result.passed is False
    assert "timed out" in result.checks[0].detail


async def test_command_shell_error_fails() -> None:
    """A shell-level error (e.g. sandbox denial) fails the check."""
    spec = VerifySpec(commands=("boom",))
    os_env = _FakeOSEnv(shell={"boom": {"error": "sandbox denied"}})

    result = await run_verify(spec, os_env)

    assert result.passed is False
    assert "sandbox denied" in result.checks[0].detail


async def test_failed_command_stdout_still_feeds_content_checks() -> None:
    """A failing command's stdout must still feed contains/not_contains.

    The real OSEnvironment.shell sets ``error`` on non-zero exit but still
    returns stdout; run_verify must capture it, so a ``not_contains`` gate on
    a failing pytest's "failed" summary fails rather than silently passing.
    """
    spec = VerifySpec(commands=("pytest",), not_contains=("failed",))
    os_env = _FakeOSEnv(
        shell={
            "pytest": {
                "exit_code": 1,
                "stdout": "3 failed, 5 passed",
                "stderr": "",
                "timed_out": False,
                "error": "Command exited with status 1: 3 failed, 5 passed",
            }
        },
    )

    result = await run_verify(spec, os_env)

    by_name = {c.name: c for c in result.checks}
    assert result.passed is False
    assert by_name["command[0]"].passed is False
    # "failed" IS in the captured stdout, so the gate must fail (not pass silently).
    assert by_name["not_contains[0]"].passed is False


async def test_contains_is_case_sensitive() -> None:
    """contains matches substrings of combined command stdout, case-sensitively."""
    os_env = _FakeOSEnv(
        shell={"pytest": {"exit_code": 0, "stdout": "3 passed", "stderr": "", "timed_out": False}},
    )

    passing = await run_verify(VerifySpec(commands=("pytest",), contains=("passed",)), os_env)
    failing = await run_verify(VerifySpec(commands=("pytest",), contains=("PASSED",)), os_env)

    assert passing.passed is True
    assert failing.passed is False


async def test_not_contains_present_fails() -> None:
    """not_contains fails when the substring is present in command stdout."""
    os_env = _FakeOSEnv(
        shell={
            "ruff": {
                "exit_code": 0,
                "stdout": "All checks passed!",
                "stderr": "",
                "timed_out": False,
            }
        },
    )

    clean = await run_verify(VerifySpec(commands=("ruff",), not_contains=("error",)), os_env)
    dirty = await run_verify(VerifySpec(commands=("ruff",), not_contains=("passed",)), os_env)

    assert clean.passed is True
    assert dirty.passed is False


async def test_no_stubs_clean_passes() -> None:
    """no_stubs passes when no path content matches the pattern."""
    spec = VerifySpec(no_stubs=("TODO|FIXME",), paths=("a.py",))
    os_env = _FakeOSEnv(read={"a.py": {"content": "print('clean')\n", "path": "a.py"}})

    result = await run_verify(spec, os_env)

    assert result.passed is True
    assert result.checks[0].name == "no_stubs[0]"


async def test_no_stubs_match_names_the_file() -> None:
    """no_stubs fails and names the file whose content matched."""
    spec = VerifySpec(no_stubs=("TODO|FIXME",), paths=("a.py", "b.py"))
    os_env = _FakeOSEnv(
        read={
            "a.py": {"content": "done\n", "path": "a.py"},
            "b.py": {"content": "# TODO: fix this\n", "path": "b.py"},
        },
    )

    result = await run_verify(spec, os_env)

    assert result.passed is False
    assert "b.py" in result.checks[0].detail
    assert "a.py" not in result.checks[0].detail


async def test_no_stubs_read_error_fails() -> None:
    """A path that cannot be read fails the no_stubs check rather than passing silently."""
    spec = VerifySpec(no_stubs=("TODO",), paths=("missing.py",))
    result = await run_verify(spec, _FakeOSEnv())

    assert result.passed is False
    assert "missing.py" in result.checks[0].detail


async def test_combined_checks_aggregate_in_order() -> None:
    """All check families run, ordered commands → contains → not_contains → no_stubs."""
    spec = VerifySpec(
        commands=("pytest", "ruff check ."),
        contains=("passed",),
        not_contains=("error",),
        no_stubs=("NotImplementedError",),
        paths=("a.py",),
    )
    os_env = _FakeOSEnv(
        shell={
            "pytest": {"exit_code": 0, "stdout": "passed", "stderr": "", "timed_out": False},
            "ruff check .": {
                "exit_code": 0,
                "stdout": "All clean",
                "stderr": "",
                "timed_out": False,
            },
        },
        read={"a.py": {"content": "x = 1\n", "path": "a.py"}},
    )

    result = await run_verify(spec, os_env)

    assert result.passed is True
    assert [c.name for c in result.checks] == [
        "command[0]",
        "command[1]",
        "contains[0]",
        "not_contains[0]",
        "no_stubs[0]",
    ]


async def test_failed_command_stdout_feeds_contains() -> None:
    """A failing command's stdout still satisfies a contains gate (stdout is captured)."""
    spec = VerifySpec(commands=("pytest",), contains=("5 passed",))
    os_env = _FakeOSEnv(
        shell={
            "pytest": {
                "exit_code": 1,
                "stdout": "1 failed, 5 passed",
                "stderr": "",
                "timed_out": False,
                "error": "Command exited with status 1: 1 failed, 5 passed",
            }
        },
    )

    result = await run_verify(spec, os_env)

    by_name = {c.name: c for c in result.checks}
    assert result.passed is False
    assert by_name["command[0]"].passed is False
    # stdout was captured despite the failure, so the contains gate passes.
    assert by_name["contains[0]"].passed is True


async def test_commands_stdout_is_combined_across_commands() -> None:
    """contains sees the stdout of every command, not just the first."""
    spec = VerifySpec(commands=("echo-a", "echo-b"), contains=("alpha", "beta"))
    os_env = _FakeOSEnv(
        shell={
            "echo-a": {"exit_code": 0, "stdout": "alpha", "stderr": "", "timed_out": False},
            "echo-b": {"exit_code": 0, "stdout": "beta", "stderr": "", "timed_out": False},
        },
    )

    result = await run_verify(spec, os_env)

    assert result.passed is True


async def test_timeout_partial_stdout_is_captured() -> None:
    """A timed-out command's partial stdout still feeds the content checks."""
    spec = VerifySpec(commands=("slow",), contains=("progress",))
    os_env = _FakeOSEnv(
        shell={
            "slow": {
                "exit_code": None,
                "stdout": "partial progress before kill",
                "stderr": "",
                "timed_out": True,
                "error": "Command timed out after 120 seconds",
            }
        },
    )

    result = await run_verify(spec, os_env)

    by_name = {c.name: c for c in result.checks}
    assert by_name["command[0]"].passed is False  # timed out
    assert by_name["contains[0]"].passed is True  # partial stdout was captured


async def test_no_stubs_invalid_regex_records_failure() -> None:
    """An unparseable no_stubs regex fails its check.

    Defense-in-depth: the parser rejects invalid regex at load time, but
    run_verify still handles one gracefully when called directly.
    """
    spec = VerifySpec(no_stubs=("(",), paths=("a.py",))
    os_env = _FakeOSEnv(read={"a.py": {"content": "x = 1\n", "path": "a.py"}})

    result = await run_verify(spec, os_env)

    assert result.passed is False
    assert "invalid regex" in result.checks[0].detail


async def test_command_detail_truncates_long_output() -> None:
    """A command's verbose output is truncated so the verdict stays compact."""
    spec = VerifySpec(commands=("noisy",))
    os_env = _FakeOSEnv(
        shell={
            "noisy": {
                "exit_code": 0,
                "stdout": "x" * 5000,
                "stderr": "",
                "timed_out": False,
            }
        },
    )

    result = await run_verify(spec, os_env)

    detail = result.checks[0].detail
    assert "…" in detail  # truncated marker
    assert len(detail) < 2000


def test_verify_spec_is_frozen() -> None:
    """VerifySpec is a frozen value object — parsed gates must not be mutated."""
    spec = VerifySpec(commands=("pytest",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.commands = ("ruff",)  # type: ignore[misc]
