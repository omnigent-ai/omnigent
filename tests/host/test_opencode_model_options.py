"""Launch-picker model options for the OpenCode-native harness.

Mirrors the Pi/Codex lane tests in test_connect.py: the pre-launch preview
serves the host's ambient ``opencode models`` catalog, and a probe that cannot
run is a failed lookup — never invented rows.
"""

from __future__ import annotations

import pytest

from omnigent.host.frames import HostModelOptionsFrame

from .test_connect import _make_host_process


async def test_handle_model_options_serves_the_opencode_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch picker lists the host's ambient OpenCode CLI catalog."""
    from omnigent.harnesses.opencode_native import app_server as opencode_app_server

    monkeypatch.setattr(
        opencode_app_server,
        "list_opencode_cli_model_options_isolated",
        lambda **_kwargs: [
            {
                "id": "zai/glm-5.3",
                "model": "glm-5.3",
                "providerID": "zai",
                "displayName": "zai/glm-5.3",
                "name": "glm-5.3",
            }
        ],
    )
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_opencode_models", harness="opencode-native"),
    )

    assert result.status == "ok"
    assert [model["id"] for model in result.models] == ["zai/glm-5.3"]


async def test_handle_model_options_opencode_probe_failure_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No OpenCode binary / failed CLI listing means a failed lookup."""

    def _boom(**_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("opencode CLI not found on PATH")

    from omnigent.harnesses.opencode_native import app_server as opencode_app_server

    monkeypatch.setattr(opencode_app_server, "list_opencode_cli_model_options_isolated", _boom)
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_opencode_models_fail", harness="opencode-native"),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.models == []
