"""Receiver-independent AST scan for unencoded owned-text I/O.

Ruff's PLW1514 and PEP 597's EncodingWarning both depend on the receiver's type
being inferable / the path being exercised. This scan instead matches the *call*
— regardless of the receiver's static type — for every way omnigent opens text:

* ``open(...)`` / ``Path.open(...)`` / ``.read_text(...)`` / ``.write_text(...)``
* ``os.fdopen(...)`` — a text stream layered over a raw fd
* ``ConfigParser.read(...)`` — decodes the named file with the given encoding
* ``open(...)`` embedded inside a string literal that is run as a Python
  one-liner (``python -c`` / ``exec(open(...))``)

Any of those in text mode without an explicit ``encoding=`` is a violation. This
is the structural guard behind ``tests/test_file_encoding.py``; it also runs as
an enumerator (``python tests/_encoding_scan.py``).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_METHODS = {"read_text", "write_text", "open"}
# Inline escape hatch for a call that genuinely can't name an encoding (e.g.
# importlib.metadata's Distribution.read_text, which has no encoding parameter).
# Preferred over a line-number allowlist, which drifts onto the wrong line on
# any edit above it. Annotate the call's line with ``# enc-ok: <reason>``.
_SUPPRESS = "# enc-ok"
# Line-number escape hatch of last resort (``"relpath:line"``); empty by design —
# use the inline ``# enc-ok`` pragma instead so suppressions can't drift silently.
ALLOWLIST: frozenset[str] = frozenset()
# ``X.open(...)`` on these modules is not a text-file open (binary archives / a
# raw fd / a browser), so an ``encoding`` argument is neither expected nor valid.
_NON_FILE_OPEN_RECEIVERS = {
    "tarfile",
    "zipfile",
    "os",  # os.open -> low-level fd (int), no encoding
    "webbrowser",  # webbrowser.open -> launches a browser
    "socket",
    "subprocess",
}
# ``<module>.open(...)`` here is text-capable but *binary by default*: a bare
# call or a binary mode is fine, but any other mode (text, or one not statically
# provable as binary) must name an encoding. Matched by *import binding*, not by
# literal receiver name, so an alias (``import gzip as gz``) is caught and an
# unrelated ``gzip = Path(...)`` is not misread as the module.
_COMPRESSED_MODULES = {"gzip", "bz2", "lzma"}
_TEXT_OPEN_MODULES = {"builtins", "codecs", "io"}
_CONFIGPARSER_CTORS = {"ConfigParser", "RawConfigParser", "SafeConfigParser"}

# Only str literals containing one of these are candidate embedded scripts —
# a cheap prefilter before the (costlier) ``ast.parse`` gate.
_EMBEDDED_HINTS = ("open", "read_text", "write_text")


def _has_encoding(call: ast.Call) -> bool:
    """True only when a *real* encoding is named.

    ``encoding=None`` selects the platform default codec — identical to omitting
    the argument — so it is treated as a violation, not a fix.
    """
    for k in call.keywords:
        if k.arg == "encoding":
            return not (isinstance(k.value, ast.Constant) and k.value.value is None)
    return False


def _mode_is_binary(call: ast.Call, mode_argindex: int) -> bool:
    """True when a literal, syntactically valid ``mode`` is binary.

    Validating the whole mode matters: a module-style call such as
    ``io.open("blob.txt")`` can otherwise look binary merely because the
    filename occupies the position used by a bound ``Path.open`` method and
    happens to contain the letter ``b``.
    """

    def is_binary_mode(value: object) -> bool:
        if not isinstance(value, str) or not value:
            return False
        return (
            set(value) <= set("rwaxbt+")
            and sum(value.count(ch) for ch in "rwax") == 1
            and value.count("b") <= 1
            and value.count("t") <= 1
            and not ("b" in value and "t" in value)
            and value.count("+") <= 1
            and "b" in value
        )

    args = call.args
    if len(args) > mode_argindex >= 0 and isinstance(args[mode_argindex], ast.Constant):
        if is_binary_mode(args[mode_argindex].value):
            return True
    for k in call.keywords:  # keyword mode=
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            if is_binary_mode(k.value.value):
                return True
    return False


def _mode_present(call: ast.Call, mode_argindex: int) -> bool:
    """True when a ``mode`` argument is supplied (positionally or by keyword)."""
    if mode_argindex >= 0 and len(call.args) > mode_argindex:
        return True
    return any(k.arg == "mode" for k in call.keywords)


def _has_dynamic_args(call: ast.Call) -> bool:
    """True when the call uses ``*args`` or ``**kwargs``.

    Such a call's mode and encoding can't be read statically (``open(*[p, "rt"])``,
    ``open(p, **{"mode": "rt"})``), so it must be treated conservatively.
    """
    return any(isinstance(a, ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    )


def _import_bound_name(alias: ast.alias) -> str:
    """Name introduced by an import alias."""
    return alias.asname or alias.name.split(".", 1)[0]


def _other_bound_names(
    tree: ast.AST,
    *,
    exempt_module_imports: set[str] = _COMPRESSED_MODULES,
    exempt_open_imports: set[str] = _COMPRESSED_MODULES,
) -> set[str]:
    """Names with any non-compressed-import binding anywhere in *tree*.

    The scanner intentionally treats cross-scope/order conflicts conservatively:
    a parameter, later assignment, other import, definition, exception target, or
    pattern capture with the same spelling makes a compressed binding ambiguous.
    Ambiguous calls are then required to name an encoding rather than falling
    through to a receiver signature we cannot prove.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        bound_name: str | None = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound_name = node.id
        elif isinstance(node, ast.arg):
            bound_name = node.arg
        elif isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.ExceptHandler,
                ast.MatchAs,
                ast.MatchStar,
            ),
        ):
            bound_name = node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound_name = node.rest
        if bound_name is not None:
            names.add(bound_name)

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] not in exempt_module_imports:
                    names.add(_import_bound_name(alias))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if not (node.module in exempt_open_imports and alias.name == "open"):
                    names.add(alias.asname or alias.name)
    return names


