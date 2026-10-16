"""Upload production native-wrapper specs to a real test server."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
    CURSOR_NATIVE_WRAPPER_VALUE,
    GOOSE_NATIVE_WRAPPER_VALUE,
    HERMES_NATIVE_WRAPPER_VALUE,
    KIRO_NATIVE_WRAPPER_VALUE,
    PI_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from tests._helpers.session import bundle_files, post_session_bundle

NativeHarness = Literal["claude", "codex", "cursor", "goose", "kiro", "hermes", "pi"]


def create_native_session(
    client: httpx.Client | ModuleType,
    base_url: str,
    *,
    harness: NativeHarness,
    metadata: Mapping[str, Any] | None = None,
    model: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create an unbound wrapper session with caller metadata; return its response.

    Only Codex/Kiro materializers accept a model. Workspace, launch arguments,
    and extra labels belong in metadata. Binding and cleanup stay with callers.
    """
    if model is not None and harness not in {"codex", "kiro"}:
        raise ValueError(f"The {harness} wrapper materializer does not accept a model")
    with tempfile.TemporaryDirectory() as tmp:
        if harness == "claude":
            from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

            spec = _materialize_claude_agent_spec(Path(tmp))
            wrapper = CLAUDE_NATIVE_WRAPPER_VALUE
        elif harness == "codex":
            from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec

            spec = _materialize_codex_agent_spec(Path(tmp), model=model)
            wrapper = CODEX_NATIVE_WRAPPER_VALUE
        elif harness == "cursor":
            from omnigent.harnesses.cursor_native.main import _materialize_cursor_agent_spec

            spec = _materialize_cursor_agent_spec(Path(tmp))
            wrapper = CURSOR_NATIVE_WRAPPER_VALUE
        elif harness == "goose":
            from omnigent.harnesses.goose_native.main import _materialize_goose_agent_spec

            spec = _materialize_goose_agent_spec(Path(tmp))
            wrapper = GOOSE_NATIVE_WRAPPER_VALUE
        elif harness == "kiro":
            from omnigent.harnesses.kiro_native.main import _materialize_kiro_agent_spec

            spec = _materialize_kiro_agent_spec(Path(tmp), model=model)
            wrapper = KIRO_NATIVE_WRAPPER_VALUE
        elif harness == "hermes":
            from omnigent.harnesses.hermes_native.main import _materialize_hermes_agent_spec

            spec = _materialize_hermes_agent_spec(Path(tmp))
            wrapper = HERMES_NATIVE_WRAPPER_VALUE
        elif harness == "pi":
            from omnigent.harnesses.pi_native.main import _materialize_pi_agent_spec

            spec = _materialize_pi_agent_spec(Path(tmp))
            wrapper = PI_NATIVE_WRAPPER_VALUE
        else:
            raise ValueError(f"Unsupported native harness: {harness}")
        data = spec.read_text().encode()

    payload = dict(metadata) if metadata is not None else {}
    labels = dict(payload.get("labels", {}))
    required = {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE, WRAPPER_LABEL_KEY: wrapper}
    for key, value in required.items():
        if key in labels and labels[key] != value:
            raise ValueError(f"Native {harness} sessions require {key}={value!r}")
    payload["labels"] = {**labels, **required}
    name = f"{harness}-native-ui"
    # A non-config.yaml member exercises the legacy spec translator.
    response = post_session_bundle(
        client.post,
        f"{base_url}/v1/sessions",
        bundle_files({f"{name}.yaml": data}),
        metadata=payload,
        filename=f"{name}.tar.gz",
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()
