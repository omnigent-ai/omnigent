"""
Schema derivation for ``@tool``-decorated functions.

Given a typed Python function, produce the function-calling JSON
schema the LLM sees. The pipeline:

1. Inspect the signature for parameters, annotations, and defaults.
2. Parse the Google-style docstring for description and per-param
   descriptions (see :mod:`omnigent.tools._docstring`).
3. Build a Pydantic model from the parameters via ``create_model``;
   Pydantic does the heavy lifting for type → schema (primitives,
   Pydantic models, ``Optional``, ``Literal``, ``Annotated[..., Field]``,
   etc.).
4. Apply strict-mode normalization (see
   :mod:`omnigent.tools._strict`) when ``strict=True``.

Permissive types (``Any``, ``object``, missing annotations) are
allowed but produce an INFO-level warning so authors can find them.
"""

from __future__ import annotations

import inspect
import logging
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from pydantic import Field, create_model
from pydantic.fields import FieldInfo

from ._docstring import parse_google_docstring
from ._state import ToolState
from ._strict import ensure_strict_schema

_logger = logging.getLogger(__name__)

# Reserved parameter name for framework-injected per-conversation
# per-agent tool state. A ``@tool`` function that declares a
# parameter with this name receives a live :class:`ToolState` at
# call time; the parameter is stripped from the LLM-facing JSON
# schema. Convention over configuration — every stateful tool uses
# the same identifier.
STATE_PARAM_NAME = "tool_state"


@dataclass(frozen=True)
class FunctionSchemaResult:
    """
    Output of :func:`build_function_schema`.

    :param description: Function-level description, derived from
        the docstring's leading paragraph(s).
    :param parameters_json_schema: JSON schema for the function's
        parameters, in the OpenAI function-calling shape (an
        ``object`` schema with ``properties`` and ``required``).
        Already normalized to strict mode if requested.
    :param return_annotation: The function's return type annotation,
        or ``None`` if no return annotation was provided. Used by
        the executor to deserialize the tool's return value via
        ``pydantic.TypeAdapter``.
    """

    description: str
    parameters_json_schema: dict[str, Any]
    return_annotation: type[Any] | None
    uses_tool_state: bool


def build_function_schema(
    fn: Callable[..., Any],
    *,
    strict: bool = True,
) -> FunctionSchemaResult:
    """
    Build the function-calling schema for a Python function.

    :param fn: The Python function to derive a schema for. Must be
        a module-level ``def`` or ``async def`` (the ``@tool``
        decorator enforces this elsewhere; we do not re-validate
        here).
    :param strict: If ``True``, apply strict-mode normalization to
        the resulting schema (see :mod:`._strict`).
    :returns: A :class:`FunctionSchemaResult` with description,
        JSON schema, and return-type annotation.
    """
    sig = inspect.signature(fn)
    type_hints = typing.get_type_hints(fn, include_extras=True)
    parsed_doc = parse_google_docstring(fn.__doc__ or "")

    fields: dict[str, tuple[Any, FieldInfo]] = {}
    uses_tool_state = False
    for name, param in sig.parameters.items():
        ann = type_hints.get(name, Any)

        # Framework-injected parameter — reserved by convention:
        # any parameter named exactly ``tool_state`` is filled by
        # the runtime with a :class:`ToolState`. Skipped from the
        # LLM-facing schema; the LLM has no way to supply it.
        # Enforce the convention: if someone types a param as
        # ToolState but names it something else, fail loud so they
        # know the right contract.
        if name == STATE_PARAM_NAME:
            if ann is not ToolState and ann is not Any:
                raise TypeError(
                    f"@tool function {fn.__name__!r} declares parameter "
                    f"'{STATE_PARAM_NAME}' with unexpected type "
                    f"{ann!r}. It must be typed as ToolState "
                    f"(or left unannotated); any other type is a bug."
                )
            uses_tool_state = True
            continue
        if ann is ToolState:
            raise TypeError(
                f"@tool function {fn.__name__!r} types parameter "
                f"{name!r} as ToolState but the parameter must be named "
                f"{STATE_PARAM_NAME!r}. Rename it and the framework "
                f"will inject a live ToolState at call time."
            )

        _warn_if_permissive(fn.__name__, name, ann)

        doc_desc = parsed_doc.param_descriptions.get(name)
        default = param.default if param.default is not inspect.Parameter.empty else ...
        field_info = _build_field_info(ann, default, doc_desc)
        fields[name] = (ann, field_info)

    if fields:
        # Pydantic uses the model name when generating $defs refs;
        # capitalize so it looks reasonable in the schema output.
        model_name = f"{_pascal_case(fn.__name__)}Args"
        # mypy can't statically narrow create_model's overload for our
        # dynamic field dict, but pydantic accepts (Type, FieldInfo)
        # tuples here — they're the documented field-definition shape.
        Model = create_model(model_name, **fields)  # type: ignore[call-overload]
        params_schema: dict[str, Any] = Model.model_json_schema()
    else:
        # Zero-arg tool: the schema is an empty object.
        params_schema = {
            "type": "object",
            "properties": {},
            "required": [],
        }

    if strict:
        params_schema = ensure_strict_schema(params_schema)

    return_annotation = type_hints.get("return")

    return FunctionSchemaResult(
        description=parsed_doc.description,
        parameters_json_schema=params_schema,
        return_annotation=return_annotation,
        uses_tool_state=uses_tool_state,
    )