def _assignment_aliases(
    tree: ast.AST,
    module_candidates: set[str],
    func_candidates: set[str],
    *,
    recursive: bool = True,
) -> tuple[set[str], set[str]]:
    """Conservatively propagate simple aliases of compressed modules/openers.

    Assignment aliases are always ambiguous (the whole-tree policy cannot prove
    their value at every call), but retaining them as candidates makes their calls
    fail closed instead of disappearing from the scan.
    """

    def target_names(target: ast.expr) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Starred):
            return target_names(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return {name for elt in target.elts for name in target_names(elt)}
        return set()

    def value_kinds(
        value: ast.expr,
        known_modules: set[str],
        known_funcs: set[str],
    ) -> tuple[bool, bool]:
        """Return possible ``(module, opener)`` provenance for safe aliases."""
        if isinstance(value, ast.Name):
            return value.id in known_modules, value.id in known_funcs
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "open"
            and isinstance(value.value, ast.Name)
        ):
            return False, value.value.id in known_modules
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            kinds = [value_kinds(elt, known_modules, known_funcs) for elt in value.elts]
        elif isinstance(value, ast.IfExp):
            kinds = [
                value_kinds(value.body, known_modules, known_funcs),
                value_kinds(value.orelse, known_modules, known_funcs),
            ]
        elif isinstance(value, ast.BoolOp):
            kinds = [value_kinds(item, known_modules, known_funcs) for item in value.values]
        elif isinstance(value, (ast.NamedExpr, ast.Starred)):
            return value_kinds(value.value, known_modules, known_funcs)
        else:
            return False, False
        return any(kind[0] for kind in kinds), any(kind[1] for kind in kinds)

    module_aliases: set[str] = set()
    func_aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        known_modules = module_candidates | module_aliases
        known_funcs = func_candidates | func_aliases
        for node in ast.walk(tree):
            targets: list[ast.expr]
            value: ast.expr
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue

            names = {name for target in targets for name in target_names(target)}
            has_module, has_func = value_kinds(value, known_modules, known_funcs)
            if not recursive and not isinstance(value, (ast.Name, ast.Attribute)):
                has_module = has_func = False
            if has_module:
                before = len(module_aliases)
                module_aliases.update(names)
                changed |= len(module_aliases) != before
            if has_func:
                before = len(func_aliases)
                func_aliases.update(names)
                changed |= len(func_aliases) != before
    return module_aliases, func_aliases


