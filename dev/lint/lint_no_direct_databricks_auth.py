"""Reject direct SDK authentication outside the credential broker."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_ALLOWED = Path("omnigent/databricks_auth_broker.py")


def main() -> int:
    violations: list[str] = []
    for path in Path("omnigent").rglob("*.py"):
        if path == _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "authenticate"
            ):
                violations.append(
                    f"{path}:{node.lineno}: use token_for_config(), not .authenticate()"
                )
    if violations:
        print("Direct credential refresh bypasses machine-wide serialization:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
