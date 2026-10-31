"""Gateway provider selection and the shared Pi streaming adapter."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.inner.pi_gateway import pi_gateway_extension_args


@pytest.mark.parametrize(
    ("api", "provider_url", "expected"),
    [
        ("openai-completions", "https://example.test/ai-gateway/mlflow/v1", True),
        ("openai-completions", "https://example.test/ai-gateway/mlflow/v1/", True),
        ("openai-completions", "https://example.test/v1", False),
        ("openai-responses", "https://example.test/ai-gateway/mlflow/v1", False),
        ("google-generative-ai", "https://example.test/ai-gateway/mlflow/v1", False),
    ],
)
def test_gateway_extension_only_overlays_its_chat_transport(
    tmp_path: Path, api: str, provider_url: str, expected: bool
) -> None:
    config = {
        "providers": {"custom-name": {"api": api, "baseUrl": provider_url, "apiKey": "secret"}}
    }
    args = pi_gateway_extension_args(tmp_path, config)
    if not expected:
        assert args == []
        assert list(tmp_path.iterdir()) == []
        return
    assert args == ["--extension", str(tmp_path / "omnigent_gateway.js")]
    assert json.loads((tmp_path / "omnigent_gateway_providers.json").read_text()) == [
        "custom-name"
    ]
    assert "secret" not in "".join(p.read_text() for p in tmp_path.iterdir())


@pytest.mark.parametrize("metadata_shape", ["together", "split"])
def test_gateway_stream_handles_fragmented_metadata_and_preserves_callbacks(
    tmp_path: Path,
    metadata_shape: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("requires Node")
    pi_gateway_extension_args(
        tmp_path,
        {
            "providers": {
                "gateway": {
                    "api": "openai-completions",
                    "baseUrl": "https://example.test/ai-gateway/mlflow/v1",
                }
            }
        },
    )
    extension = tmp_path / "omnigent_gateway.js"
    source = extension.read_text().replace(
        'from "@earendil-works/pi-ai"',
        'from "data:text/javascript,export const streamSimpleOpenAICompletions = () => {}"',
    )
    assert "@earendil-works/pi-ai" not in source, "Pi import stub no longer matches the extension"
    # Pi supplies this dependency via its extension loader; process tests cover it.
    module = tmp_path / "adapter.mjs"
    module.write_text(source)
    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("pi_gateway_protocol.mjs")),
            str(module),
            metadata_shape,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
