#!/usr/bin/env python3
"""relocate_modules.py — deterministic module relocation codemod.

Moves modules/packages to new locations, rewrites all in-repo references, and
generates backward-compat stubs.  Idempotent: re-running after success is a
no-op; re-running after a partial failure resumes from where it stopped.

CLI
---
    python dev/refactor/relocate_modules.py apply  --map MAP.toml [--repo DIR]
                                                   [--dry-run] [--report out.json]
    python dev/refactor/relocate_modules.py rewrite --map MAP.toml [--repo DIR]
                                                    [paths...]
    python dev/refactor/relocate_modules.py check   --map MAP.toml [--repo DIR]

Map format (TOML)
-----------------
    [[move]]
    old = "omnigent.inner.foo_exec"
    new = "omnigent.harnesses.foo.executor"

    [[alias]]
    old = "omnigent.harness_plugins"
    new = "omnigent.harnesses.registry"
    removal = "0.19.0"

    [packages."omnigent.harnesses.foo"]
    doc = "Foo harness package."

    [cleanup]
    delete_empty = ["omnigent.inner"]
    exclude = ["CHANGELOG.md", "uv.lock"]

Environment
-----------
    RELOCATE_SKIP_FORMAT=1   skip the ruff post-format step (handy in tests)
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Negative lookahead for file-extension-like suffixes that indicate a dotted
# reference is actually a file path (e.g. "omnigent.inner.foo.py") and should
# NOT be matched as a Python module name.
_EXT_NEG = (
    r"(?!\.(?:py|pyc|pyi|yaml|yml|json|toml|md|txt|sh|ini|cfg|html|js|ts|jsx|tsx)"
    r"(?![a-zA-Z0-9_]))"
)

_SKIP_FORMAT_ENV = "RELOCATE_SKIP_FORMAT"

# Pre-compiled file-extension check (used in the fast text-replacer callback).
_FILE_EXT_RE = re.compile(
    r"\.(?:py|pyc|pyi|yaml|yml|json|toml|md|txt|sh|ini|cfg|html|js|ts|jsx|tsx)"
    r"(?![a-zA-Z0-9_])"
)


def _is_file_ext(s: str) -> bool:
    """True if *s* starts with a file-extension suffix (e.g. '.py', '.yaml')."""
    return bool(_FILE_EXT_RE.match(s))


def is_regular_file(path: Path) -> bool:
    """True only for plain files — never symlinks, directories, or gitlinks."""
    return not path.is_symlink() and path.is_file()


# Stub template for [[alias]] entries.
_STUB_TEMPLATE = '''\
"""Deprecated alias for :mod:`{new}`; removed in {removal}."""

import sys

from {new_parent} import {new_leaf} as _target

sys.modules[__name__] = _target
'''

_STUB_TEMPLATE_NO_VERSION = '''\
"""Alias for :mod:`{new}`, kept for references stored outside the repo."""

import sys

from {new_parent} import {new_leaf} as _target

sys.modules[__name__] = _target
'''

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class MoveEntry:
    old: str
    new: str
    replace: bool = False


@dataclass
class AliasEntry:
    old: str
    new: str
    removal: str = ""


@dataclass
class PackageDef:
    module: str
    doc: str = ""


@dataclass
class CleanupConfig:
    delete_empty: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class MapConfig:
    moves: list[MoveEntry] = field(default_factory=list)
    aliases: list[AliasEntry] = field(default_factory=list)
    packages: list[PackageDef] = field(default_factory=list)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)


@dataclass
class Report:
    moves_done: list[str] = field(default_factory=list)
    moves_skipped: list[str] = field(default_factory=list)
    files_rewritten: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "moves_done": self.moves_done,
            "moves_skipped": self.moves_skipped,
            "files_rewritten": self.files_rewritten,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Map parsing
# ---------------------------------------------------------------------------


def parse_map(map_file: Path) -> MapConfig:
    """Parse a MAP.toml file into a MapConfig."""
    with open(map_file, "rb") as fh:
        data = tomllib.load(fh)

    config = MapConfig()

    for entry in data.get("move", []):
        config.moves.append(
            MoveEntry(
                old=entry["old"],
                new=entry["new"],
                replace=entry.get("replace", False),
            )
        )

    for entry in data.get("alias", []):
        config.aliases.append(
            AliasEntry(
                old=entry["old"],
                new=entry["new"],
                removal=entry.get("removal", ""),
            )
        )

    for module, pkg_data in data.get("packages", {}).items():
        config.packages.append(PackageDef(module=module, doc=pkg_data.get("doc", "")))

    if "cleanup" in data:
        config.cleanup = CleanupConfig(
            delete_empty=data["cleanup"].get("delete_empty", []),
            exclude=data["cleanup"].get("exclude", []),
        )

    return config


# ---------------------------------------------------------------------------
# Git utilities
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=check,
    )


def get_tracked_files(root: Path) -> list[Path]:
    """Return all git-tracked files as absolute paths."""
    result = _git(root, "ls-files", "-z")
    return [root / f for f in result.stdout.split("\0") if f]


def git_add(root: Path, path: Path) -> None:
    _git(root, "add", str(path))


def git_rm(root: Path, path: Path, force: bool = False) -> None:
    args = ["rm", "-f"] if force else ["rm"]
    _git(root, *args, str(path))


def git_mv(root: Path, src: Path, dst: Path) -> None:
    _git(root, "mv", str(src), str(dst))


# ---------------------------------------------------------------------------
# Module ↔ path utilities
# ---------------------------------------------------------------------------


def module_to_path(module: str, root: Path) -> Path | None:
    """Return the Path for *module* (file or package dir), or None."""
    rel = Path(module.replace(".", "/"))
    pkg = root / rel
    if pkg.is_dir() and (pkg / "__init__.py").exists():
        return pkg
    mod_file = root / rel.with_suffix(".py")
    if mod_file.is_file():
        return mod_file
    return None


def path_to_module(path: Path, root: Path) -> str | None:
    """Convert an absolute path to a dotted module name, or None."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
        if not parts:
            return None
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    else:
        return None
    return ".".join(parts)


