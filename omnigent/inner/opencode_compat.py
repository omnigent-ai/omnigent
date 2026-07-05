"""Compatibility shims for the ``opencode-ai`` SDK.

The SDK (through 0.1.0a36) caches discriminated-union metadata by setting a
``__discriminator__`` attribute on the ``typing.Union[...]`` object itself.
Python 3.14 unified ``typing.Union`` with ``types.UnionType``, whose
instances have no ``__dict__``, so the first discriminated-union parse (any
SSE event) raises ``AttributeError: 'typing.Union' object has no attribute
'__discriminator__' and no __dict__ for setting new attributes``.

Sibling Stainless SDKs fixed this by moving the cache into a module-level
``weakref.WeakKeyDictionary`` (see ``openai._models.DISCRIMINATOR_CACHE``);
:func:`patch_discriminator_cache` retrofits that same fix onto
``opencode_ai`` until a release ships it.
"""

from __future__ import annotations

import contextlib
import weakref
from typing import Any

# Names the replacement function resolves off ``opencode_ai._models``. If a
# future SDK drops any of them, its discriminator code was refactored and the
# shim silently steps aside rather than patching blind.
_REQUIRED_MODEL_HELPERS: tuple[str, ...] = (
    "PropertyInfo",
    "get_args",
    "strip_annotated_type",
    "is_basemodel_type",
    "PYDANTIC_V2",
    "is_literal_type",
    "_extract_field_schema_pv2",
    "DiscriminatorDetails",
    "_build_discriminated_union_meta",
)


def patch_discriminator_cache() -> None:
    """Replace the SDK's union-attribute discriminator cache with a dict.

    Rebinds ``opencode_ai._models._build_discriminated_union_meta`` to a
    behavior-identical copy whose cache is an external
    ``weakref.WeakKeyDictionary`` (exposed as ``_models.DISCRIMINATOR_CACHE``,
    matching the upstream fix's name) instead of a ``setattr`` on the union
    object. No-op when the installed SDK already defines
    ``DISCRIMINATOR_CACHE`` (upstream fix present, or this shim already ran)
    or when the module's internals no longer match what the copy needs.
    """
    from opencode_ai import _models

    if hasattr(_models, "DISCRIMINATOR_CACHE"):
        return
    if not all(hasattr(_models, name) for name in _REQUIRED_MODEL_HELPERS):
        return

    cache: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()

    def _build_discriminated_union_meta(*, union: type, meta_annotations: tuple[Any, ...]) -> Any:
        try:
            cached = cache.get(union)
        except TypeError:
            # Not weakref-able (e.g. `X | Y` unions on Python <= 3.13);
            # recompute below and skip caching, same net behavior as the
            # SDK's own setattr failing on such objects.
            cached = None
        if cached is not None:
            return cached

        discriminator_field_name: str | None = None
        for annotation in meta_annotations:
            if (
                isinstance(annotation, _models.PropertyInfo)
                and annotation.discriminator is not None
            ):
                discriminator_field_name = annotation.discriminator
                break
        if not discriminator_field_name:
            return None

        mapping: dict[str, type] = {}
        discriminator_alias: str | None = None
        for variant in _models.get_args(union):
            variant = _models.strip_annotated_type(variant)
            if not _models.is_basemodel_type(variant):
                continue
            if _models.PYDANTIC_V2:
                field = _models._extract_field_schema_pv2(variant, discriminator_field_name)
                if not field:
                    continue
                # Note: if one variant defines an alias then they all should.
                discriminator_alias = field.get("serialization_alias")
                field_schema = field["schema"]
                if field_schema["type"] == "literal":
                    for entry in field_schema["expected"]:
                        if isinstance(entry, str):
                            mapping[entry] = variant
            else:
                field_info = variant.__fields__.get(discriminator_field_name)
                if not field_info:
                    continue
                discriminator_alias = field_info.alias
                field_annotation = getattr(field_info, "annotation", None)
                if field_annotation and _models.is_literal_type(field_annotation):
                    for entry in _models.get_args(field_annotation):
                        if isinstance(entry, str):
                            mapping[entry] = variant

        if not mapping:
            return None

        details = _models.DiscriminatorDetails(
            mapping=mapping,
            discriminator_field=discriminator_field_name,
            discriminator_alias=discriminator_alias,
        )
        with contextlib.suppress(TypeError):
            cache.setdefault(union, details)
        return details

    _models.DISCRIMINATOR_CACHE = cache
    _models._build_discriminated_union_meta = _build_discriminated_union_meta
