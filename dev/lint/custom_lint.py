"""Single entry point for the repo's custom (project-specific) lint rules.

One pre-commit hook (``custom-lint``) runs this, so adding a new project lint
rule no longer means adding another hook — just register it in :data:`RULES`.

A rule is a module in ``dev/lint/`` exposing:

* ``RULE_NAME: str`` — the short hyphenated id shown in output.
* ``check() -> list[str]`` — runs the rule over its own surface and returns one
  human-readable ``path:line: message`` per violation (empty list == clean).
* ``HINT: str`` (optional) — one paragraph of fix guidance printed after that
  rule's violations.

Each rule owns its own file scope inside ``check()`` (via ``git ls-files``), so
this runner takes no filenames and always evaluates the full surface. It exits
non-zero if any rule reports a violation.

To add a rule: write ``dev/lint/lint_<name>.py`` with the interface above and
append it to :data:`RULES`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from dev.lint import lint_workspace_scoped_cache


@dataclass(frozen=True)
class Rule:
    """One registered custom lint rule."""

    name: str
    check: Callable[[], list[str]]
    hint: str = ""


# Registered rules, run in order. lint_workspace_scoped_cache is the first.
RULES: list[Rule] = [
    Rule(
        name=lint_workspace_scoped_cache.RULE_NAME,
        check=lint_workspace_scoped_cache.check,
        hint=lint_workspace_scoped_cache.HINT,
    ),
]


def main() -> int:
    """Run every registered rule; return 1 if any reported a violation."""
    failed = False
    for rule in RULES:
        violations = rule.check()
        if not violations:
            continue
        failed = True
        sys.stdout.write(f"\n[{rule.name}] {len(violations)} violation(s):\n")
        for message in violations:
            sys.stdout.write(f"  {message}\n")
        if rule.hint:
            sys.stdout.write(f"\n{rule.hint}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
