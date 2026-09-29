"""Require explicit visibility on session-list client requests.

Python and JavaScript/TypeScript requests are checked with their language ASTs.
Shell checks cover literal list URLs, including endpoint loops used by smoke
scripts. Direct server tests and mock fixtures retain default-parameter coverage.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

from dev.lint._framework import disabled_rules_by_line
from dev.lint._session_list_visibility_python import scan as scan_python

RULE_NAME = "session-list-visibility"
HINT = (
    "Session-list requests must send visibility explicitly (all, mine, shared, or archived). "
    "Add it to sessions.list(...), the request's params, or its URLSearchParams builder. "
    "For a request the static checker cannot resolve, use a comment "
    "`custom-lint: disable=session-list-visibility -- <reason>` on the request line "
    "(or disable-next on the preceding line)."
)
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".sh"}
_LIVE_TEST_ROOTS = (
    "tests/e2e/",
    "tests/e2e_ui/",
    "tests/integration/",
    "tests/_helpers/",
    "tests/harness_bench/",
)
_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "assistant-ui",
    "build",
    "dist",
    "node_modules",
    "vendor",
}
_LIST_URL = re.compile(r"/v1/sessions/?(?:\?[^#]*)?(?:#.*)?$")
_SCRIPT = Path(__file__).with_name("session_list_visibility.mjs")


def is_scannable_path(path: Path) -> bool:
    """Include maintained client sources, excluding generated code and unit fixtures."""
    if path.suffix not in SOURCE_EXTENSIONS or _EXCLUDED_PARTS.intersection(path.parts):
        return False
    name = path.as_posix()
    if name.startswith(("omnigent/server/static/", "dev/lint/")):
        return False
    if name.startswith(_LIVE_TEST_ROOTS):
        return True
    return not (
        {"tests", "__tests__"}.intersection(path.parts)
        or path.name.startswith("test_")
        or ".test." in path.name
        or ".spec." in path.name
    )


def _iter_scannable_paths() -> list[Path]:
    """Find tracked sources, including newly staged files."""
    output = subprocess.check_output(["git", "ls-files", "-z"], text=True)
    return [path for raw in output.split("\0") if raw and is_scannable_path(path := Path(raw))]


def _scan_shell(source: str) -> list[tuple[int, str]]:
    """Check literal GET URLs in shell commands and endpoint iteration lists."""
    findings: list[tuple[int, str]] = []
    pending = ""
    start_line = 1
    disabled = disabled_rules_by_line(source)
    for line_number, line in enumerate(source.splitlines(), 1):
        if not pending:
            start_line = line_number
        pending += line.removesuffix("\\") + " "
        if line.endswith("\\"):
            continue
        statement, pending = pending, ""
        try:
            lexer = shlex.shlex(statement, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue
        if RULE_NAME in disabled.get(start_line, set()):
            continue
        commands: list[list[str]] = [[]]
        for token in tokens:
            if token in {";", "&", "&&", "||", "|"}:
                commands.append([])
            else:
                commands[-1].append(token)
        for command in commands:
            if not command or command[0] in {"echo", "printf"}:
                continue
            method = None
            for index, token in enumerate(command):
                if token in {"-X", "--request"} and index + 1 < len(command):
                    method = command[index + 1].upper()
                elif token.startswith("--request="):
                    method = token.partition("=")[2].upper()
                elif token.startswith("-X"):
                    method = token[2:].upper()
            if method in {"POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                continue
            data_flags = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"}
            data = []
            for index, token in enumerate(command):
                if token in data_flags:
                    data.append(command[index + 1] if index + 1 < len(command) else "")
                elif token.startswith("-d"):
                    data.append(token[2:])
                elif token.partition("=")[0] in data_flags:
                    data.append(token.partition("=")[2])
            query_flags = {"-G", "--get"}.intersection(command)
            if data and not query_flags and method is None:
                continue
            curl_query = query_flags and any(
                part.startswith("visibility=") and part != "visibility=" for part in data
            )
            for token in command:
                if any(char.isspace() for char in token) or not token.startswith(
                    ("/", "$", "http")
                ):
                    continue
                if not _LIST_URL.search(token):
                    continue
                query = parse_qs(token.partition("?")[2].partition("#")[0])
                if not query.get("visibility") and not curl_query:
                    findings.append((start_line, "session-list URL must specify visibility"))
    return findings


def _check_typescript(paths: list[Path]) -> list[str]:
    """Run the TypeScript AST checker, failing closed if its tooling is unavailable."""
    if not paths:
        return []
    try:
        result = subprocess.run(
            ["node", str(_SCRIPT), *(str(path) for path in paths)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return [
            f"{_SCRIPT.name}:1: cannot run visibility lint: {exc}; "
            "install Node and web dependencies"
        ]
    if result.returncode:
        return [
            f"{_SCRIPT.name}:1: visibility lint failed: {result.stderr.strip()}; "
            "run pnpm install --frozen-lockfile --filter web"
        ]
    try:
        findings = json.loads(result.stdout)
        if not isinstance(findings, list):
            raise ValueError("expected a JSON list of findings")
        return [f"{hit['path']}:{hit['line']}: {hit['message']}" for hit in findings]
    except (ValueError, KeyError, TypeError) as exc:
        return [f"{_SCRIPT.name}:1: invalid visibility lint output: {exc}"]


def check_paths(paths: list[Path]) -> list[str]:
    """Check explicit paths; source-scope filtering belongs to the caller."""
    messages: list[str] = []
    typescript: list[Path] = []
    for path in paths:
        if path.suffix not in {".py", ".sh"}:
            typescript.append(path)
            continue
        source = path.read_text()
        if "sessions" not in source:
            continue
        disabled = disabled_rules_by_line(source)
        hits = scan_python(source) if path.suffix == ".py" else _scan_shell(source)
        messages.extend(
            f"{path}:{line}: {message}"
            for line, message in hits
            if RULE_NAME not in disabled.get(line, set())
        )
    return messages + _check_typescript(typescript)


def check() -> list[str]:
    """Check the full maintained client surface for the custom-lint runner."""
    return check_paths(_iter_scannable_paths())


def main() -> int:
    """Check supplied files, or the full source surface when none are supplied."""
    messages = check_paths([Path(arg) for arg in sys.argv[1:]]) if sys.argv[1:] else check()
    if messages:
        print("\n".join(messages))
        print(HINT)
    return int(bool(messages))


if __name__ == "__main__":
    sys.exit(main())
