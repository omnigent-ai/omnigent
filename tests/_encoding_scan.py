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
# ``X.open(...)`` here is text-capable but *binary by default*: a bare call or a
# binary mode is fine, but an explicit text mode ('t') must name an encoding.
_BINARY_DEFAULT_OPENERS = {"gzip", "bz2", "lzma"}
_CONFIGPARSER_CTORS = {"ConfigParser", "RawConfigParser", "SafeConfigParser"}

# Only str literals containing one of these are candidate embedded scripts —
# a cheap prefilter before the (costlier) ``ast.parse`` gate.
_EMBEDDED_HINTS = ("open(", "read_text(", "write_text(")


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
    """True when a literal ``mode`` argument opens the file in binary."""
    args = call.args
    if len(args) > mode_argindex >= 0 and isinstance(args[mode_argindex], ast.Constant):
        val = args[mode_argindex].value
        if isinstance(val, str) and "b" in val:
            return True
    for k in call.keywords:  # keyword mode=
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            val = k.value.value
            if isinstance(val, str) and "b" in val:
                return True
    return False


def _mode_is_text(call: ast.Call, mode_argindex: int) -> bool:
    """True when a literal ``mode`` argument explicitly requests text ('t')."""
    args = call.args
    if len(args) > mode_argindex >= 0 and isinstance(args[mode_argindex], ast.Constant):
        val = args[mode_argindex].value
        if isinstance(val, str) and "t" in val:
            return True
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            val = k.value.value
            if isinstance(val, str) and "t" in val:
                return True
    return False


def _fdopen_names(tree: ast.AST) -> set[str]:
    """Bare names that call ``os.fdopen``: ``fdopen`` and any import alias."""
    names = {"fdopen"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for a in node.names:
                if a.name == "fdopen":
                    names.add(a.asname or a.name)
    return names


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
    return names


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
    chains: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Call) and _is_configparser_ctor(value.func, ctor_names):
            chains.update(c for t in targets if (c := _attr_chain(t)) is not None)
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


def _call_violations(
    tree: ast.AST, instances: set[str], ctor_names: set[str], fdopen_names: set[str]
) -> list[tuple[int, str]]:
    """Unencoded text I/O among the *Call* nodes of a single parsed tree."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            name, mode_idx = "open", 1
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
            if func.attr == "open" and recv_id in _NON_FILE_OPEN_RECEIVERS:
                continue  # tarfile.open() etc. — binary archive, not text I/O
            if func.attr == "open" and recv_id in _BINARY_DEFAULT_OPENERS:
                # gzip/bz2/lzma default to binary; only an explicit text mode
                # ('t', e.g. "rt"/"wt") needs an encoding.
                if not _has_encoding(node) and _mode_is_text(node, 1):
                    out.append((node.lineno, f"{recv_id}.open"))
                continue
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
    out = _call_violations(
        tree, _configparser_instances(tree, ctor_names), ctor_names, _fdopen_names(tree)
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
        if _call_violations(
            sub, _configparser_instances(sub, sub_ctor), sub_ctor, _fdopen_names(sub)
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
