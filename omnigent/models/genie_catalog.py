"""Genie's bundled model catalog, pinned to one Genie release.

Genie (Databricks' codex fork) runs a model only when its catalog lists the
slug. On the Databricks AI gateway it silently replaces any other model with
its default, and it forwards an effort the model does not list for the gateway
to reject. Without a running ``genie app-server`` there is no catalog to query,
so ``sys_list_models`` and the ``genie`` effort vocabulary read this pinned
copy. A live session checks the running binary's own ``model/list`` instead
(see :class:`~omnigent.inner.genie_executor.GenieExecutor`), which also covers
models the workspace gateway adds through ``/models``.

Re-pin for a new Genie release:

1. Take ``codex-rs/models-manager/models.json`` at the release tag of
   ``databricks-eng/genie-code-codex``, or the ``model/list``
   (``includeHidden: true``) of ``genie app-server`` run with a throwaway
   ``GENIE_HOME`` whose ``config.toml`` sets ``model_provider = "databricks"``,
   which is Genie's static-catalog path.
2. Keep every row except ``aliases`` entries and the ``codex-auto-review``
   approval reviewer: slugs go in ``GENIE_BUNDLED_CATALOG``
   (``omnigent/models/model_fallbacks.py``), their metadata below in the same
   order. Then update :data:`GENIE_CATALOG_VERSION`, ``GENIE_EFFORTS`` in
   ``omnigent/util/reasoning_effort.py`` and the ``genie`` ladder in
   ``web/src/lib/sessionCapabilities.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.models.model_fallbacks import GENIE_BUNDLED_CATALOG

#: The Genie release whose ``models.json`` the catalog was copied from.
GENIE_CATALOG_VERSION = "0.2.1"


@dataclass(frozen=True)
class GenieCatalogModel:
    """One bundled Genie model.

    :param slug: Catalog slug Genie honors, e.g. ``"gpt-5.4-mini"``.
    :param efforts: The model's ``supported_reasoning_levels``, ascending.
    :param default_effort: ``default_reasoning_level``, e.g. ``"medium"``.
    :param context_window: ``context_window`` in tokens.
    """

    slug: str
    efforts: tuple[str, ...]
    default_effort: str
    context_window: int


_GPT_EFFORTS = ("low", "medium", "high", "xhigh")

# (efforts, default effort, context window), one row per GENIE_BUNDLED_CATALOG slug.
_MODEL_METADATA: tuple[tuple[tuple[str, ...], str, int], ...] = (
    ((*_GPT_EFFORTS, "max", "ultra"), "low", 272_000),  # 5.6 sol
    ((*_GPT_EFFORTS, "max", "ultra"), "medium", 272_000),  # 5.6 terra
    ((*_GPT_EFFORTS, "max"), "medium", 272_000),  # 5.6 luna
    (_GPT_EFFORTS, "medium", 272_000),  # 5.5
    (_GPT_EFFORTS, "medium", 272_000),  # 5.4
    (_GPT_EFFORTS, "medium", 272_000),  # 5.4 mini
    (_GPT_EFFORTS, "medium", 272_000),  # 5.2
)

GENIE_CATALOG_MODELS: tuple[GenieCatalogModel, ...] = tuple(
    GenieCatalogModel(slug, efforts, default_effort, context_window)
    for slug, (efforts, default_effort, context_window) in zip(
        GENIE_BUNDLED_CATALOG.model_ids, _MODEL_METADATA, strict=True
    )
)


def genie_model_options() -> list[dict[str, object]]:
    """Launch-picker rows for the pinned catalog, with each model's efforts.

    No row is the default: Genie's default model depends on the provider it
    runs against, so the picker shows "Default" and omits the override.

    :returns: Native model-option rows, e.g. ``{"id": "gpt-5.5", "model":
        "gpt-5.5", "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}, ...]}``.
    """
    return [
        {
            "id": model.slug,
            "model": model.slug,
            "defaultReasoningEffort": model.default_effort,
            "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in model.efforts],
        }
        for model in GENIE_CATALOG_MODELS
    ]