def _dir_is_stubs_only(dir_path: Path, root: Path, alias_old_set: set[str]) -> bool:
    """True if every non-__init__.py file under *dir_path* is a known alias stub."""
    py_files = [f for f in dir_path.rglob("*.py") if f.name != "__init__.py"]
    if not py_files:
        return True  # Only __init__.py (possibly a re-export stub itself)
    return all(path_to_module(f, root) in alias_old_set for f in py_files)


def _detect_already_moved(
    move: MoveEntry,
    old_path: Path | None,
    new_path: Path | None,
    alias_old_set: set[str],
    root: Path,
) -> bool:
    """True when this move has already been applied to the working tree.

    Conditions (any one is sufficient, given new_path exists):
    - old path is missing (simple case)
    - old path is a file that is a known ``[[alias]]`` stub
    - old path is a dir and ``new.startswith(old + ".")`` — the old path is
      the new package created by a file→package rename (e.g. cli.py → cli/)
    - old path is a dir whose non-init .py files are all alias stubs
    """
    if new_path is None:
        return False  # New location does not exist yet — not moved
    if old_path is None:
        return True  # Old gone, new present
    if old_path.is_file() and move.old in alias_old_set:
        return True  # Stub file at old location
    if old_path.is_dir() and move.new.startswith(move.old + "."):
        # The old dir IS the new package created by the file→package rename.
        return True
    if old_path.is_dir() and _dir_is_stubs_only(old_path, root, alias_old_set):
        return True  # Old dir contains only stubs
    return False


def _build_expanded_new_names(rename_map: dict[str, str]) -> frozenset[str]:
    """Return new module names AND all their parent packages.

    Used by the post-move callback to detect already-new-style references.
    """
    names: set[str] = set()
    for new in rename_map.values():
        parts = new.split(".")
        for n in range(1, len(parts) + 1):
            names.add(".".join(parts[:n]))
    return frozenset(names)


def expand_rename_map(
    moves: list[MoveEntry],
    root: Path,
    aliases: list[AliasEntry] | None = None,
) -> tuple[dict[str, str], set[str]]:
    """Build a complete old→new mapping, expanding package submodules.

    Returns (rename_map, package_olds) where *package_olds* is the set of
    old module names that are packages (not individual files).

    After ``apply`` has run, old paths may coincide with new artefacts
    (alias stubs, the new package created by a file→package rename, …).
    Pass *aliases* to enable correct "already-moved" detection and avoid
    false submodule expansion from new-style paths.
    """
    alias_old_set: set[str] = {a.old for a in aliases} if aliases else set()
    rename_map: dict[str, str] = {}
    package_olds: set[str] = set()

    for move in moves:
        rename_map[move.old] = move.new
        old_path = module_to_path(move.old, root)
        new_path = module_to_path(move.new, root)

        already_moved = _detect_already_moved(move, old_path, new_path, alias_old_set, root)
        scan_path = new_path if already_moved else old_path

        if scan_path is not None and scan_path.is_dir():
            package_olds.add(move.old)
            for py_file in sorted(scan_path.rglob("*.py")):
                rel = py_file.relative_to(scan_path)
                parts = list(rel.parts)
                is_init = parts[-1] == "__init__.py"
                if is_init:
                    parts = parts[:-1]
                    if not parts:
                        continue  # The package's own __init__ already added above.
                else:
                    parts[-1] = parts[-1][:-3]
                suffix = "." + ".".join(parts)
                sub_old = move.old + suffix
                sub_new = move.new + suffix
                rename_map[sub_old] = sub_new
                if is_init:
                    package_olds.add(sub_old)

    return rename_map, package_olds


# ---------------------------------------------------------------------------
# Exclusion / binary checks
# ---------------------------------------------------------------------------


def is_excluded(path: Path, root: Path, patterns: list[str]) -> bool:
    rel = str(path.relative_to(root))
    name = path.name
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in patterns)


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Text rewrite — fast finder + longest-prefix callback
# ---------------------------------------------------------------------------


