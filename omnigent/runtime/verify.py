"""Deterministic verification gates.

A :class:`~omnigent.spec.types.VerifySpec` declares quality checks that run
as code and produce a hard PASS/FAIL verdict. This is distinct from the
policy engine, which only answers ALLOW/ASK/DENY and cannot run
subprocesses — the policy registry bans ``subprocess`` handlers by design,
so a "did this work pass its gates?" verdict has nowhere to live there.
Checks run through the same
:class:`~omnigent.inner.os_env.OSEnvironment` that ``sys_os_shell``
already uses, so they inherit the existing sandbox and
``ask_on_os_tools`` gating rather than spawning a parallel execution path.

This module is the foundation primitive: it runs the checks and returns a
verdict. Wiring that verdict into the agent loop (and driving retry or
cross-vendor failover from a FAIL) is a separate, opt-in follow-up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omnigent.inner.os_env import OSEnvironment
from omnigent.spec.types import VerifySpec

# Keep check detail short so a verbose failing command does not bloat the
# verdict transcript that downstream tooling reads.
_DETAIL_TAIL_CHARS = 1_500


@dataclass(frozen=True)
class CheckResult:
    """The outcome of a single verification check."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifyResult:
    """The aggregate outcome of running a :class:`VerifySpec`."""

    passed: bool
    checks: tuple[CheckResult, ...] = ()


def _tail(text: str | None, limit: int = _DETAIL_TAIL_CHARS) -> str:
    """Return the trailing slice of *text*, marked when truncated."""
    text = text or ""
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


async def run_verify(spec: VerifySpec, os_env: OSEnvironment) -> VerifyResult:
    """Run every check in *spec* against *os_env* and return the verdict.

    Commands run via ``os_env.shell`` (the same path ``sys_os_shell``
    uses); ``no_stubs`` patterns are matched against file contents read via
    ``os_env.read``. ``VerifyResult.passed`` is the conjunction of all
    checks; an empty spec (no checks) passes vacuously.
    """
    checks: list[CheckResult] = []
    combined_stdout: list[str] = []

    for index, command in enumerate(spec.commands):
        result = await os_env.shell(command)
        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        exit_code = result.get("exit_code")
        timed_out = bool(result.get("timed_out"))
        # The real OSEnvironment.shell sets ``error`` on non-zero exit and
        # timeout but still returns stdout — fall through so stdout feeds the
        # content checks. Only error-without-completion (bad input, OSError) is fatal.
        completed = exit_code is not None or timed_out
        if result.get("error") is not None and not completed:
            checks.append(
                CheckResult(
                    name=f"command[{index}]",
                    passed=False,
                    detail=f"shell error: {result['error']}",
                )
            )
            continue
        combined_stdout.append(stdout)
        passed = exit_code == 0 and not timed_out
        status = "timed out" if timed_out else f"exit {exit_code}"
        checks.append(
            CheckResult(
                name=f"command[{index}]",
                passed=passed,
                detail=f"{status}: {command}\n{_tail(stderr or stdout)}",
            )
        )

    stdout_blob = "\n".join(combined_stdout)
    for index, needle in enumerate(spec.contains):
        found = needle in stdout_blob
        checks.append(
            CheckResult(
                name=f"contains[{index}]",
                passed=found,
                detail=("found " if found else "missing ") + repr(needle),
            )
        )
    for index, needle in enumerate(spec.not_contains):
        absent = needle not in stdout_blob
        checks.append(
            CheckResult(
                name=f"not_contains[{index}]",
                passed=absent,
                detail=("absent " if absent else "present ") + repr(needle),
            )
        )

    for index, pattern in enumerate(spec.no_stubs):
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            checks.append(
                CheckResult(
                    name=f"no_stubs[{index}]",
                    passed=False,
                    detail=f"invalid regex {pattern!r}: {exc}",
                )
            )
            continue
        matched: list[str] = []
        read_errors: list[str] = []
        for path in spec.paths:
            read_result = await os_env.read(path)
            if read_result.get("error") is not None:
                read_errors.append(f"{path}: {read_result['error']}")
                continue
            match = regex.search(read_result.get("content") or "")
            if match is not None:
                matched.append(path)
        if read_errors:
            checks.append(
                CheckResult(
                    name=f"no_stubs[{index}]",
                    passed=False,
                    detail="; ".join(read_errors),
                )
            )
        else:
            checks.append(
                CheckResult(
                    name=f"no_stubs[{index}]",
                    passed=not matched,
                    detail=(
                        f"clean {pattern!r}"
                        if not matched
                        else f"{pattern!r} matched: {', '.join(matched)}"
                    ),
                )
            )

    return VerifyResult(passed=all(c.passed for c in checks), checks=tuple(checks))
