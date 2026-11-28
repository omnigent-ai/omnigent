"""OpenAPI bodies for routes that dispatch on content type themselves."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from omnigent.server.schemas import (
    ProjectSessionCreateRequest,
    SessionCreateMetadata,
    SessionCreateRequest,
    SessionStartupInput,
)

_REF_TEMPLATE = "#/components/schemas/{model}"


def _ref(model: type[BaseModel]) -> dict[str, str]:
    """Reference a model registered in ``components.schemas``."""
    return {"$ref": _REF_TEMPLATE.format(model=model.__name__)}


def _session_create_input_schema() -> dict[str, Any]:  # type: ignore[explicit-any]
    """Keep an absent or null project on the required-agent legacy path."""
    return {
        "anyOf": [
            {
                "allOf": [
                    _ref(SessionCreateRequest),
                    {"properties": {"project_id": {"type": "null"}}},
                ]
            },
            {
                "allOf": [
                    _ref(ProjectSessionCreateRequest),
                    {"required": ["project_id"], "properties": {"project_id": {"type": "string"}}},
                ]
            },
        ]
    }


SESSION_CREATE_BODY_MODELS: tuple[type[BaseModel], ...] = (
    SessionCreateRequest,
    ProjectSessionCreateRequest,
    SessionCreateMetadata,
)

_INITIAL_FILES_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {"type": "string", "format": "binary"},
    "description": "Files attached to initial_input, sent as repeated initial_files parts.",
}
_INITIAL_INPUT_METADATA_SCHEMA = {
    "required": ["host_type", "initial_input"],
    "properties": {
        "host_type": {"const": "managed"},
        "initial_input": _ref(SessionStartupInput),
    },
}

SESSION_CREATE_OPENAPI_EXTRA: dict[str, Any] = {  # type: ignore[explicit-any]
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": _session_create_input_schema()},
            "multipart/form-data": {
                "schema": {
                    "oneOf": [
                        {
                            "type": "object",
                            "required": ["metadata", "bundle"],
                            "properties": {
                                "metadata": _ref(SessionCreateMetadata),
                                "bundle": {
                                    "type": "string",
                                    "format": "binary",
                                    "contentMediaType": "application/gzip",
                                    "description": "Gzipped tar of the agent directory.",
                                },
                                "initial_files": _INITIAL_FILES_SCHEMA,
                            },
                            "dependentSchemas": {
                                "initial_files": {
                                    "properties": {"metadata": _INITIAL_INPUT_METADATA_SCHEMA}
                                }
                            },
                        },
                        {
                            "type": "object",
                            "required": ["metadata", "initial_files"],
                            "not": {"required": ["bundle"]},
                            "properties": {
                                "metadata": {
                                    "allOf": [
                                        _session_create_input_schema(),
                                        _INITIAL_INPUT_METADATA_SCHEMA,
                                    ]
                                },
                                "initial_files": _INITIAL_FILES_SCHEMA,
                            },
                        },
                    ],
                    "description": (
                        "A bundle selects bundled creation, even with initial_files. "
                        "Without a bundle, initial_files selects the JSON create contract. "
                        "Send metadata as JSON text in a part without a filename."
                    ),
                },
                "encoding": {
                    "metadata": {"contentType": "application/json"},
                    "bundle": {"contentType": "application/gzip"},
                },
            },
        },
    }
}


def register_component_models(app: FastAPI, models: tuple[type[BaseModel], ...]) -> None:
    """Register hand-parsed body models and their nested validation schemas.

    :param app: The application whose live OpenAPI document is extended.
    :param models: Models referenced by ``openapi_extra`` request bodies.
    """
    original: Callable[[], dict[str, Any]] = app.openapi  # type: ignore[explicit-any]

    def openapi() -> dict[str, Any]:  # type: ignore[explicit-any]
        if app.openapi_schema is not None:
            return app.openapi_schema
        spec = original()
        _, top_level = models_json_schema(
            [(model, "validation") for model in models],
            ref_template=_REF_TEMPLATE,
        )
        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        for name, schema in top_level.get("$defs", {}).items():
            schemas.setdefault(name, schema)
        app.openapi_schema = spec
        return spec

    app.openapi = openapi  # type: ignore[method-assign]
