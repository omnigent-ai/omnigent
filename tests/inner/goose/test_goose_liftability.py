"""Structural guards that keep the Goose harness liftable and one-directional.

The mirror of :mod:`tests.inner.devin.test_devin_liftability` for Goose: the
package is shaped to become a community harness plugin
(``omnigent.community.harness.goose``) if we move it out of core, and the generic
ACP layer is shaped to never know Goose exists. Both properties are easy to erode
with one convenient import, so they are asserted mechanically rather than left to
review.

Goose's allowlist is one entry wider than Devin's: its launch quirks mean the wrap
resolves the ``goose`` binary through ``harness_startup_config`` instead of taking
a command from a catalog row.

Imports are read from the AST, so a docstring cross-reference between layers is
fine — only real dependencies count.
"""

from __future__ import annotations

import ast
import pathlib

import omnigent

_OMNIGENT_ROOT = pathlib.Path(omnigent.__file__).parent
_GOOSE_PKG = _OMNIGENT_ROOT / "inner" / "goose"

# What a lifted ``omnigent-goose`` package could still import from core. The ACP
# executor/extension entries are the deliberate coupling: reusing Omnigent's ACP
# client is why this package is ~370 lines rather than the ~750 a from-scratch
# community ACP harness carries (cf. ``omnigent-rovo``). ``harness_startup_config``
# is Goose's own extra — the wrap resolves the vendor binary path, which a catalog
# row would otherwise supply; a lifted package would read its own env var instead.
_ALLOWED_CORE_IMPORTS = frozenset(
    {
        "omnigent.harness_startup_config",
        "omnigent.inner.acp_executor",
        "omnigent.inner.acp_extension",
        "omnigent.inner.acp_harness",
        "omnigent.inner.acp_toolnames",
        "omnigent.inner.datamodel",
        "omnigent.inner.executor",
        "omnigent.runtime.harnesses._executor_adapter",
    }
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
    """Whether a dotted import name resolves within the allowed module set."""
    return any(
        name == mod or name.startswith(f"{mod}.") or mod.startswith(f"{name}.") for mod in allowed
    )


def test_goose_package_only_imports_the_liftable_core_surface() -> None:
    """Goose imports nothing from core that a community plugin could not.

    **What breaks if this fails**: lifting Goose out stops being a move — the new
    import drags in core internals (the server, runner, registry, or stores) that
    a plugin cannot reach, and the extraction turns into a rewrite.
    """
    modules = sorted(_GOOSE_PKG.glob("*.py"))
    assert modules, f"no modules found under {_GOOSE_PKG}"

    offenders: dict[str, set[str]] = {}
    for path in modules:
        core = {
            name
            for name in _imported_modules(path)
            if name.split(".")[0] == "omnigent"
            and not name.startswith("omnigent.inner.goose")
            and not _is_allowed(name, _ALLOWED_CORE_IMPORTS)
        }
        if core:
            offenders[path.name] = core

    assert not offenders, (
        f"Goose imports core modules outside the liftable surface: {offenders}. "
        f"Either route the need through AcpExtension / AcpAgentConfig, or add the "
        f"module to _ALLOWED_CORE_IMPORTS with a note on how a lifted package "
        f"would satisfy it."
    )


def test_goose_wrap_exposes_the_harness_entry_point() -> None:
    """The package exports ``create_app()`` — the whole harness-module contract.

    Same requirement a community plugin's ``harness_modules`` target must meet, so
    the wrap already satisfies the plugin contract as written.
    """
    from omnigent.inner.goose import harness

    assert callable(harness.create_app)
    assert harness.create_app.__code__.co_argcount == 0


def test_registry_routes_goose_at_the_package_wrap() -> None:
    """``harness: goose`` resolves to the package's wrap, not a flat module.

    **What breaks if this fails**: the registry and the package disagree, so the
    directory move that lifts Goose out leaves the harness unroutable.
    """
    from omnigent.harness_plugins import harness_modules

    assert harness_modules()["goose"] == "omnigent.inner.goose.harness"


def test_goose_declares_no_subagent_dialect() -> None:
    """Goose surfaces no sub-agents, and the capability row must agree.

    Goose has no sub-agent reporting to read, so its extension supplies no source
    and ``surfaces_subagents`` is False — the same derivation Devin's ``True``
    comes from, which is what keeps the capability matrix from drifting.
    """
    from omnigent.harness_plugins import harness_capabilities
    from omnigent.inner.goose import GOOSE_ACP_EXTENSION

    assert GOOSE_ACP_EXTENSION.surfaces_subagents is False
    assert harness_capabilities()["goose"].subagents is False
