"""Architecture tests for the omnigent package. See docs/ARCHITECTURE.md."""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repository constants
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
_OMNIGENT = _REPO / "omnigent"


# ---------------------------------------------------------------------------
# Shared file-system helpers
# ---------------------------------------------------------------------------


def _get_tracked_files(root: Path = _REPO) -> frozenset[str]:
    """Return git-tracked paths relative to *root*."""
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if result.returncode != 0:
        return frozenset()
    return frozenset(line for line in result.stdout.split("\n") if line)


def _file_to_module(filepath: str) -> str:
    """Convert a relative filepath to a dotted module name."""
    mod = filepath.replace("/", ".").removesuffix(".py")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def _is_compat_stub(filepath: str) -> bool:
    """Return True if this file is a compat alias stub."""
    if filepath in (
        "omnigent/harness_plugins.py",
        "omnigent/harness_install_spec.py",
    ):
        return True
    if (
        filepath.startswith("omnigent/inner/")
        and not filepath.startswith("omnigent/inner/nessie/")
        and filepath != "omnigent/inner/__init__.py"
    ):
        return True
    return (
        filepath.startswith("omnigent/runtime/harnesses/")
        and filepath != "omnigent/runtime/harnesses/__init__.py"
    )


def _collect_imports(filepath: str, root: Path = _REPO) -> list[tuple[str, int]]:
    """Parse all imports (including TYPE_CHECKING / function-level) from a file.

    Returns a list of *(resolved_module_name, lineno)* pairs.
    """
    full_path = root / filepath
    try:
        source = full_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return []

    module = _file_to_module(filepath)
    parts = module.split(".")

    def resolve_relative(level: int, mod_name: str) -> str:
        base = parts[:-level] if level <= len(parts) else []
        if mod_name:
            return ".".join(base + mod_name.split("."))
        return ".".join(base)

    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.level and node.level > 0:
                resolved = resolve_relative(node.level, node.module)
            else:
                resolved = node.module
            imports.append((resolved, node.lineno))
    return imports


# ---------------------------------------------------------------------------
# Rule 1 — package root layout
# ---------------------------------------------------------------------------

ROOT_MODULES: frozenset[str] = frozenset(
    {
        "__init__",
        "__main__",
        "version",
        "errors",
        "config",
        "_env_compat",
        "git_credential_github",
    }
)
ROOT_COMPAT_ALIASES: frozenset[str] = frozenset(
    {
        "harness_plugins",
        "harness_install_spec",
    }
)
_TOLERATED_GENERATED: frozenset[str] = frozenset({"_build_info"})


def test_package_root_holds_only_foundation_modules() -> None:
    """Only ROOT_MODULES, ROOT_COMPAT_ALIASES, and _build_info.py may live directly in omnigent/.

    New modules belong in the owning subpackage — see docs/ARCHITECTURE.md.
    """
    allowed = ROOT_MODULES | ROOT_COMPAT_ALIASES | _TOLERATED_GENERATED
    actual = frozenset(p.stem for p in _OMNIGENT.iterdir() if p.is_file() and p.suffix == ".py")
    extras = actual - allowed
    assert not extras, (
        f"Unexpected modules at omnigent/ root: {sorted(extras)}. "
        "Put new modules in the owning subpackage per docs/ARCHITECTURE.md."
    )


# ---------------------------------------------------------------------------
# Rule 2 — compat namespace stubs
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"\b0\.\d+\.\d+\b")


def _compat_stub_paths(root: Path = _REPO) -> list[Path]:
    """Collect every alias stub path (excludes package __init__.py files)."""
    omnigent = root / "omnigent"
    stubs: list[Path] = []

    # Root compat aliases
    for name in ("harness_plugins.py", "harness_install_spec.py"):
        p = omnigent / name
        if p.exists():
            stubs.append(p)

    # omnigent/inner/ stubs (immediate children, not nessie/ subpackage)
    inner = omnigent / "inner"
    if inner.is_dir():
        for p in sorted(inner.iterdir()):
            if p.is_file() and p.suffix == ".py" and p.name != "__init__.py":
                stubs.append(p)

    # omnigent/runtime/harnesses/ stubs (immediate children)
    rt_harnesses = omnigent / "runtime" / "harnesses"
    if rt_harnesses.is_dir():
        for p in sorted(rt_harnesses.iterdir()):
            if p.is_file() and p.suffix == ".py" and p.name != "__init__.py":
                stubs.append(p)

    return stubs


