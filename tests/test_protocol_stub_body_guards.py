"""Guard: ``typing.Protocol`` method stubs use ``...``, never ``pass``.

A Protocol method has no implementation by construction — the class *is* the
contract. ``...`` and ``pass`` behave identically, but only ``...`` reads as a
stub, and static analysis reports a bare ``pass`` body as an unexplained empty
method. The package already writes ~150 stubs as ``...``.

This is a source-level invariant, so the check is a static walk rather than a
per-module test: the next Protocol added by copy-paste must not regress it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OMNIGENT = _REPO_ROOT / "omnigent"

# Caches and third-party trees, not our sources.
_SKIPPED_DIRS = frozenset({"__pycache__", "node_modules", "_vendor", "vendor", ".venv"})


def _is_protocol_class(node: ast.ClassDef) -> bool:
    """
    Whether a class declares ``Protocol`` among its bases.

    Accepts the bare name, the ``typing.Protocol`` attribute form, and the
    generic ``Protocol[T]`` subscript.

    :param node: The class definition to inspect.
    :returns: ``True`` when the class is a Protocol.
    """
    for base in node.bases:
        if isinstance(base, ast.Subscript):
            base = base.value
        if isinstance(base, ast.Name) and base.id == "Protocol":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "Protocol":
            return True
    return False


def _body_is_only_pass(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """
    Whether a method's body is nothing but ``pass``.

    A leading docstring is stripped first, so ``...``, a docstring, a docstring
    followed by ``...`` and ``raise NotImplementedError`` all pass the guard.

    :param node: The method to inspect.
    :returns: ``True`` when only ``pass`` remains.
    """
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def _pass_bodied_protocol_stubs(path: Path) -> list[str]:
    """
    Find Protocol methods in one module whose body is only ``pass``.

    :param path: Module to scan.
    :returns: ``file:line method`` descriptions, one per offending stub.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = path.relative_to(_REPO_ROOT)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_protocol_class(node):
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _body_is_only_pass(member):
                offenders.append(f"{relative}:{member.lineno} {node.name}.{member.name}")
    return offenders


def _source_modules() -> list[Path]:
    """
    Every first-party module under ``omnigent/``.

    :returns: Sorted module paths, caches and vendored trees excluded.
    """
    return sorted(
        path for path in _OMNIGENT.rglob("*.py") if not _SKIPPED_DIRS.intersection(path.parts)
    )


def test_protocol_method_stubs_use_ellipsis_not_pass() -> None:
    """No Protocol method body may be a bare ``pass``.

    ``pass`` is flagged as an unexplained empty method; ``...`` is the
    convention the rest of the package already follows.
    """
    modules = _source_modules()
    assert modules, "the guard found no modules at all — has the package moved?"

    offenders = [stub for module in modules for stub in _pass_bodied_protocol_stubs(module)]

    assert not offenders, (
        "Protocol method stubs with a bare `pass` body:\n  "
        + "\n  ".join(offenders)
        + "\nA `pass` body is reported as an unexplained empty method. Use `...`, "
        "the repo convention for Protocol stubs."
    )
