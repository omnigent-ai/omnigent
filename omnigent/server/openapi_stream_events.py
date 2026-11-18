"""Register the ``ServerStreamEvent`` union in the app's OpenAPI document.

``GET /v1/sessions/{session_id}/stream`` declares its ``text/event-stream``
response as ``$ref: #/components/schemas/ServerStreamEvent``. FastAPI only adds
component schemas for models it sees on typed parameters and response models,
so without this the live ``/openapi.json`` (and ``/docs``) carried a ``$ref``
to a schema it never defined. :func:`install_stream_event_schemas` adds the
union and its per-event variants when the app builds its document, so the live
document and ``scripts/dump_openapi.py`` share one definition.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from pydantic import TypeAdapter

STREAM_EVENT_SCHEMA_NAME = "ServerStreamEvent"


def server_stream_event_schemas() -> tuple[dict[str, Any], dict[str, Any]]:  # type: ignore[explicit-any]
    """Build the ``ServerStreamEvent`` union schema and its variant schemas.

    :returns: ``(root, definitions)``: the discriminated-union schema for
        ``components.schemas.ServerStreamEvent`` and the per-event schemas it
        references, keyed by component name.
    """
    from omnigent.server.schemas import ServerStreamEvent

    adapter: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)
    root = adapter.json_schema(ref_template="#/components/schemas/{model}")
    definitions: dict[str, Any] = root.pop("$defs", {})  # type: ignore[explicit-any]
    return root, definitions


def add_stream_event_schemas(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    """Add the ``ServerStreamEvent`` union and its variants to *spec*.

    A variant FastAPI already emitted under the same name (e.g.
    ``ResponseObject``) is kept; the serialized shape is the same model.

    :param spec: An OpenAPI document; ``components.schemas`` is updated.
    """
    root, definitions = server_stream_event_schemas()
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    schemas[STREAM_EVENT_SCHEMA_NAME] = root
    for name, definition in definitions.items():
        schemas.setdefault(name, definition)


def install_stream_event_schemas(app: FastAPI) -> None:
    """Make ``app.openapi()`` include the ``ServerStreamEvent`` schemas.

    :param app: The FastAPI application.
    """
    original: Callable[[], dict[str, Any]] = app.openapi  # type: ignore[explicit-any]

    def openapi() -> dict[str, Any]:  # type: ignore[explicit-any]
        if app.openapi_schema is not None:
            return app.openapi_schema
        spec = original()
        add_stream_event_schemas(spec)
        app.openapi_schema = spec
        return spec

    app.openapi = openapi  # type: ignore[method-assign]