def _check_stub_ast(path: Path) -> str | None:
    """Validate that a stub file has the correct alias-stub structure.

    Returns an error description, or ``None`` if the file is valid.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"syntax error: {exc}"

    body = tree.body
    if not body:
        return "empty module body"

    # 1. Docstring with removal version
    if not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Constant):
        return "missing module docstring"
    docstring = body[0].value.value
    if not isinstance(docstring, str):
        return "module docstring is not a string"
    if not _VERSION_RE.search(docstring):
        return f"docstring does not mention a removal version (0.X.Y): {docstring!r}"

    # 2. Strip docstring and __future__ imports from the remainder
    remaining: list[ast.stmt] = [
        stmt
        for stmt in body[1:]
        if not (isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__")
    ]
    if not remaining:
        return "stub has nothing after docstring / future imports"

    last = remaining[-1]

    def _is_sys_modules_swap(node: ast.stmt) -> bool:
        if not isinstance(node, ast.Assign):
            return False
        if len(node.targets) != 1:
            return False
        target = node.targets[0]
        return (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Attribute)
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "sys"
            and target.value.attr == "modules"
        )

    def _is_runpy_main_block(node: ast.stmt) -> bool:
        if not isinstance(node, ast.If):
            return False
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return False
        return any(
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Call)
            and isinstance(s.value.func, ast.Attribute)
            and s.value.func.attr == "run_module"
            and isinstance(s.value.func.value, ast.Name)
            and s.value.func.value.id == "runpy"
            for s in node.body
        )

    if _is_sys_modules_swap(last):
        for stmt in remaining[:-1]:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                return f"unexpected non-import statement before sys.modules swap: {ast.dump(stmt)}"
        return None

    if _is_runpy_main_block(last):
        for stmt in remaining[:-1]:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                return f"unexpected non-import statement before __main__ block: {ast.dump(stmt)}"
        return None

    return (
        f"final statement is neither sys.modules swap nor runpy __main__ block: {ast.dump(last)}"
    )


def _check_compat_package_init_ast(path: Path) -> str | None:
    """Validate that a compat package __init__.py contains only allowed constructs.

    Allowed: docstring, ``from __future__``, imports, ``__getattr__``/``__dir__``.
    Returns an error description, or ``None`` if valid.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"syntax error: {exc}"

    _pep562_names: frozenset[str] = frozenset({"__getattr__", "__dir__"})
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.FunctionDef) and stmt.name in _pep562_names:
            continue
        return f"disallowed statement in compat __init__.py: {ast.dump(stmt)}"
    return None


def test_compat_namespaces_hold_only_alias_stubs() -> None:
    """Every .py under inner/ and runtime/harnesses/ (excl __init__.py) is a valid alias stub."""
    errors: list[str] = []

    # Package __init__.py files for the two compat namespaces
    for init_path in (
        _OMNIGENT / "inner" / "__init__.py",
        _OMNIGENT / "runtime" / "harnesses" / "__init__.py",
    ):
        if not init_path.exists():
            continue
        err = _check_compat_package_init_ast(init_path)
        if err:
            errors.append(f"{init_path.relative_to(_REPO)}: {err}")

    for stub_path in _compat_stub_paths():
        err = _check_stub_ast(stub_path)
        if err:
            errors.append(f"{stub_path.relative_to(_REPO)}: {err}")

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Rule 3 — compat aliases resolve to the canonical module
# ---------------------------------------------------------------------------


def _collect_alias_pairs(root: Path = _REPO) -> list[tuple[str, str]]:
    """Return (old_module, canonical_module) pairs for sys.modules-swap stubs."""
    omnigent = root / "omnigent"

    def _file_to_mod(path: Path) -> str:
        rel = path.relative_to(omnigent)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return "omnigent." + ".".join(parts)

    def _parse_canonical(path: Path) -> str | None:
        """Extract the canonical module name from ``from X import Y as _target``."""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            return None
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.asname == "_target":
                        return f"{node.module}.{alias.name}"
        return None

    pairs: list[tuple[str, str]] = []
    for stub_path in _compat_stub_paths(root):
        canonical = _parse_canonical(stub_path)
        if canonical is None:
            continue  # runpy script stub — not a sys.modules redirect
        pairs.append((_file_to_mod(stub_path), canonical))
    return pairs


