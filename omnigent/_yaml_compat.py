"""Prefer libyaml for safe YAML loading, with a pure-Python fallback.

Reparse structural errors with SafeLoader to restore source-line and caret
diagnostics without adding work to successful parses."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    # ``CSafeLoader`` is not a subclass of ``SafeLoader`` — the two are
    # siblings sharing ``SafeConstructor`` and ``Resolver``. mypy also
    # cannot subclass a value chosen at runtime, so the pure-Python
    # loader stands in as the static base for both.
    SafeLoaderBase = yaml.SafeLoader
else:
    SafeLoaderBase = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# True when the libyaml-backed parser is in use. Exposed for tests that
# need to report which base they exercised.
USING_LIBYAML = SafeLoaderBase is not yaml.SafeLoader

# Failures raised while reading the document's structure, before any tag is
# resolved or any constructor runs. Both parsers raise these same classes,
# and at this stage a loader's resolver and constructor tables cannot have
# influenced the outcome — which is what makes the reparse in :func:`load`
# a faithful substitution.
_PARSE_STAGE_ERRORS = (
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
    use the Python resolver; copying its table leaves other YAML callers intact."""
    # Copy before mutating. ``yaml_implicit_resolvers`` lives on PyYAML's
    # shared ``BaseResolver``, so the same dict object backs SafeLoader and
    # CSafeLoader alike; an in-place edit would strip bool parsing from
    # every yaml.safe_load caller in the process.
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


# YAML documents are open-ended trees whose shape is only known after the
# caller's isinstance checks, so the return type is the same ``Any`` that
# ``yaml.safe_load`` itself returns.
def load(text: str, loader: type[yaml.SafeLoader]) -> Any:  # type: ignore[explicit-any]
    """Parse text with loader, retaining detailed structural-error diagnostics.

    Retry only scanner, parser and composer errors with stock SafeLoader. Tag
    constructors have not run at those stages; constructor errors must retain
    the original loader diagnosis. Text input allows the retry to reread it.

    :returns: Parsed document, or None for empty input.
    :raises yaml.YAMLError: If parsing fails."""
    try:
        return yaml.load(text, Loader=loader)
    except _PARSE_STAGE_ERRORS as fast_error:
        if not USING_LIBYAML:
            raise
        try:
            yaml.load(text, Loader=yaml.SafeLoader)
        except _PARSE_STAGE_ERRORS as detailed_error:
            # Same failure, better message. Drop the libyaml error from the
            # chain so the traceback shows one diagnosis, not two.
            raise detailed_error from None
        except Exception:  # noqa: BLE001 - any other outcome is not our failure
            # The two parsers disagree about where the document breaks: ``!] ``
            # fails libyaml's scanner but scans clean for pure-Python, which
            # then dies in the constructor. That diagnosis describes a
            # different problem, so it must not be substituted.
            pass
        # The retry either succeeded or failed elsewhere. Either way libyaml's
        # error is the accurate one; surface it rather than pretending the
        # document was fine or reporting an unrelated failure.
        raise fast_error from None


def safe_load(text: str) -> Any:  # type: ignore[explicit-any]
    """Prefer libyaml while preserving yaml.safe_load YAML 1.1 semantics.

    For YAML 1.2 booleans, subclass SafeLoaderBase and narrow its resolver.
    Returns None for empty input."""
    return load(text, SafeLoaderBase)
