"""Fixed-vocabulary causes for native terminal start failures.

A start failure's exception text can carry launch paths or secrets, so it never
reaches the client. The few causes that text (and the Codex stderr it embeds)
identifies with certainty are instead named by a reason token from a closed
set, so operators and dashboards can tell a host problem from an Omnigent bug.
The matched text itself is never returned.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from omnigent.util.tmux_compat import MIN_TMUX_VERSION_HINT

__all__ = ["StartFailureCause", "classify_start_failure"]

_CHAIN_MAX_DEPTH = 3


@dataclass(frozen=True)
class StartFailureCause:
    """
    A recognized native terminal start failure cause.

    :param reason: Fixed token naming the cause, e.g. ``"tmux_missing"``.
    :param remedy: One sentence telling the user how to fix it, or ``None``
        when the cause has no single clear fix.
    """

    reason: str
    remedy: str | None = None


@dataclass(frozen=True)
class _Matcher:
    """
    One rule mapping exception text to a cause.

    :param cause: The cause to report when the rule matches.
    :param pattern: Pattern searched in each exception text.
    :param scope: Pattern the same text must also match, or ``None``. Keeps a
        generic phrase from being blamed on Codex outside its readiness error.
    """

    cause: StartFailureCause
    pattern: re.Pattern[str]
    scope: re.Pattern[str] | None = None

    def matches(self, text: str) -> bool:
        """Whether ``text`` is in scope and contains the rule's pattern."""
        if self.scope is not None and self.scope.search(text) is None:
            return False
        return self.pattern.search(text) is not None


def _ci(*alternatives: str) -> re.Pattern[str]:
    """Compile a case-insensitive pattern matching any of ``alternatives``."""
    return re.compile("|".join(alternatives), re.IGNORECASE)


# Codex app-server readiness errors name it before quoting its stderr.
_CODEX_APP_SERVER = _ci(r"codex app-server")

# Ordered most-specific first: a Codex app-server readiness failure quotes its
# stderr, so the install and config causes must win over the generic
# "exited early" / "timed out" symptoms.
_MATCHERS: tuple[_Matcher, ...] = (
    _Matcher(
        StartFailureCause(
            "codex_install_incomplete",
            "Reinstall Codex on the host (`npm install -g @openai/codex@latest`), then retry.",
        ),
        _ci(r"missing optional dependency @openai/codex"),
    ),
    _Matcher(
        StartFailureCause(
            "codex_config_rejected",
            "Codex rejected the host's ~/.codex/config.toml; fix or remove the setting "
            "named in the runner log, then retry.",
        ),
        _ci(
            r"legacy `profile[^`]*` [^\n]*no longer supported",
            r"(?:model provider|config profile) `[^`]*` not found",
            r"error loading (?:default )?config",
            r"toml parse error",
            r"invalid configuration",
        ),
        scope=_CODEX_APP_SERVER,
    ),
    _Matcher(
        StartFailureCause("tmux_missing", "Install tmux on the host, then retry."),
        _ci(r"tmux is not installed or not on path"),
    ),
    _Matcher(
        StartFailureCause(
            "tmux_unsupported",
            f"Upgrade tmux on the host to {MIN_TMUX_VERSION_HINT} or newer, then retry.",
        ),
        _ci(r"managed terminals require tmux"),
    ),
    _Matcher(
        StartFailureCause(
            "cli_not_found",
            "Install the agent's CLI on the host and make sure it is on PATH, then retry.",
        ),
        _ci(r"requires the '[^']+' cli on path", r"cli not found on path"),
    ),
    _Matcher(
        StartFailureCause("app_server_start_timeout"),
        _ci(r"timed out after [\d.]+s waiting for the codex app-server"),
    ),
    _Matcher(
        StartFailureCause("app_server_exited_early"),
        _ci(r"codex app-server exited early"),
    ),
)


def _exception_texts(exc: BaseException) -> Iterator[str]:
    """
    Yield the message of ``exc`` and of a short chain of its causes.

    :param exc: Exception raised by the native terminal creation path.
    :returns: Each message text; one whose ``__str__`` raises reads as empty.
    """
    current: BaseException | None = exc
    for _ in range(_CHAIN_MAX_DEPTH):
        if current is None:
            return
        try:
            text = str(current)
        except Exception:  # noqa: BLE001 - classifying must never mask the start failure
            text = ""
        yield text
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def classify_start_failure(exc: BaseException) -> StartFailureCause | None:
    """
    Recognize a native terminal start failure from its exception text.

    Searches the exception's message and a short cause chain. A Codex
    app-server readiness failure embeds its last stderr lines there, which is
    how a broken install or rejected config is identified.

    :param exc: Exception raised by the native terminal creation path, e.g.
        ``RuntimeError("tmux is not installed or not on PATH")``.
    :returns: The recognized cause, e.g. ``StartFailureCause("tmux_missing", ...)``,
        or ``None`` when no known cause matches. Never carries any of the text.
    """
    texts = list(_exception_texts(exc))
    for matcher in _MATCHERS:
        if any(matcher.matches(text) for text in texts):
            return matcher.cause
    return None
