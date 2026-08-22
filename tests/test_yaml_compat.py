"""Resolver behaviour must survive the libyaml-backed loader base.

``omnigent._yaml_compat.SafeLoaderBase`` is ``yaml.CSafeLoader`` wherever
PyYAML was built against libyaml, and ``yaml.SafeLoader`` otherwise. The two
parsers are different C/Python implementations, so every guarantee the spec
loaders rely on is asserted here against whichever base is actually active.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
import yaml

from omnigent._yaml_compat import USING_LIBYAML, SafeLoaderBase, load, safe_load
from omnigent.inner.loader import _OmnigentYamlLoader
from omnigent.spec.parser import _ConfigYamlLoader

# Both spec loaders narrow the bool resolver the same way, so they carry the
# same expectations.
_NARROWED_LOADERS = (_ConfigYamlLoader, _OmnigentYamlLoader)

# ``on``/``off``/``yes``/``no`` are YAML 1.1 bool aliases. The policy system
# keys on ``on:``, so they must stay strings; ``true``/``false`` must not.
_YAML_1_2_BOOLS = """\
a: on
b: off
c: yes
d: no
e: true
f: false
"""


def test_base_is_libyaml_when_available() -> None:
    """The base tracks libyaml availability rather than being hard-coded."""
    assert SafeLoaderBase is getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert USING_LIBYAML is (SafeLoaderBase is not yaml.SafeLoader)


def test_narrowed_loaders_build_on_the_active_base() -> None:
    """Both spec loaders subclass the shared base, not a hard-coded loader."""
    for loader in _NARROWED_LOADERS:
        assert issubclass(loader, SafeLoaderBase), loader


def test_narrowed_loaders_keep_yaml_1_1_bool_aliases_as_strings() -> None:
    """``on``/``off``/``yes``/``no`` stay strings; ``true``/``false`` stay bools.

    This is the one behaviour the C parser could plausibly break — libyaml
    scans tokens itself, and only calls back into the Python resolver to tag
    them. If that callback ever stopped honouring the subclass's resolver
    table, ``on:`` would silently become the key ``True`` and every policy
    selector in the repo would break.
    """
    for loader in _NARROWED_LOADERS:
        parsed = yaml.load(_YAML_1_2_BOOLS, Loader=loader)
        assert parsed == {
            "a": "on",
            "b": "off",
            "c": "yes",
            "d": "no",
            "e": True,
            "f": False,
        }, loader
        # Bools must be real bools, not the strings "true"/"false".
        assert parsed["e"] is True, loader
        assert parsed["f"] is False, loader


def test_narrowed_loaders_own_their_resolver_table() -> None:
    """Narrowing must not mutate the resolver dict shared by every loader.

    ``yaml_implicit_resolvers`` lives on PyYAML's ``BaseResolver`` and is the
    same object for ``SafeLoader`` and ``CSafeLoader`` alike, so an in-place
    edit would strip bool parsing from every ``yaml.safe_load`` caller in the
    process.
    """
    for loader in _NARROWED_LOADERS:
        assert loader.yaml_implicit_resolvers is not SafeLoaderBase.yaml_implicit_resolvers
        assert loader.yaml_implicit_resolvers is not yaml.SafeLoader.yaml_implicit_resolvers
    assert yaml.safe_load("on") is True
    assert yaml.safe_load("false") is False


def test_safe_load_matches_yaml_safe_load() -> None:
    """The drop-in keeps stock ``safe_load`` semantics, YAML 1.1 bools included."""
    source = _YAML_1_2_BOOLS + "g: 2026-07-24\nh: [1, 2]\ni: null\n"
    assert safe_load(source) == yaml.safe_load(source)
    # Unlike the narrowed loaders, this one still resolves the 1.1 aliases.
    assert safe_load("on") is True


def test_safe_load_handles_empty_and_non_mapping_documents() -> None:
    """Callers isinstance-check the result, so the empty cases must match."""
    for source in ("", "# comment only\n", "- a\n- b\n", "just a string\n"):
        assert safe_load(source) == yaml.safe_load(source), source


# A description with an unquoted colon — the classic hand-authoring typo.
_BROKEN = "name: agent\ndescription: has: unquoted colon\n"


def test_parse_errors_stay_marked_yaml_errors() -> None:
    """Call sites catch ``yaml.YAMLError`` and report the mark's line/column.

    ``diagnose_yaml_rejection`` promises the user a location, so the exception
    type and the mark must survive whichever parser produced them.
    """
    for parse in (safe_load, lambda s: load(s, _ConfigYamlLoader)):
        with pytest.raises(yaml.YAMLError) as excinfo:
            parse(_BROKEN)
        exc = excinfo.value
        assert isinstance(exc, yaml.MarkedYAMLError)
        assert exc.problem_mark is not None
        assert exc.problem_mark.line == 1
        assert exc.problem_mark.column == 16


def test_load_keeps_the_source_excerpt_in_parse_errors() -> None:
    """A failed parse must still echo the offending line and caret.

    libyaml reports line/column but drops that echo, which is the part that
    makes a typo obvious. :func:`load` reparses failures with the pure-Python
    scanner so the diagnostic never regresses — the whole reason spec parsing
    can take the fast path at all.
    """
    for parse in (safe_load, lambda s: load(s, _ConfigYamlLoader)):
        with pytest.raises(yaml.YAMLError) as excinfo:
            parse(_BROKEN)
        message = str(excinfo.value)
        assert "description: has: unquoted colon" in message, message
        assert "^" in message, message
        if USING_LIBYAML:
            # The retry must not print the libyaml attempt above the real
            # diagnosis — one traceback, one error. Without libyaml nothing
            # is retried, so there is no chained attempt to suppress.
            assert excinfo.value.__cause__ is None
            assert excinfo.value.__suppress_context__ is True


def test_load_does_not_reparse_documents_that_succeed() -> None:
    """The retry is failure-only, so the hot path pays nothing for it."""
    calls: list[object] = []
    real_load = yaml.load

    def counting_load(stream: object, Loader: object) -> object:
        calls.append(Loader)
        return real_load(stream, Loader=Loader)  # type: ignore[arg-type]

    yaml.load = counting_load  # type: ignore[assignment]
    try:
        assert load("a: 1\n", _ConfigYamlLoader) == {"a": 1}
    finally:
        yaml.load = real_load  # type: ignore[assignment]
    assert calls == [_ConfigYamlLoader]


def test_load_does_not_substitute_constructor_errors() -> None:
    """A loader's own constructor error must reach the caller intact.

    The reparse only ever runs stock ``SafeLoader``, which knows nothing of
    a caller's custom tags. Retrying a constructor failure would swap a
    precise error for the stock loader's unrelated "could not determine a
    constructor for the tag" — so construction-stage failures are not
    retried at all.
    """

    class _CustomLoader(_ConfigYamlLoader):  # type: ignore[valid-type,misc]
        pass

    def _reject(loader: object, node: object) -> object:
        raise yaml.constructor.ConstructorError(None, None, "tag !secret is not permitted here")

    _CustomLoader.add_constructor("!secret", _reject)

    with pytest.raises(yaml.constructor.ConstructorError) as excinfo:
        load("token: !secret abc\n", _CustomLoader)
    message = str(excinfo.value)
    assert "tag !secret is not permitted here" in message, message
    assert "could not determine a constructor" not in message, message


# Inputs the two parsers classify into different stages: libyaml rejects the
# tag while scanning, pure-Python scans it clean and then fails in the
# constructor. The retry must not swap one diagnosis for the other.
_STAGE_DIVERGENT = ("!] ", "!]", "!!] x", "a: !] b\n")


@pytest.mark.skipif(
    not USING_LIBYAML,
    reason="no libyaml here, so there is no second parser to disagree with — "
    "pure-Python's constructor error is then the document's genuine diagnosis",
)
@pytest.mark.parametrize("source", _STAGE_DIVERGENT)
def test_load_keeps_libyaml_error_when_the_parsers_disagree(source: str) -> None:
    """A retry that fails somewhere else entirely must not replace the original.

    ``!]`` is a scanner error for libyaml but a constructor error for the
    pure-Python parser. Reporting the latter would point the user at a
    "could not determine a constructor" problem the document does not have.
    """
    with pytest.raises(yaml.scanner.ScannerError) as excinfo:
        load(source, _ConfigYamlLoader)
    exc = excinfo.value
    assert "could not determine a constructor" not in str(exc), str(exc)
    # One diagnosis, not a chain of two.
    assert exc.__suppress_context__ is True
    assert exc.__cause__ is None


def test_pure_python_fallback_when_libyaml_is_absent() -> None:
    """A PyYAML built without libyaml must still import and resolve correctly.

    Runs in a subprocess that deletes ``yaml.CSafeLoader`` before omnigent is
    imported, which is the only way to exercise the fallback branch: the base
    is chosen once at import time.
    """
    script = textwrap.dedent(
        """
        import yaml

        # Simulate a source install without libyaml. Guarded because this
        # test must also pass where libyaml is genuinely absent — the very
        # environment it exists to cover.
        if hasattr(yaml, "CSafeLoader"):
            del yaml.CSafeLoader

        from omnigent._yaml_compat import USING_LIBYAML, SafeLoaderBase
        from omnigent.inner.loader import _OmnigentYamlLoader
        from omnigent.spec.parser import _ConfigYamlLoader

        assert SafeLoaderBase is yaml.SafeLoader, SafeLoaderBase
        assert USING_LIBYAML is False

        src = "a: on\\nb: off\\nc: yes\\nd: no\\ne: true\\nf: false\\n"
        expected = {"a": "on", "b": "off", "c": "yes", "d": "no", "e": True, "f": False}
        for loader in (_ConfigYamlLoader, _OmnigentYamlLoader):
            assert issubclass(loader, yaml.SafeLoader), loader
            got = yaml.load(src, Loader=loader)
            assert got == expected, (loader, got)
            assert got["e"] is True and got["f"] is False, loader
            assert (
                loader.yaml_implicit_resolvers
                is not yaml.SafeLoader.yaml_implicit_resolvers
            )

        assert yaml.safe_load("on") is True
        assert yaml.safe_load("false") is False
        print("OK")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
