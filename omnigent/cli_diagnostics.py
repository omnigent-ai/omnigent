"""
Always-on CLI diagnostics log.

Captures exceptions, warnings, and diagnostic info to a per-invocation
log file under ``<data-dir>/logs/cli/cli-*.log``. Separate from the
``--log`` conversation JSON transcript and the ``--debug-events`` SSE
tape — this layer is always on so crash context is available even when
the user didn't know to enable debugging ahead of time.

**Privacy contract:** At ``INFO`` level, no user prompts, message text,
tool arguments, or conversation content are logged. Only lifecycle
events (startup, shutdown, error tracebacks) appear. A redaction
filter strips obvious secrets (``Authorization`` headers, bearer
tokens, env vars matching ``*_TOKEN`` / ``*_API_KEY`` / ``*SECRET*``,
``sk-*``, ``dapi*``). Redaction runs on the fully-formatted output
(after ``%``-interpolation and traceback rendering) so secrets in
``logger.info("key=%s", val)`` args and exception frames are covered.

Log files are created with ``0o600`` permissions and pruned to keep
at most :data:`MAX_LOG_FILES` entries. A ``latest-cli.log`` symlink
is maintained for quick access.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from omnigent.cli_invocation import cli_invocation
from omnigent.process_logging import (
    TerminalLogFormatter,
    effective_log_level,
    env_truthy,
    process_log_dir,
    terminal_supports_color,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Destination subdirectory under ``<data-dir>/logs`` for CLI diagnostics.
_LOG_DESTINATION = "cli"

#: Maximum number of ``cli-*.log`` files kept before pruning.
MAX_LOG_FILES = 20

#: Per-file size cap before rotation (bytes).
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB

#: Backup count for the rotating handler (per invocation — rarely
#: hits this, but guards runaway loops).
_BACKUP_COUNT = 1

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliLogContext:
    """
    Returned by :func:`setup_cli_logging` to let callers reference
    the log path (e.g. in error messages, the Ctrl+O debug overlay,
    or ``/report``).

    :param path: Absolute path to the current invocation's log file,
        e.g. ``~/.omnigent/logs/cli-20260518-143012-12345-a1b2c3.log``.
    :param invocation_id: Short unique id for this CLI run, e.g.
        ``"12345-a1b2c3"``.
    """

    path: Path
    invocation_id: str


# Module-level holder so ``current_cli_log_path()`` works without
# threading the context through every call site.
_current: CliLogContext | None = None


@dataclass(frozen=True)
class _LoggingStreamSnapshot:
    """
    Original stream for a logging handler retargeted during TUI stderr capture.

    :param handler: Stream handler whose output was redirected.
    :param stream: Stream the handler wrote to before redirection.
    """

    handler: logging.StreamHandler[io.TextIOBase]
    stream: io.TextIOBase


_redirected_logging_streams: list[_LoggingStreamSnapshot] = []

# ---------------------------------------------------------------------------
# Secret redaction filter
# ---------------------------------------------------------------------------

#: Patterns that match values likely to be secrets.  Applied to every
#: log record's formatted message before it hits the file.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # URL userinfo is confined to one whitespace-bounded authority.
    re.compile(
        r"(?i)(https?://)[^/\s?#]*"
        r"(?=@(?:\[[^/\s@?#]+\]|[^/\s@?#:]+)(?::[0-9]+)?(?:[/?#\s]|$))"
    ),
]
_REDACTED = "[REDACTED]"
_MAX_UNTERMINATED_AUTHORIZATION_FOLDS = 1
_MAX_CREDENTIAL_WORD_GAPS = 1
_MAX_ENV_VALUE_NEWLINE_FOLDS = 1
# Keyed anchors (env-style ``key=``/``key:`` and gap-separated ``Bearer``)
# redact any non-empty value: the key names the value sensitive, so no
# length floor applies (parity with the regexes this scanner replaced).
# Length floors remain only on the unkeyed fixed-prefix families.
_KEYED_VALUE_MIN_LENGTH = 1
# A Bearer value glued directly to the anchor (no word gap) is either an
# obfuscated credential whose splitters were stripped upstream or ordinary
# prose like "bearers" — the old floor separates the two shapes.
_GAPLESS_BEARER_MIN_LENGTH = 8
_KEYED_VALUE_ALPHABET = "._~+/=-"
_WORD_GAP_TEXT = (
    "\t \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u202f\u205f\u3000"
)
_WORD_GAP_CHARACTERS = frozenset(_WORD_GAP_TEXT)
_CREDENTIAL_ANCHORS: tuple[tuple[str, str, bool], ...] = tuple(
    [("github", f"gh{kind}_", False) for kind in "pousr"]
    + [("databricks", "dapi", False)]
    + [("slack", f"xox{kind}-", False) for kind in "baprs"]
    + [("aws", prefix, False) for prefix in ("AKIA", "ASIA")]
    + [("sk", "sk-", False), ("bearer", "bearer", True)]
)
_CREDENTIAL_ANCHORS_BY_INITIAL: dict[str, tuple[tuple[str, str, bool], ...]] = {
    initial: tuple(
        anchor
        for anchor in _CREDENTIAL_ANCHORS
        if anchor[1][0] == initial or (anchor[2] and anchor[1][0].upper() == initial)
    )
    for initial in "gdxAsbB"
}
_CREDENTIAL_ANCHOR_INITIAL_RE = re.compile(r"[gdxAsbB]")
_CREDENTIAL_ANCHOR_INITIAL_WITHOUT_BEARER_RE = re.compile(r"[gdxAs]")
_AUTHORIZATION_INITIAL_RE = re.compile(r"[aA]")
_ENV_CREDENTIAL_ANCHOR_RE = re.compile(
    rf"(?<!\w)\w*(?i:token|api_key|secret|password)[{re.escape(_WORD_GAP_TEXT)}]*[:=]"
)
_BEARER_ALPHABET_RE = r"[A-Za-z0-9._~+/=-]"
# Removing every non-gap splitter makes this a conservative impossibility
# check; the forward scanner below remains the only redaction matcher.
_BEARER_SHADOW_STRIP_RE = re.compile(rf"[^{re.escape(_WORD_GAP_TEXT)}A-Za-z0-9._~+/=\-\r\n]+")
_BEARER_SHADOW_POSSIBLE_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_])bearer"
    rf"[{re.escape(_WORD_GAP_TEXT)}]{{0,{1 + _MAX_CREDENTIAL_WORD_GAPS}}}"
    rf"{_BEARER_ALPHABET_RE}"
)
_AUTHORIZATION_SHADOW_STRIP_RE = re.compile(rf"[^{re.escape(_WORD_GAP_TEXT)}A-Za-z0-9:=\r\n]+")
_AUTHORIZATION_SHADOW_POSSIBLE_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_])authorization[{re.escape(_WORD_GAP_TEXT)}\r\n]*[:=]"
)


def _is_word_gap(char: str) -> bool:
    """Return whether *char* is genuine horizontal word-separating whitespace."""
    return char in _WORD_GAP_CHARACTERS


def _is_ascii_credential_character(char: str) -> bool:
    """Return whether *char* belongs to the shared ASCII credential alphabet."""
    return "0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z"


def _anchor_character_matches(char: str, expected: str, *, casefold: bool) -> bool:
    """Match one required ASCII anchor character."""
    return char.casefold() == expected if casefold else char == expected


def _interleaved_anchor_end(
    text: str,
    start: int,
    literal: str,
    *,
    casefold: bool,
) -> int | None:
    """Match *literal* while absorbing every non-boundary splitter between characters."""
    if start > 0 and (_is_ascii_credential_character(text[start - 1]) or text[start - 1] == "_"):
        return None
    index = start
    for expected in literal:
        while index < len(text):
            char = text[index]
            if _anchor_character_matches(char, expected, casefold=casefold):
                index += 1
                break
            if char in "\r\n" or _is_word_gap(char) or _is_ascii_credential_character(char):
                return None
            index += 1
        else:
            return None
    return index


def _find_credential_anchor(
    text: str,
    start: int,
    *,
    include_bearer: bool,
) -> tuple[int, int, str] | None:
    """Find the next fixed credential anchor with property-agnostic splitters."""
    initial_re = (
        _CREDENTIAL_ANCHOR_INITIAL_RE
        if include_bearer
        else _CREDENTIAL_ANCHOR_INITIAL_WITHOUT_BEARER_RE
    )
    search_start = start
    while initial_match := initial_re.search(text, search_start):
        anchor_start = initial_match.start()
        for family, literal, casefold in _CREDENTIAL_ANCHORS_BY_INITIAL.get(
            text[anchor_start], ()
        ):
            anchor_end = _interleaved_anchor_end(
                text,
                anchor_start,
                literal,
                casefold=casefold,
            )
            if anchor_end is not None:
                return anchor_start, anchor_end, family
        search_start = anchor_start + 1
    return None


def _credential_span_end(
    text: str,
    start: int,
    *,
    alphabet: str,
    minimum: int,
    allow_lowercase: bool = True,
    max_word_gaps: int = _MAX_CREDENTIAL_WORD_GAPS,
) -> int | None:
    """
    Scan one credential body in a single forward pass.

    Every non-alphabet splitter is absorbed except a newline. Genuine word gaps
    have a small pre-floor budget and end an already-confirmed credential span.
    """
    index = start
    alphabet_count = 0
    word_gaps = 0
    while index < len(text):
        char = text[index]
        if char in "\r\n":
            break
        in_alphabet = (
            "0" <= char <= "9"
            or "A" <= char <= "Z"
            or (allow_lowercase and "a" <= char <= "z")
            or char in alphabet
        )
        if in_alphabet:
            alphabet_count += 1
            index += 1
            continue
        if alphabet_count >= minimum:
            break
        if char in _WORD_GAP_CHARACTERS:
            word_gaps += 1
            if word_gaps > max_word_gaps:
                break
        index += 1
    return index if alphabet_count >= minimum else None


def _bearer_value_start(text: str, anchor_end: int) -> tuple[int, int]:
    """Return the preserved prefix end and scan start for keyless Bearer auth."""
    scan_start = anchor_end
    if scan_start < len(text) and _is_word_gap(text[scan_start]):
        scan_start += 1
    preserved_end = scan_start
    while text.startswith("(REDACTED)", scan_start):
        marker_end = scan_start + len("(REDACTED)")
        if marker_end >= len(text) or not _is_word_gap(text[marker_end]):
            break
        scan_start = marker_end + 1
        preserved_end = scan_start
    return preserved_end, scan_start


def _is_keyed_value_character(char: str) -> bool:
    """Return whether *char* belongs to the keyed-value span alphabet."""
    return _is_ascii_credential_character(char) or char in _KEYED_VALUE_ALPHABET


def _marker_glue_span_start(text: str, start: int) -> int | None:
    """
    Resolve a redaction marker sitting at a keyed value's start.

    :param text: Text being scanned.
    :param start: Position of the keyed value.
    :returns: The position span scanning must start from, or ``None`` when
        the value is exactly this module's own completed ``[REDACTED]``
        marker (already redacted on a previous pass; re-scanning would break
        idempotence for double-redacting log paths). A marker glued directly
        to span-alphabet text is attacker input: scanning resumes after the
        marker so the whole glued token is consumed into one redacted span
        instead of leaving an un-redacted suffix behind.
    """
    for marker in (_REDACTED, "(REDACTED)"):
        if not text.startswith(marker, start):
            continue
        boundary = start + len(marker)
        word_gaps = 0
        while boundary < len(text):
            char = text[boundary]
            if char in "\r\n":
                return None
            if _is_keyed_value_character(char):
                return boundary
            if _is_word_gap(char):
                if marker == _REDACTED:
                    return None
                word_gaps += 1
                if word_gaps > _MAX_CREDENTIAL_WORD_GAPS:
                    return None
            boundary += 1
        return None
    return start


def _redact_scanned_credentials(
    text: str,
    *,
    bearer_soft_gap_marker: str | None = None,
) -> str:
    """Redact prefixed credentials with bounded, forward-only span scans."""
    parts: list[str] = []
    cursor = 0
    search_start = 0
    bearer_shadow = _BEARER_SHADOW_STRIP_RE.sub("", text)
    scan_bearer = _BEARER_SHADOW_POSSIBLE_RE.search(bearer_shadow) is not None
    while anchor := _find_credential_anchor(
        text,
        search_start,
        include_bearer=scan_bearer,
    ):
        anchor_start, anchor_end, family = anchor
        search_start = anchor_end
        if family in {"github", "databricks", "slack", "aws", "sk"}:
            alphabet, minimum = {
                "github": ("", 20),
                "databricks": ("", 10),
                "slack": ("-", 10),
                "aws": ("", 16),
                "sk": ("_-", 10),
            }[family]
            span_end = _credential_span_end(
                text,
                anchor_end,
                alphabet=alphabet,
                minimum=minimum,
                allow_lowercase=family != "aws",
            )
            if span_end is not None:
                parts.extend((text[cursor:anchor_start], _REDACTED))
                cursor = span_end
                search_start = span_end
                continue

        if family == "bearer":
            if not scan_bearer:
                continue
            gapped = anchor_end < len(text) and _is_word_gap(text[anchor_end])
            if bearer_soft_gap_marker is not None and text.startswith(
                bearer_soft_gap_marker, anchor_end
            ):
                soft_gapped = True
                preserved_end = anchor_end
                body_start = anchor_end + len(bearer_soft_gap_marker)
            else:
                soft_gapped = False
                preserved_end, body_start = _bearer_value_start(text, anchor_end)
            exotic_gapped = (
                anchor_end < len(text)
                and text[anchor_end] not in "\r\n"
                and not _is_keyed_value_character(text[anchor_end])
            )
            floorless = gapped or soft_gapped or exotic_gapped
            span_start = _marker_glue_span_start(text, body_start)
            if span_start is None:
                continue
            span_end = _credential_span_end(
                text,
                span_start,
                alphabet=_KEYED_VALUE_ALPHABET,
                minimum=_KEYED_VALUE_MIN_LENGTH if floorless else _GAPLESS_BEARER_MIN_LENGTH,
                max_word_gaps=_MAX_CREDENTIAL_WORD_GAPS if floorless else 0,
            )
            if span_end is not None:
                if (
                    soft_gapped
                    and text[span_start:span_end].isascii()
                    and text[span_start:span_end].isalpha()
                    and span_end < len(text)
                    and _is_word_gap(text[span_end])
                ):
                    continue
                parts.extend((text[cursor:preserved_end], _REDACTED))
                cursor = span_end
                search_start = span_end
                continue

    parts.append(text[cursor:])
    return _redact_scanned_env_credentials("".join(parts))


def _unquoted_keyed_value_end(text: str, start: int) -> int | None:
    """Return the end of one non-empty, whitespace-bounded keyed value."""
    index = start
    while index < len(text) and not text[index].isspace():
        index += 1
    return index if index > start else None


def _redact_scanned_env_credentials(text: str) -> str:
    """Redact env-style values in a separate fixed linear pass."""
    parts: list[str] = []
    cursor = 0
    search_start = 0
    while match := _ENV_CREDENTIAL_ANCHOR_RE.search(text, search_start):
        search_start = match.end()
        body_start = match.end()
        while body_start < len(text) and _is_word_gap(text[body_start]):
            body_start += 1
        # Fold the value across a bounded number of newlines after the
        # anchor, mirroring Authorization folding — hook text preserves
        # line boundaries, so `password=\n<value>` must still match.
        newline_folds = 0
        while (
            newline_folds < _MAX_ENV_VALUE_NEWLINE_FOLDS
            and body_start < len(text)
            and text[body_start] in "\r\n"
        ):
            newline_end = body_start + 1
            if text[body_start] == "\r" and text.startswith("\n", newline_end):
                newline_end += 1
            body_start = newline_end
            newline_folds += 1
            while body_start < len(text) and _is_word_gap(text[body_start]):
                body_start += 1
        span_start = _marker_glue_span_start(text, body_start)
        if span_start is None:
            continue
        quote = (
            text[span_start] if span_start < len(text) and text[span_start] in {'"', "'"} else None
        )
        if quote is not None:
            # A quoted value runs to its closing quote across bounded folds,
            # mirroring quoted-Authorization handling — otherwise the
            # continuation lines of a multiline secret survive redaction.
            value_end, closed = _authorization_value_end(text, span_start + 1, quote)
            parts.extend((text[cursor:span_start], quote, _REDACTED))
            if closed:
                parts.append(quote)
                cursor = value_end + 1
            else:
                cursor = value_end
            search_start = cursor
            continue
        span_end = _unquoted_keyed_value_end(text, span_start)
        if span_end is None:
            continue
        parts.extend((text[cursor:body_start], _REDACTED))
        cursor = span_end
        search_start = span_end
    parts.append(text[cursor:])
    return "".join(parts)


def _authorization_value_end(text: str, start: int, quote: str | None) -> tuple[int, bool]:
    """
    Find one Authorization value boundary with a forward-only scan.

    Quoted values end at an unescaped matching quote; unquoted values end at
    end-of-line. In either form, CR, LF, or CRLF followed by indentation
    continues the value. A closed quote may span any number of folds; an
    unclosed quote or an unquoted value retains at most one continuation
    line of redaction, so an indented traceback after the value survives.
    """
    index = start
    unterminated_end: int | None = None
    folded_lines = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                return index, True
            if char == "\\" and index + 1 < len(text) and text[index + 1] not in "\r\n":
                index += 2
                continue
        if char in "\r\n":
            newline_end = index + 1
            if char == "\r" and newline_end < len(text) and text[newline_end] == "\n":
                newline_end += 1
            if newline_end < len(text) and text[newline_end] in " \t":
                folded_lines += 1
                if quote is None and folded_lines > _MAX_UNTERMINATED_AUTHORIZATION_FOLDS:
                    return index, False
                if (
                    folded_lines > _MAX_UNTERMINATED_AUTHORIZATION_FOLDS
                    and unterminated_end is None
                ):
                    unterminated_end = index
                index = newline_end
                continue
            return unterminated_end if unterminated_end is not None else index, False
        index += 1
    return unterminated_end if unterminated_end is not None else index, False


def _find_authorization_prefix(text: str, start: int) -> tuple[int, int] | None:
    """Find an Authorization key and the start of its value."""
    search_start = start
    while initial_match := _AUTHORIZATION_INITIAL_RE.search(text, search_start):
        anchor_start = initial_match.start()
        anchor_end = _interleaved_anchor_end(
            text,
            anchor_start,
            "authorization",
            casefold=True,
        )
        if anchor_end is None:
            search_start = anchor_start + 1
            continue

        delimiter = anchor_end
        while delimiter < len(text):
            char = text[delimiter]
            if char in ":=":
                break
            if _is_ascii_credential_character(char):
                break
            delimiter += 1
        if delimiter >= len(text) or text[delimiter] not in ":=":
            search_start = anchor_start + 1
            continue

        value_start = delimiter + 1
        while value_start < len(text) and _is_word_gap(text[value_start]):
            value_start += 1
        while value_start < len(text) and text[value_start] in "\r\n":
            newline_end = value_start + 1
            if text[value_start] == "\r" and text.startswith("\n", newline_end):
                newline_end += 1
            if newline_end >= len(text) or not _is_word_gap(text[newline_end]):
                break
            value_start = newline_end + 1
            while value_start < len(text) and _is_word_gap(text[value_start]):
                value_start += 1
        return anchor_start, value_start
    return None


def _redact_authorization_values(text: str) -> str:
    """Redact explicit Authorization values using linear boundary scans."""
    shadow = _AUTHORIZATION_SHADOW_STRIP_RE.sub("", text)
    if _AUTHORIZATION_SHADOW_POSSIBLE_RE.search(shadow) is None:
        return text
    parts: list[str] = []
    cursor = 0
    search_start = 0
    while anchor := _find_authorization_prefix(text, search_start):
        _, value_start = anchor
        parts.append(text[cursor:value_start])
        quote = (
            text[value_start]
            if value_start < len(text) and text[value_start] in {'"', "'"}
            else None
        )
        content_start = value_start + 1 if quote is not None else value_start
        value_end, closed = _authorization_value_end(text, content_start, quote)
        if quote is not None:
            parts.append(quote)
        parts.append(_REDACTED)
        if quote is not None and closed:
            parts.append(quote)
            cursor = value_end + 1
        else:
            cursor = value_end
        search_start = cursor
    parts.append(text[cursor:])
    return "".join(parts)


def redact_secrets(
    text: str,
    *,
    bearer_soft_gap_marker: str | None = None,
) -> str:
    """
    Replace secret-shaped substrings in *text* with :data:`_REDACTED`.

    :param text: Arbitrary log text (may include tracebacks).
    :param bearer_soft_gap_marker: Trusted bridge-only separator preserving
        whether an invisible gap followed a ``Bearer`` anchor.
    :returns: Scrubbed text.
    """

    text = _redact_authorization_values(text)
    text = _redact_scanned_credentials(
        text,
        bearer_soft_gap_marker=bearer_soft_gap_marker,
    )
    for pat in _SECRET_PATTERNS:
        text = pat.sub(
            lambda m: m.group(1) + _REDACTED if m.lastindex else _REDACTED,
            text,
        )
    return text


class _RedactingFormatter(TerminalLogFormatter):
    """
    Formatter that scrubs obvious secrets from the *final* formatted
    output — after ``%``-interpolation of ``record.args`` and after
    traceback rendering.

    A ``logging.Filter`` on ``record.msg`` would run *before*
    formatting, so secrets passed as ``logger.info("key=%s", secret)``
    or appearing in exception tracebacks would slip through.
    Overriding :meth:`format` is the correct interception point
    because the base class returns the fully-assembled string
    (message + traceback) and nothing downstream mutates it before
    the handler writes.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Format *record* then redact secrets from the result.

        :param record: The log record to format.
        :returns: Formatted, redacted string ready for the handler.
        """
        return redact_secrets(super().format(record))


class _RedactingStderr(io.TextIOBase):
    """
    Text stream that redirects stderr writes to the CLI log with redaction.

    :param inner: Open log file handle receiving redirected stderr writes.
    :param original: Original terminal stderr stream to restore after the TUI
        exits.
    """

    def __init__(self, inner: io.TextIOWrapper, original: io.TextIOBase) -> None:
        """
        Create a redacting wrapper around an open log file.

        :param inner: Log file handle opened in append text mode.
        :param original: Original terminal stderr stream saved for restoration.
        """
        self._inner = inner
        self._original_stderr = original

    def write(self, text: str) -> int:
        """
        Redact and write a stderr text chunk to the log file.

        :param text: Text sent to ``sys.stderr.write``.
        :returns: The length of the caller's original text.
        """
        self._inner.write(redact_secrets(text))
        return len(text)

    def flush(self) -> None:
        """
        Flush the wrapped log file.

        :returns: ``None``.
        """
        self._inner.flush()

    def close(self) -> None:
        """
        Close the wrapped log file.

        :returns: ``None``.
        """
        if self.closed:
            return
        super().close()
        if not self._inner.closed:
            self._inner.close()

    def isatty(self) -> bool:
        """
        Report that redirected stderr is not an interactive terminal.

        :returns: Always ``False`` because writes are redirected to a file.
        """
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _log_dir() -> Path:
    """
    Return the CLI diagnostics log directory.

    Uses the shared Omnigent runtime data dir so ``OMNIGENT_DATA_DIR``
    isolates diagnostics with the DB, artifacts, and process logs.

    :returns: ``<data-dir>/logs/cli``.
    """
    return process_log_dir(_LOG_DESTINATION)


def setup_cli_logging(argv: list[str]) -> CliLogContext:
    """
    Configure the always-on CLI diagnostics log.

    Creates the log directory, opens a per-invocation log file,
    installs the redaction filter, wires up the ``omnigent`` and
    ``omnigent_ui_sdk`` logger hierarchies, and prunes old log
    files beyond :data:`MAX_LOG_FILES`.

    Call as early as possible in :func:`omnigent.cli.main` —
    before Click dispatch — so unhandled startup exceptions are
    captured.

    :param argv: ``sys.argv[1:]`` snapshot, logged as the first line
        for post-mortem context.
    :returns: A :class:`CliLogContext` with the log path and
        invocation id.
    """
    global _current

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    invocation_id = f"{os.getpid():05d}-{_short_id()}"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"cli-{timestamp}-{invocation_id}.log"
    log_path = log_dir / filename

    # Rotating handler — caps a single invocation at MAX_LOG_BYTES.
    log_level = effective_log_level()
    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(log_level)
    # Best-effort 0600 permissions on the log file.
    with contextlib.suppress(OSError):
        os.chmod(log_path, 0o600)

    handler.setFormatter(
        _RedactingFormatter(
            use_colors=False,
        )
    )

    stream_handler: logging.Handler | None = None
    if env_truthy(os.environ.get("OMNIGENT_LOG_TO_STDERR")) and sys.stderr.isatty():
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(log_level)
        stream_handler.setFormatter(_RedactingFormatter(use_colors=terminal_supports_color()))

    # Wire our two package hierarchies at the effective level so their records reach
    # the file handler.
    for name in ("omnigent", "omnigent_ui_sdk"):
        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        logger.addHandler(handler)
        if stream_handler is not None:
            logger.addHandler(stream_handler)
        logger.propagate = False

    # Suppress noisy third-party loggers that are commonly present.
    for name in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # NOTE: stderr is NOT redirected here — that's scoped to the TUI
    # lifetime via ``redirect_stderr_to_log`` /
    # ``restore_stderr``.  Non-TUI subcommands (``server``,
    # ``version``, one-shot ``run -p``) keep stderr on the
    # terminal so Click errors, tracebacks, and Ctrl-C output
    # remain visible.

    # Symlink latest-cli.log → this file.
    _update_latest_symlink(log_dir, log_path)

    # Prune old cli-*.log files beyond the cap.
    _prune_old_logs(log_dir)

    ctx = CliLogContext(path=log_path, invocation_id=invocation_id)
    _current = ctx

    # First line: the invocation context for post-mortem debugging.
    root = logging.getLogger("omnigent.cli_diagnostics")
    root.info("CLI start — argv=%s pid=%d", argv, os.getpid())

    return ctx


def current_cli_log_path() -> Path | None:
    """
    Return the active invocation's log path, or ``None`` if
    :func:`setup_cli_logging` has not been called yet.

    :returns: Absolute path to the current ``cli-*.log`` file, or
        ``None``.
    """
    return _current.path if _current is not None else None


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
    Install a custom exception handler on *loop* that logs unhandled
    asyncio exceptions (e.g. fire-and-forget tasks that raise) to
    the CLI diagnostics log instead of printing to stderr.

    :param loop: The running event loop (typically from
        ``asyncio.get_running_loop()`` inside the REPL's async
        context).
    """
    logger = logging.getLogger("omnigent.asyncio")

    def _handler(
        loop: asyncio.AbstractEventLoop,  # noqa: ARG001 — signature mandated by asyncio
        context: dict[str, object],
    ) -> None:
        """
        Log unhandled asyncio exceptions with full traceback.

        :param loop: The event loop that caught the exception.
        :param context: Exception context dict from asyncio.
        """
        exc = context.get("exception")
        msg = context.get("message", "Unhandled asyncio exception")
        if isinstance(exc, BaseException):
            logger.error("%s", msg, exc_info=exc)
        else:
            logger.error("asyncio: %s — context=%s", msg, context)

    loop.set_exception_handler(_handler)


