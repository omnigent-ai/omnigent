"""OpenAPI request bodies for routes that parse their body by hand.

FastAPI infers ``requestBody`` from a typed body parameter. A route that reads
``request.json()`` or ``request.form()`` itself (for example to dispatch on
``Content-Type``) gets no ``requestBody`` at all, so generated clients cannot
discover what to send. Such a route passes an ``openapi_extra`` built here, and
the models it references are added to ``components.schemas`` by
:func:`register_component_models`, because FastAPI only collects models it
sees on typed parameters and response models.
"""

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
)

_REF_TEMPLATE = "#/components/schemas/{model}"


def _ref(model: type[BaseModel]) -> dict[str, str]:
    """Return a ``$ref`` to *model* under ``components.schemas``.

    :param model: A model registered with :func:`register_component_models`.
    :returns: e.g. ``{"$ref": "#/components/schemas/SessionCreateRequest"}``.
    """
    return {"$ref": _REF_TEMPLATE.format(model=model.__name__)}


# Models whose schemas the session-create request body references.
SESSION_CREATE_BODY_MODELS: tuple[type[BaseModel], ...] = (
    SessionCreateRequest,
    ProjectSessionCreateRequest,
    SessionCreateMetadata,
)

SESSION_CREATE_OPENAPI_EXTRA: dict[str, Any] = {  # type: ignore[explicit-any]
    "requestBody": {
        "required": True,
        "content": {
            # JSON: bind an already-registered agent. The route validates
            # ProjectSessionCreateRequest when ``project_id`` is non-null and
            # SessionCreateRequest otherwise.
            "application/json": {
                "schema": {
                    "anyOf": [
                        _ref(SessionCreateRequest),
                        _ref(ProjectSessionCreateRequest),
                    ]
                }
            },
            # Multipart: upload the agent bundle inline with JSON metadata.
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["metadata", "bundle"],
                    "properties": {
                        "metadata": _ref(SessionCreateMetadata),
                        "bundle": {
                            "type": "string",
                            "contentMediaType": "application/gzip",
                            "description": "Gzipped tar of the agent directory.",
                        },
                    },
                },
                "encoding": {"metadata": {"contentType": "application/json"}},
            },
        },
    }
}


def register_component_models(app: FastAPI, models: tuple[type[BaseModel], ...]) -> None:
    """Add *models* (and the models they nest) to the app's ``components.schemas``.

    Wraps ``app.openapi`` so both the live ``/openapi.json`` and
    ``scripts/dump_openapi.py`` include them. A schema FastAPI already emitted
    under the same name is left untouched.

    :param app: The FastAPI application.
    :param models: Models referenced by hand-written ``openapi_extra`` bodies.
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
