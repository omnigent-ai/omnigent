"""Guard: the client SDK's public barrel resolves its exports lazily.

``omnigent_client/__init__.py`` re-exports 50 names from 15 submodules.
Two of those submodules (``_sessions``, ``_sse``) import
:mod:`omnigent.server.schemas`, whose Pydantic models cost ~290ms to
build. Because importing any submodule first executes the package
``__init__``, an eager barrel made ``from omnigent_client._http import
is_loopback_url`` — a URL helper used on the CLI launch path — pay for
the entire client plus the server's schemas.

These tests pin both halves of the contract: the lazy barrel exposes
exactly what the eager one did, and importing a light submodule stays
light.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import omnigent_client

# Importing this submodule must not drag in the heavy graph below it.
_LIGHT_SUBMODULE = "omnigent_client._http"
_MUST_NOT_LOAD = (
    "fastapi",
    "omnigent.server.schemas",
    "omnigent_client._client",
    "omnigent_client._sessions",
    "omnigent_client._sse",
    "pydantic",
)


def _import_in_fresh_interpreter(statement: str, report: str) -> str:
    """
    Run *statement* in a clean interpreter and echo *report*.

    A subprocess is used rather than :mod:`importlib` so the assertion
    sees a pristine ``sys.modules`` — an earlier test in the same
    session may already have imported the heavy graph.

    :param statement: Code to execute first, e.g. ``"import x"``.
    :param report: Expression whose ``print`` output is returned.
    :returns: The subprocess's stripped stdout.
    """
    proc = subprocess.run(
        [sys.executable, "-c", f"{statement}\nimport sys\nprint({report})"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_importing_a_light_submodule_does_not_load_the_server_schemas() -> None:
    """The CLI's URL-helper import must not build the whole client."""
    loaded = _import_in_fresh_interpreter(
        f"from {_LIGHT_SUBMODULE} import is_loopback_url",
        f"sorted(m for m in {_MUST_NOT_LOAD!r} if m in sys.modules)",
    )
    assert loaded == "[]", f"{_LIGHT_SUBMODULE} pulled in heavy modules: {loaded}"


def test_every_exported_name_resolves() -> None:
    """``__getattr__`` must cover all of ``__all__`` — no dead exports."""
    unresolvable = [name for name in omnigent_client.__all__ if not hasattr(omnigent_client, name)]
    assert unresolvable == []


def test_export_table_matches_all() -> None:
    """The submodule table and ``__all__`` must not drift apart."""
    mapped = {name for names in omnigent_client._EXPORTS.values() for name in names}
    assert mapped == set(omnigent_client.__all__)
    # A one-name group written without its trailing comma would be a
    # plain string here, and would iterate as characters.
    non_tuples = [mod for mod, names in omnigent_client._EXPORTS.items() if not isinstance(names, tuple)]
    assert non_tuples == []


def test_dir_lists_the_public_surface() -> None:
    """``dir()`` stays useful without importing every submodule."""
    assert dir(omnigent_client) == sorted(omnigent_client.__all__)


def test_unknown_attribute_still_raises() -> None:
    """A typo must not silently return ``None``."""
    with pytest.raises(AttributeError, match="no attribute 'NoSuchExport'"):
        omnigent_client.NoSuchExport  # noqa: B018
