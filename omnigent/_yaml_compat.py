"""Prefer libyaml for safe YAML loading, with a pure-Python fallback.

Reparse structural errors with SafeLoader to restore source-line and caret
diagnostics without adding work to successful parses."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    # CSafeLoader is a sibling of SafeLoader (shared SafeConstructor/Resolver),
    # and mypy cannot subclass a runtime-chosen value, so the pure-Python
    # loader stands in as the static base for both.
    SafeLoaderBase = yaml.SafeLoader
else:
    SafeLoaderBase = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# True when the libyaml-backed parser is in use. Exposed for tests that
# need to report which base they exercised.
USING_LIBYAML = SafeLoaderBase is not yaml.SafeLoader

# Structural failures raised before any tag resolution or constructor runs.
# Both parsers raise these same classes, so the SafeLoader reparse in
# :func:`load` is a faithful substitution.
_PARSE_STAGE_ERRORS = (
    yaml.reader.ReaderError,
    yaml.scanner.ScannerError,
    yaml.parser.ParserError,
    yaml.composer.ComposerError,
)

_BOOL_TAG = "tag:yaml.org,2002:bool"
# YAML 1.2 spellings only — the 1.1 aliases keyed on o/O/y/Y/n/N are dropped.
_YAML_1_2_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def narrow_bools_to_yaml_1_2(loader: type[yaml.SafeLoader]) -> None:
    """Keep on/off/yes/no as strings and true/false as booleans.

    Apply only to a dedicated SafeLoader or CSafeLoader subclass. Both parsers
    use the Python resolver; copying its table leaves other YAML callers intact.

    :raises TypeError: If *loader* is the shared base rather than a subclass."""
    if loader is yaml.SafeLoader or loader is SafeLoaderBase:
        raise TypeError("narrow a dedicated SafeLoader/CSafeLoader subclass, not the shared base")
    # Copy before mutating: the resolver dict on BaseResolver is shared by
    # SafeLoader and CSafeLoader, so an in-place edit would strip bool parsing
    # from every yaml.safe_load caller in the process.
    loader.yaml_implicit_resolvers = {
        key: [(tag, regexp) for tag, regexp in value if tag != _BOOL_TAG]
        for key, value in loader.yaml_implicit_resolvers.items()
    }
    # mypy flags BaseResolver.add_implicit_resolver as untyped (PyYAML ships
    # no stubs for this classmethod); it is the only way to register one.
    loader.add_implicit_resolver(  # type: ignore[no-untyped-call]
        _BOOL_TAG,
        _YAML_1_2_BOOL_RE,
        list("tTfF"),
    )


def _named_mark(mark: yaml.Mark | None, name: str) -> yaml.Mark | None:
    # libyaml's marks are read-only extension objects; rebuild them as the
    # pure-Python Mark, keeping the position and any excerpt buffer.
    if mark is None:
        return None
    pointer = getattr(mark, "pointer", None)
    return yaml.Mark(
        name,
        mark.index,
        mark.line,
        mark.column,
        getattr(mark, "buffer", None),
        0 if pointer is None else pointer,  # unused without a buffer
    )


def _name_source(error: yaml.YAMLError, name: str | None) -> None:
    """Point *error* at *name* instead of PyYAML's ``<unicode string>``.

    Parsing the text (not a named stream) keeps the source excerpt in marks."""
    if name is None:
        return
    if isinstance(error, yaml.reader.ReaderError):
        error.name = name
    elif isinstance(error, yaml.MarkedYAMLError):
        error.problem_mark = _named_mark(error.problem_mark, name)
        error.context_mark = _named_mark(error.context_mark, name)


# YAML documents are open-ended trees whose shape is only known after the
# caller's isinstance checks, so the return type is the same ``Any`` that
# ``yaml.safe_load`` itself returns.
def load(text: str, loader: type[yaml.SafeLoader], *, name: str | None = None) -> Any:  # type: ignore[explicit-any]
    """Parse text with loader, retaining detailed structural-error diagnostics.

    Retry only reader, scanner, parser and composer errors with stock
    SafeLoader. Tag constructors have not run at those stages; constructor
    errors must retain the original loader diagnosis. The retry only improves
    the message: a document that libyaml alone rejects still fails, because
    stock SafeLoader lacks the caller's resolver narrowing and so cannot stand
    in for a successful parse.

    :param name: Source name for error marks, e.g. the file path. ``None``
        keeps PyYAML's ``<unicode string>``.
    :returns: Parsed document, or None for empty input.
    :raises yaml.YAMLError: If parsing fails."""
    try:
        return yaml.load(text, Loader=loader)
    except _PARSE_STAGE_ERRORS as fast_error:
        _name_source(fast_error, name)
        if not USING_LIBYAML:
            raise
        try:
            yaml.load(text, Loader=yaml.SafeLoader)
        except _PARSE_STAGE_ERRORS as detailed_error:
            _name_source(detailed_error, name)
            # Same failure, better message. Drop the libyaml error from the
            # chain so the traceback shows one diagnosis, not two.
            raise detailed_error from None
        except Exception:  # noqa: BLE001 - any other outcome is not our failure
            # The parsers disagree on where the document breaks (``!]`` fails
            # libyaml's scanner but pure-Python's constructor); that diagnosis
            # describes a different problem and must not be substituted.
            pass
        # The retry either succeeded or failed elsewhere. Either way libyaml's
        # error is the accurate one; surface it rather than pretending the
        # document was fine or reporting an unrelated failure.
        raise fast_error from None
    except yaml.MarkedYAMLError as error:
        # Constructor errors keep their own diagnosis; only the source name changes.
        _name_source(error, name)
        raise


def safe_load(text: str, *, name: str | None = None) -> Any:  # type: ignore[explicit-any]
    """Prefer libyaml while preserving yaml.safe_load YAML 1.1 semantics.

    For YAML 1.2 booleans, subclass SafeLoaderBase and narrow its resolver.
    Returns None for empty input."""
    return load(text, SafeLoaderBase, name=name)
