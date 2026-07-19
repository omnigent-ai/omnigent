"""Receiver-independent AST scan for unencoded owned-text I/O.

Ruff's PLW1514 and PEP 597's EncodingWarning both depend on the receiver's type
being inferable / the path being exercised. This scan instead matches the *call*
— any ``open``/``Path.open``/``read_text``/``write_text`` in text mode without an
explicit ``encoding=`` — regardless of the receiver's static type. It is the
structural guard that owned-text reads/writes must name an encoding.

Used both as an enumerator (``python tests/_encoding_scan.py``) and by
``tests/test_file_encoding.py`` as an enforced guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

_METHODS = {"read_text", "write_text", "open"}
# Not filesystem-path text I/O: importlib.metadata Distribution.read_text(name)
# reads packaged metadata by filename and decodes internally.
ALLOWLIST = {"omnigent/update_check.py:1174"}
# ``X.open(...)`` on these modules is not a text-file open (binary archives), so
# an ``encoding`` argument is neither expected nor valid.
_NON_FILE_OPEN_RECEIVERS = {
    "tarfile",
    "gzip",
    "zipfile",
    "bz2",
    "lzma",
    "os",  # os.open -> low-level fd (int), no encoding
    "webbrowser",  # webbrowser.open -> launches a browser
    "socket",
    "subprocess",
}


def _has_encoding(call: ast.Call) -> bool:
    return any(k.arg == "encoding" for k in call.keywords)


def _mode_is_binary(call: ast.Call, mode_argindex: int) -> bool:
    """True when a literal ``mode`` argument opens the file in binary."""
    args = call.args
    if len(args) > mode_argindex and isinstance(args[mode_argindex], ast.Constant):
        val = args[mode_argindex].value
        if isinstance(val, str) and "b" in val:
            return True
    # keyword mode=
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            val = k.value.value
            if isinstance(val, str) and "b" in val:
                return True
    return False


def find_violations(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, call_name)`` for each unencoded text I/O call."""
    out: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            name, mode_idx = "open", 1
        elif isinstance(func, ast.Attribute) and func.attr in _METHODS:
            if (
                func.attr == "open"
                and isinstance(func.value, ast.Name)
                and func.value.id in _NON_FILE_OPEN_RECEIVERS
            ):
                continue  # tarfile.open() etc. — binary archive, not text I/O
            name = func.attr
            mode_idx = 0 if func.attr == "open" else -1
        else:
            continue
        if _has_encoding(node):
            continue
        if mode_idx >= 0 and _mode_is_binary(node, mode_idx):
            continue
        out.append((node.lineno, name))
    return out


def scan_package(pkg: Path, allow: set[str]) -> list[str]:
    """Return ``"relpath:line call"`` for every violation under *pkg*."""
    hits: list[str] = []
    for f in sorted(pkg.rglob("*.py")):
        rel = f.relative_to(pkg.parent).as_posix()
        for lineno, name in find_violations(f.read_text(encoding="utf-8")):
            key = f"{rel}:{lineno}"
            if key in allow:
                continue
            hits.append(f"{key} {name}")
    return hits


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "omnigent"
    for h in scan_package(root, ALLOWLIST):
        print(h)
