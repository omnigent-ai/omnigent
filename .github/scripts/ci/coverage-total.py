"""Reuse coverage.py's formatted total instead of analyzing every file again."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def main(markdown_path: Path) -> int:
    # Read only the TOTAL row; coverage.py already applies precision and rounding.
    totals = re.findall(
        r"^\|\s*\*\*TOTAL\*\*\s*\|[^\n]*?\|\s*\*\*(\d+(?:\.\d+)?)%\*\*\s*\|",
        markdown_path.read_text(),
        re.MULTILINE,
    )
    if len(totals) == 1 and 0 <= float(totals[0]) <= 100:
        print(totals[0])
        return 0

    # A changed Markdown format can use the original, slower reporting path.
    print("Unrecognized coverage total; regenerating with coverage.py.", file=sys.stderr)
    return subprocess.call(
        [sys.executable, "-m", "coverage", "report", "--format=total", "--ignore-errors"]
    )


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
