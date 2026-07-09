"""Count lines in Python files under a folder."""

from __future__ import annotations

import sys
from pathlib import Path


def count_lines(path: Path) -> int | None:
    """Return the number of lines in path, or None if it cannot be read."""
    try:
        with path.open("r", encoding="utf-8") as file:
            return sum(1 for _ in file)
    except OSError as exc:
        print(f"Skipping {path}: {exc}", file=sys.stderr)
    except UnicodeError as exc:
        print(f"Skipping {path}: {exc}", file=sys.stderr)
    return None


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    total = 0

    for path in sorted(folder.rglob("*.py")):
        line_count = count_lines(path)
        if line_count is None:
            continue

        print(f"{path}: {line_count}")
        total += line_count

    print(f"Total: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
