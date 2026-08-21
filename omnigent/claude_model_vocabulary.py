"""Claude Code's model vocabulary, and how to speak it.

Omnigent routes to servable catalog ids (``databricks-claude-sonnet-5``),
but two Claude Code surfaces accept only the family *aliases*:

* the ``Agent`` / ``Task`` tool's ``model`` parameter — a closed enum
  (``sonnet``, ``opus``, ``haiku``, ``fable``), so a catalog id fails
  schema validation and the spawn dies before it starts;
* the ``/model`` slash command — an alias (or the custom slot's exact id)
  resolves offline with no validation; ANY other value, catalog id or
  canonical vendor id alike, is accepted only if a live one-token request
  to the configured endpoint succeeds, so it depends on the gateway
  answering mid-turn and fails as a network error otherwise.

Claude Code resolves each alias to a concrete id via the workspace's
``ANTHROPIC_DEFAULT_*_MODEL`` env (set by omnigent's launch config), so
inverting that mapping is exact — and only exact: a family segment alone
is not enough, because a workspace serving two generations of a family
pins the alias to the newer one, and speaking the alias would run a model
nobody routed to. Both surfaces fail OPEN on an id with no accepted
spelling: skip the switch rather than send something the CLI drops.

``--model`` at launch is a different contract: it takes any string
verbatim, so a session STARTS on an exact id without needing a pin.

Stdlib-only so hook subprocesses can import it on the spawn path.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

#: Family aliases both surfaces accept, longest-lived family first.
CLAUDE_MODEL_ALIASES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

#: Alias → env var Claude Code reads to pin that alias to one model id.
ALIAS_MODEL_ENV_VARS: dict[str, str] = {
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}

#: Extra picker slot pinned to one exact id. ``/model`` accepts that id
#: offline, compared BYTE-EXACTLY (case included) against this value — so
#: translation returns the env's own spelling, never the caller's. The
#: Agent tool's enum has no such slot, so only ``/model`` uses it.
CUSTOM_MODEL_OPTION_ENV_VAR = "ANTHROPIC_CUSTOM_MODEL_OPTION"

#: Display name Claude Code labels the custom slot's ``/model`` picker row
#: with, e.g. ``"Sonnet 5"``. Cosmetic — the slot's id is what ``/model``
#: takes — so it is not part of the vocabulary below.
CUSTOM_MODEL_OPTION_NAME_ENV_VAR = "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"

#: BACK-COMPAT. Omnigent's picker-row id for the custom slot. Named for the
#: model the slot first carried (Sonnet 5, which had no family alias of its
#: own), but a Smart Routing launch pins ITS model there, so the id does not
#: describe the contents — read the slot, never this name. Sessions persist
#: it as a model override, so retiring it needs a migration; slated for
#: removal in 0.10.0 along with the row, once the ``sonnet`` pin is Sonnet 5.
LEGACY_CUSTOM_SLOT_ROW_ID = "sonnet_5"

#: BACK-COMPAT. Spellings the pre-0.10 substring test read as "this is the
#: custom slot's model", kept because :func:`normalized_model_id` does not
#: fold a vendor-prefixed ``anthropic/claude-sonnet-5`` onto the catalog id
#: the slot holds. Consulted only after an exact match misses; retired with
#: :data:`LEGACY_CUSTOM_SLOT_ROW_ID` in 0.10.0.
LEGACY_CUSTOM_SLOT_SPELLINGS: tuple[str, ...] = ("sonnet-5", "sonnet_5")

#: Launch-env keys that define this session's model vocabulary.
MODEL_VOCABULARY_ENV_VARS: tuple[str, ...] = (
    *ALIAS_MODEL_ENV_VARS.values(),
    CUSTOM_MODEL_OPTION_ENV_VAR,
)

#: Catalog prefixes stripped before comparing ids. Must equal
#: :data:`omnigent.server.smart_routing.MODEL_ID_PREFIXES` (asserted by
#: ``test_catalog_prefixes_match_the_routing_defaults``); duplicated because
#: this module stays stdlib-only for hook subprocesses, which also means it
#: cannot honour a deployment's ``routing.model_prefix`` override.
_CATALOG_PREFIXES: tuple[str, ...] = ("databricks-", "system.ai.")
_SEGMENT_RE = re.compile(r"[^a-z0-9]+")


def normalized_model_id(model: str) -> str:
    """Lower-case a model id, dropping catalog prefix and ``[1m]`` suffix.

    :param model: Any model id or alias.
    :returns: The comparable bare id, e.g. ``"claude-sonnet-5"``.
    """
    bare = model.strip().lower().removesuffix("[1m]")
    for prefix in _CATALOG_PREFIXES:
        if bare.startswith(prefix):
            return bare[len(prefix) :]
    return bare


def alias_pins(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read the session's alias → model-id pinning.

    :param env: Environment mapping. ``None`` reads :data:`os.environ`.
    :returns: Alias → pinned model id, for the aliases that are pinned.
    """
    environ = os.environ if env is None else env
    pins: dict[str, str] = {}
    for alias, env_var in ALIAS_MODEL_ENV_VARS.items():
        pinned = environ.get(env_var, "").strip()
        if pinned:
            pins[alias] = pinned
    return pins