@pytest.mark.parametrize("old,new", _collect_alias_pairs())
def test_compat_aliases_resolve_to_canonical_module(old: str, new: str) -> None:
    """importlib.import_module(old) and import_module(new) must return the same object."""
    old_mod = importlib.import_module(old)
    new_mod = importlib.import_module(new)
    assert old_mod is new_mod, (
        f"{old!r} did not redirect to {new!r}: got {old_mod!r} vs {new_mod!r}"
    )


# ---------------------------------------------------------------------------
# Rule 4 — harness-specific files live under harnesses/
# ---------------------------------------------------------------------------

# Files in omnigent/ that match harness name patterns but legitimately
# live outside omnigent/harnesses/: runner-side title-generation helpers
# that are orchestration code rather than harness-specific execution.
_HARNESS_NAME_ALLOWLIST: frozenset[str] = frozenset(
    {
        # runner-side title generation helper, not harness-specific dispatch code
        "omnigent/runner/background_titles/claude_native.py",
        # runner-side title generation helper, not harness-specific dispatch code
        "omnigent/runner/background_titles/codex_native.py",
    }
)

_HARNESS_NAME_PATTERN: re.Pattern[str] = re.compile(
    r".*_executor\.py$|.*_harness\.py$|.*_native.*\.py$"
)

_COMPAT_NAMESPACES: tuple[str, ...] = (
    "omnigent/inner/",
    "omnigent/runtime/harnesses/",
)


def test_harness_specific_modules_live_under_harnesses() -> None:
    """No omnigent/ file named *_executor, *_harness, or *_native* lives outside harnesses/."""
    tracked = _get_tracked_files()
    violations: list[str] = []
    for fp in sorted(tracked):
        if not fp.startswith("omnigent/") or not fp.endswith(".py"):
            continue
        if fp.startswith("omnigent/harnesses/"):
            continue
        if any(fp.startswith(ns) for ns in _COMPAT_NAMESPACES):
            continue
        if fp in _HARNESS_NAME_ALLOWLIST:
            continue
        if _HARNESS_NAME_PATTERN.match(Path(fp).name):
            violations.append(fp)
    assert not violations, (
        f"Harness-specific files outside omnigent/harnesses/: {violations}. "
        "Move them into the owning harness package."
    )


# ---------------------------------------------------------------------------
# Rule 5 — layering
# ---------------------------------------------------------------------------

_HARNESS_REGISTRY_SUBMODULES: frozenset[str] = frozenset(
    {
        "registry",
        "aliases",
        "availability",
        "capabilities",
        "install_spec",
        "wrapper_labels",
        "startup_config",
    }
)

# Layer table from docs/ARCHITECTURE.md.  Packages absent from the doc
# are assigned the lowest layer consistent with their actual import graph
# (comment explains the choice).
_LAYER: dict[str, int] = {
    # 0 — foundation
    "core": 0,
    "util": 0,
    "entities": 0,
    "errors": 0,
    "version": 0,
    "config": 0,
    "_env_compat": 0,
    "api": 0,  # protobuf data schemas; no omnigent runtime deps
    "resources": 0,  # static bundled files; empty __init__.py, no deps
    # 1 — libraries
    "spec": 1,
    "policies": 1,
    "models": 1,
    "llms": 1,
    "observability": 1,
    "telemetry": 1,
    "db": 1,
    "stores": 1,
    "connections": 1,
    "extensions": 1,
    "harness_registry": 1,  # lightweight metadata, no execution deps
    "inner": 1,  # compat namespace; nessie/policies bridges into policies (layer 1)
    "community": 1,  # optional extension namespace, no internal service deps
    # 2 — execution
    "sandbox": 2,
    "environments": 2,
    "terminals": 2,
    "tools": 2,
    "runtime": 2,
    "harnesses": 2,
    "client_tools": 2,  # client-side tool execution helpers
    "session_import": 2,  # imports harnesses (layer 2) for transcript reading
    "testing": 2,  # test-env safety helpers reference execution constructs
    # 3 — services
    "runner": 3,
    "host": 3,
    "server": 3,
    "git_credential_github": 3,  # root module; sandbox ~/.gitconfig references it
    # 4 — front-ends
    "cli": 4,
    "repl": 4,
    "onboarding": 4,
}