def _callable_assignment_aliases(
    tree: ast.AST,
    seeds: set[str],
    attribute_names: set[str],
) -> set[str]:
    """Propagate simple/conditional/unpacked aliases of known callables."""

    def target_names(target: ast.expr) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Starred):
            return target_names(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return {name for elt in target.elts for name in target_names(elt)}
        return set()

    def matches(value: ast.expr, known: set[str]) -> bool:
        if isinstance(value, ast.Name):
            return value.id in known
        if isinstance(value, ast.Attribute):
            return value.attr in attribute_names
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return any(matches(elt, known) for elt in value.elts)
        if isinstance(value, ast.IfExp):
            return matches(value.body, known) or matches(value.orelse, known)
        if isinstance(value, ast.BoolOp):
            return any(matches(item, known) for item in value.values)
        if isinstance(value, (ast.NamedExpr, ast.Starred)):
            return matches(value.value, known)
        return False

    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        known = seeds | aliases
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not matches(value, known):
                continue
            before = len(aliases)
            aliases.update(name for target in targets for name in target_names(target))
            changed |= len(aliases) != before
    return aliases


def _compressed_bindings(
    tree: ast.AST,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return stable and ambiguous bindings for gzip/bz2/lzma openers.

    The result is ``(modules, funcs, ambiguous_modules, ambiguous_funcs)``.
    Stable imports retain compressed-open's binary-default semantics. A candidate
    with any conflicting binding is never discarded: it becomes ambiguous and
    its calls fail closed by requiring an explicit encoding.
    """
    direct_modules: set[str] = set()
    direct_funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in _COMPRESSED_MODULES:
                    direct_modules.add(_import_bound_name(alias))
        elif isinstance(node, ast.ImportFrom) and node.module in _COMPRESSED_MODULES:
            for alias in node.names:
                if alias.name == "open":
                    direct_funcs.add(alias.asname or alias.name)

    alias_modules, alias_funcs = _assignment_aliases(tree, direct_modules, direct_funcs)
    module_candidates = direct_modules | alias_modules
    func_candidates = direct_funcs | alias_funcs
    other = _other_bound_names(tree)
    both = module_candidates & func_candidates

    ambiguous_modules = (module_candidates & other) | both | alias_modules
    ambiguous_funcs = (func_candidates & other) | both | alias_funcs
    modules = module_candidates - ambiguous_modules
    # Bare ``open`` always follows the stricter builtin/text-default path.
    funcs = func_candidates - ambiguous_funcs - {"open"}
    ambiguous_funcs.discard("open")
    return modules, funcs, ambiguous_modules, ambiguous_funcs


def _non_file_open_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return proven and ambiguous aliases for non-text ``module.open`` calls."""
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _NON_FILE_OPEN_RECEIVERS:
                    direct.add(_import_bound_name(alias))

    other = _other_bound_names(
        tree,
        exempt_module_imports=_NON_FILE_OPEN_RECEIVERS,
        exempt_open_imports=set(),
    )
    ambiguous = direct & other
    return direct - ambiguous, ambiguous


def _text_open_bindings(
    tree: ast.AST,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Bindings for modules/functions whose ``open`` uses builtin-style args."""
    direct_modules: set[str] = set()
    # Seed the builtin too so ``fopen = open`` cannot evade the strict path.
    direct_funcs: set[str] = {"open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _TEXT_OPEN_MODULES:
                    direct_modules.add(_import_bound_name(alias))
        elif isinstance(node, ast.ImportFrom) and node.module in _TEXT_OPEN_MODULES:
            for alias in node.names:
                if alias.name == "open":
                    direct_funcs.add(alias.asname or alias.name)

    alias_modules, alias_funcs = _assignment_aliases(
        tree,
        direct_modules,
        direct_funcs,
    )
    module_candidates = direct_modules | alias_modules
    func_candidates = direct_funcs | alias_funcs
    other = _other_bound_names(
        tree,
        exempt_module_imports=_TEXT_OPEN_MODULES,
        exempt_open_imports=_TEXT_OPEN_MODULES,
    )
    both = module_candidates & func_candidates
    ambiguous_modules = (module_candidates & other) | both | alias_modules
    ambiguous_funcs = (func_candidates & other) | both | alias_funcs
    modules = module_candidates - ambiguous_modules
    funcs = func_candidates - ambiguous_funcs - {"open"}
    ambiguous_funcs.discard("open")
    return modules, funcs, ambiguous_modules, ambiguous_funcs


def _fdopen_names(tree: ast.AST) -> set[str]:
    """Bare names that call ``os.fdopen``: ``fdopen`` and any import alias."""
    names = {"fdopen"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for a in node.names:
                if a.name == "fdopen":
                    names.add(a.asname or a.name)
    return names | _callable_assignment_aliases(tree, names, {"fdopen"})


def _attr_chain(node: ast.expr) -> str | None:
    """Render a Name/Attribute reference to a dotted string (``self.cfg``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_chain(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _configparser_ctor_names(tree: ast.AST) -> set[str]:
    """Names that construct a ConfigParser: the classes plus any import alias.

    Resolves ``from configparser import ConfigParser as CP`` so an aliased
    constructor is still recognized.
    """
    names = set(_CONFIGPARSER_CTORS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "configparser":
            for a in node.names:
                if a.name in _CONFIGPARSER_CTORS:
                    names.add(a.asname or a.name)
    return names | _callable_assignment_aliases(tree, names, _CONFIGPARSER_CTORS)


def _is_configparser_ctor(func: ast.expr, ctor_names: set[str]) -> bool:
    """True for ``ConfigParser()`` / ``configparser.RawConfigParser()`` / an alias."""
    if isinstance(func, ast.Name):
        return func.id in ctor_names
    if isinstance(func, ast.Attribute):
        return func.attr in _CONFIGPARSER_CTORS  # module.ConfigParser(), cp.ConfigParser()
    return False


def _configparser_instances(tree: ast.AST, ctor_names: set[str]) -> set[str]:
    """Dotted references bound to a ConfigParser instance anywhere in the module.

    Tracks both plain names (``cfg = ConfigParser()``) and attribute targets
    (``self.cfg = ConfigParser()``), keyed by their dotted chain.
    """

    def targets_and_value(node: ast.AST) -> tuple[list[ast.expr], ast.expr] | None:
        if isinstance(node, ast.Assign):
            return node.targets, node.value
        if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            return [node.target], node.value
        return None

    def value_chains(value: ast.expr) -> set[str]:
        if isinstance(value, (ast.Name, ast.Attribute)):
            chain = _attr_chain(value)
            return {chain} if chain is not None else set()
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return {chain for elt in value.elts for chain in value_chains(elt)}
        if isinstance(value, ast.IfExp):
            return value_chains(value.body) | value_chains(value.orelse)
        if isinstance(value, ast.BoolOp):
            return {chain for item in value.values for chain in value_chains(item)}
        if isinstance(value, (ast.NamedExpr, ast.Starred)):
            return value_chains(value.value)
        return set()

    chains: set[str] = set()
    for node in ast.walk(tree):
        assignment = targets_and_value(node)
        if assignment is None:
            continue
        targets, value = assignment
        if isinstance(value, ast.Call) and _is_configparser_ctor(value.func, ctor_names):
            chains.update(c for t in targets if (c := _attr_chain(t)) is not None)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            assignment = targets_and_value(node)
            if assignment is None:
                continue
            targets, value = assignment
            if not (value_chains(value) & chains):
                continue
            before = len(chains)
            chains.update(c for target in targets if (c := _attr_chain(target)) is not None)
            changed |= len(chains) != before
    return chains


def _is_configparser_read(func: ast.Attribute, instances: set[str], ctor_names: set[str]) -> bool:
    """True for ``cfg.read(...)`` / ``self.cfg.read(...)`` / ``ConfigParser().read(...)``."""
    if func.attr != "read":
        return False
    recv = func.value
    chain = _attr_chain(recv)
    if chain is not None and chain in instances:
        return True
    return isinstance(recv, ast.Call) and _is_configparser_ctor(recv.func, ctor_names)


def _literal_text(node: ast.AST) -> str | None:
    """Reconstruct a str constant or f-string (interpolations -> ``_X_``)."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "_X_"
            for v in node.values
        )
    return None


def _compressed_open_violation(node: ast.Call, label: str) -> tuple[int, str] | None:
    """A gzip/bz2/lzma open needs an encoding unless it is statically proven safe:
    a bare call or a proven-binary mode stays clean. A dynamic-args call, an
    explicit text mode, or a computed/dynamic mode all require the encoding."""
    if _has_encoding(node):
        return None
    if _has_dynamic_args(node) or (_mode_present(node, 1) and not _mode_is_binary(node, 1)):
        return (node.lineno, label)
    return None


def _call_violations(
    tree: ast.AST,
    instances: set[str],
    ctor_names: set[str],
    fdopen_names: set[str],
    comp_modules: set[str],
    comp_funcs: set[str],
    ambiguous_comp_modules: set[str],
    ambiguous_comp_funcs: set[str],
    non_file_modules: set[str],
    ambiguous_non_file_modules: set[str],
    text_open_modules: set[str],
    text_open_funcs: set[str],
    ambiguous_text_open_modules: set[str],
    ambiguous_text_open_funcs: set[str],
) -> list[tuple[int, str]]:
    """Unencoded text I/O among the *Call* nodes of a single parsed tree."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # A bare name spelled ``open`` always takes builtin/text-default
        # semantics, including ``from gzip import open``. This is conservative
        # for gzip's binary default and sound for unrelated builtin calls.
        if isinstance(func, ast.Name) and func.id == "open":
            name, mode_idx = "open", 1
        # Ambiguous compressed aliases never fall through to a guessed receiver
        # signature: requiring an encoding is the fail-closed policy.
        elif isinstance(func, ast.Name) and func.id in ambiguous_comp_funcs:
            if not _has_encoding(node):
                out.append((node.lineno, "open(ambiguous compressed alias)"))
            continue
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "open"
            and isinstance(func.value, ast.Name)
            and func.value.id in ambiguous_comp_modules
        ):
            if not _has_encoding(node):
                out.append((node.lineno, f"{func.value.id}.open(ambiguous)"))
            continue
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "open"
            and isinstance(func.value, ast.Name)
            and func.value.id in ambiguous_non_file_modules
        ):
            if not _has_encoding(node):
                out.append((node.lineno, f"{func.value.id}.open(ambiguous receiver)"))
            continue
        elif isinstance(func, ast.Name) and func.id in ambiguous_text_open_funcs:
            if not _has_encoding(node):
                out.append((node.lineno, "open(ambiguous text alias)"))
            continue
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "open"
            and isinstance(func.value, ast.Name)
            and func.value.id in ambiguous_text_open_modules
        ):
            if not _has_encoding(node):
                out.append((node.lineno, f"{func.value.id}.open(ambiguous text receiver)"))
            continue
        # Compressed openers first (matched by import binding), so a from-import
        # ``open`` alias or aliased module attribute isn't mishandled below.
        elif isinstance(func, ast.Name) and func.id in comp_funcs:
            if (v := _compressed_open_violation(node, "gzip.open")) is not None:
                out.append(v)
            continue
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "open"
            and isinstance(func.value, ast.Name)
            and func.value.id in comp_modules
        ):
            if (v := _compressed_open_violation(node, f"{func.value.id}.open")) is not None:
                out.append(v)
            continue
        elif isinstance(func, ast.Name) and func.id in text_open_funcs:
            name, mode_idx = "open", 1
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "open"
            and isinstance(func.value, ast.Name)
            and func.value.id in text_open_modules
        ):
            name, mode_idx = f"{func.value.id}.open", 1
        elif isinstance(func, ast.Name) and func.id in fdopen_names:
            name, mode_idx = "os.fdopen", 1  # fdopen / from-os alias
        elif isinstance(func, ast.Attribute) and func.attr == "fdopen":
            name, mode_idx = "os.fdopen", 1  # os.fdopen / aliased module.fdopen
        elif isinstance(func, ast.Attribute) and _is_configparser_read(
            func, instances, ctor_names
        ):
            if not _has_encoding(node):
                out.append((node.lineno, "ConfigParser.read"))
            continue
        elif isinstance(func, ast.Attribute) and func.attr in _METHODS:
            recv_id = func.value.id if isinstance(func.value, ast.Name) else None
            if func.attr == "open" and recv_id in non_file_modules:
                continue  # tarfile.open() etc. — binary archive, not text I/O
            name = func.attr
            mode_idx = 0 if func.attr == "open" else -1
        else:
            continue
        if _has_encoding(node) or _mode_is_binary(node, mode_idx):
            continue
        out.append((node.lineno, name))
    return out


def find_violations(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, call_name)`` for each unencoded text I/O call.

    Covers direct calls plus ``open()`` embedded in a string literal that is run
    as Python (``python -c`` / ``exec(open(...))``). Embedded candidates are
    gated on ``ast.parse`` succeeding, so docstring prose and non-Python
    templates (e.g. generated JS) are excluded rather than false-flagged.
    """
    tree = ast.parse(source)
    ctor_names = _configparser_ctor_names(tree)
    comp_modules, comp_funcs, ambiguous_modules, ambiguous_funcs = _compressed_bindings(tree)
    non_file_modules, ambiguous_non_file_modules = _non_file_open_bindings(tree)
    text_modules, text_funcs, ambiguous_text_modules, ambiguous_text_funcs = _text_open_bindings(
        tree
    )
    out = _call_violations(
        tree,
        _configparser_instances(tree, ctor_names),
        ctor_names,
        _fdopen_names(tree),
        comp_modules,
        comp_funcs,
        ambiguous_modules,
        ambiguous_funcs,
        non_file_modules,
        ambiguous_non_file_modules,
        text_modules,
        text_funcs,
        ambiguous_text_modules,
        ambiguous_text_funcs,
    )

    # Constants nested inside an f-string are reconstructed as part of that
    # f-string; skip them so a half-open interpolated fragment isn't parsed alone.
    joinedstr_children = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for child in ast.walk(node)
        if child is not node
    }
    for node in ast.walk(tree):
        if id(node) in joinedstr_children:
            continue
        text = _literal_text(node)
        if text is None or not any(h in text for h in _EMBEDDED_HINTS):
            continue
        try:
            # dedent so a uniformly-indented embedded script (common when the
            # literal sits inside an indented block) still parses as Python.
            sub = ast.parse(textwrap.dedent(text))
        except SyntaxError:
            continue  # not Python (prose, JS, a fragment) — not an embedded script
        sub_ctor = _configparser_ctor_names(sub)
        sub_modules, sub_funcs, sub_ambiguous_modules, sub_ambiguous_funcs = _compressed_bindings(
            sub
        )
        sub_non_file_modules, sub_ambiguous_non_file_modules = _non_file_open_bindings(sub)
        sub_text_modules, sub_text_funcs, sub_ambiguous_text_modules, sub_ambiguous_text_funcs = (
            _text_open_bindings(sub)
        )
        if _call_violations(
            sub,
            _configparser_instances(sub, sub_ctor),
            sub_ctor,
            _fdopen_names(sub),
            sub_modules,
            sub_funcs,
            sub_ambiguous_modules,
            sub_ambiguous_funcs,
            sub_non_file_modules,
            sub_ambiguous_non_file_modules,
            sub_text_modules,
            sub_text_funcs,
            sub_ambiguous_text_modules,
            sub_ambiguous_text_funcs,
        ):
            out.append((node.lineno, "open(embedded)"))
    return out


def scan_package(pkg: Path, allow: frozenset[str] | set[str] = ALLOWLIST) -> list[str]:
    """Return ``"relpath:line call"`` for every violation under *pkg*."""
    hits: list[str] = []
    for f in sorted(pkg.rglob("*.py")):
        source = f.read_text(encoding="utf-8")
        lines = source.splitlines()
        rel = f.relative_to(pkg.parent).as_posix()
        for lineno, name in find_violations(source):
            key = f"{rel}:{lineno}"
            line = lines[lineno - 1] if 0 <= lineno - 1 < len(lines) else ""
            if key in allow or _SUPPRESS in line:
                continue
            hits.append(f"{key} {name}")
    return hits


# Every first-party Python package shipped from this repo (each scanned against
# its own parent so the reported path stays package-relative).
def owned_packages() -> list[Path]:
    repo = Path(__file__).resolve().parents[1]
    return [
        repo / "omnigent",
        repo / "sdks" / "python-client" / "omnigent_client",
        repo / "sdks" / "ui" / "omnigent_ui_sdk",
    ]


if __name__ == "__main__":
    for pkg in owned_packages():
        for h in scan_package(pkg, ALLOWLIST):
            print(h)
