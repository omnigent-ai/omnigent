"""Flag unconditional ``pytest.mark.skip`` on tests.

Per CLAUDE.md and the omnigent-testing skill, skipped tests are
invisible coverage loss — they silently rot and nobody remembers to
unskip them. If a test can't pass, rewrite it or delete it. Never
defer broken tests behind ``@pytest.mark.skip``.

What this catches
-----------------

- ``@pytest.mark.skip`` as a decorator (with or without args)
- ``pytestmark = pytest.mark.skip(...)`` as module-level skip
- ``pytest.skip(...)`` called as a statement at module scope
- ``pytest.mark.skip`` imported under an alias (e.g. ``from pytest
  import mark; @mark.skip``) — detected conservatively when the
  attribute chain resolves to ``skip``.

What this does NOT catch
------------------------

- ``@pytest.mark.skipif(condition, reason=...)``. Conditional skip
  is legitimate when the condition tests an environmental
  precondition (missing binary, wrong platform, missing optional
  dep). Flagging it uniformly would produce too many false
  positives; reviewers judge each ``skipif`` on whether the
  condition is a real precondition or a hack to hide a broken
  test.

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_skip_attribute(node: ast.expr) -> bool:
    """Return True if ``node`` spells ``pytest.mark.skip`` (or ``mark.skip``).

    Conservative: follows the ``.skip`` attribute access. Any chain
    whose leaf attribute is ``skip`` and whose root resolves to
    ``pytest`` or ``mark`` is flagged.
    """
    if not isinstance(node, ast.Attribute):
        return False
    if node.attr != "skip":
        return False
    # Walk back up the chain.
    base = node.value
    while isinstance(base, ast.Attribute):
        if base.attr == "mark":
            return True
        base = base.value
    if isinstance(base, ast.Name) and base.id in ("pytest", "mark"):
        return True
    return False


def _decorator_calls_skip(decorator: ast.expr) -> bool:
    """Return True if ``decorator`` is ``@pytest.mark.skip`` or ``@...skip(...)``."""
    if _is_skip_attribute(decorator):
        return True
    if isinstance(decorator, ast.Call) and _is_skip_attribute(decorator.func):
        return True
    return False


def _is_module_level_skip_assignment(node: ast.stmt) -> bool:
    """Return True if ``node`` is ``pytestmark = pytest.mark.skip(...)``.

    Assigning to ``pytestmark`` at module scope applies the mark to
    every test collected from the file — the nuclear option for
    skipping.
    """
    if not isinstance(node, ast.Assign):
        return False
    if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
        return False
    return _decorator_calls_skip(node.value)


def _is_module_level_skip_call(node: ast.stmt) -> bool:
    """Return True if ``node`` is a bare ``pytest.skip(...)`` call.

    A ``pytest.skip()`` call at module scope during collection skips
    the entire module. (Inside a test body it's a conditional skip
    expression — not what we flag; we only care about module-level
    or top-of-class uses.)
    """
    if not isinstance(node, ast.Expr):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "skip":
        base = func.value
        if isinstance(base, ast.Name) and base.id == "pytest":
            return True
    return False


def scan(path: Path) -> list[tuple[int, str]]:
    """
    Return ``[(lineno, message), ...]`` for each skip site in ``path``.

    :param path: File to scan.
    :returns: Hits with line numbers and descriptive messages.
    """
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []

    hits: list[tuple[int, str]] = []

    # Decorator-level skips on test functions / classes.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if _decorator_calls_skip(decorator):
                    hits.append(
                        (
                            decorator.lineno,
                            f"`@pytest.mark.skip` on `{node.name}`: skipped tests "
                            "rot invisibly; rewrite or delete",
                        ),
                    )

    # Module-level `pytestmark = pytest.mark.skip(...)` and bare
    # `pytest.skip(...)` calls at module scope.
    for stmt in tree.body:
        if _is_module_level_skip_assignment(stmt):
            hits.append(
                (
                    stmt.lineno,
                    "`pytestmark = pytest.mark.skip(...)` at module scope skips "
                    "every test in the file",
                ),
            )
        elif _is_module_level_skip_call(stmt):
            hits.append(
                (
                    stmt.lineno,
                    "`pytest.skip(...)` at module scope skips the entire module during collection",
                ),
            )

    return hits


def main(argv: list[str]) -> int:
    """
    Scan every file in ``argv[1:]`` and report unconditional skips.

    :param argv: Command-line args (``argv[0]`` is the script name).
    :returns: Exit code — 0 on clean scan, 1 if any hits.
    """
    failed = False
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        hits = scan(path)
        if hits:
            failed = True
            for line, msg in hits:
                sys.stdout.write(f"{path}:{line}: {msg}\n")
    if failed:
        sys.stdout.write(
            "\nSkipped tests are invisible coverage loss. If a test can't "
            "pass, rewrite it for the current architecture or delete it. "
            "For genuine environmental gates (missing binary, platform), "
            "use ``@pytest.mark.skipif`` with a clear reason — this rule "
            "only flags unconditional ``skip``.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