def _get_unit(module_path: str) -> str | None:
    """Map a dotted omnigent.* module name to its architectural unit.

    Returns ``None`` for non-omnigent modules or the bare ``omnigent`` package.
    """
    if not module_path.startswith("omnigent."):
        return None
    rest = module_path[len("omnigent.") :]
    if not rest:
        return None
    parts = rest.split(".")
    first = parts[0]
    if len(parts) == 1:
        return first  # root module
    if first == "harnesses" and parts[1] in _HARNESS_REGISTRY_SUBMODULES:
        return "harness_registry"
    return first


def _find_layer_violations(
    tracked: frozenset[str],
    root: Path = _REPO,
) -> dict[tuple[str, str], list[tuple[str, int]]]:
    """Build the import graph and return all upward-import violations.

    A violation is an edge from a unit at layer *L* to a unit at layer *L* > *L*.
    The dict maps *(src_unit, dst_unit)* to the list of *(file, lineno)* examples.
    """
    violations: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for filepath in sorted(tracked):
        if not filepath.startswith("omnigent/") or not filepath.endswith(".py"):
            continue
        if _is_compat_stub(filepath):
            continue
        src_unit = _get_unit(_file_to_module(filepath))
        if src_unit is None:
            continue
        src_layer = _LAYER.get(src_unit)
        if src_layer is None:
            continue
        for imp_module, lineno in _collect_imports(filepath, root):
            if not imp_module.startswith("omnigent."):
                continue
            dst_unit = _get_unit(imp_module)
            if dst_unit is None:
                continue
            dst_layer = _LAYER.get(dst_unit)
            if dst_layer is None:
                continue
            if dst_layer > src_layer:
                violations[(src_unit, dst_unit)].append((filepath, lineno))
    return violations


