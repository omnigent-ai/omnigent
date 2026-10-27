"""Flag GitHub Actions runner labels that float to whatever image GitHub ships next.

``ubuntu-latest`` and friends move to a new OS image without notice; a move to
ubuntu-26.04 broke every Playwright job at once. Workflows name an explicit
image such as ``ubuntu-24.04`` instead. Values are matched exactly, so ``runs-on``
labels, matrix entries and input defaults are checked but prose is not.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import yaml

RULE_NAME = "floating-runner-image"
HINT = (
    "GitHub repoints `*-latest` runner labels at new OS images without notice.\n"
    "Name the image explicitly, e.g. `runs-on: ubuntu-24.04`, and move it\n"
    "deliberately once the workflow is known to work on the newer image."
)

FLOATING_LABEL = re.compile(r"^(?:ubuntu|windows|macos)-latest$")
SCANNED_ROOT = ".github/"


def _strings(node: object) -> Iterator[str]:
    """Yield every string key and value in a parsed YAML document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def floating_labels(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, label)`` for each floating runner label in a YAML document."""
    labels = {value for value in _strings(yaml.safe_load(source)) if FLOATING_LABEL.match(value)}
    hits = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0]
        hits.extend(
            (lineno, label)
            for label in sorted(labels)
            if re.search(rf"(?<![\w-]){label}(?![\w-])", code)
        )
    return hits


def check() -> list[str]:
    """Report floating runner labels across tracked workflow and action files."""
    output = subprocess.check_output(["git", "ls-files", "-z", SCANNED_ROOT])
    violations = []
    for name in sorted(filter(None, output.decode().split("\0"))):
        if not name.endswith((".yml", ".yaml")):
            continue
        for lineno, label in floating_labels(Path(name).read_text()):
            violations.append(f"{name}:{lineno}: floating runner image `{label}`")
    return violations


if __name__ == "__main__":
    found = check()
    for message in found:
        print(message)
    sys.exit(1 if found else 0)