def model_vocabulary_env(options: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Rebuild a session's model vocabulary from its picker rows.

    The native model picker's rows ARE the launch env's pinning read back
    out: a row keyed by a family alias is that alias's pin, and any other
    row occupies the single custom slot. This lets a process that never
    saw the terminal's env (the server) ask
    :func:`claude_model_command_arg` the same question the executor will.

    Rows that only restate their own key (a direct Claude login's curated
    ``opus`` / ``sonnet`` rows) pin nothing — Claude resolves those
    itself — so they are skipped rather than read as a pin onto an alias.

    :param options: Picker rows, e.g.
        ``[{"id": "opus", "model": "databricks-claude-opus-5"}]``.
    :returns: A vocabulary env mapping, e.g.
        ``{"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5"}``.
        Empty when the rows pin no concrete model ids.
    """
    env: dict[str, str] = {}
    for option in options:
        if not isinstance(option, Mapping):
            continue
        row_id = option.get("id")
        model = option.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        if model.strip().lower() in CLAUDE_MODEL_ALIASES or model == row_id:
            continue
        key = ALIAS_MODEL_ENV_VARS.get(row_id if isinstance(row_id, str) else "")
        if key is None:
            key = CUSTOM_MODEL_OPTION_ENV_VAR
        env.setdefault(key, model.strip())
    return env


def claude_model_alias(
    model: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Translate a servable model id into Claude's alias vocabulary.

    An exact hit on the pinning is authoritative. The id's own family
    segment names the alias only when NOTHING is pinned at all (a direct
    Anthropic login, where the alias resolves to the vendor's own model
    of that family). Once this session pins aliases, a family segment is
    not enough: an unpinned alias resolves to a canonical vendor id the
    gateway rejects, and a MISMATCHED pin is worse — the alias resolves
    to the pinned id, so the pane runs a model nobody routed to while
    the record claims the routed one (workspace serving both
    ``claude-opus-4-8`` and ``claude-opus-5``, ``opus`` pinned to the
    latter, ``claude-opus-4-8`` routed).

    :param model: Model id from a routing decision, or an alias already.
    :param env: Environment mapping holding the alias pinning. ``None``
        reads :data:`os.environ` — a hook subprocess inherits the CLI's.
    :returns: An accepted alias, or ``None`` when the id maps to nothing
        Claude would accept; callers must then leave the model alone.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    candidate = model.strip().lower()
    if candidate in CLAUDE_MODEL_ALIASES:
        return candidate
    # Bracket variants of the family aliases (``sonnet[1m]``) are settable
    # aliases in their own right — the harness enumerates them in /model's
    # usage line and resolves the marker itself (the family pin plus the
    # marker on a pinned env). Stepping one down to its family would
    # silently drop the marker; refusing it blocks a switch the pane accepts.
    base, bracket, marker = candidate.partition("[")
    if bracket and marker.endswith("]") and base in CLAUDE_MODEL_ALIASES:
        return candidate
    pins = alias_pins(env)
    normalized = normalized_model_id(model)
    for alias, pinned in pins.items():
        if normalized_model_id(pinned) == normalized:
            return alias
    if pins:
        # Every pinned alias was compared exactly above, so reaching here
        # means the routed id is not what any alias resolves to.
        return None
    segments = set(_SEGMENT_RE.split(normalized))
    for alias in CLAUDE_MODEL_ALIASES:
        if alias in segments:
            return alias
    return None


def claude_model_command_arg(
    model: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Translate a model id into a ``/model`` argument.

    Same alias vocabulary as :func:`claude_model_alias`, except the extra
    picker slot: ``/model`` takes that exact id, so a routed model pinned
    there is applied precisely instead of stepping down to its family
    alias.

    :param model: Model id from a routing decision, or an alias already.
    :param env: Environment mapping holding the session's pinning.
        ``None`` reads :data:`os.environ`.
    :returns: The ``/model`` argument, or ``None`` when the id maps to
        nothing the command accepts (the caller must skip the switch —
        an unaccepted value silently keeps the current model).
    """
    if not isinstance(model, str) or not model.strip():
        return None
    environ = os.environ if env is None else env
    custom = environ.get(CUSTOM_MODEL_OPTION_ENV_VAR, "").strip()
    if custom and normalized_model_id(custom) == normalized_model_id(model):
        return custom
    candidate = model.strip()
    if not alias_pins(env) and candidate.lower().startswith("claude-"):
        # A full Anthropic model id names an EXACT generation, and ``/model``
        # on an unpinned (canonical-endpoint) session accepts full ids
        # verbatim — the same spelling the harness's own enumeration
        # resolves. Stepping down to the family alias here would switch to
        # claude's CURRENT generation of that family instead (picking
        # "Opus 4.8 (1M context)" used to type ``/model opus`` and land on
        # Opus 5).
        return candidate
    return claude_model_alias(model, env)
