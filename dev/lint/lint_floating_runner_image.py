"""Flag GitHub Actions runner labels that float to whatever image GitHub ships next.

``ubuntu-latest`` and friends move to each new OS image GitHub ships; the
announced move to ubuntu-26.04 still broke every Playwright job at once.
Workflows name an explicit image such as ``ubuntu-24.04`` instead. Every YAML
string is checked, including labels inside ``${{ }}`` expressions, except
script and prose keys like ``run``.
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
    "GitHub repoints `*-latest` runner labels at each new OS image it ships.\n"
    "Name the image explicitly, e.g. `runs-on: ubuntu-24.04`, and move it\n"
    "deliberately once the workflow is known to work on the newer image."
)

FLOATING_LABEL = re.compile(r"(?<![\w-])(?:ubuntu|windows|macos)-latest(?:-x?large)?(?![\w-])")
PROSE_KEYS = frozenset({"run", "script", "name", "description"})
SCANNED_ROOT = ".github/"


def _scalars(node: yaml.Node | None, key: str | None = None) -> Iterator[yaml.ScalarNode]:
    """Yield every scalar node that is not the value of a script or prose key."""
    if isinstance(node, yaml.ScalarNode):
        if key not in PROSE_KEYS:
            yield node
    elif isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            yield from _scalars(key_node)
            child_key = key_node.value if isinstance(key_node, yaml.ScalarNode) else None
            yield from _scalars(value_node, child_key)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _scalars(item, key)


def floating_labels(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, label)`` for each floating runner label in a YAML document."""
    hits = {
        (scalar.start_mark.line + 1, label)
        for scalar in _scalars(yaml.compose(source, Loader=yaml.SafeLoader))
        for label in FLOATING_LABEL.findall(scalar.value)
    }
    return sorted(hits)


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
