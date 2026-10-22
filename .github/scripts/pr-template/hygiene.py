"""Advisory checks for verbose PR bodies and added source comments."""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

MAX_BODY_WORDS = 600
MAX_COMMENT_LINES = 3

_HASH_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".rb"}
_SLASH_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".cjs",
    ".rs",
    ".scss",
    ".swift",
    ".ts",
    ".tsx",
}
_BLOCK_SUFFIXES = _SLASH_SUFFIXES | {".css"}
_HTML_SUFFIXES = {".html", ".htm", ".svg", ".xml"}
_HASH_NAMES = {"Dockerfile", "Makefile", "Justfile", "justfile"}
_WORD_RE = re.compile(r"\b\w+(?:[-'’]\w+)*\b")
_LINK_RE = re.compile(r"!?\[([^\]]+)\]\([^\n)]+\)")
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class CommentBlock:
    path: str
    line: int
    lines: int


def _visible_text(body: str) -> str:
    body = re.sub(
        r"<!--.*?-->",
        lambda match: "\n" * match.group().count("\n"),
        body,
        flags=re.DOTALL,
    )
    body = _LINK_RE.sub(r"\1", body)
    body = re.sub(r"https?://[^\s)>]+", "link", body)
    return re.sub(r"</?[A-Za-z][^>\n]*>", "", body)


def visible_word_count(body: str) -> int:
    return len(_WORD_RE.findall(_visible_text(body)))


def _prose_chunks(body: str) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    current: list[str] = []
    start = 0
    fence = ""

    def finish() -> None:
        if current:
            chunks.append((start, " ".join(current)))
            current.clear()

    for line_number, line in enumerate(_visible_text(body).splitlines(), 1):
        stripped = line.strip()
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else ""
        if marker:
            finish()
            fence = "" if fence == marker else marker
            continue
        if fence:
            continue
        if (
            not stripped
            or re.match(r"#{1,6}\s", stripped)
            or re.match(r"[-*+]\s+\[[ xX]\]", stripped)
        ):
            finish()
            continue
        bullet = re.match(r"(?:[-*+]|\d+\.)\s+", stripped)
        if bullet:
            finish()
            chunks.append((line_number, stripped[bullet.end() :]))
            continue
        if not current:
            start = line_number
        current.append(stripped.lstrip("> "))
    finish()
    return chunks


def repeated_prose(body: str) -> list[tuple[int, ...]]:
    locations: dict[str, set[int]] = {}
    for line_number, chunk in _prose_chunks(body):
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        candidates = [part for part in sentences if len(_WORD_RE.findall(part)) >= 12]
        if not candidates and len(_WORD_RE.findall(chunk)) >= 12:
            candidates = [chunk]
        for candidate in candidates:
            normalized = " ".join(word.casefold() for word in _WORD_RE.findall(candidate))
            locations.setdefault(normalized, set()).add(line_number)
    return [tuple(sorted(lines)) for lines in locations.values() if len(lines) > 1]


def _comment_state(content: str, path: str, mode: str) -> tuple[bool, str]:
    stripped = content.strip()
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    if mode:
        closing = "-->" if mode == "<!--" else "*/" if mode == "/*" else mode
        return True, "" if closing in stripped else mode
    if (
        suffix in _HASH_SUFFIXES or pathlib.PurePosixPath(path).name in _HASH_NAMES
    ) and stripped.startswith("#"):
        return True, ""
    if suffix == ".py":
        for quote in ('"""', "'''"):
            if stripped.startswith(quote):
                return True, "" if quote in stripped[len(quote) :] else quote
    if suffix in _SLASH_SUFFIXES and stripped.startswith("//"):
        return True, ""
    if suffix in _BLOCK_SUFFIXES and stripped.startswith("/*"):
        return True, "" if "*/" in stripped[2:] else "/*"
    if suffix in _BLOCK_SUFFIXES and stripped.startswith(("* ", "*/")):
        return True, ""
    if suffix in _HTML_SUFFIXES and stripped.startswith("<!--"):
        return True, "" if "-->" in stripped[4:] else "<!--"
    if suffix == ".sql" and stripped.startswith("--"):
        return True, ""
    return False, ""


def added_comment_blocks(diff: str) -> list[CommentBlock]:
    blocks: list[CommentBlock] = []
    path = ""
    line_number = 0
    block_start = 0
    block_lines = 0
    mode = ""

    def finish() -> None:
        nonlocal block_lines
        if block_lines > MAX_COMMENT_LINES:
            blocks.append(CommentBlock(path, block_start, block_lines))
        block_lines = 0

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            finish()
            path, line_number, mode = "", 0, ""
        elif line.startswith("+++ "):
            finish()
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@ "):
            finish()
            match = _HUNK_RE.match(line)
            line_number = int(match.group(1)) if match else 0
            mode = ""
        elif line.startswith("+") and line_number:
            is_comment, mode = _comment_state(line[1:], path, mode)
            if is_comment:
                if not block_lines:
                    block_start = line_number
                block_lines += 1
            else:
                finish()
            line_number += 1
        elif line.startswith(" ") and line_number:
            finish()
            line_number += 1
            mode = ""
        elif line.startswith("-"):
            finish()
    finish()
    return blocks


def check_hygiene(body: str | None, diff: str) -> list[str]:
    warnings: list[str] = []
    if body is not None:
        words = visible_word_count(body)
        if words > MAX_BODY_WORDS:
            warnings.append(
                f"PR description has {words} visible words (review threshold: {MAX_BODY_WORDS}); "
                "trim repeated detail, not necessary safety context."
            )
        for lines in repeated_prose(body):
            shown = ", ".join(str(line) for line in lines)
            warnings.append(f"Repeated PR prose at lines {shown}; keep the evidence once.")
    for block in added_comment_blocks(diff):
        warnings.append(
            f"Long added comment block at {block.path}:{block.line} "
            f"({block.lines} lines; review threshold: {MAX_COMMENT_LINES})."
        )
    return warnings


def _git_diff(base: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--unified=0",
            "--find-renames",
            f"{base}...HEAD",
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base ref, such as origin/main")
    parser.add_argument("--body-file", type=pathlib.Path)
    args = parser.parse_args()
    try:
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else None
        diff = _git_diff(args.base)
    except (OSError, RuntimeError) as exc:
        print(f"PR hygiene check could not run: {exc}", file=sys.stderr)
        return 2
    warnings = check_hygiene(body, diff)
    for warning in warnings:
        print(f"warning: {warning}")
    if not warnings:
        print("PR hygiene check: no warnings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
