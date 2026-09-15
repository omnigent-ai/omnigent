"""libyaml-backed YAML loading, with a pure-Python fallback.

PyYAML ships two implementations of the same safe loader: the pure-Python
``SafeLoader`` and ``CSafeLoader``, which wraps libyaml. Both accept the
same grammar, share the same constructor and resolver classes, and build
identical Python objects — the C one is just far faster. Parsing the
19.2 KB ``examples/polly/config.yaml`` takes 3.14 ms pure-Python and
0.18 ms through libyaml.

Spec and config parsing is the bulk of the cost of loading an agent bundle,
so those loaders build on the C parser where it is available. PyYAML wheels
bundle libyaml, but a source install built without the libyaml headers
exposes no ``CSafeLoader`` at all — hence the fallback.

libyaml reports the line and column of a syntax error but not the echoed
source line and caret that the pure-Python parser prints. :func:`load`
restores those by reparsing failed documents, so the speedup costs nothing
in authoring diagnostics.
"""

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
    """Stop *loader* resolving ``on``/``off``/``yes``/``no`` as booleans.

    Default PyYAML follows YAML 1.1, where those spellings are bools — a
    trap for our specs, whose policy system uses ``on:`` as the selector
    field. Without this, ``on: [request]`` yields a dict keyed by ``True``.
    ``true``/``false`` keep resolving as bools.

    Safe on either base: libyaml scans tokens itself but calls back into
    the Python resolver to tag them, so the narrowed table applies to the
    C parser too.

    :param loader: A ``SafeLoader``/``CSafeLoader`` subclass to narrow
        in place. Must be a dedicated subclass, never a PyYAML loader
        itself — see the copy below.
    """
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
    """Parse *text* with *loader*, keeping pure-Python error detail.

    libyaml's messages carry the line and column but drop the echoed
    source line and caret, which are what make a typo in a hand-written
    config obvious. A document that failed to parse is already off the
    hot path, so reparsing it with the pure-Python scanner costs nothing
    in the success case and restores the better message.

    Only :data:`_PARSE_STAGE_ERRORS` are retried, and the retry uses stock
    ``SafeLoader`` rather than a pure-Python twin of *loader*. Those errors
    are raised before any tag is resolved, so *loader*'s resolver and
    constructor tables cannot have affected whether — or where — the
    document failed. Constructor errors are left alone: a loader carrying
    its own constructors would see its real error replaced by an unrelated
    "could not determine a constructor" from the stock loader.

    :param text: The YAML source. Must be text rather than a stream, so
        the retry can reread it.
    :param loader: Loader class to parse with, typically built on
        :data:`SafeLoaderBase`.
    :returns: The parsed document, or ``None`` for an empty string.
    :raises yaml.YAMLError: If *text* is not valid YAML.
    """
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
    """Drop-in ``yaml.safe_load`` that prefers the libyaml parser.

    Resolver and constructor behaviour match ``yaml.safe_load`` exactly,
    including YAML 1.1 booleans (``on``/``off``/``yes``/``no``). Callers
    that need YAML 1.2 boolean semantics subclass :data:`SafeLoaderBase`
    and apply :func:`narrow_bools_to_yaml_1_2`.

    :param text: The YAML source.
    :returns: The parsed document, or ``None`` for an empty string.
    """
    return load(text, SafeLoaderBase)
