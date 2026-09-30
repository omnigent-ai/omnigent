"""Shared text formatting and size limits for opt-in harness diagnostics."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

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
        "truncated": omitted_bytes > 0,
        "lines_omitted": len(lines) - len(retained),
        "bytes_omitted": omitted_bytes,
    }


@dataclass(frozen=True)
class SignInPrompt:
    """A sign-in prompt a launcher printed to the terminal before the agent started.

    :param url: The address the user must open, e.g.
        ``"https://dbcert.example.com/device"``.
    :param code: The one-time code shown next to it, e.g. ``"HQ7M-2KPD"``, or
        ``None`` when the prompt shows only an address.
    """

    url: str
    code: str | None = None


_SIGN_IN_URL = re.compile(r"https?://[^\s<>\"'`)\]]+")
# Addresses that are themselves OAuth or device-flow endpoints.
_SIGN_IN_URL_SHAPE = re.compile(
    r"oauth|authoriz|/login|signin|sign-in|/sso\b|/device\b|devicelogin", re.IGNORECASE
)
# Instructions a launcher prints around the address. An address with neither
# an auth-shaped path nor one of these nearby is ordinary output (a PR link, a
# docs pointer in an agent banner), not a gate.
_SIGN_IN_CUE = re.compile(
    r"sign[ -]?in|log[ -]?in|logging in|authenticat|verification code|one-time code"
    r"|device code|enter (?:the |this |your )?code"
    r"|open (?:the following|this) (?:url|link|address)",
    re.IGNORECASE,
)
_SIGN_IN_CODE_LINE = re.compile(r"\bcode\b", re.IGNORECASE)
# Lines a prompt prints below its address: the code, "waiting for sign-in",
# "press Enter". Any other output below the address means the launcher has
# moved on (dbcert's success lines, the agent's own banner) and the address
# still on screen is stale.
_SIGN_IN_TRAILER = re.compile(
    r"\bcode\b|waiting|wait for|press |browser|expire|sign[ -]?in|log[ -]?in|authenticat",
    re.IGNORECASE,
)
# Keys echoed into a waiting launcher's pane (Escape shows as ``^[``, arrows as
# ``^[[A``) are not output the launcher printed.
_ECHOED_CONTROL = re.compile(r"\^\[(?:\[[0-9;?]*[A-Za-z~]?)?")
# A device code: hyphenated groups, or one 6-9 character group mixing letters
# and digits. Pure words ("CODE", "ENTER") and short numbers never match.
_SIGN_IN_CODE = re.compile(
    r"\b(?:[A-Z0-9]{4,8}-[A-Z0-9]{4,8}|(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,9})\b"
)


def detect_sign_in_prompt(screen: str | None) -> SignInPrompt | None:
    """Find a device-style sign-in prompt in terminal screen text.

    A launcher wrapper can stop before the agent binary runs and print a
    device-code sign-in: an address to open and a short code to enter. The
    runner reads the pane while it waits for the agent to start; this lifts
    that prompt so the chat can offer to open the live link.

    Only an address that is itself an OAuth or device-flow endpoint, or that
    sits within a few lines of sign-in instructions, counts. A running agent's
    screen is full of ordinary addresses (pull requests, docs links in a
    banner) and none of those is a gate. The prompt must also still be the
    launcher's latest output: an agent that draws inline leaves the launcher's
    lines on screen above its own, so any output below the address other than
    the prompt's own trailing lines (the code, "waiting for sign-in") means the
    sign-in already completed and the address is stale. The code is the first
    device-code-shaped token on a line that mentions "code"; the address
    itself is never taken as the code. An address wrapped across two pane
    lines is truncated at the wrap, so callers should capture with wrapped
    rows joined.

    :param screen: ANSI-stripped terminal screen text, or ``None``.
    :returns: The prompt, or ``None`` when no pending sign-in address is on screen.
    """
    if not screen:
        return None
    lines = screen.splitlines()
    url: str | None = None
    for index, line in enumerate(lines):
        for match in _SIGN_IN_URL.finditer(line):
            candidate = match.group(0).rstrip(".,;:")
            nearby = "\n".join(lines[max(0, index - 3) : index + 3])
            if _SIGN_IN_URL_SHAPE.search(candidate) or _SIGN_IN_CUE.search(nearby):
                url = candidate
                break
        if url is not None:
            break
    if url is None:
        return None
    if any(
        re.search(r"\w", _ECHOED_CONTROL.sub("", line)) and not _SIGN_IN_TRAILER.search(line)
        for line in lines[index + 1 :]
    ):
        return None
    code: str | None = None
    for line in lines:
        if not _SIGN_IN_CODE_LINE.search(line):
            continue
        candidates = _SIGN_IN_CODE.findall(_SIGN_IN_URL.sub(" ", line))
        if candidates:
            code = candidates[0]
            break
    return SignInPrompt(url=url, code=code)


def sign_in_next_step(agent: str) -> str:
    """Phrase the next step for a card whose harness is waiting on a launcher sign-in.

    The address itself is deliberately left out: it is a one-time device-flow
    URL bound to the launcher process that printed it, so a copy saved in the
    transcript would go stale within minutes. The card fetches the live link
    from the host when the person clicks.

    :param agent: The harness's display name, e.g. ``"Codex"`` or ``"Claude Code"``.
    :returns: e.g. ``"Open the sign-in link and sign in. Codex continues on its
        own once the sign-in completes; then send your message again."``
    """
    return (
        f"Open the sign-in link and sign in. {agent} continues on its own once the "
        "sign-in completes; then send your message again."
    )