def build_text_replacer(
    rename_map: dict[str, str],
    package_olds: set[str],
    post_move: bool = False,
) -> tuple[re.Pattern[str], Callable[[re.Match[str]], str]]:
    """Build (finder, callback) for fast single-pass text rewriting.

    Instead of a ~N-alternative combined regex, we use one short "finder" that
    detects candidate tokens starting with a known first segment (e.g. "omnigent")
    and then do an O(K·depth) longest-prefix dict lookup in the callback.

    *finder* is the fast candidate-detection pattern.
    *callback* is passed to ``finder.sub(callback, text)``; it enforces the
    same boundary rules as the old per-alternative patterns.

    When *post_move* is True (``rewrite``/``check`` run after ``apply``), the
    callback applies a new-style guard: if the longest prefix of a candidate
    that is a **new** name is strictly longer than the longest **old** prefix,
    or equals it AND the candidate itself IS a new name, the candidate is left
    unchanged.  This prevents double-rewriting tokens like
    ``omnigent.cli.commands.main`` (where ``omnigent.cli.commands`` is already
    the new location) or ``from omnigent.cli import x`` (where ``omnigent.cli``
    is now the new package).  See README for the residual ambiguity.
    """
    # Populate subs dicts
    dotted_subs: dict[str, str] = {}  # dotted module names → dotted new names
    path_subs: dict[str, str] = {}  # slash-paths → slash-paths

    for old, new in rename_map.items():
        dotted_subs[old] = new
        old_dir = old.replace(".", "/")
        new_dir = new.replace(".", "/")
        if old in package_olds:
            path_subs[old_dir] = new_dir
            path_subs[old_dir + "/__init__.py"] = new_dir + "/__init__.py"
        else:
            path_subs[old_dir + ".py"] = new_dir + ".py"

    if not dotted_subs and not path_subs:
        return re.compile(r"(?!x)x"), lambda m: m.group(0)

    # First segments for fast candidate detection.
    first_segs = sorted(
        {old.split(".")[0] for old in rename_map},
        key=len,
        reverse=True,
    )
    first_segs_pat = "|".join(map(re.escape, first_segs))

    # One short finder: tokens starting with a known first segment.
    # Covers dotted names (a.b.c) AND slash paths (a/b/c.py).
    finder = re.compile(r"(?<![\w./\-])(?:" + first_segs_pat + r")(?:[./][A-Za-z_]\w*)+(?:\.py)?")

    def callback(m: re.Match[str]) -> str:
        candidate = m.group(0)
        orig = m.string
        end = m.end()
        char_after = orig[end] if end < len(orig) else ""

        if "/" in candidate:
            # ---- path form: O(depth) lookup by splitting on "/" ----------
            # Exact .py match first
            if candidate.endswith(".py") and candidate in path_subs:
                return path_subs[candidate]
            # Try progressively shorter path prefixes (longest → shortest)
            parts = candidate.rstrip("/").split("/")
            if candidate.endswith(".py"):
                # Already checked exact match above; strip .py to find pkg dirs
                parts[-1] = parts[-1][:-3]
            for n in range(len(parts), 0, -1):
                key = "/".join(parts[:n])
                if key not in path_subs:
                    continue
                # Package dir key: compute what follows in the candidate
                cand_prefix = "/".join(parts[:n])
                # Map back to the original candidate slicing position
                # (candidate may have ".py" or trailing slash that changed parts)
                after_key = candidate[len(cand_prefix) :]
                if after_key:
                    c0 = after_key[0]
                    if c0 != "/" and (c0.isalnum() or c0 == "_"):
                        continue
                else:
                    if char_after and (char_after.isalnum() or char_after == "_"):
                        continue
                return path_subs[key] + after_key
            return candidate

        # ---- dotted-name form: O(depth) lookup by splitting on "." ------
        parts_dot = candidate.split(".")
        # Find the longest old-name key that matches.
        old_key: str | None = None
        old_after: str = ""
        for n in range(len(parts_dot), 0, -1):
            key = ".".join(parts_dot[:n])
            if key not in dotted_subs:
                continue
            after_key = candidate[len(key) :]
            # Right boundary: nothing immediately word-like after the key.
            first_after = after_key[0] if after_key else char_after
            if first_after and (first_after.isalnum() or first_after == "_"):
                continue
            # File-extension negative lookahead.
            ext_check = after_key if after_key else char_after
            if _is_file_ext(ext_check):
                continue
            old_key = key
            old_after = after_key
            break

        if old_key is None:
            return candidate

        if post_move:
            # Guard: if the candidate is already a new-style name, leave it.
            # Find the longest prefix of *candidate* that is in expanded_new_names.
            new_match_len = 0
            for n in range(len(parts_dot), 0, -1):
                prefix = ".".join(parts_dot[:n])
                if prefix in expanded_new_names:
                    new_match_len = len(prefix)
                    break
            old_match_len = len(old_key)
            if new_match_len > old_match_len:
                return candidate  # Longer new prefix → definitely already new-style
            if new_match_len == old_match_len == len(candidate):
                return candidate  # Candidate IS exactly a new name

        return dotted_subs[old_key] + old_after

    if post_move:
        expanded_new_names = _build_expanded_new_names(rename_map)

    return finder, callback


def _first_segments(rename_map: dict[str, str]) -> frozenset[str]:
    """Return the set of first dotted components across all old module names."""
    return frozenset(old.split(".")[0] for old in rename_map)


# ---------------------------------------------------------------------------
# Relative import pre-pass (tokenize-based, operates on moved files or files
# whose relative imports resolve to a moved module/package)
# ---------------------------------------------------------------------------


def _pkg_module(file_module: str, file_path: Path) -> str:
    """Return the package portion of a module path."""
    if file_path.name == "__init__.py":
        return file_module
    parts = file_module.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else ""


def _resolve_relative(level: int, module_part: str, pkg: str) -> str | None:
    """Resolve a relative import to an absolute module name."""
    pkg_parts = pkg.split(".") if pkg else []
    if level - 1 > len(pkg_parts):
        return None
    base = ".".join(pkg_parts[: len(pkg_parts) - (level - 1)])
    if module_part:
        return base + "." + module_part if base else module_part
    return base or None


