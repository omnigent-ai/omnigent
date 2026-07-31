"""Flag new hardcoded LLM model ids outside tests and owned fallbacks.

The codebase still has a curated baseline of model pins that predate this
check. This hook requires every path/model count to exactly match
``dev/lint/hardcoded_model_allowlist.txt`` so new pins fail and removed pins
must ratchet the baseline down. Unavoidable static aliases are accepted only
inside complete ``StaticModelFallback`` records in the central fallback module.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MODEL_ID_RE = re.compile(
    r"""
    \b(?:
        databricks-(?:claude|gpt|gemini|llama|mistral|mixtral|deepseek|qwen|kimi|dbrx|grok|meta)-[a-z0-9][a-z0-9._:/-]*
      | system\.ai\.[a-z0-9][a-z0-9._:/-]*
      | (?:openai/)?gpt-(?:\d|oss)[a-z0-9._:/-]*
      | o[134](?:-[a-z0-9][a-z0-9._:/-]*)?
      | claude-(?:opus|sonnet|haiku|fable|\d)[a-z0-9._:/-]*
      | gemini-\d[a-z0-9][a-z0-9._:/-]*
      | kimi-k\d[a-z0-9._:/-]*
      | qwen\d[a-z0-9][a-z0-9._:/-]*
      | llama-\d[a-z0-9][a-z0-9._:/-]*
      | mistral-[a-z0-9][a-z0-9._:/-]*
      | deepseek-[a-z0-9][a-z0-9._:/-]*
    )\b
    """,
    re.VERBOSE,
)

TEXT_EXTENSIONS = {".json", ".toml", ".yaml", ".yml", ".sh"}
SOURCE_EXTENSIONS = {".py", *TEXT_EXTENSIONS}
SCAN_ROOTS = (
    Path("omnigent"),
    Path("scripts"),
    Path("examples"),
    Path(".github"),
    Path("dev/lint"),
)
SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tests",
}
ALLOWLIST_PATH = Path("dev/lint/hardcoded_model_allowlist.txt")
OWNED_FALLBACK_PATH = Path("omnigent/model_fallbacks.py")
FALLBACK_METADATA_FIELDS = frozenset({"owner", "provenance", "discovery_gap"})


@dataclass(frozen=True)
class Hit:
    """One hardcoded model occurrence."""

    path: Path
    line: int
    model: str


def _repo_relative(path: Path) -> str:
    """Return a stable repo-relative path when possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _target_name(node: ast.expr) -> str:
    """Return the user-visible name for an assignment target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _target_name(node.value)
    if isinstance(node, ast.Starred):
        return _target_name(node.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return " ".join(filter(None, (_target_name(item) for item in node.elts)))
    return ""


def _key_name(node: ast.expr | None) -> str:
    """Return a literal dict key name when statically knowable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_model_context(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Return True if a string literal lives in model-selection data."""
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.keyword) and parent.arg and "model" in parent.arg.lower():
            return True
        if isinstance(parent, ast.Assign):
            if any("model" in _target_name(target).lower() for target in parent.targets):
                return True
        elif isinstance(parent, ast.AnnAssign):
            if "model" in _target_name(parent.target).lower():
                return True
        elif isinstance(parent, ast.Dict):
            for key, value in zip(parent.keys, parent.values, strict=True):
                if value is current and "model" in _key_name(key).lower():
                    return True
        current = parent
    return False


def _extract_models(text: str) -> list[str]:
    """Return hardcoded model ids in ``text``."""
    return [match.group(0) for match in MODEL_ID_RE.finditer(text)]


def _literal_string_nodes(node: ast.expr) -> tuple[ast.Constant, ...] | None:
    """Return string literal elements when *node* is a literal sequence."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    literals: list[ast.Constant] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        literals.append(element)
    return tuple(literals)


def _fallback_keywords(node: ast.Call) -> dict[str, ast.expr] | None:
    """Return complete owned-fallback keywords for a direct constructor call."""
    if not isinstance(node.func, ast.Name) or node.func.id != "StaticModelFallback":
        return None
    keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg is not None}
    if "model_ids" not in keywords:
        return None
    for field in FALLBACK_METADATA_FIELDS:
        value = keywords.get(field)
        if (
            not isinstance(value, ast.Constant)
            or not isinstance(value.value, str)
            or not value.value.strip()
        ):
            return None
    return keywords


def _owned_fallback_model_literals(tree: ast.Module) -> set[ast.Constant]:
    """Return model literals confined to complete central fallback records."""
    fallback_model_values: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (keywords := _fallback_keywords(node)) is not None:
            fallback_model_values.append(keywords["model_ids"])

    exempt: set[ast.Constant] = set()
    for value in fallback_model_values:
        if (literals := _literal_string_nodes(value)) is not None:
            exempt.update(literals)

    # Only module-level tuples qualify; nested or conditional aliases fail closed.
    assignments: dict[str, tuple[ast.Constant, ...] | None] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                literals = _literal_string_nodes(statement.value)
                assignments[target.id] = literals if target.id not in assignments else None
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            literals = (
                _literal_string_nodes(statement.value) if statement.value is not None else None
            )
            assignments[statement.target.id] = (
                literals if statement.target.id not in assignments else None
            )

    valid_named_uses = {
        value
        for value in fallback_model_values
        if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load)
    }
    for name, literals in assignments.items():
        if literals is None:
            continue
        loads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name
        ]
        if loads and all(load in valid_named_uses for load in loads):
            exempt.update(literals)
    return exempt


