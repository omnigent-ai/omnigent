"""Launch-picker model options for the Hermes-native harness.

Mirrors the OpenCode lane tests in test_opencode_model_options.py: the
pre-launch preview serves the host's Hermes config default plus the cached
``hermes model`` catalog, and a reader failure is a failed lookup — never
invented rows.
"""

from __future__ import annotations

import pytest

from omnigent.host.frames import HostModelOptionsFrame

from .test_connect import _make_host_process


async def test_handle_model_options_serves_the_hermes_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch picker lists the host's Hermes config default + catalog."""
    from omnigent.harnesses.hermes_native import models as hermes_models

    monkeypatch.setattr(
        hermes_models,
        "hermes_native_model_options",
        lambda **_kwargs: [
            {
                "id": "zai/glm-5.3-flash",
                "model": "zai/glm-5.3-flash",
                "displayName": "glm-5.3-flash",
                "default": True,
            },
            {
                "id": "nous/nous/hermes-5",
                "model": "nous/nous/hermes-5",
                "displayName": "nous/hermes-5",
            },
        ],
    )
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_hermes_models", harness="hermes-native"),
    )

    assert result.status == "ok"
    assert [model["id"] for model in result.models] == [
        "zai/glm-5.3-flash",
        "nous/nous/hermes-5",
    ]


async def test_handle_model_options_hermes_reader_failure_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable Hermes config/catalog means a failed lookup."""

    def _boom(**_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("hermes home unreadable")

    from omnigent.harnesses.hermes_native import models as hermes_models

    monkeypatch.setattr(hermes_models, "hermes_native_model_options", _boom)
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_hermes_models_fail", harness="hermes-native"),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.models == []
