"""Structural guards that keep the Devin harness liftable and one-directional.

The Devin package is shaped to become a community harness plugin
(``omnigent.community.harness.devin``) if we ever move it out of core, and the
generic ACP layer is shaped to never know Devin exists. Both properties are easy
to erode with one convenient import, so they are asserted mechanically here
rather than left to review.

Imports are read from the AST, so a docstring cross-reference between layers is
fine — only real dependencies count.
"""

from __future__ import annotations

import ast
import pathlib

import omnigent

_OMNIGENT_ROOT = pathlib.Path(omnigent.__file__).parent
_DEVIN_PKG = _OMNIGENT_ROOT / "inner" / "devin"

# What a lifted ``omnigent-devin`` package could still import from core. The ACP
# executor/wrap entries are the deliberate coupling: reusing Omnigent's ACP
# client is why this package is ~150 lines instead of the ~750 a from-scratch
# community ACP harness carries (cf. ``omnigent-rovo``). Everything else here is
# the plugin contract's own public surface.
_ALLOWED_CORE_IMPORTS = frozenset(
    {
        "omnigent.inner.acp_executor",
        "omnigent.inner.acp_extension",
        "omnigent.inner.acp_harness",
        "omnigent.inner.acp_subagents",
        "omnigent.inner.executor",
        "omnigent.runtime.harnesses._executor_adapter",
    }
)

# The generic ACP layer. None of it may depend on a vendor package.
_GENERIC_ACP_MODULES = (
    _OMNIGENT_ROOT / "inner" / "acp_executor.py",
    _OMNIGENT_ROOT / "inner" / "acp_extension.py",
    _OMNIGENT_ROOT / "inner" / "acp_harness.py",
    _OMNIGENT_ROOT / "inner" / "acp_subagents.py",
)


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Return every dotted name *path* imports, from its AST.

    ``from X import Y`` is ambiguous statically — ``Y`` may be a submodule or a
    symbol — so both ``X`` and ``X.Y`` are recorded and :func:`_is_allowed`
    resolves the ambiguity against the allowlist.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _is_allowed(name: str, allowed: frozenset[str]) -> bool:
    """Whether a dotted import name resolves within the allowed module set.

    Accepts an allowed module itself, a symbol imported from one
    (``…acp_subagents.SubAgentStart``), and a parent package on the way to one
    (``omnigent.inner`` from ``from omnigent.inner import acp_harness``) — none of
    which is a dependency beyond the allowed module.
    """
    return any(
        name == mod or name.startswith(f"{mod}.") or mod.startswith(f"{name}.") for mod in allowed
    )


def test_devin_package_only_imports_the_liftable_core_surface() -> None:
    """Devin imports nothing from core that a community plugin could not.

    **What breaks if this fails**: lifting Devin out stops being a move — the new
    import drags in core internals (the server, runner, registry, or stores) that
    a plugin cannot reach, and the extraction turns into a rewrite.
    """
    modules = sorted(_DEVIN_PKG.glob("*.py"))
    assert modules, f"no modules found under {_DEVIN_PKG}"

    offenders: dict[str, set[str]] = {}
    for path in modules:
        core = {
            name
            for name in _imported_modules(path)
            if name.split(".")[0] == "omnigent"
            and not name.startswith("omnigent.inner.devin")
            and not _is_allowed(name, _ALLOWED_CORE_IMPORTS)
        }
        if core:
            offenders[path.name] = core

    assert not offenders, (
        f"Devin imports core modules outside the liftable surface: {offenders}. "
        f"Either route the need through AcpExtension, or add the module to "
        f"_ALLOWED_CORE_IMPORTS with a note on how a lifted package would satisfy it."
    )


def test_generic_acp_layer_does_not_import_a_vendor() -> None:
    """The generic ACP modules never import a vendor package.

    **What breaks if this fails**: the vendor coupling the extension seam exists
    to contain is back in the shared path, so every ACP agent pays for it and
    lifting the vendor out breaks core.
    """
    offenders = {
        path.name: sorted(n for n in _imported_modules(path) if "devin" in n)
        for path in _GENERIC_ACP_MODULES
    }
    offenders = {name: found for name, found in offenders.items() if found}

    assert not offenders, f"generic ACP modules import vendor code: {offenders}"


def test_devin_wrap_exposes_the_harness_entry_point() -> None:
    """The package exports ``create_app()`` — the whole harness-module contract.

    Same requirement a community plugin's ``harness_modules`` target must meet,
    so the wrap already satisfies the plugin contract as written.
    """
    from omnigent.inner.devin import harness

    assert callable(harness.create_app)
    assert harness.create_app.__code__.co_argcount == 0