def _build_field_info(
    annotation: Any,
    default: Any,
    doc_description: str | None,
) -> FieldInfo:
    """
    Construct a Pydantic ``FieldInfo`` for one parameter.

    Handles three description sources, with this priority:
    1. An explicit ``Field(description=...)`` in
       ``Annotated[T, Field(description=...)]``.
    2. A bare string in ``Annotated[T, "desc"]`` (a common shorthand
       supported by some agent SDKs).
    3. The docstring entry for this parameter (Google-style ``Args:``).

    :param annotation: The parameter's type annotation, possibly
        wrapped in ``Annotated[...]``.
    :param default: The parameter's default value, or ``...`` if
        the parameter is required.
    :param doc_description: Description from the docstring's
        ``Args:`` section, or ``None`` if absent.
    :returns: A ``FieldInfo`` ready to pass to
        ``pydantic.create_model``. The default (if any) and
        description are baked in at construction time so
        ``model_json_schema`` picks them up correctly.
    """
    # Pull metadata out of Annotated[T, ...] for description discovery.
    annotated_str_desc: str | None = None
    annotated_field: FieldInfo | None = None
    if get_origin(annotation) is Annotated:
        for extra in get_args(annotation)[1:]:
            if isinstance(extra, FieldInfo) and annotated_field is None:
                annotated_field = extra
            elif isinstance(extra, str) and annotated_str_desc is None:
                annotated_str_desc = extra

    # Determine the effective description with the priority above.
    description: str | None = None
    if annotated_field is not None and annotated_field.description is not None:
        description = annotated_field.description
    elif annotated_str_desc is not None:
        description = annotated_str_desc
    elif doc_description:
        description = doc_description

    # Field(default=PydanticUndefined) is the marker for "required";
    # we map our `...` sentinel to it via PydanticUndefined import.
    # Easier: build the constructor kwargs and let Pydantic translate
    # default=... directly (it accepts ``...`` as "required" too).
    field_kwargs: dict[str, Any] = {}
    if description is not None:
        field_kwargs["description"] = description
    if default is not ...:
        field_kwargs["default"] = default

    if annotated_field is not None:
        # Merge: preserve other metadata (gt/lt/min_length/etc.) from
        # the author's Field, but override description and default.
        # merge_field_infos's stub return is too loose for mypy; cast.
        merged: FieldInfo = FieldInfo.merge_field_infos(annotated_field, FieldInfo(**field_kwargs))
        return merged

    # pydantic.Field stub returns Any (it's polymorphic by default).
    # We're constructing a fresh FieldInfo; cast to the documented type.
    field_obj: FieldInfo = Field(**field_kwargs)
    return field_obj


def _warn_if_permissive(fn_name: str, param_name: str, annotation: Any) -> None:
    """
    Log a warning if a parameter's type provides no validation constraint.

    ``Any``, ``object``, and missing annotations all produce a
    permissive schema (no ``type`` field) that the LLM can fill
    with arbitrary structure. Useful but easy to write by accident.

    :param fn_name: The decorated function's ``__name__``, for the
        log message, e.g. ``"process_payload"``.
    :param param_name: The offending parameter's name, e.g.
        ``"data"``.
    :param annotation: The annotation as resolved by
        ``typing.get_type_hints``.
    """
    # Strip Annotated[...] so we inspect the underlying type.
    underlying = annotation
    if get_origin(underlying) is Annotated:
        underlying = get_args(underlying)[0]

    if underlying is Any or underlying is object:
        type_name = (
            "Any" if underlying is Any else getattr(underlying, "__name__", str(underlying))
        )
        _logger.info(
            "Tool '%s' parameter '%s' has no concrete type annotation "
            "(resolved to %s); LLM will get a permissive schema with "
            "no validation.",
            fn_name,
            param_name,
            type_name,
        )


def _pascal_case(snake: str) -> str:
    """
    Convert a snake_case identifier to PascalCase.

    Used to give the dynamically-created Pydantic model a readable
    name in schema ``$defs`` references.

    :param snake: A snake_case identifier, e.g. ``"word_count"``.
    :returns: PascalCase form, e.g. ``"WordCount"``.
    """
    return "".join(part.capitalize() for part in snake.split("_") if part)
