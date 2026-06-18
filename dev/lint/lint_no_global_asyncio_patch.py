"""Flag patches that globally clobber ``asyncio`` module attributes.

What goes wrong
---------------

When test code writes::

    with patch("omnigent.tools.mcp.asyncio.sleep", new_callable=AsyncMock):
        ...

``unittest.mock``'s dotted-path resolver walks:

1. ``import omnigent.tools.mcp``
2. ``getattr(omnigent.tools.mcp, "asyncio")`` -> returns the
   ``asyncio`` module singleton (because production code did
   ``import asyncio`` at module top).
3. ``setattr(asyncio_module, "sleep", mock)`` -> globally clobbers
   ``asyncio.sleep`` for the entire process for the patch's lifetime.

Under ``pytest-xdist``, concurrent tests in other workers that touch
``asyncio.sleep`` (directly or via any of the many helpers built on
top of it) silently get the mock, causing hard-to-debug cross-test
flakiness.

The same bug shape applies to:

- ``monkeypatch.setattr("...asyncio.sleep", ...)`` (pytest)
- ``patch.object(mod.asyncio, "sleep", ...)``

What this catches
-----------------

1. ``patch("...asyncio.<name>", ...)`` / ``mock.patch(...)`` where the
   first positional arg is a string literal containing ``.asyncio.``
   or ending in ``.asyncio``.
2. ``monkeypatch.setattr("...asyncio.<name>", ...)`` with the same
   string-literal shape.
3. ``patch.object(<expr>, "<name>", ...)`` where ``<expr>`` is an
   attribute access whose leaf attribute is ``asyncio``
   (e.g. ``patch.object(mcp_mod.asyncio, "sleep", ...)``).

Correct shapes
--------------

Option A (preferred, when production has few sleep sites): add a
thin ``_sleep`` indirection in the production module and patch
``module._sleep`` from tests. The real ``asyncio.sleep`` is never
touched.

Option B (when A is too invasive): replace the ``asyncio`` binding
in the target module's namespace with a ``SimpleNamespace`` that
mirrors the real module but with the desired attribute swapped.
The real ``asyncio`` module is left alone.

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_global_asyncio_literal(value: str) -> bool:
    """Return True if ``value`` is a one-segment getattr off the
    stdlib ``asyncio`` module name (the shape that clobbers the
    singleton).

    The dangerous shape is::

        patch("<pkg>.<mod>.asyncio.<leaf>", ...)

    where ``<leaf>`` is exactly one segment. The dotted-path resolver
    imports ``<pkg>.<mod>``, calls ``getattr(_, "asyncio")`` -- which
    returns the stdlib ``asyncio`` module singleton because the
    consumer did ``import asyncio`` -- then sets ``<leaf>`` on it.

    NOT flagged:

    * ``"<pkg>.asyncio.<sub>.<leaf>"`` -- here ``asyncio`` is a
      subpackage name (e.g. ``websockets.asyncio.client``) and the
      resolver descends *through* a package directory, not through
      a getattr on the stdlib module.
    * ``"<pkg>.asyncio"`` -- the bare module path is used to *replace*
      the binding (the safe Option B pattern), not to clobber an
      attribute inside it.

    :param value: The dotted-path string passed to ``patch`` /
        ``monkeypatch.setattr``.
    :returns: ``True`` if the path has exactly one segment after the
        last ``.asyncio.``.
    """
    marker = ".asyncio."
    idx = value.rfind(marker)
    if idx == -1:
        return False
    tail = value[idx + len(marker) :]
    # Exactly one segment after `.asyncio.` -> getattr on the stdlib
    # module singleton. Two or more segments -> the path descends
    # through a subpackage named `asyncio`.
    return bool(tail) and "." not in tail


def _is_patch_func(func: ast.expr) -> bool:
    """Return True if ``func`` spells ``patch`` or ``mock.patch``.

    Plain ``patch.object`` is handled separately -- this only matches
    the dotted-path form ``patch("...")`` / ``mock.patch("...")``.
    """
    if isinstance(func, ast.Name) and func.id == "patch":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "patch":
        return True
    return False


def _is_patch_object(func: ast.expr) -> bool:
    """Return True if ``func`` spells ``patch.object`` or ``mock.patch.object``."""
    if not isinstance(func, ast.Attribute) or func.attr != "object":
        return False
    inner = func.value
    if isinstance(inner, ast.Name) and inner.id == "patch":
        return True
    if isinstance(inner, ast.Attribute) and inner.attr == "patch":
        return True
    return False


def _is_monkeypatch_setattr(func: ast.expr) -> bool:
    """Return True if ``func`` spells ``<monkeypatch>.setattr``.

    Conservative: matches any attribute access where the leaf is
    ``setattr`` and the receiver is a ``Name`` containing
    ``monkeypatch`` (case-insensitive). This covers the common
    ``monkeypatch.setattr(...)`` and ``m.setattr(...)`` (from
    ``with monkeypatch.context() as m:``).

    :param func: The function-position expression of a ``Call``.
    :returns: ``True`` if this looks like ``monkeypatch.setattr``.
    """
    if not isinstance(func, ast.Attribute) or func.attr != "setattr":
        return False
    base = func.value
    if isinstance(base, ast.Name):
        return "monkeypatch" in base.id.lower() or base.id == "m"
    return False


def _attribute_ends_in_asyncio(node: ast.expr) -> bool:
    """Return True if ``node`` is an attribute access ending in ``.asyncio``.

    e.g. matches ``mcp_mod.asyncio`` and ``omnigent.tools.mcp.asyncio``.
    """
    return isinstance(node, ast.Attribute) and node.attr == "asyncio"


def _check_string_literal_first_arg(node: ast.Call) -> bool:
    """Return True if ``node.args[0]`` is a string literal flagged by
    :func:`_is_global_asyncio_literal`."""
    if not node.args:
        return False
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return False
    return _is_global_asyncio_literal(first.value)


def scan(path: Path) -> list[tuple[int, str]]:
    """
    Return ``[(lineno, snippet), ...]`` for each violation in ``path``.

    :param path: File to scan.
    :returns: One entry per flagged call site.
    """
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []
    source_lines = source.splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_flagged_call(node):
            continue
        line = node.lineno
        snippet = source_lines[line - 1].strip() if 0 < line <= len(source_lines) else ""
        hits.append((line, snippet))
    return hits


def _is_flagged_call(node: ast.Call) -> bool:
    """Return True if ``node`` is one of the banned patch shapes.

    Covers three call shapes:

    1. ``patch("...asyncio.<name>", ...)`` / ``mock.patch(...)``.
    2. ``monkeypatch.setattr("...asyncio.<name>", ...)``.
    3. ``patch.object(<expr ending in .asyncio>, "<name>", ...)``.

    :param node: A ``Call`` node from the AST.
    :returns: ``True`` if the call matches a banned shape.
    """
    # patch("...asyncio.<name>", ...) / mock.patch("...asyncio.<name>", ...)
    if _is_patch_func(node.func) and _check_string_literal_first_arg(node):
        return True
    # monkeypatch.setattr("...asyncio.<name>", ...)
    if _is_monkeypatch_setattr(node.func) and _check_string_literal_first_arg(node):
        return True
    # patch.object(<...>.asyncio, "<name>", ...)
    if (
        _is_patch_object(node.func)
        and len(node.args) >= 1
        and _attribute_ends_in_asyncio(node.args[0])
    ):
        return True
    return False


def main(argv: list[str]) -> int:
    """
    Scan every file in ``argv[1:]`` and report violations.

    :param argv: Command-line args (``argv[0]`` is the script name).
    :returns: Exit code -- 0 on clean scan, 1 if any hits.
    """
    failed = False
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        hits = scan(path)
        if hits:
            failed = True
            for line, snippet in hits:
                sys.stdout.write(
                    f"{path}:{line}: globally-clobbering asyncio patch detected: {snippet}\n"
                )
                sys.stdout.write(
                    "   hint: see dev/lint/lint_no_global_asyncio_patch.py "
                    "docstring for correct shapes.\n"
                )
    if failed:
        sys.stdout.write(
            "\nPatching ``module.asyncio.sleep`` (or any other asyncio "
            "attribute via a dotted path that walks through the asyncio "
            "module) mutates the real asyncio module singleton, which "
            "leaks into every other test running in the same process "
            "(critical under pytest-xdist). Prefer adding a thin "
            "``_sleep`` indirection in the production module and "
            "patching that, or replace the module's ``asyncio`` binding "
            "wholesale with a ``SimpleNamespace`` -- see the script "
            "docstring for Option A / Option B examples.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
