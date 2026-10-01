"""Upload production native-wrapper specs to a real test server."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)


def create_native_session(
    client: httpx.Client,
    base_url: str,
    *,
    harness: Literal["claude", "codex"],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a wrapper session; callers select session/agent IDs from the response."""
    with tempfile.TemporaryDirectory() as tmp:
        if harness == "claude":
            from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

            spec = _materialize_claude_agent_spec(Path(tmp))
            wrapper = CLAUDE_NATIVE_WRAPPER_VALUE
        elif harness == "codex":
            from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec

            spec = _materialize_codex_agent_spec(Path(tmp), model=None)
            wrapper = CODEX_NATIVE_WRAPPER_VALUE
        else:
            raise ValueError(f"Unsupported native harness: {harness}")
        data = spec.read_text().encode()

    name = f"{harness}-native-ui"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # A non-config.yaml member exercises the legacy spec translator.
        info = tarfile.TarInfo(f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE, WRAPPER_LABEL_KEY: wrapper}
    response = client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": (f"{name}.tar.gz", buf.getvalue(), "application/gzip")},
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()
