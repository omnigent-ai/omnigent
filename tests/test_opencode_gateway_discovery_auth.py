"""A failed gateway-discovery auth command must never leak its secret to logs.

The discovery token is minted by running the family's ``auth_command`` under
``subprocess.run(..., check=True)``. Both ``CalledProcessError`` and
``TimeoutExpired`` embed the raw command (which may carry an inline credential)
in their text, so the failure path must log only a sanitized category — never
the exception, traceback, or captured output.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from omnigent.harnesses.opencode_native import provider as prov
from omnigent.onboarding.provider_config import FamilyConfig

_SECRET = "sk-do-not-log-this-token"


def _families() -> list[tuple[str, str, FamilyConfig]]:
    family = FamilyConfig(
        base_url="https://ws.example.com/ai-gateway/openai/v1",
        auth_command=f"mint-token --secret {_SECRET}",
        wire_api="chat",
        models={"default": "eng_dev.ai_gateway.omni-gpt"},
    )
    return [("openai", "@ai-sdk/openai-compatible", family)]


def test_auth_command_failure_never_logs_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise subprocess.CalledProcessError(
            returncode=1, cmd=f"mint-token --secret {_SECRET}", stderr=f"denied {_SECRET}"
        )

    monkeypatch.setattr(prov.subprocess, "run", _raise)

    with caplog.at_level(logging.INFO):
        assert prov._mint_gateway_discovery_token(_families()) is None

    assert _SECRET not in caplog.text


def test_auth_command_timeout_never_logs_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=f"mint-token --secret {_SECRET}", timeout=15)

    monkeypatch.setattr(prov.subprocess, "run", _timeout)

    with caplog.at_level(logging.INFO):
        assert prov._mint_gateway_discovery_token(_families()) is None

    assert _SECRET not in caplog.text
