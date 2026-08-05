"""Codex's model vocabulary, and how to speak it.

Omnigent routes to servable catalog ids (``databricks-gpt-5-6-luna``), but
codex names the same model ``gpt-5.6-luna`` — the version segment is dotted
where the catalog hyphenates it. Two paths need the translation, and they need
it from opposite directions:

**Spawns** (``spawn_agent``). Codex validates ``model`` **client-side**,
against its own bundled catalog, before any request leaves the CLI. A catalog
id is rejected outright (probed live on codex 0.145.0)::

    Unknown model `databricks-gpt-5-6-luna` for spawn_agent.
    Available models: gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.2

The same validation caps the effort per model, again client-side::

    Reasoning effort `xhigh` is not supported for model `system.ai.glm-5-2`.
    Supported reasoning efforts: low, medium, high

so a session default of ``xhigh`` kills a GLM spawn unless the spawn's own
``reasoning_effort`` is clamped alongside its model. Models outside codex's
bundled catalog (GLM) have no slug until the session's codex-home extends the
catalog — see :data:`EXTENDED_CATALOG_MODELS`. :func:`codex_spawn_model`
returns ``None`` for anything else, and the caller falls open rather than
sending a value the CLI drops.

**Turns** (``thread/setModel`` on a live thread). Here codex is its own
vocabulary authority: the live ``model/list`` response IS the mapping, so
:func:`codex_model_slug` hardcodes no model id. The gateway serves either
spelling, so a thread switched onto a catalog id still RUNS; codex just warns
"Model metadata for databricks-gpt-5-6-luna not found. Defaulting to fallback
metadata" and leaves ``/model`` pointing at the launch slug, which reads as
"routing did nothing". Extended-catalog rows (``system.ai.glm-5-2``) are
listed under the catalog spelling, so they translate to themselves. An id no
row matches is returned verbatim — the turn still runs, which beats skipping
the switch.

Stdlib-only so hook subprocesses can import it on the spawn/routing paths.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

#: Catalog prefixes stripped before comparing ids. Same list as
#: :data:`omnigent.claude_model_vocabulary._CATALOG_PREFIXES`, and equal to
#: :data:`omnigent.server.smart_routing.MODEL_ID_PREFIXES` (both asserted by
#: ``tests/test_codex_model_vocabulary.py``); duplicated because this module
#: stays stdlib-only for hook subprocesses, which also means it cannot honour
#: a deployment's ``routing.model_prefix`` override.
#: The prefix a gateway model ROUTE carries, as opposed to a serving
#: endpoint's ``databricks-``; the extended catalog's ids are spelled with it.
_MODEL_ROUTE_PREFIX = "system.ai."
_CATALOG_PREFIXES: tuple[str, ...] = ("databricks-", _MODEL_ROUTE_PREFIX)

#: A bare gpt id, split into family, version digits, and optional tier —
#: ``gpt-5-6-luna`` → ``("gpt", "5", "6", "luna")``. Codex spells the
#: version with a dot and keeps the tier hyphenated.
_GPT_ID_RE = re.compile(r"^(gpt|codex)-(\d+)-(\d+)(?:-([a-z0-9]+))?$")

#: Models the gateway serves that codex's bundled catalog does not carry, so
#: omnigent adds them to the session's own catalog (``model_catalog_json``)
#: to make them spawnable. Bare id → the exact slug the entry is written
#: under, which is also the id the gateway serves the model as.
_GLM_ARM = "glm-5-2"
EXTENDED_CATALOG_MODELS: dict[str, str] = {_GLM_ARM: f"{_MODEL_ROUTE_PREFIX}{_GLM_ARM}"}

#: Efforts each extended model's catalog entry declares. Codex refuses any
#: other value for that model, so this is both the entry's ladder and the
#: clamp the spawn hook applies. Cheapest-safe fallback first.
EXTENDED_MODEL_EFFORTS: dict[str, tuple[str, ...]] = {_GLM_ARM: ("low", "medium", "high")}

#: Effort an extended model falls back to when the session asks for one its
#: ladder bars. Must agree with
#: :data:`omnigent.reasoning_effort._MODEL_EFFORT_FALLBACK` (asserted by
#: ``test_codex_effort_clamp_matches_the_runtime_clamp``).
EXTENDED_MODEL_DEFAULT_EFFORT: dict[str, str] = {_GLM_ARM: "medium"}


def bare_model_id(model: str) -> str:
    """Strip a catalog prefix and fold case, keeping codex's punctuation.

    :param model: Any model id, e.g. ``"databricks-gpt-5-6-luna"``.
    :returns: The bare id, e.g. ``"gpt-5-6-luna"``. A codex slug keeps its
        dotted version (``"gpt-5.6-luna"``); use :func:`comparable_model_id`
        to fold the two spellings together.
    """
    bare = model.strip().lower().removesuffix("[1m]")
    for prefix in _CATALOG_PREFIXES:
        if bare.startswith(prefix):
            return bare[len(prefix) :]
    return bare


def comparable_model_id(model: str) -> str:
    """Fold a model id to the spelling codex ids compare in.

    Comparison only, never a value to send anywhere: codex writes version
    numbers with dots (``gpt-5.6-luna``) where the catalog writes dashes
    (``databricks-gpt-5-6-luna``), and the prefix/case folding is the shared
    catalog rule.

    :param model: Any model id, catalog or codex spelling.
    :returns: The comparable bare id, e.g. ``"gpt-5-6-luna"``.
    """
    return bare_model_id(model).replace(".", "-")


def codex_spawn_model(model: str) -> str | None:
    """Translate a servable model id into codex's ``spawn_agent`` slug.

    :param model: Servable catalog id, e.g. ``"databricks-gpt-5-6-luna"``.
    :returns: The slug codex's spawn tool accepts, e.g.
        ``"gpt-5.6-luna"``; ``None`` when the id has no slug in codex's
        catalog (Kimi), so the caller can fall open instead of sending a
        value the CLI rejects.
    """
    bare = comparable_model_id(model)
    extended = EXTENDED_CATALOG_MODELS.get(bare)
    if extended is not None:
        return extended
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
    bare = comparable_model_id(model)
    supported = EXTENDED_MODEL_EFFORTS.get(bare)
    if supported is None or effort in supported:
        return effort
    return EXTENDED_MODEL_DEFAULT_EFFORT.get(bare, effort)


def codex_model_slug(
    model: str,
    options: Iterable[Mapping[str, Any]],  # type: ignore[explicit-any]  # raw model/list rows
) -> str:
    """Translate a routed model id into codex's own spelling.

    :param model: Model id from a routing decision, e.g.
        ``"databricks-gpt-5-6-luna"``.
    :param options: Raw ``model/list`` rows, e.g.
        ``[{"id": "gpt-5.6-luna", "model": "gpt-5.6-luna"}]``.
    :returns: The matching row's ``id``, or *model* verbatim when no row
        names the same model (an empty catalog included).
    """
    if not isinstance(model, str) or not model.strip():
        return model
    target = comparable_model_id(model)
    for option in options:
        if not isinstance(option, Mapping):
            continue
        slug = option.get("id")
        if not isinstance(slug, str) or not slug.strip():
            continue
        # ``model`` is the servable id behind the row when codex reports one
        # separately from its own slug; matching either side keeps the
        # translation working whichever spelling the deployment lists.
        for spelling in (slug, option.get("model")):
            if isinstance(spelling, str) and comparable_model_id(spelling) == target:
                return slug.strip()
    return model
