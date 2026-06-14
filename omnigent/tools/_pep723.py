"""PEP 723 inline script metadata parser.

Extracts dependency declarations from Python tool files that use the
`PEP 723 <https://peps.python.org/pep-0723/>`_ inline metadata format::

    # /// script
    # dependencies = ["requests>=2.28", "beautifulsoup4"]
    # requires-python = ">=3.10"
    # ///

When dependencies are found, the tool subprocess is invoked via
``uv run --with dep1 --with dep2 -- python _runner.py`` so that
deps are auto-resolved and cached by uv.

Per PEP 723 the metadata block's content is a TOML document, so it is
recovered by stripping the comment prefix from each line and parsed with
``tomllib``. This correctly handles arbitrary field ordering, multi-line
arrays, single/double quoting, and dependency specifiers that contain
``[extras]`` (e.g. ``uvicorn[standard]``) -- none of which an ad-hoc regex
over the raw lines handles reliably.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tomllib


@dataclass(frozen=True)
class InlineMetadata:
    """
    Parsed PEP 723 inline script metadata.

    :param dependencies: List of PEP 508 dependency specifiers,
        e.g. ``["requests>=2.28", "beautifulsoup4"]``.
    """

    dependencies: list[str]


# Matches the opening marker: ``# /// script``
_BLOCK_START_RE = re.compile(r"^#\s*///\s*script\s*$")
# Matches the closing marker: ``# ///``
_BLOCK_END_RE = re.compile(r"^#\s*///\s*$")


def parse_inline_metadata(source: str) -> InlineMetadata | None:
    """
    Extract PEP 723 inline script metadata from Python source.

    Scans for a ``# /// script`` ... ``# ///`` block, recovers the embedded
    TOML document, and reads its ``dependencies`` array. Returns ``None`` if
    no metadata block is found, the block is not valid TOML, or it declares
    no dependencies.

    :param source: The full source text of a Python file.
    :returns: Parsed metadata with dependencies, or ``None``.
    """
    toml_text = _extract_block_toml(source)
    if toml_text is None:
        return None

    try:
        metadata = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError:
        return None

    deps = metadata.get("dependencies")
    if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
        return None
    if not deps:
        return None
    return InlineMetadata(dependencies=deps)


def _extract_block_toml(source: str) -> str | None:
    """
    Recover the TOML document embedded in a ``# /// script`` block.

    Per PEP 723 the metadata content is each block line with its comment
    prefix removed: drop a leading ``# `` (hash + space) when present,
    otherwise drop a leading ``#``. Only a block terminated by ``# ///`` is
    recognized, matching the spec's reference implementation.

    :param source: The full source text of a Python file.
    :returns: The block's TOML text, or ``None`` if no terminated block
        is present.
    """
    in_block = False
    content_lines: list[str] = []

    for line in source.splitlines():
        if not in_block:
            if _BLOCK_START_RE.match(line):
                in_block = True
            continue
        if _BLOCK_END_RE.match(line):
            return "\n".join(content_lines)
        content_lines.append(line[2:] if line.startswith("# ") else line[1:])

    return None
