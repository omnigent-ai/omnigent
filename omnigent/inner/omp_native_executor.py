"""Executor delivering turns to the isolated native OMP terminal."""

from pathlib import Path

from omnigent.harnesses.omp_native.bridge import (
    OMP_NATIVE_BRIDGE_DIR_ENV_VAR,
    OMP_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
)
from omnigent.inner.executor import EnqueuedContent
from omnigent.inner.native_attachments import parse_data_uri
from omnigent.inner.pi_native_executor import PiNativeExecutor
from omnigent.util.json_types import JsonObject


class OmpNativeExecutor(PiNativeExecutor):
    """Reuse the native inbox protocol without inheriting Pi's identity or state."""

    def __init__(self, bridge_dir: Path | None = None) -> None:
        super().__init__(
            bridge_dir,
            bridge_env_var=OMP_NATIVE_BRIDGE_DIR_ENV_VAR,
            session_env_var=OMP_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
            display_name="OMP",
        )

    def _bridge_content(self, content: EnqueuedContent) -> str | list[JsonObject]:
        """Deliver images inline to OMP rather than asking a file tool to view them."""
        if not isinstance(content, list) or not any(
            isinstance(block, dict) and block.get("type") == "input_image" for block in content
        ):
            return super()._bridge_content(content)
        native: list[JsonObject] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "input_image":
                uri = block.get("image_url")
                if not isinstance(uri, str) or not uri.startswith("data:image/"):
                    raise ValueError("OMP input_image requires a resolved inline image data URI")
                image = parse_data_uri(uri)
                native.append(
                    {
                        "type": "image",
                        "data": image.base64_payload,
                        "mimeType": image.mime_type,
                    }
                )
            else:
                text = super()._bridge_content([block])
                if text:
                    native.append({"type": "text", "text": text})
        return native
