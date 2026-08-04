"""Codex's spawn-tool model vocabulary, and how to speak it.

Omnigent routes to servable catalog ids (``databricks-gpt-5-6-luna``), but
codex's ``spawn_agent`` validates ``model`` **client-side** against its own
model catalog, before any request leaves the CLI. A catalog id is rejected
outright (probed live on codex 0.145.0)::

    Unknown model `databricks-gpt-5-6-luna` for spawn_agent.
    Available models: gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.2

Codex's slugs differ from the catalog's only in punctuation — the version
segment is dotted (``gpt-5.6-luna``) where the catalog hyphenates it
(``gpt-5-6-luna``) — so translating is mechanical, no per-model table.

The same validation caps the effort per model, again client-side::

    Reasoning effort `xhigh` is not supported for model `system.ai.glm-5-2`.
    Supported reasoning efforts: low, medium, high

so a session default of ``xhigh`` kills a GLM spawn unless the spawn's own
``reasoning_effort`` is clamped alongside its model.

Models outside codex's own catalog (GLM) have no slug until the session's
codex-home extends the catalog — see :data:`EXTENDED_CATALOG_MODELS`.
Translation returns ``None`` for anything else, and the caller falls open
rather than sending a value the CLI drops.

Stdlib-only so hook subprocesses can import it on the spawn path.
"""

from __future__ import annotations

import re

#: Catalog prefixes stripped before comparing ids. Same list as
#: :data:`omnigent.claude_model_vocabulary._CATALOG_PREFIXES`.
_CATALOG_PREFIXES: tuple[str, ...] = ("databricks-", "system.ai.")

#: A bare gpt id, split into family, version digits, and optional tier —
#: ``gpt-5-6-luna`` → ``("gpt", "5", "6", "luna")``. Codex spells the
#: version with a dot and keeps the tier hyphenated.
_GPT_ID_RE = re.compile(r"^(gpt|codex)-(\d+)-(\d+)(?:-([a-z0-9]+))?$")

#: The same id already in codex's spelling, so translating is a no-op — a
#: parent model read straight off the hook payload arrives this way.
_GPT_SLUG_RE = re.compile(r"^(?:gpt|codex)-\d+\.\d+(?:-[a-z0-9]+)?$")

#: Models the gateway serves that codex's bundled catalog does not carry, so
#: omnigent adds them to the session's own catalog (``model_catalog_json``)
#: to make them spawnable. Bare id → the exact slug the entry is written
#: under, which is also the id the gateway serves the model as.
EXTENDED_CATALOG_MODELS: dict[str, str] = {"glm-5-2": "system.ai.glm-5-2"}

#: Efforts each extended model's catalog entry declares. Codex refuses any
#: other value for that model, so this is both the entry's ladder and the
#: clamp the spawn hook applies. Cheapest-safe fallback first.
EXTENDED_MODEL_EFFORTS: dict[str, tuple[str, ...]] = {"glm-5-2": ("low", "medium", "high")}

#: Effort an extended model falls back to when the session asks for one its
#: ladder bars. Must agree with
#: :data:`omnigent.reasoning_effort._MODEL_EFFORT_FALLBACK` (asserted by
#: ``test_codex_effort_clamp_matches_the_runtime_clamp``).
EXTENDED_MODEL_DEFAULT_EFFORT: dict[str, str] = {"glm-5-2": "medium"}


def bare_model_id(model: str) -> str:
    """Strip a catalog prefix and fold to the comparison spelling.

    :param model: Any model id, e.g. ``"databricks-gpt-5-6-luna"``.
    :returns: The bare id, e.g. ``"gpt-5-6-luna"``.
    """
    bare = model.strip().lower().removesuffix("[1m]")
    for prefix in _CATALOG_PREFIXES:
        if bare.startswith(prefix):
            return bare[len(prefix) :]
    return bare


def codex_spawn_model(model: str) -> str | None:
    """Translate a servable model id into codex's ``spawn_agent`` slug.

    :param model: Servable catalog id, e.g. ``"databricks-gpt-5-6-luna"``.
    :returns: The slug codex's spawn tool accepts, e.g.
        ``"gpt-5.6-luna"``; ``None`` when the id has no slug in codex's
        catalog (Kimi), so the caller can fall open instead of sending a
        value the CLI rejects.
    """
    bare = bare_model_id(model)
    extended = EXTENDED_CATALOG_MODELS.get(bare)
    if extended is not None:
        return extended
    if _GPT_SLUG_RE.match(bare):
        return bare
    match = _GPT_ID_RE.match(bare)
    if match is None:
        return None
    family, major, minor, tier = match.groups()
    slug = f"{family}-{major}.{minor}"
    return f"{slug}-{tier}" if tier else slug


def clamp_spawn_effort(effort: str | None, model: str | None) -> str | None:
    """Coerce a spawn's ``reasoning_effort`` to one *model* accepts.

    Codex validates the pairing client-side, so an effort outside the
    model's ladder fails the spawn rather than degrading it. A model with no
    declared ladder keeps whatever the caller asked for.

    :param effort: The spawn's requested effort, or ``None`` when it named
        none (codex then applies the model's catalog default, which is
        already inside the ladder — nothing to clamp).
    :param model: The spawn's model, after translation.
    :returns: The effort to send, or ``None`` to leave it unset.
    """
    if effort is None or model is None:
        return effort
    bare = bare_model_id(model)
    supported = EXTENDED_MODEL_EFFORTS.get(bare)
    if supported is None or effort in supported:
        return effort
    return EXTENDED_MODEL_DEFAULT_EFFORT.get(bare, effort)
