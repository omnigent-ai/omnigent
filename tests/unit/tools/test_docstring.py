"""Tests for the Google-style docstring parser used by ``@tool``."""

from __future__ import annotations

import pytest
from omnigent_client.tools._docstring import ParsedDocstring, parse_google_docstring


def test_parse_empty_docstring() -> None:
    """Empty input returns empty description and no params."""
    result = parse_google_docstring("")
    assert result == ParsedDocstring(description="", param_descriptions={})


def test_parse_none_docstring_via_empty_string() -> None:
    """The parser is documented to treat falsy doc as empty."""
    result = parse_google_docstring("")
    assert result.description == ""
    assert result.param_descriptions == {}


def test_parse_description_only() -> None:
    """A docstring with no Args section returns just the description."""
    doc = """
    Calculate the sum of two numbers.

    Multi-paragraph descriptions are joined with double newlines.
    """
    result = parse_google_docstring(doc)
    assert result.description == (
        "Calculate the sum of two numbers.\n\n"
        "Multi-paragraph descriptions are joined with double newlines."
    )
    assert result.param_descriptions == {}


def test_parse_google_args_section() -> None:
    """Args section produces per-param descriptions."""
    doc = """
    Compute something.

    Args:
        x: The first operand.
        y: The second operand.
    """
    result = parse_google_docstring(doc)
    assert result.description == "Compute something."
    assert result.param_descriptions == {
        "x": "The first operand.",
        "y": "The second operand.",
    }


def test_parse_arguments_synonym() -> None:
    """``Arguments:`` is treated identically to ``Args:``."""
    doc = """
    Top desc.

    Arguments:
        a: Description of a.
    """
    result = parse_google_docstring(doc)
    assert result.param_descriptions == {"a": "Description of a."}


def test_parse_parameters_synonym() -> None:
    """``Parameters:`` is treated identically to ``Args:``."""
    doc = """
    Top.

    Parameters:
        z: zee.
    """
    result = parse_google_docstring(doc)
    assert result.param_descriptions == {"z": "zee."}


def test_parse_google_args_with_types_in_parens() -> None:
    """The ``(type)`` part of param entries is stripped from the name."""
    doc = """
    Compute.

    Args:
        x (int): An integer parameter.
        y (str | None): A nullable string.
    """
    result = parse_google_docstring(doc)
    # The descriptions should include only the right-of-colon text.
    # The name is extracted from the part before the parens.
    assert result.param_descriptions == {
        "x": "An integer parameter.",
        "y": "A nullable string.",
    }


def test_parse_google_args_multi_line_descriptions() -> None:
    """Continuation lines join the previous param's description."""
    doc = """
    Top.

    Args:
        x: First line of x's description
            and second line that continues it.
        y: Single-line y description.
    """
    result = parse_google_docstring(doc)
    # Continuation lines join with single space; whitespace collapsed.
    assert (
        result.param_descriptions["x"]
        == "First line of x's description and second line that continues it."
    )
    assert result.param_descriptions["y"] == "Single-line y description."


def test_parse_google_args_followed_by_returns_section() -> None:
    """Args parsing stops at the next section header."""
    doc = """
    Compute.

    Args:
        x: The input.
        y: The other input.

    Returns:
        The sum of x and y.
    """
    result = parse_google_docstring(doc)
    # The Returns: section content should NOT leak into the y description.
    assert result.param_descriptions == {
        "x": "The input.",
        "y": "The other input.",
    }
    # Description excludes the Returns: section entirely.
    assert result.description == "Compute."


def test_parse_args_section_with_no_entries() -> None:
    """An empty Args: section produces no param entries."""
    doc = """
    Top.

    Args:

    Returns:
        nothing.
    """
    result = parse_google_docstring(doc)
    assert result.param_descriptions == {}


@pytest.mark.parametrize(
    "header",
    ["Args:", "Arguments:", "Parameters:"],
)
def test_parse_recognizes_all_args_headers(header: str) -> None:
    """All three accepted header forms produce the same parsed param."""
    doc = f"""
    Top.

    {header}
        a: A description.
    """
    result = parse_google_docstring(doc)
    assert result.param_descriptions == {"a": "A description."}


def test_parse_skips_malformed_param_lines() -> None:
    """Lines without a colon or with non-identifier names are ignored."""
    doc = """
    Top.

    Args:
        valid_one: This is valid.
        not-an-identifier: This isn't valid.
        also-bad
        valid_two: This is the second valid one.
    """
    result = parse_google_docstring(doc)
    # Non-identifier names are skipped; valid ones survive.
    # Note: "also-bad" has no colon, so it's skipped.
    assert "valid_one" in result.param_descriptions
    assert "valid_two" in result.param_descriptions
    assert result.param_descriptions["valid_one"] == "This is valid."
    assert result.param_descriptions["valid_two"] == "This is the second valid one."
    # Confirm the malformed names did NOT appear under any sanitized form.
    assert "not-an-identifier" not in result.param_descriptions
    assert "also-bad" not in result.param_descriptions