def _is_in_rename_scope(abs_module: str, names: list[str], rename_map: dict[str, str]) -> bool:
    """True if *abs_module* or any of its imported *names* is in *rename_map*."""
    if abs_module in rename_map:
        return True
    for name in names:
        if abs_module + "." + name in rename_map:
            return True
    # Package prefix check.
    return any(old.startswith(abs_module + ".") for old in rename_map)


def convert_relative_imports(
    source: str,
    file_path: Path,
    root: Path,
    rename_map: dict[str, str],
    convert_all: bool,
) -> str:
    """Convert relative imports to absolute form.

    If *convert_all* is True (moved file), all relative imports are converted.
    Otherwise only imports that resolve to a moved module are converted.
    """
    file_module = path_to_module(file_path, root)
    if file_module is None:
        return source

    pkg = _pkg_module(file_module, file_path)

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return source

    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, str]] = []  # (line_0idx, new_line_text)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            continue

        level = node.level
        module_part = node.module or ""
        abs_module = _resolve_relative(level, module_part, pkg)
        if abs_module is None:
            continue

        names = [a.name for a in node.names]

        if not convert_all and not _is_in_rename_scope(abs_module, names, rename_map):
            continue

        # Replace the ".(dots)(module)?" portion on the source line.
        line_idx = node.lineno - 1
        if line_idx >= len(lines):
            continue
        line = lines[line_idx]
        col = node.col_offset

        # Match: from(ws)(dots)(module)?  at the right column.
        m = re.match(r"(from\s+)(\.+)([a-zA-Z_][\w.]*)?", line[col:])
        if not m:
            continue

        old_part = m.group(0)  # e.g. "from ..foo.bar"
        new_part = "from " + abs_module  # absolute form
        new_line = line[:col] + new_part + line[col + len(old_part) :]
        edits.append((line_idx, new_line))

    if not edits:
        return source

    for line_idx, new_line in sorted(edits, key=lambda x: x[0], reverse=True):
        lines[line_idx] = new_line

    return "".join(lines)


# ---------------------------------------------------------------------------
# Python import rewrite — AST-based, handles from P import n1, n2
# ---------------------------------------------------------------------------


def _make_import_stmt(
    indent: str,
    new_parent: str,
    new_leaf: str,
    original_name: str,
    alias: str | None,
) -> str:
    """Generate a replacement `from … import …` statement."""
    if alias is not None:
        # Explicit alias: always keep it.
        binding = alias
    elif new_leaf == original_name:
        binding = None  # Same name → no alias needed.
    else:
        binding = original_name  # Preserve local binding with an alias.

    if new_parent:
        stmt = f"from {new_parent} import {new_leaf}"
    else:
        stmt = f"import {new_leaf}"
    if binding is not None and binding != new_leaf:
        stmt += f" as {binding}"
    return indent + stmt


_TRAILING_COMMENT_RE = re.compile(r"((?:  *| *\t)#.*?)(\n?)$")
_INNER_COMMENT_RE = re.compile(r"(?:  *| *\t)#")


def _extract_trailing_comment(text: str) -> tuple[str, str]:
    """Return (text_without_comment, comment_with_space) for a single line.

    e.g. ``"from foo import bar  # noqa: F401\\n"``
    → ``("from foo import bar", "  # noqa: F401")``
    """
    m = _TRAILING_COMMENT_RE.search(text)
    if m:
        comment = m.group(1)
        eol = m.group(2)
        stripped = text[: m.start()]
        return stripped.rstrip(), comment + eol
    return text.rstrip("\n"), "\n" if text.endswith("\n") else ""


def _has_inner_comments(lines_block: list[str]) -> bool:
    """True if any interior line of a multi-line import block has a comment."""
    for line in lines_block[1:]:  # Skip first line; check interior/last lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return True
        if _INNER_COMMENT_RE.search(line):
            return True
    return False


