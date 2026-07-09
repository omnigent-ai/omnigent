"""Count lines in Python files under a folder."""

from __future__ import annotations

import sys
from pathlib import Path


def count_lines(path: Path) -> int | None:
    """Return the number of lines in path, or None if it cannot be read."""
    try:
        count = 0
        saw_data = False
        last_byte = b""
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                count += chunk.count(b"\n")
                saw_data = True
                last_byte = chunk[-1:]
        if saw_data and last_byte != b"\n":
            count += 1
        return count
    except OSError as exc:
        print(f"Skipping {path}: {exc}", file=sys.stderr)
    return None


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    if not folder.is_dir():
        print(f"Error: {folder} is not an existing directory", file=sys.stderr)
        return 1

    total = 0

    for directory, directory_names, filenames in folder.walk(follow_symlinks=False):
        directory_names.sort()
        for filename in sorted(filenames):
            path = directory / filename
            if path.suffix != ".py":
                continue

            line_count = count_lines(path)
            if line_count is None:
                continue

            print(f"{path}: {line_count}")
            total += line_count

    print(f"Total: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