def log_cli_error_hint(exc: BaseException) -> None:
    """
    Print a one-line pointer to the log file on stderr.

    Call this in the outermost exception handler (``main()``) so the
    user knows where to find the full traceback. No-op if
    :func:`setup_cli_logging` was never called.

    :param exc: The exception that triggered the hint.
    """
    path = current_cli_log_path()
    if path is None:
        return
    # Log the full traceback to the file.
    log_cli_exception(exc, prefix="Fatal CLI error")
    # One quiet line on the real terminal for the user.  sys.stderr
    # may have been redirected to the log file, so reach through to
    # the original.
    dest = getattr(sys.stderr, "_original_stderr", sys.stderr)
    print(f"Details logged to {path}", file=dest)


def print_stale_host_hint() -> None:
    """
    Print a one-line stale-host recovery hint on stderr.

    Used by the top-level :func:`omnigent.cli.main` exception
    handlers so errors that wrap runner startup failures include the
    recovery path for stale host processes. Those processes can retain
    invalid server authentication and cause runner tunnel rejections.

    Like :func:`log_cli_error_hint`, the line is written through
    to the original ``stderr`` so it survives any logging-driven
    stderr redirection that may have already happened during the
    failing turn.

    :returns: ``None``.
    """
    dest = getattr(sys.stderr, "_original_stderr", sys.stderr)
    print(
        "If this is a runner tunnel rejection (HTTP 401), stale host processes "
        f"may be the cause. Run `{cli_invocation()} stop` to stop existing Omnigent "
        "host instances, then try again.",
        file=dest,
    )


