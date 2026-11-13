"""Flag tests that sleep a fixed time and then assert as if the wait guaranteed it.

A fixed ``time.sleep`` / ``asyncio.sleep`` / ``page.wait_for_timeout`` followed
within a few statements by a plain assertion stands in for "the other thread,
task or process has done its work by now". On a loaded CI runner it has not,
and the test fails. Waiting on the condition itself (``poll_until`` /
``poll_until_async`` in ``tests/_helpers/polling.py``) passes as soon as it is
true and fails with a clear timeout otherwise.

Not flagged: sleeps inside loops (polling), sleeps followed by Playwright
``expect(...).to_*()`` (it retries by itself), and sleeps followed only by absence
checks (``assert not ...``, ``== 0``, ``is None``, ``not in``, ``assert_not_called``),
which need a fixed window. A presence check after an absence check is still flagged.

Existing cases are grandfathered per file in ``sleep_then_assert_baseline.json``.
A file may not exceed its count; a file below its count must lower it, so fixes
ratchet the baseline down. Intentional cases carry
``# custom-lint: disable=sleep-then-assert -- <reason>`` on the sleep line.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from dev.lint._framework import disabled_rules_by_line

RULE_NAME = "sleep-then-assert"
HINT = (
    "Wait for the condition instead of a fixed time, e.g.\n"
    "`await poll_until_async(lambda: <condition>, timeout_s=5)` from tests/_helpers/polling.py.\n"
    "If the fixed window is intentional, add\n"
    "`# custom-lint: disable=sleep-then-assert -- <reason>` on the sleep line.\n"
    "After fixing grandfathered cases, lower that file's count in\n"
    "dev/lint/sleep_then_assert_baseline.json."
)
BASELINE = Path(__file__).with_name("sleep_then_assert_baseline.json")
SCANNED_ROOTS = ("tests/", "integrations/")
SLEEP_CALLS = frozenset({"sleep", "wait_for_timeout"})
# asyncio.sleep(delay=...) and page.wait_for_timeout(timeout=...).
DURATION_KEYWORDS = frozenset({"delay", "timeout"})
ABSENCE_CALLS = frozenset({"assert_not_called", "assert_not_awaited"})
EMPTY_VALUES = (0, False, None, "", (), [], {})
LOOKAHEAD = 3


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
    return None


def _is_fixed_sleep(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Expr) or _call_name(statement.value) not in SLEEP_CALLS:
        return False
    call = statement.value.value if isinstance(statement.value, ast.Await) else statement.value
    durations = [
        *call.args[:1],
        *(keyword.value for keyword in call.keywords if keyword.arg in DURATION_KEYWORDS),
    ]
    return any(isinstance(arg, ast.Constant) and bool(arg.value) for arg in durations)


def _is_empty_literal(node: ast.AST) -> bool:
    try:
        return ast.literal_eval(node) in EMPTY_VALUES
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def _checks_absence(test: ast.expr) -> bool:
    # `a and b` checks absence only when every operand does.
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return all(_checks_absence(value) for value in test.values)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        operator, right = test.ops[0], test.comparators[0]
        if isinstance(operator, ast.NotIn):
            return True
        if isinstance(operator, (ast.Eq, ast.Is)) and _is_empty_literal(right):
            return True
    return False


def _is_expect_assertion(node: ast.AST) -> bool:
    """Match ``expect(...).to_*()`` / ``expect(...).not_to_*()``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith(("to_", "not_to_"))
        and _call_name(node.func.value) == "expect"
    )


def _assertion_kind(statement: ast.stmt) -> str | None:
    """Classify a statement as a "presence", "absence" or "waiting" assertion, or None."""
    if isinstance(statement, ast.Assert):
        return "absence" if _checks_absence(statement.test) else "presence"
    if any(_is_expect_assertion(node) for node in ast.walk(statement)):
        return "waiting"  # Playwright's expect() retries until its own timeout.
    names = {_call_name(node) for node in ast.walk(statement)} - {None}
    if names & ABSENCE_CALLS:
        return "absence"
    if any(name.startswith("assert_") for name in names):
        return "presence"
    return None


def _blocks(node: ast.AST, in_loop: bool = False):
    """Yield every statement list not nested in a loop."""
    loop = isinstance(node, (ast.For, ast.AsyncFor, ast.While))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        in_loop = False
    for field in ("body", "orelse", "finalbody", "handlers"):
        value = getattr(node, field, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            if not (in_loop or loop):
                yield value
    for child in ast.iter_child_nodes(node):
        yield from _blocks(child, in_loop or loop)


def sleep_then_assert_lines(source: str) -> list[int]:
    """Return the line of each fixed sleep that an instant presence assertion relies on."""
    disabled = disabled_rules_by_line(source)
    lines = []
    for block in _blocks(ast.parse(source)):
        for index, statement in enumerate(block):
            if not _is_fixed_sleep(statement) or RULE_NAME in disabled.get(statement.lineno, ()):
                continue
            for follower in block[index + 1 : index + 1 + LOOKAHEAD]:
                kind = _assertion_kind(follower)
                if _is_fixed_sleep(follower) or kind == "waiting":
                    break  # A new wait: later assertions rely on it, not on this sleep.
                if kind == "presence":
                    lines.append(statement.lineno)
                    break
                # An absence check needs the window, but a later presence check still relies on it.
    return sorted(lines)


def compare_with_baseline(found: dict[str, list[int]], baseline: dict[str, int]) -> list[str]:
    """Report files above their grandfathered count, and files whose count can be lowered."""
    violations = []
    for name, lines in sorted(found.items()):
        allowed = baseline.get(name, 0)
        if len(lines) > allowed:
            where = ", ".join(str(line) for line in lines)
            violations.append(
                f"{name}:{lines[0]}: {len(lines)} fixed sleep(s) before an instant assert "
                f"(lines {where}); {allowed} grandfathered"
            )
    for name, allowed in sorted(baseline.items()):
        remaining = len(found.get(name, []))
        if remaining < allowed:
            violations.append(
                f"{name}: {remaining} sleep-then-assert case(s) left but {allowed} grandfathered; "
                f"lower its count in {BASELINE.name}"
            )
    return violations


def _file_lines(name: str) -> tuple[str, list[int]]:
    try:
        source = Path(name).read_text()
        # Most test files never sleep; skip parsing them.
        if "sleep(" not in source and "wait_for_timeout(" not in source:
            return name, []
        return name, sleep_then_assert_lines(source)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return name, []


def check() -> list[str]:
    """Compare every tracked test file's sleep-then-assert cases with the baseline."""
    output = subprocess.check_output(["git", "ls-files", "-z", *SCANNED_ROOTS])
    names = [
        name
        for name in sorted(filter(None, output.decode().split("\0")))
        if name.endswith(".py") and "/tests/" in f"/{name}"
    ]
    found = {name: lines for name, lines in map(_file_lines, names) if lines}
    return compare_with_baseline(found, json.loads(BASELINE.read_text()))


if __name__ == "__main__":
    found = check()
    for message in found:
        print(message)
    sys.exit(1 if found else 0)
