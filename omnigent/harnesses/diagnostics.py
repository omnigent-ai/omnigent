"""Shared text formatting and size limits for opt-in harness diagnostics."""

from __future__ import annotations

import re
import unicodedata

from omnigent.process_logging import redact_log_text

DIAGNOSTIC_TAIL_BYTES = 64 * 1024
_TERMINAL_ESCAPE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_].*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-~]",
    re.DOTALL,
)
_URL_USERINFO = re.compile(r"(?i)((?<![\w+.-])[a-z][a-z0-9+.-]*://)[^/\s?#\"'<>]*@")
_HTTP_COOKIE = re.compile(
    r"(?i)((?<![\w-])(?:set-cookie|cookie)\b[\"']?[ \t]*[:=][ \t]*)"
    # Rust HeaderValue debug leaves existing backslashes before its escaped quotes.
    r'''("[^"\r\n]*(?:(?<=\\)"[^"\r\n]*)*(?<!\\)"'''
    r"|'[^'\r\n]*(?:(?<=\\)'[^'\r\n]*)*(?<!\\)'"
    r"|[^\r\n]*)"
)
_NATIVE_SEVERITY = re.compile(
    r"^(?:(?:\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?)\s+)?"
    r"(?:\[(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR)\]"
    r"|(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR))(?=[\s:])"
)
_NATIVE_SEVERITY_RANK = {
    "TRACE": 0,
    "DEBUG": 1,
    "INFO": 2,
    "WARN": 3,
    "WARNING": 3,
    "ERROR": 4,
}


def _redact_http_cookie(match: re.Match[str]) -> str:
    """Keep the field's quoting without retaining any of its cookie value."""
    quote = match[2][:1]
    replacement = f"{quote}[REDACTED]{quote}" if quote in {"'", '"'} else "[REDACTED]"
    return match[1] + replacement


def sanitize_diagnostic_text(text: str) -> str:
    """Strip terminal controls and redact known credential patterns."""
    text = _TERMINAL_ESCAPE.sub("", text).translate(str.maketrans("\t\v\f", "   "))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        char for char in text if char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"}
    ).rstrip()
    cleaned = _URL_USERINFO.sub(r"\1[REDACTED]@", cleaned)
    cleaned = _HTTP_COOKIE.sub(_redact_http_cookie, cleaned)
    return redact_log_text(cleaned, include_whitespace_credentials=True)


def native_diagnostic_severity(entries: list[str]) -> str | None:
    """Return the highest leading native log severity in retained records."""
    highest: str | None = None
    highest_rank = -1
    for entry in entries:
        first_line = entry.lstrip().split("\n", 1)[0]
        match = _NATIVE_SEVERITY.match(first_line)
        if match is None:
            continue
        marker = match[1] or match[2]
        if marker is None:
            continue
        rank = _NATIVE_SEVERITY_RANK[marker]
        if rank > highest_rank:
            highest = "WARN" if marker == "WARNING" else marker
            highest_rank = rank
    return highest


def bounded_diagnostic_tail(entries: list[str]) -> dict[str, object]:
    """Retain recent redacted entries within a 64 KiB UTF-8 export budget."""
    lines = [sanitize_diagnostic_text(line) for line in entries]
    retained: list[str] = []
    remaining = DIAGNOSTIC_TAIL_BYTES
    for line in reversed(lines):
        encoded = line.encode("utf-8")
        required = len(encoded) + bool(retained)
        if required > remaining:
            # Keep complete entries unless even the newest entry exceeds the budget.
            if not retained:
                retained.append(encoded[-remaining:].decode("utf-8", errors="ignore"))
            break
        retained.append(line)
        remaining -= required

    tail = "\n".join(reversed(retained))
    omitted_bytes = len("\n".join(lines).encode("utf-8")) - len(tail.encode("utf-8"))
    return {
        "tail": tail,
        "native_severity": native_diagnostic_severity(list(reversed(retained))),
        "truncated": omitted_bytes > 0,
        "lines_omitted": len(lines) - len(retained),
        "bytes_omitted": omitted_bytes,
    }