def log_cli_exception(exc: BaseException, *, prefix: str = "CLI error") -> None:
    """
    Write a CLI exception and traceback to the active diagnostics log.

    Use this for expected CLI exception boundaries that should be
    visible in ``cli-*.log`` without necessarily printing the
    user-facing "Details logged..." hint.

    :param exc: Exception to record, e.g. ``click.ClickException("bad")``.
    :param prefix: Log message prefix, e.g. ``"Fatal CLI error"``.
    :returns: ``None``.
    """
    if current_cli_log_path() is None:
        return
    logging.getLogger("omnigent.cli_diagnostics").error(
        "%s: %s",
        prefix,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def redirect_stderr_to_log() -> None:
    """
    Replace ``sys.stderr`` with a file object appending to the active
    CLI log.

    Call when the TUI takes over the terminal.  Every write that
    previously went to the terminal's stderr now lands in the CLI
    diagnostic log.  The original stderr is saved so
    :func:`restore_stderr` can bring it back when the TUI exits.

    No-op if :func:`setup_cli_logging` has not been called yet or
    if the log file cannot be opened.
    """
    path = current_cli_log_path()
    if path is None:
        return
    try:
        log_fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — intentionally kept open for TUI lifetime
    except OSError as exc:
        log_cli_exception(exc, prefix="Failed to redirect stderr to CLI log")
        return
    original = cast(io.TextIOBase, sys.stderr)
    redirected = _RedactingStderr(log_fh, original)
    sys.stderr = redirected
    _retarget_stderr_logging_handlers(original, redirected)


def restore_stderr() -> None:
    """
    Undo :func:`redirect_stderr_to_log` — restore the real terminal
    stderr.

    Safe to call even if the redirect was never installed.
    """
    original = getattr(sys.stderr, "_original_stderr", None)
    if original is None:
        return
    redirected = sys.stderr
    sys.stderr = original
    _restore_logging_handlers()
    try:
        redirected.close()
    except OSError as exc:
        log_cli_exception(exc, prefix="Failed to close redirected stderr")


def _retarget_stderr_logging_handlers(
    original: io.TextIOBase,
    redirected: io.TextIOBase,
) -> None:
    """
    Point existing stderr-backed logging handlers at redirected stderr.

    ``logging.StreamHandler`` stores a concrete stream object at handler
    construction time.  Replacing ``sys.stderr`` later does not affect
    handlers that already captured the old stream, including root handlers
    installed by third-party SDKs.  During the TUI lifetime, retarget those
    handlers so their warning/error records land in the diagnostics log
    instead of painting over prompt-toolkit.

    :param original: Stderr stream that was current before redirection.
    :param redirected: Replacement stream writing to the CLI log.
    :returns: ``None``.
    """
    global _redirected_logging_streams

    if _redirected_logging_streams:
        return
    seen_handlers: set[int] = set()
    for logger in _existing_loggers():
        for handler in logger.handlers:
            if id(handler) in seen_handlers:
                continue
            seen_handlers.add(id(handler))
            if not isinstance(handler, logging.StreamHandler):
                continue
            stream = cast(io.TextIOBase, handler.stream)
            if stream is not original:
                continue
            handler.setStream(redirected)
            _redirected_logging_streams.append(
                _LoggingStreamSnapshot(handler=handler, stream=stream)
            )


def _restore_logging_handlers() -> None:
    """
    Restore logging handlers retargeted by stderr redirection.

    :returns: ``None``.
    """
    global _redirected_logging_streams

    snapshots = _redirected_logging_streams
    _redirected_logging_streams = []
    for snapshot in snapshots:
        snapshot.handler.setStream(snapshot.stream)


def _existing_loggers() -> list[logging.Logger]:
    """
    Return root plus all instantiated loggers in the logging registry.

    :returns: Existing loggers that may own stderr-backed handlers.
    """
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    return loggers


def _short_id() -> str:
    """
    Generate a 6-character hex id for the invocation.

    :returns: A short random hex string, e.g. ``"a1b2c3"``.
    """
    return os.urandom(3).hex()


def _update_latest_symlink(log_dir: Path, log_path: Path) -> None:
    """
    Point ``latest-cli.log`` at *log_path*.

    Best-effort — silently ignored if the filesystem doesn't support
    symlinks (e.g. some Windows configurations).

    :param log_dir: Parent directory containing the symlink.
    :param log_path: Absolute path to the current log file.
    """
    link = log_dir / "latest-cli.log"
    try:
        link.unlink(missing_ok=True)
        link.symlink_to(log_path.name)
    except OSError:
        pass


def _safe_mtime(path: Path) -> float:
    """Return *path*'s mtime, or ``0.0`` if it has vanished.

    ``_prune_old_logs`` runs at the start of every ``omnigent run``, so two
    concurrent launches can glob the same log set then race to delete it. A
    plain ``p.stat()`` in the sort key would then hit a just-removed file and
    raise ``FileNotFoundError``, aborting the whole prune and crashing CLI
    startup. Treat a vanished file as oldest (it's already gone, so the
    suppressed ``unlink`` below is a no-op).
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune_old_logs(log_dir: Path) -> None:
    """
    Remove the oldest ``cli-*.log`` files when the count exceeds
    :data:`MAX_LOG_FILES`.

    Sorts by mtime (oldest first) and removes excess files. Backup
    files from rotation (``cli-*.log.1``) are included in the count.

    :param log_dir: Directory to prune.
    """
    pattern = "cli-*.log*"
    logs = sorted(log_dir.glob(pattern), key=_safe_mtime)
    # Keep the newest MAX_LOG_FILES; delete the rest.
    excess = logs[: max(0, len(logs) - MAX_LOG_FILES)]
    for old in excess:
        with contextlib.suppress(OSError):
            old.unlink()
