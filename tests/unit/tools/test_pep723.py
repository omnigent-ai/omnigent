"""Tests for omnigent.tools._pep723 (PEP 723 inline metadata parser)."""

from __future__ import annotations

from omnigent.tools._pep723 import InlineMetadata, parse_inline_metadata


def test_parse_with_dependencies() -> None:
    """
    A source file with a ``# /// script`` block containing
    ``dependencies = [...]`` returns the parsed dep list.
    """
    source = """\
# /// script
# dependencies = ["requests>=2.28", "beautifulsoup4"]
# requires-python = ">=3.10"
# ///

SCHEMA = {}
async def run(args):
    return "ok"
"""
    result = parse_inline_metadata(source)
    assert result is not None
    assert result == InlineMetadata(
        dependencies=["requests>=2.28", "beautifulsoup4"],
    )


def test_parse_no_metadata_block() -> None:
    """
    A source file without a ``# /// script`` block returns None.
    """
    source = """\
SCHEMA = {}
async def run(args):
    return "ok"
"""
    assert parse_inline_metadata(source) is None


def test_parse_empty_dependencies() -> None:
    """
    A metadata block with an empty dependency list returns None
    (no deps to install).
    """
    source = """\
# /// script
# dependencies = []
# ///
"""
    assert parse_inline_metadata(source) is None


def test_parse_single_quotes() -> None:
    """
    Dependencies with single quotes are parsed correctly.
    """
    source = """\
# /// script
# dependencies = ['httpx', 'pydantic>=2.0']
# ///
"""
    result = parse_inline_metadata(source)
    assert result is not None
    assert result.dependencies == ["httpx", "pydantic>=2.0"]


def test_parse_block_without_dependencies_key() -> None:
    """
    A metadata block that exists but has no ``dependencies``
    key returns None.
    """
    source = """\
# /// script
# requires-python = ">=3.10"
# ///
"""
    assert parse_inline_metadata(source) is None


def test_parse_dependencies_after_requires_python() -> None:
    """
    PEP 723 imposes no field ordering, so ``dependencies`` is parsed
    even when it is not the first line of the block (e.g. when
    ``requires-python`` precedes it, as in the spec's own examples).
    """
    source = """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.28", "beautifulsoup4"]
# ///

SCHEMA = {}
async def run(args):
    return "ok"
"""
    result = parse_inline_metadata(source)
    assert result is not None
    assert result == InlineMetadata(
        dependencies=["requests>=2.28", "beautifulsoup4"],
    )


def test_parse_multiline_dependencies_after_requires_python() -> None:
    """
    A multi-line ``dependencies`` array that follows ``requires-python``
    (the canonical PEP 723 layout) is parsed correctly.
    """
    source = """\
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests<3",
#   "rich",
# ]
# ///
"""
    result = parse_inline_metadata(source)
    assert result is not None
    assert result.dependencies == ["requests<3", "rich"]


def test_parse_dependencies_with_extras() -> None:
    """
    Dependency specifiers containing PEP 508 ``[extras]`` (e.g.
    ``uvicorn[standard]``) are parsed in full, including the bracketed
    extras and any trailing version constraint.
    """
    source = """\
# /// script
# dependencies = ["requests[security]>=2.0", "uvicorn[standard]"]
# ///
"""
    result = parse_inline_metadata(source)
    assert result is not None
    assert result.dependencies == ["requests[security]>=2.0", "uvicorn[standard]"]


def test_parse_multiline_dependencies_with_extras() -> None:
    """
    A multi-line dependency array whose specifiers carry ``[extras]`` is
    parsed correctly (the array's closing ``]`` is not confused with the
    ``]`` that closes an extras group).
    """
    source = """\
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic[email]>=2",
#   "fastapi[all]",
# ]
# ///
"""
    result = parse_inline_metadata(source)
    assert result is not None
    assert result.dependencies == ["pydantic[email]>=2", "fastapi[all]"]


def test_parse_malformed_toml_returns_none() -> None:
    """
    A metadata block whose content is not valid TOML degrades gracefully
    to ``None`` rather than raising, so tool discovery is never broken by
    a malformed inline-metadata block.
    """
    source = """\
# /// script
# dependencies = ["unterminated
# ///
"""
    assert parse_inline_metadata(source) is None