# fmt: off
# Known layer violations in the current tree (generated from the tree at
# commit 16af69762).  This list only shrinks — a new upward import is a
# regression; a pair that no longer occurs must be removed.
# The comment on each line shows one offending import location.
KNOWN_LAYER_VIOLATIONS: frozenset[tuple[str, str]] = frozenset({
    ("connections", "server"),           # omnigent/connections/databricks/__init__.py:17
    ("core", "policies"),                # omnigent/core/loader.py:140
    ("core", "runtime"),                 # omnigent/core/executor.py:18
    ("core", "sandbox"),                 # omnigent/core/loader.py:301
    ("core", "spec"),                    # omnigent/core/loader.py:865
    ("entities", "llms"),                # omnigent/entities/conversation.py:11
    ("entities", "spec"),                # omnigent/entities/agent.py:8
    ("entities", "terminals"),           # omnigent/entities/session_resources.py:17
    ("harness_registry", "cli"),         # omnigent/harnesses/startup_config.py:185
    ("harness_registry", "harnesses"),   # omnigent/harnesses/registry.py:17
    ("harness_registry", "onboarding"),  # omnigent/harnesses/registry.py:1260
    ("harnesses", "cli"),                # omnigent/harnesses/antigravity_native/main.py:79
    ("harnesses", "host"),               # omnigent/harnesses/antigravity_native/main.py:146
    ("harnesses", "onboarding"),         # omnigent/harnesses/antigravity/executor.py:129
    ("harnesses", "repl"),               # omnigent/harnesses/antigravity_native/main.py:1674
    ("harnesses", "runner"),             # omnigent/harnesses/claude_native/bridge.py:2491
    ("harnesses", "server"),             # omnigent/harnesses/antigravity_native/interactions.py:70
    ("host", "cli"),                     # omnigent/host/connect.py:4895
    ("host", "onboarding"),              # omnigent/host/connect.py:132
    ("llms", "onboarding"),              # omnigent/llms/context_window.py:17
    ("llms", "runtime"),                 # omnigent/llms/adapters/databricks.py:20
    ("models", "harnesses"),             # omnigent/models/gateway_inference.py:49
    ("models", "host"),                  # omnigent/models/databricks_token.py:135
    ("models", "onboarding"),            # omnigent/models/databricks_model_discovery.py:366
    ("models", "runtime"),               # omnigent/models/model_catalog.py:75
    ("models", "sandbox"),               # omnigent/models/signer/auth.py:16
    ("runner", "cli"),                   # omnigent/runner/_entry.py:1017
    ("runner", "onboarding"),            # omnigent/runner/_entry.py:2060
    ("runtime", "host"),                 # omnigent/runtime/workflow.py:1747
    ("runtime", "onboarding"),           # omnigent/runtime/policies/builder.py:661
    ("runtime", "runner"),               # omnigent/runtime/__init__.py:16
    ("runtime", "server"),               # omnigent/runtime/caps.py:11
    ("server", "cli"),                   # omnigent/server/accounts_bootstrap.py:218
    ("server", "onboarding"),            # omnigent/server/app.py:1144
    ("spec", "harnesses"),               # omnigent/spec/codex_plugin_skills.py:47
    ("spec", "sandbox"),                 # omnigent/spec/parser.py:35
    ("spec", "tools"),                   # omnigent/spec/validator.py:364
    # omnigent/stores/conversation_store/sqlalchemy_store.py:90
    ("stores", "harnesses"),
    ("stores", "session_import"),        # omnigent/stores/conversation_store/__init__.py:18
    ("telemetry", "host"),               # omnigent/telemetry/installation_id.py:60
    ("terminals", "cli"),                # omnigent/terminals/registry.py:85
    ("tools", "runner"),                 # omnigent/tools/mcp.py:713
    ("tools", "server"),                 # omnigent/tools/manager.py:490
    ("util", "cli"),                     # omnigent/util/server_url.py:120
    ("util", "harness_registry"),        # omnigent/util/reasoning_effort.py:126
    ("util", "llms"),                    # omnigent/util/reasoning_effort.py:9
    ("util", "observability"),           # omnigent/util/attachments.py:35
    ("util", "server"),                  # omnigent/util/reasoning_effort.py:222
})
# fmt: on


def test_layering_is_respected() -> None:
    """No new upward imports exist; no KNOWN_LAYER_VIOLATIONS entry is stale.

    A new violation means: move the code to a lower layer or justify adding it here.
    A stale entry means: it was fixed — remove it from KNOWN_LAYER_VIOLATIONS.

    Note: another worker is concurrently refactoring omnigent/runner/app.py and
    omnigent/runner/native/__init__.py.  Regenerate KNOWN_LAYER_VIOLATIONS after
    that change lands if the test reports new stale entries.
    """
    tracked = _get_tracked_files()
    actual_violations = _find_layer_violations(tracked)
    actual = frozenset(actual_violations.keys())

    new_violations = actual - KNOWN_LAYER_VIOLATIONS
    stale_entries = KNOWN_LAYER_VIOLATIONS - actual

    messages: list[str] = []
    if new_violations:
        lines = ["New layer violations (move the code down a layer or justify adding it here):"]
        for src, dst in sorted(new_violations):
            for fp, lineno in actual_violations[(src, dst)][:3]:
                lines.append(f"  {fp}:{lineno}  ({src!r} -> {dst!r})")
        messages.append("\n".join(lines))
    if stale_entries:
        lines = ["Stale KNOWN_LAYER_VIOLATIONS entries (remove them — the list only shrinks):"]
        for src, dst in sorted(stale_entries):
            lines.append(f"  ({src!r}, {dst!r})")
        messages.append("\n".join(lines))

    assert not messages, "\n\n".join(messages)


# ---------------------------------------------------------------------------
# Rule 7 — repo code must not import compat alias paths
# ---------------------------------------------------------------------------

# Old module paths that exist only as alias stubs and must not be imported.
_COMPAT_OLD_PATHS: frozenset[str] = frozenset(
    {
        "omnigent.harness_plugins",
        "omnigent.harness_install_spec",
        "omnigent.inner.datamodel",
        "omnigent.inner.executor",
        "omnigent.inner.tools",
        "omnigent.inner.policies",
        "omnigent.inner.loader",
        "omnigent.runtime.harnesses._executor_adapter",
        "omnigent.runtime.harnesses._scaffold",
    }
)