def rewrite_absolute_imports(source: str, rename_map: dict[str, str]) -> str:
    """Rewrite ``from P import n`` statements where P.n is a moved module.

    Preserves trailing comments (``# noqa``, ``# type:``, ``# pragma`` …):
    - The trailing comment of the *last* line of the original statement is
      appended to every generated replacement statement.
    - Multi-line imports that contain inner comments that cannot be placed are
      left unchanged and trigger a warning.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    lines = source.splitlines(keepends=True)
    # Edits: (start_line_0idx, end_line_0idx_exclusive, new_text)
    edits: list[tuple[int, int, str]] = []
    warnings_out: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0:
            continue
        module = node.module or ""
        if not module:
            continue

        moved_aliases: list[tuple[ast.alias, str]] = []  # (alias, new_full)
        kept_aliases: list[ast.alias] = []

        for al in node.names:
            full_old = module + "." + al.name
            if full_old in rename_map:
                moved_aliases.append((al, rename_map[full_old]))
            else:
                kept_aliases.append(al)

        if not moved_aliases:
            continue

        # Get indentation from the first line of this statement.
        start_line = node.lineno - 1
        # end_lineno can be None in theory; fall back to lineno (exclusive, 0-indexed).
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        first_line = lines[start_line] if start_line < len(lines) else ""
        indent = ""
        for ch in first_line:
            if ch in (" ", "\t"):
                indent += ch
            else:
                break

        # Extract the original block for comment analysis.
        orig_block = lines[start_line:end_line]

        # Check for inner comments in multi-line imports (can't be placed safely).
        if len(orig_block) > 1 and _has_inner_comments(orig_block):
            warnings_out.append(
                f"line {node.lineno}: multi-line import with inner comments "
                "cannot be rewritten automatically — left unchanged"
            )
            continue

        # Extract trailing comment from the last line (e.g. a noqa or type-ignore marker).
        last_line = orig_block[-1] if orig_block else ""
        _, trailing = _extract_trailing_comment(last_line)
        # Normalise: ensure trailing always ends with newline.
        if trailing and not trailing.endswith("\n"):
            trailing += "\n"
        if not trailing:
            trailing = "\n"

        new_stmts: list[str] = []

        for al, new_full in moved_aliases:
            new_parts = new_full.rsplit(".", 1)
            new_parent = new_parts[0] if len(new_parts) == 2 else ""
            new_leaf = new_parts[-1]
            stmt = _make_import_stmt(indent, new_parent, new_leaf, al.name, al.asname)
            new_stmts.append(stmt)

        if kept_aliases:
            kept_str = ", ".join(
                (f"{a.name} as {a.asname}" if a.asname else a.name) for a in kept_aliases
            )
            new_stmts.append(f"{indent}from {module} import {kept_str}")

        # Attach the trailing comment to every emitted statement.
        if len(new_stmts) == 1:
            new_text = new_stmts[0] + trailing
        else:
            # Multiple statements: put the comment on the last one; add newlines between.
            new_text = "\n".join(new_stmts[:-1]) + "\n" + new_stmts[-1] + trailing

        edits.append((start_line, end_line, new_text))

    if not edits:
        return source

    # Apply bottom-up to preserve line number validity.
    edits.sort(key=lambda x: x[0], reverse=True)
    for start, end, new_text in edits:
        lines[start:end] = [new_text]

    return "".join(lines)


# ---------------------------------------------------------------------------
# Warning detection
# ---------------------------------------------------------------------------


_FILE_SENSITIVE_RE = re.compile(
    r"\b__file__\b|importlib\.resources|\.with_name\s*\(|\.parents\s*\["
)


def _make_dyn_ref_re(rename_map: dict[str, str]) -> re.Pattern[str]:
    """Build a single regex that detects dynamic references to any moved module."""
    segs = sorted(
        {old.split(".")[0] for old in rename_map},
        key=len,
        reverse=True,
    )
    segs_pat = "|".join(map(re.escape, segs))
    return re.compile(
        r'(?:["\'](?:' + segs_pat + r")[\w.]*\." + r'[^"\']*["\'])\s*\+'
        r"|f(?:\"|').*?(?:" + segs_pat + r")[\w.]*\.\{"
    )


def find_warnings(source: str, file_path: Path, rename_map: dict[str, str]) -> list[str]:
    """Return informational warnings for constructs that cannot be auto-fixed.

    Compiles warning patterns fresh from *rename_map*.  For repeated calls on
    many files, prefer :func:`find_warnings_fast` with pre-compiled patterns.
    """
    dyn_re = _make_dyn_ref_re(rename_map)
    return _find_warnings_with(source, file_path, dyn_re)


def _find_warnings_with(source: str, file_path: Path, dyn_re: re.Pattern[str]) -> list[str]:
    """Inner warning check using pre-compiled patterns — O(1) per file."""
    warnings: list[str] = []
    fname = str(file_path)
    if dyn_re.search(source):
        warnings.append(
            f"{fname}: possible dynamic reference to a moved module "
            "(string concatenation or f-string — check manually)"
        )
    if _FILE_SENSITIVE_RE.search(source):
        warnings.append(
            f"{fname}: contains __file__ / importlib.resources / path-depth "
            "sensitive calls — verify after move"
        )
    return warnings


# ---------------------------------------------------------------------------
# Compat stub generation
# ---------------------------------------------------------------------------


def _init_py_content(doc: str, module: str) -> str:
    actual_doc = doc or f"{module} package."
    return f'"""{actual_doc}"""\n'


def generate_stub(alias: AliasEntry) -> str:
    """Return the source text for a compatibility re-export stub."""
    parts = alias.new.rsplit(".", 1)
    new_parent = parts[0] if len(parts) == 2 else ""
    new_leaf = parts[-1]
    if alias.removal:
        tmpl = _STUB_TEMPLATE
    else:
        tmpl = _STUB_TEMPLATE_NO_VERSION
    return tmpl.format(
        old=alias.old,
        new=alias.new,
        removal=alias.removal,
        new_parent=new_parent,
        new_leaf=new_leaf,
    )


# ---------------------------------------------------------------------------
# Move operations
# ---------------------------------------------------------------------------


def _ensure_package(
    pkg_module: str,
    root: Path,
    packages: list[PackageDef],
    dry_run: bool,
) -> None:
    """Create a package directory + __init__.py if missing."""
    pkg_path = root / pkg_module.replace(".", "/")
    init = pkg_path / "__init__.py"
    if not pkg_path.is_dir():
        if not dry_run:
            pkg_path.mkdir(parents=True, exist_ok=True)
    if not init.exists():
        # Find docstring from [packages] map entry.
        doc = ""
        for pd in packages:
            if pd.module == pkg_module:
                doc = pd.doc
                break
        if not dry_run:
            init.write_text(_init_py_content(doc, pkg_module))
            git_add(root, init)


def perform_moves(
    moves: list[MoveEntry],
    root: Path,
    packages_def: list[PackageDef],
    dry_run: bool,
    report: Report,
    aliases: list[AliasEntry] | None = None,
) -> None:
    """Execute git mv operations for each [[move]] entry."""
    alias_old_set: set[str] = {a.old for a in aliases} if aliases else set()

    def _ensure_parents(dest: Path) -> None:
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)

    for move in moves:
        old_path = module_to_path(move.old, root)
        new_path = module_to_path(move.new, root)

        # Idempotency: skip moves that have already been applied.
        if _detect_already_moved(move, old_path, new_path, alias_old_set, root):
            report.moves_skipped.append(f"{move.old} -> {move.new} (already moved)")
            continue

        if old_path is None:
            # Source not found and not an already-moved case → warn.
            report.moves_skipped.append(f"{move.old} -> {move.new} (source not found)")
            continue

        if old_path.is_file():
            # Single module file.
            dest_file = root / (move.new.replace(".", "/") + ".py")
            if dest_file.exists():
                if move.replace:
                    if not dry_run:
                        git_rm(root, dest_file, force=True)
                else:
                    report.warnings.append(
                        f"Destination {dest_file} already exists for move "
                        f"{move.old} -> {move.new}; skipping"
                    )
                    report.moves_skipped.append(f"{move.old} -> {move.new} (dest exists)")
                    continue

            # Ensure the parent package exists (handles file→package collisions).
            new_module_parent = move.new.rsplit(".", 1)[0] if "." in move.new else ""
            if new_module_parent:
                _ensure_package(new_module_parent, root, packages_def, dry_run)

            _ensure_parents(dest_file)
            if not dry_run:
                git_mv(root, old_path, dest_file)
            report.moves_done.append(f"{move.old} -> {move.new}")

        else:
            # Package directory.
            dest_pkg = root / move.new.replace(".", "/")
            if dest_pkg.exists():
                if move.replace:
                    if not dry_run:
                        _git(root, "rm", "-rf", str(dest_pkg))
                else:
                    report.warnings.append(
                        f"Destination {dest_pkg} already exists for package move "
                        f"{move.old} -> {move.new}; skipping"
                    )
                    report.moves_skipped.append(f"{move.old} -> {move.new} (dest exists)")
                    continue

            # Ensure parent package exists.
            new_pkg_parent = move.new.rsplit(".", 1)[0] if "." in move.new else ""
            if new_pkg_parent:
                _ensure_package(new_pkg_parent, root, packages_def, dry_run)

            _ensure_parents(dest_pkg)
            if not dry_run:
                git_mv(root, old_path, dest_pkg)
            report.moves_done.append(f"{move.old} -> {move.new} (package)")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_empty_packages(
    delete_empty: list[str],
    root: Path,
    dry_run: bool,
    report: Report,
) -> None:
    """Remove packages that contain only __init__.py after moves."""
    for module in delete_empty:
        pkg_path = root / module.replace(".", "/")
        if not pkg_path.is_dir():
            continue
        contents = [p for p in pkg_path.iterdir() if p.name not in ("__init__.py", "__pycache__")]
        if contents:
            report.warnings.append(
                f"Package {module} ({pkg_path}) is not empty after moves; "
                f"remaining: {[c.name for c in contents]}"
            )
            continue
        init = pkg_path / "__init__.py"
        if not dry_run:
            if init.exists():
                git_rm(root, init)
            # Untracked bytecode would keep the emptied directory importable as a
            # namespace package; git rm may already have removed the directory.
            shutil.rmtree(pkg_path / "__pycache__", ignore_errors=True)
            if pkg_path.exists():
                pkg_path.rmdir()
        report.moves_done.append(f"deleted empty package {module}")


# ---------------------------------------------------------------------------
# Post-format
# ---------------------------------------------------------------------------


def run_ruff(root: Path, changed_files: list[Path]) -> None:
    """Run `ruff check --select I --fix` then `ruff format` on *changed_files*."""
    if os.environ.get(_SKIP_FORMAT_ENV):
        return
    if not changed_files:
        return

    ruff = root / ".venv" / "bin" / "ruff"
    if not ruff.exists():
        # Fall back to PATH.
        import shutil

        ruff_path = shutil.which("ruff")
        if ruff_path:
            ruff = Path(ruff_path)
        else:
            print("warning: ruff not found; skipping post-format", file=sys.stderr)
            return

    str_files = [str(f) for f in changed_files if f.suffix == ".py" and f.exists()]
    if not str_files:
        return

    subprocess.run([str(ruff), "check", "--select", "I", "--fix", "--quiet", *str_files], cwd=root)
    subprocess.run([str(ruff), "format", "--quiet", *str_files], cwd=root)


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def validate(
    moves: list[MoveEntry],
    root: Path,
    aliases: list[AliasEntry] | None = None,
) -> list[str]:
    """Return a list of validation errors (empty = OK).

    Already-moved entries (detected via *aliases* and the file-to-package
    heuristic) are silently skipped so that re-running ``apply`` on an
    already-applied tree exits 0.
    """
    alias_old_set: set[str] = {a.old for a in aliases} if aliases else set()
    errors: list[str] = []
    seen_new: dict[str, str] = {}

    for move in moves:
        if move.new in seen_new:
            errors.append(
                f"Two moves target the same destination {move.new!r}: "
                f"{seen_new[move.new]!r} and {move.old!r}"
            )
        seen_new[move.new] = move.old

        old_path = module_to_path(move.old, root)
        new_path = module_to_path(move.new, root)

        if _detect_already_moved(move, old_path, new_path, alias_old_set, root):
            continue  # Idempotent: already applied

        if old_path is None:
            # Source genuinely missing (not an "already moved" case).
            errors.append(
                f"Source {move.old!r} not found (expected "
                f"{root}/{move.old.replace('.', '/')}.py or dir)"
            )
            continue

        dest_file = root / (move.new.replace(".", "/") + ".py")
        dest_pkg = root / move.new.replace(".", "/")
        dest_exists = dest_file.exists() or (
            dest_pkg.is_dir() and (dest_pkg / "__init__.py").exists()
        )
        if dest_exists and not move.replace:
            errors.append(
                f"Destination for {move.old!r} -> {move.new!r} already exists "
                "and `replace` is not set"
            )

    return errors


# ---------------------------------------------------------------------------
# apply command
# ---------------------------------------------------------------------------


def cmd_apply(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    map_file = Path(args.map).resolve()
    dry_run: bool = args.dry_run
    report = Report()
    t0 = time.perf_counter()

    def _phase(name: str, t_start: float) -> float:
        elapsed = time.perf_counter() - t_start
        print(f"  phase {name}: {elapsed:.1f}s")
        return time.perf_counter()

    try:
        config = parse_map(map_file)

        # Step 1: Validate.
        errors = validate(config.moves, repo, aliases=config.aliases)
        if errors:
            for err in errors:
                print(f"error: {err}", file=sys.stderr)
            sys.exit(1)

        # Build rename_map from the CURRENT state (before moves).
        rename_map, package_olds = expand_rename_map(config.moves, repo, aliases=config.aliases)
        first_segs = _first_segments(rename_map)

        # Collect all tracked files (before moves); skip symlinks and non-files.
        tracked = [p for p in get_tracked_files(repo) if is_regular_file(p)]
        exclude = config.cleanup.exclude
        tp = time.perf_counter()

        # Compute set of files that will be moved (for relative-import pre-pass).
        moved_files: set[Path] = set()
        for move in config.moves:
            old_path = module_to_path(move.old, repo)
            if old_path is None:
                continue
            if old_path.is_file():
                moved_files.add(old_path)
            else:
                for py in old_path.rglob("*.py"):
                    moved_files.add(py)

        # Step 2: Relative-import pre-pass.
        changed: list[Path] = []
        for path in tracked:
            if path.suffix != ".py":
                continue
            if is_excluded(path, repo, exclude):
                continue
            is_moved = path in moved_files
            source = path.read_text(encoding="utf-8", errors="replace")
            new_source = convert_relative_imports(source, path, repo, rename_map, is_moved)
            if new_source != source:
                if not dry_run:
                    path.write_text(new_source, encoding="utf-8")
                changed.append(path)
                report.files_rewritten.append(str(path.relative_to(repo)))
        tp = _phase("2-relative-imports", tp)

        # Step 3: Moves.
        perform_moves(
            config.moves,
            repo,
            config.packages,
            dry_run=dry_run,
            report=report,
            aliases=config.aliases,
        )
        tp = _phase("3-moves", tp)

        # After moves, re-collect tracked files; skip symlinks and non-files.
        tracked = [p for p in get_tracked_files(repo) if is_regular_file(p)]

        # Step 4 & 5: Python import rewrite + text rewrite.
        # When every move was already done (second run), use post-move semantics so
        # already-new-style references are not double-rewritten.
        post_move_apply = not report.moves_done
        finder, callback = build_text_replacer(rename_map, package_olds, post_move=post_move_apply)
        # Pre-compile warning pattern once (avoid per-key regex compilation per file).
        dyn_re = _make_dyn_ref_re(rename_map)

        for path in tracked:
            if is_excluded(path, repo, exclude):
                continue
            if path == map_file:
                continue
            # Quick pre-filter: skip files that contain no first segment at all.
            source = path.read_text(encoding="utf-8", errors="replace")
            if not any(seg in source for seg in first_segs):
                continue

            if path.suffix == ".py":
                # Collect warnings before rewriting (uses pre-compiled pattern).
                warns = _find_warnings_with(source, path, dyn_re)
                report.warnings.extend(warns)
                # Text rewrite first, then the AST import rewrite: the AST step
                # emits new names (``from pkg.cli import ui``) that can equal an
                # old name (``pkg.cli``) and must not be rewritten again.
                new_source = finder.sub(callback, source)
                new_source2 = rewrite_absolute_imports(new_source, rename_map)
            else:
                new_source2 = finder.sub(callback, source)

            if new_source2 != source:
                if not dry_run:
                    path.write_text(new_source2, encoding="utf-8")
                rel = str(path.relative_to(repo))
                if rel not in report.files_rewritten:
                    report.files_rewritten.append(rel)
                    changed.append(path)
        tp = _phase("4+5-rewrite", tp)

        # Step 6: Compat stubs (generated AFTER rewrite so they're not rewritten).
        for alias in config.aliases:
            stub_source = generate_stub(alias)
            stub_path = repo / (alias.old.replace(".", "/") + ".py")
            # Ensure parent package exists.
            parent_mod = alias.old.rsplit(".", 1)[0] if "." in alias.old else ""
            if parent_mod and not dry_run:
                _ensure_package(parent_mod, repo, config.packages, dry_run=False)
            if not dry_run:
                stub_path.write_text(stub_source, encoding="utf-8")
                git_add(repo, stub_path)
            changed.append(stub_path)
            rel = str(stub_path.relative_to(repo))
            if rel not in report.files_rewritten:
                report.files_rewritten.append(rel)

        # Step 7: Cleanup.
        if not dry_run:
            cleanup_empty_packages(config.cleanup.delete_empty, repo, dry_run=False, report=report)
        tp = _phase("6+7-stubs+cleanup", tp)

        # Step 8: Post-format.
        if not dry_run:
            run_ruff(repo, changed)
        _phase("8-format", tp)

    finally:
        # Step 9: Report — always print summary and write JSON even on error.
        elapsed_total = time.perf_counter() - t0
        print(f"total wall time: {elapsed_total:.1f}s")
        _print_report(report)
        if args.report:
            Path(args.report).write_text(json.dumps(report.to_dict(), indent=2))


# ---------------------------------------------------------------------------
# rewrite command
# ---------------------------------------------------------------------------


def cmd_rewrite(args: argparse.Namespace) -> None:
    """Rewrite import references without performing any git mv operations."""
    repo = Path(args.repo).resolve()
    map_file = Path(args.map).resolve()
    config = parse_map(map_file)
    report = Report()

    rename_map, package_olds = expand_rename_map(config.moves, repo, aliases=config.aliases)
    # post_move=True: leave already-new-style references unchanged.
    finder, callback = build_text_replacer(rename_map, package_olds, post_move=True)
    first_segs = _first_segments(rename_map)
    exclude = config.cleanup.exclude

    # Determine files to rewrite; skip symlinks and non-regular files.
    if args.paths:
        candidates = [p for p in (Path(x).resolve() for x in args.paths) if is_regular_file(p)]
    else:
        candidates = [p for p in get_tracked_files(repo) if is_regular_file(p)]

    changed: list[Path] = []
    for path in candidates:
        if is_excluded(path, repo, exclude):
            continue
        if path == map_file:
            continue

        source = path.read_text(encoding="utf-8", errors="replace")
        if not any(seg in source for seg in first_segs):
            continue

        if path.suffix == ".py":
            # Step 2 (partial): convert relative imports that resolve to old names.
            new_source = convert_relative_imports(
                source, path, repo, rename_map, convert_all=False
            )
            # Text rewrite before the AST import rewrite (see cmd_apply).
            new_source = finder.sub(callback, new_source)
            new_source = rewrite_absolute_imports(new_source, rename_map)
        else:
            new_source = finder.sub(callback, source)

        if new_source != source:
            path.write_text(new_source, encoding="utf-8")
            report.files_rewritten.append(str(path.relative_to(repo)))
            changed.append(path)

    run_ruff(repo, changed)
    _print_report(report)


# ---------------------------------------------------------------------------
# check command
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> None:
    """Scan for remaining references to old module names; exit 1 if any."""
    repo = Path(args.repo).resolve()
    map_file = Path(args.map).resolve()
    config = parse_map(map_file)

    rename_map, package_olds = expand_rename_map(config.moves, repo, aliases=config.aliases)
    # post_move=True: new-style references are not flagged as hits.
    finder, callback = build_text_replacer(rename_map, package_olds, post_move=True)
    first_segs = _first_segments(rename_map)
    exclude = config.cleanup.exclude

    # Alias stub paths are excluded from the scan (they intentionally reference old names).
    stub_paths: set[Path] = set()
    for alias in config.aliases:
        stub_paths.add(repo / (alias.old.replace(".", "/") + ".py"))

    found: list[str] = []
    for path in get_tracked_files(repo):
        if not is_regular_file(path):
            continue
        if is_excluded(path, repo, exclude):
            continue
        if path in stub_paths:
            continue
        if path == map_file:
            continue
        if is_binary(path):
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(seg in text for seg in first_segs):
            continue
        matches = finder.findall(text)
        # Filter to only those that the callback would actually replace
        # (i.e., are genuine old-name references, not just candidates).
        real_hits = {m for m in matches if finder.sub(callback, m) != m}
        if real_hits:
            rel = str(path.relative_to(repo))
            for m in sorted(real_hits):
                found.append(f"{rel}: {m!r}")

    if found:
        for line in found:
            print(line)
        sys.exit(1)
    else:
        print("check: no remaining references to old module names")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_report(report: Report) -> None:
    if report.moves_done:
        print(f"moves done ({len(report.moves_done)}):")
        for m in report.moves_done:
            print(f"  {m}")
    if report.moves_skipped:
        print(f"moves skipped ({len(report.moves_skipped)}):")
        for m in report.moves_skipped:
            print(f"  {m}")
    if report.files_rewritten:
        print(f"files rewritten: {len(report.files_rewritten)}")
    if report.warnings:
        print(f"warnings ({len(report.warnings)}):")
        for w in report.warnings:
            print(f"  WARNING: {w}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic module relocation codemod.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # apply
    ap = sub.add_parser("apply", help="Move modules and rewrite all references.")
    ap.add_argument("--map", required=True, metavar="MAP.toml", help="Path to the move map.")
    ap.add_argument("--repo", default=".", metavar="DIR", help="Repository root (default: .).")
    ap.add_argument("--dry-run", action="store_true", help="Print plan without making changes.")
    ap.add_argument("--report", metavar="out.json", help="Write JSON report to this file.")
    ap.set_defaults(func=cmd_apply)

    # rewrite
    rp = sub.add_parser("rewrite", help="Rewrite references only (no moves).")
    rp.add_argument("--map", required=True, metavar="MAP.toml")
    rp.add_argument("--repo", default=".", metavar="DIR")
    rp.add_argument("paths", nargs="*", metavar="PATH", help="Files to rewrite (default: all).")
    rp.set_defaults(func=cmd_rewrite)

    # check
    cp = sub.add_parser("check", help="Report remaining references; exit 1 if any.")
    cp.add_argument("--map", required=True, metavar="MAP.toml")
    cp.add_argument("--repo", default=".", metavar="DIR")
    cp.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
