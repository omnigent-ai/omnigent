"""Flag hardcoded LLM model ids outside tests and owned fallbacks.

Unavoidable static aliases are accepted only inside complete
``StaticModelFallback`` records in the central fallback module.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MODEL_ID_RE = re.compile(
    r"""
    \b(?:
        databricks-(?:claude|gpt|gemini|llama|mistral|mixtral|deepseek|qwen|kimi|dbrx|grok|meta)-[a-z0-9][a-z0-9._:/-]*
      | system\.ai\.[a-z0-9][a-z0-9._:/-]*
      | (?:openai/)?(?:gpt-\d[a-z0-9._:/-]*|gpt-oss-[a-z0-9][a-z0-9._:/-]*)
      | o[134](?:-[a-z0-9][a-z0-9._:/-]*)?
      | claude-(?:opus|sonnet|haiku|fable|\d)[a-z0-9._:/-]*
      | gemini-\d[a-z0-9][a-z0-9._:/-]*
      | kimi-k\d[a-z0-9._:/-]*
      | qwen\d[a-z0-9][a-z0-9._:/-]*
      | llama-\d[a-z0-9][a-z0-9._:/-]*
      | mistral-[a-z0-9][a-z0-9._:/-]*
      | deepseek-[a-z0-9][a-z0-9._:/-]*
    )\b(?![a-z0-9._:/-])
    """,
    re.VERBOSE,
)

TEXT_EXTENSIONS = {".json", ".toml", ".yaml", ".yml", ".sh"}
SOURCE_EXTENSIONS = {".py", *TEXT_EXTENSIONS}
GENERATED_PATHS = {Path("openapi.json")}
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


def _docstring_nodes(tree: ast.Module) -> set[ast.Constant]:
    """Return literal nodes used as module, class, or function docstrings."""
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings: set[ast.Constant] = set()
    for owner in ast.walk(tree):
        if not isinstance(owner, owners) or not owner.body:
            continue
        first = owner.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(first.value)
    return docstrings


def _scan_python(path: Path) -> list[Hit]:
    """Scan every non-docstring Python string literal."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []

    exempt = (
        _owned_fallback_model_literals(tree)
        if _repo_relative(path) == OWNED_FALLBACK_PATH.as_posix()
        else set()
    )
    exempt.update(_docstring_nodes(tree))
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node in exempt:
            continue
        hits.extend(Hit(path, node.lineno, model) for model in _extract_models(node.value))
    return hits


def _scan_text(path: Path) -> list[Hit]:
    """Scan non-comment config and shell lines for model-looking ids."""
    try:
        lines = path.read_text().splitlines()
    except UnicodeDecodeError:
        return []

    hits: list[Hit] = []
    for line_number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        hits.extend(Hit(path, line_number, model) for model in _extract_models(line))
    return hits


def scan(path: Path) -> list[Hit]:
    """Return hardcoded model hits in ``path``."""
    if (
        not path.is_file()
        or path.suffix not in SOURCE_EXTENSIONS
        or Path(_repo_relative(path)) in GENERATED_PATHS
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
    output = subprocess.check_output(["git", "ls-files", "-z"])
    return [
        path
        for raw_path in output.decode().split("\0")
        if raw_path
        if (path := Path(raw_path)).suffix in SOURCE_EXTENSIONS
        if path not in GENERATED_PATHS
        if not any(part in SKIP_PARTS for part in path.parts)
    ]


def main() -> int:
    """Scan the full supported surface and reject every non-owned model id."""
    paths = _iter_scannable_paths()
    hits = [hit for path in paths for hit in scan(path)]
    if not hits:
        return 0

    for hit in hits:
        sys.stdout.write(
            f"{hit.path}:{hit.line}: hardcoded model id `{hit.model}`; "
            "resolve from the configured provider/model catalog instead\n"
        )
    sys.stdout.write(
        "\nAvoid adding hardcoded model names outside tests. Unavoidable static aliases "
        "belong in complete StaticModelFallback records in omnigent/model_fallbacks.py.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
