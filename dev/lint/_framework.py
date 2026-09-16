"""Shared helpers for the custom-lint runner (``dev/lint/custom_lint.py``).

Inline suppression, ruff / MLflow-clint style: a violation on a line carrying
``# custom-lint: disable=<rule-id>`` is suppressed, and ``disable-next``
suppresses the line that follows the comment. Rule ids are lowercase-hyphenated
(e.g. ``workspace-scoped-cache``); several may be comma-separated. Any text
after the id list is ignored, so a human reason can ride along:

    _cache = {}  # custom-lint: disable=workspace-scoped-cache -- call_id is unique

Detection is tokenizer-based, so the marker is only honored inside a real
comment, never inside a string literal.
"""

from __future__ import annotations

import io
import re
import tokenize

_DISABLE_RE = re.compile(r"custom-lint:\s*disable(-next)?=([a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*)")
# Cheap pre-screen so files with no markers skip tokenization entirely.
_PRESCREEN = re.compile(r"custom-lint:\s*disable")


def disabled_rules_by_line(source: str) -> dict[int, set[str]]:
    """Map each 1-indexed line to the rule ids suppressed on it.

    ``disable`` applies to the comment's own line; ``disable-next`` applies to
    the following line. Malformed / partial source degrades gracefully to no
    suppressions rather than raising.
    """
    disabled: dict[int, set[str]] = {}
    if not _PRESCREEN.search(source):
        return disabled
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            match = _DISABLE_RE.search(token.string)
            if match is None:
                continue
            target = token.start[0] + 1 if match.group(1) else token.start[0]
            rules = {rule.strip() for rule in match.group(2).split(",")}
            disabled.setdefault(target, set()).update(rules)
    except tokenize.TokenError:
        # Incomplete/malformed source (e.g. a file mid-edit): degrade to "no
        # suppressions found so far" per the docstring, rather than crashing
        # the whole lint run.
        pass
    return disabled