def _scan_python(path: Path) -> list[Hit]:
    """Scan Python syntax-aware string literals in model contexts."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []

    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    exempt = (
        _owned_fallback_model_literals(tree)
        if _repo_relative(path) == OWNED_FALLBACK_PATH.as_posix()
        else set()
    )
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node in exempt:
            continue
        if not _is_model_context(node, parents):
            continue
        hits.extend(Hit(path, node.lineno, model) for model in _extract_models(node.value))
    return hits


def _scan_text(path: Path) -> list[Hit]:
    """Scan config/shell files for model-looking ids on model-looking lines."""
    try:
        lines = path.read_text().splitlines()
    except UnicodeDecodeError:
        return []

    hits: list[Hit] = []
    for line_number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        if "model" not in line.lower():
            continue
        hits.extend(Hit(path, line_number, model) for model in _extract_models(line))
    return hits


def scan(path: Path) -> list[Hit]:
    """Return hardcoded model hits in ``path``."""
    if (
        not path.is_file()
        or path.suffix not in SOURCE_EXTENSIONS
        or any(part in SKIP_PARTS for part in path.parts)
    ):
        return []
    if path.suffix == ".py":
        return _scan_python(path)
    if path.suffix in TEXT_EXTENSIONS:
        return _scan_text(path)
    return []


def _iter_scannable_paths() -> list[Path]:
    """Return every tracked source/config file in the lint surface."""
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--", *(root.as_posix() for root in SCAN_ROOTS)],
    )
    return [
        path
        for raw_path in output.decode().split("\0")
        if raw_path
        if (path := Path(raw_path)).suffix in SOURCE_EXTENSIONS
        if not any(part in SKIP_PARTS for part in path.parts)
    ]


def _load_allowlist(path: Path = ALLOWLIST_PATH) -> Counter[tuple[str, str]]:
    """Load allowed ``(path, model)`` occurrence counts."""
    allowed: Counter[tuple[str, str]] = Counter()
    if not path.exists():
        return allowed
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError(f"{path}:{line_number}: expected: <path> <model> <count>")
        rel_path, model, count_text = parts
        key = (rel_path, model)
        if key in allowed:
            raise ValueError(
                f"{path}:{line_number}: duplicate baseline entry for {rel_path} {model}"
            )
        try:
            allowed[key] = int(count_text)
        except ValueError as exc:
            raise ValueError(
                f"{path}:{line_number}: count must be an integer, got {count_text!r}"
            ) from exc
    return allowed


def _find_new_hits(hits: list[Hit], allowed: Counter[tuple[str, str]]) -> list[Hit]:
    """Return hits whose path/model count exceeds the curated baseline."""
    seen: Counter[tuple[str, str]] = Counter()
    new_hits: list[Hit] = []
    for hit in hits:
        key = (_repo_relative(hit.path), hit.model)
        seen[key] += 1
        if seen[key] > allowed[key]:
            new_hits.append(hit)
    return new_hits


def _find_stale_allowances(
    hits: list[Hit],
    allowed: Counter[tuple[str, str]],
) -> Counter[tuple[str, str]]:
    """Return baseline counts that exceed the current scan results."""
    actual = Counter((_repo_relative(hit.path), hit.model) for hit in hits)
    return allowed - actual


def main() -> int:
    """Scan the full supported surface and require an exact baseline."""
    paths = _iter_scannable_paths()
    hits = [hit for path in paths for hit in scan(path)]
    allowed = _load_allowlist()
    new_hits = _find_new_hits(hits, allowed)
    stale_allowances = _find_stale_allowances(hits, allowed)
    if not new_hits and not stale_allowances:
        return 0

    for hit in new_hits:
        sys.stdout.write(
            f"{hit.path}:{hit.line}: hardcoded model id `{hit.model}`; "
            "resolve from the configured provider/model catalog instead\n"
        )
    for (path, model), stale_count in sorted(stale_allowances.items()):
        actual_count = allowed[(path, model)] - stale_count
        sys.stdout.write(
            f"{ALLOWLIST_PATH}: stale allowance for `{model}` in {path}: "
            f"allows {allowed[(path, model)]}, found {actual_count}; "
            "lower or remove the baseline entry\n"
        )
    sys.stdout.write(
        "\nAvoid adding hardcoded model names outside tests. Unavoidable static aliases "
        "belong in complete StaticModelFallback records in omnigent/model_fallbacks.py. "
        "If this is an intentional temporary pin, document why and update "
        "dev/lint/hardcoded_model_allowlist.txt with the smallest path/model count.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
