"""Mirror of Pi's own model-scope resolution (``settings.json`` ``enabledModels``).

Pi curates the models its pickers cycle with ``enabledModels`` patterns,
resolved by its ``resolveModelScopeFromModels``. Catalogs Omnigent builds for
Pi (the pre-launch own-login picker) must apply the same curation, or they
offer models the launched Pi would never use. This module mirrors the
resolver's observable semantics:

- exact references: canonical ``provider/id``, then ``provider`` + ``id``
  split at the first slash, then a bare ``id`` when unambiguous across
  providers (all case-insensitive);
- glob patterns (``*``/``?``/``[...]``): matched minimatch-style against the
  qualified ``provider/id`` or the bare ``id`` — ``*``/``?`` do not cross
  ``/``, a whole ``**`` segment does;
- non-glob fallback: substring match on id or display name, picking one model
  and preferring alias ids (no ``-YYYYMMDD`` suffix) over dated versions;
- an optional trailing ``:<thinking level>`` suffix, which scopes the model
  but is ignored here (Omnigent pickers carry no thinking level).

Patterns that match nothing select nothing; Pi treats an entirely unmatched
scope as "no curation", so callers should fall back to the unscoped catalog
when the result is empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Pi's VALID_THINKING_LEVELS: a trailing ":<level>" scopes a model at that
# thinking level and must be stripped before matching.
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_GLOB_CHARS = ("*", "?", "[")


@dataclass(frozen=True)
class ScopableModel:
    """One catalog entry the scope patterns are resolved against."""

    provider: str
    model_id: str
    name: str | None = None

    @property
    def reference(self) -> str:
        """The canonical ``provider/id`` reference."""
        return f"{self.provider}/{self.model_id}"


def scope_models(patterns: Iterable[str], models: Sequence[ScopableModel]) -> list[ScopableModel]:
    """Resolve *patterns* against *models*, Pi's ``resolveModelScopeFromModels``.

    :param patterns: ``enabledModels`` entries from Pi's settings.
    :param models: The available catalog to scope.
    :returns: The selected models, deduplicated, in first-match order. Empty
        when no pattern matched anything (Pi then treats the scope as unset).
    """
    scoped: list[ScopableModel] = []
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        for model in _match_pattern(pattern.strip(), models):
            key = (model.provider.lower(), model.model_id.lower())
            if key not in seen:
                seen.add(key)
                scoped.append(model)
    return scoped


def _match_pattern(pattern: str, models: Sequence[ScopableModel]) -> list[ScopableModel]:
    """Return the models one pattern selects (many for globs, at most one else)."""
    if not pattern:
        return []
    if any(char in pattern for char in _GLOB_CHARS):
        glob = _strip_thinking_suffix(pattern)
        exact = _exact_reference_match(glob, models)
        if exact is not None:
            return [exact]
        regex = _glob_regex(glob)
        return [
            model
            for model in models
            if regex.fullmatch(model.reference) or regex.fullmatch(model.model_id)
        ]
    return _parse_model_pattern(pattern, models)


def _strip_thinking_suffix(pattern: str) -> str:
    """Drop a trailing ``:<valid thinking level>`` (kept otherwise)."""
    colon = pattern.rfind(":")
    if colon != -1 and pattern[colon + 1 :] in _THINKING_LEVELS:
        return pattern[:colon]
    return pattern


def _parse_model_pattern(pattern: str, models: Sequence[ScopableModel]) -> list[ScopableModel]:
    """Pi's ``parseModelPattern``: match, else retry the pre-colon prefix."""
    match = _try_match_model(pattern, models)
    if match is not None:
        return [match]
    colon = pattern.rfind(":")
    if colon == -1:
        return []
    # Pi retries the prefix whether the suffix is a valid thinking level or
    # not (scope mode allows the invalid-suffix fallback, with a warning).
    return _parse_model_pattern(pattern[:colon], models)


def _try_match_model(pattern: str, models: Sequence[ScopableModel]) -> ScopableModel | None:
    """Exact reference match, else substring match preferring alias ids."""
    exact = _exact_reference_match(pattern, models)
    if exact is not None:
        return exact
    needle = pattern.lower()
    matches = [
        model
        for model in models
        if needle in model.model_id.lower() or (model.name and needle in model.name.lower())
    ]
    if not matches:
        return None
    aliases = [model for model in matches if _is_alias(model.model_id)]
    pool = aliases or matches
    return max(pool, key=lambda model: model.model_id)


def _exact_reference_match(
    reference: str, models: Sequence[ScopableModel]
) -> ScopableModel | None:
    """Pi's ``findExactModelReferenceMatch``: unique exact matches only."""
    trimmed = reference.strip()
    if not trimmed:
        return None
    normalized = trimmed.lower()
    canonical = [model for model in models if model.reference.lower() == normalized]
    if len(canonical) == 1:
        return canonical[0]
    if len(canonical) > 1:
        return None
    slash = trimmed.find("/")
    if slash != -1:
        provider = trimmed[:slash].strip().lower()
        model_id = trimmed[slash + 1 :].strip().lower()
        if provider and model_id:
            split_matches = [
                model
                for model in models
                if model.provider.lower() == provider and model.model_id.lower() == model_id
            ]
            if len(split_matches) == 1:
                return split_matches[0]
            if len(split_matches) > 1:
                return None
    bare = [model for model in models if model.model_id.lower() == normalized]
    return bare[0] if len(bare) == 1 else None


def _is_alias(model_id: str) -> bool:
    """An alias id has no ``-YYYYMMDD`` date suffix (or ends in ``-latest``)."""
    if model_id.endswith("-latest"):
        return True
    return re.search(r"-\d{8}$", model_id) is None


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a minimatch-style glob: ``*``/``?`` stay within one ``/`` segment."""
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            # A whole ** segment spans any number of segments (including none).
            parts.append("(?:[^/]+(?:/[^/]+)*)?" if last else "(?:[^/]+/)*")
            continue
        parts.append(_segment_regex(segment))
        if not last:
            parts.append("/")
    return re.compile("".join(parts), re.IGNORECASE)


def _segment_regex(segment: str) -> str:
    """Translate one glob segment to regex (``[...]`` classes pass through)."""
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            closing = _find_class_end(segment, index)
            if closing is None:
                out.append(re.escape(char))
            else:
                inner = segment[index + 1 : closing]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append(f"[{inner}]")
                index = closing
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _find_class_end(segment: str, start: int) -> int | None:
    """Index of the ``]`` closing the class at *start*, None when unterminated."""
    index = start + 1
    if index < len(segment) and segment[index] in "!^":
        index += 1
    if index < len(segment) and segment[index] == "]":
        index += 1  # a leading ] is a literal member of the class
    while index < len(segment) and segment[index] != "]":
        index += 1
    return index if index < len(segment) else None