# omnigent.inner.nessie.policies is a permanent shim; policies/__init__.py
# loads it by design via stored policy handler strings.
_EXEMPT_COMPAT_PATHS: frozenset[str] = frozenset({"omnigent.inner.nessie.policies"})

# Stub files themselves are the canonical definition of the redirect.
_COMPAT_STUB_FILE_PATHS: frozenset[str] = frozenset(
    {
        "omnigent/harness_plugins.py",
        "omnigent/harness_install_spec.py",
        "omnigent/inner/datamodel.py",
        "omnigent/inner/executor.py",
        "omnigent/inner/tools.py",
        "omnigent/inner/policies.py",
        "omnigent/inner/loader.py",
        "omnigent/runtime/harnesses/_executor_adapter.py",
        "omnigent/runtime/harnesses/_scaffold.py",
    }
)

_SCAN_PREFIXES: tuple[str, ...] = (
    "omnigent/",
    "tests/",
    "dev/",
    "sdks/",
    "integrations/",
    "examples/",
)


def test_repo_code_does_not_import_compat_aliases() -> None:
    """No tracked file may import a compat alias path.

    Import the canonical module named in the stub's docstring instead.
    """
    # Fast pre-filter: byte-scan every file for any compat path substring,
    # then AST-parse only the tiny set of candidates.  This keeps the test
    # under budget even though it covers ~3 000 files.
    _compat_bytes = [p.encode() for p in sorted(_COMPAT_OLD_PATHS)]

    tracked = _get_tracked_files()
    violations: list[str] = []
    for fp in sorted(tracked):
        if not fp.endswith(".py"):
            continue
        if not any(fp.startswith(p) for p in _SCAN_PREFIXES):
            continue
        if fp in _COMPAT_STUB_FILE_PATHS:
            continue
        if fp == "tests/test_architecture.py":
            continue

        # Skip files that cannot possibly contain a compat alias import.
        raw = (_REPO / fp).read_bytes()
        if not any(cb in raw for cb in _compat_bytes):
            continue

        for imp_module, lineno in _collect_imports(fp):
            if imp_module in _EXEMPT_COMPAT_PATHS:
                continue
            for old_path in _COMPAT_OLD_PATHS:
                if imp_module == old_path or imp_module.startswith(old_path + "."):
                    violations.append(
                        f"{fp}:{lineno}: imports {imp_module!r} — "
                        "import the canonical module named in the stub instead"
                    )
                    break

    assert not violations, "\n".join(violations)


# ---------------------------------------------------------------------------
# Detector self-checks (use tmp_path, not edits to the repo)
# ---------------------------------------------------------------------------


def test_layer_detector_catches_upward_import(tmp_path: Path) -> None:
    """The layering detector flags a util/ file that imports from server/."""
    omnigent = tmp_path / "omnigent"
    (omnigent / "util").mkdir(parents=True)
    (omnigent / "server").mkdir(parents=True)
    (omnigent / "util" / "__init__.py").write_text("")
    (omnigent / "server" / "__init__.py").write_text("")
    (omnigent / "util" / "bad_module.py").write_text("from omnigent.server import something\n")
    fake_tracked: frozenset[str] = frozenset(
        {
            "omnigent/util/bad_module.py",
            "omnigent/server/__init__.py",
        }
    )
    violations = _find_layer_violations(fake_tracked, root=tmp_path)
    assert ("util", "server") in violations


def test_root_module_detector_catches_stray_file(tmp_path: Path) -> None:
    """The root-module check flags a .py file not in the allowed set."""
    fake_omnigent = tmp_path / "omnigent"
    fake_omnigent.mkdir()
    (fake_omnigent / "stray_util.py").write_text("")
    actual_stems = frozenset(
        p.stem for p in fake_omnigent.iterdir() if p.is_file() and p.suffix == ".py"
    )
    allowed = ROOT_MODULES | ROOT_COMPAT_ALIASES | _TOLERATED_GENERATED
    assert "stray_util" in (actual_stems - allowed)
