"""Invariants of the top-level package's lazy re-export tables.

``omnigent/__init__.py`` resolves its public names on first attribute access
(PEP 562) to keep the per-event Claude hook subprocesses cheap. The import-cost
side of that is already guarded in
``tests/test_claude_native_message_display_hook.py``; these tests pin the
*correctness* side, which is where the indirection can regress silently.

Four parallel lists have to agree — the ``TYPE_CHECKING`` stubs,
``_LAZY_EXPORTS``, ``_OPTIONAL_EXPORTS`` and ``__all__``. A name added to one
and forgotten in another still imports cleanly; it only fails when somebody
touches that attribute, and an optional executor's absent-SDK contract only
shows up on a machine that lacks the extra.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import omnigent
from omnigent import _LAZY_EXPORTS, _OPTIONAL_EXPORTS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INIT_SOURCE = _REPO_ROOT / "omnigent" / "__init__.py"


def _run(code: str) -> str:
    """
    Run *code* against this checkout in a clean interpreter, returning stdout.

    A subprocess is required for anything about resolution state: by the time
    this suite runs, pytest has already touched most names, so they sit in the
    package's ``globals()`` and say nothing about a bare ``import omnigent``.

    :param code: Python source to execute.
    :returns: Stripped stdout of the child process.
    """
    preamble = f"import sys\nsys.path.insert(0, {str(_REPO_ROOT)!r})\n"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", preamble + code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"child failed:\n{proc.stderr}"
    return proc.stdout.strip()


def _type_checking_stub_names() -> set[str]:
    """
    Collect the names imported under ``if TYPE_CHECKING:`` in the package init.

    Read from source rather than at runtime, since the block never executes.

    :returns: Every name bound by an import inside the block.
    """
    tree = ast.parse(_INIT_SOURCE.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"):
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.ImportFrom):
                names |= {alias.asname or alias.name for alias in statement.names}
    return names


def test_every_public_name_resolves() -> None:
    """Every name in ``__all__`` is reachable, not just the spot-checked few."""
    unresolved = [name for name in omnigent.__all__ if not hasattr(omnigent, name)]
    assert unresolved == []


def test_the_export_tables_cover_all_and_do_not_overlap() -> None:
    """``__all__`` is exactly the two tables, and no name is in both.

    A name in ``__all__`` but in neither table falls through to the submodule
    branch and dies as an ``AttributeError``; a name in both would resolve
    under the wrong absent-SDK contract.
    """
    assert set(_LAZY_EXPORTS) | set(_OPTIONAL_EXPORTS) == set(omnigent.__all__)
    assert not set(_LAZY_EXPORTS) & set(_OPTIONAL_EXPORTS)


def test_type_checking_stubs_match_the_public_surface() -> None:
    """The ``TYPE_CHECKING`` block re-declares every export, and only those.

    The stubs are what type checkers and IDEs see; a name missing here still
    works at runtime but silently loses its type, which no other test notices.
    """
    assert _type_checking_stub_names() == set(omnigent.__all__)


def test_dir_lists_every_export_before_anything_is_touched() -> None:
    """``dir(omnigent)`` shows the full surface in a fresh interpreter.

    Module ``__getattr__`` is not consulted by ``dir()``. Asserting this
    in-process would be vacuous — pytest has already resolved most names into
    ``globals()``, where ``dir()`` finds them with or without ``__dir__``.
    """
    missing = _run(
        "import omnigent\nprint(','.join(sorted(set(omnigent.__all__) - set(dir(omnigent)))))"
    )
    assert missing == ""


@pytest.mark.parametrize("name", sorted(_OPTIONAL_EXPORTS))
def test_optional_executor_is_none_when_its_sdk_is_missing(name: str) -> None:
    """An optional executor degrades to ``None`` rather than raising.

    Each entry declares its own absent-SDK exceptions, so a machine with the
    extra installed exercises none of them.
    """
    module_path = _OPTIONAL_EXPORTS[name][0]
    resolved = _run(
        f"import sys\nsys.modules[{module_path!r}] = None\nimport omnigent\nprint(omnigent.{name})"
    )
    assert resolved == "None"


def test_required_export_propagates_its_import_error() -> None:
    """A non-optional export must raise, not quietly resolve to ``None``."""
    raised = _run(
        "import sys\nsys.modules['omnigent.inner.datamodel'] = None\n"
        "import omnigent\n"
        "try:\n"
        "    omnigent.AgentDef\n"
        "except ImportError:\n"
        "    print('ImportError')\n"
    )
    assert raised == "ImportError"


def test_a_submodules_own_missing_dependency_is_not_masked() -> None:
    """A dependency missing *inside* a submodule surfaces as itself.

    The submodule fallback turns ``ModuleNotFoundError`` into ``AttributeError``
    so unknown names behave normally; it must not swallow a genuine broken
    import and report the submodule as absent.
    """
    raised = _run(
        "import sys\nsys.modules['httpx'] = None\n"
        "import omnigent\n"
        "try:\n"
        "    omnigent.model_catalog\n"
        "except ModuleNotFoundError as exc:\n"
        "    print(exc.name)\n"
        "except AttributeError:\n"
        "    print('masked')\n"
    )
    assert raised == "httpx"


def test_unknown_attribute_still_raises_attribute_error() -> None:
    """Names outside the tables keep normal module semantics."""
    with pytest.raises(AttributeError, match="no attribute 'NotAnExport'"):
        _ = omnigent.NotAnExport  # type: ignore[attr-defined]
