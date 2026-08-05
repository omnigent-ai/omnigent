"""
Tests for the Lambda MicroVM hooks server's payload extraction.

The hooks server (``deploy/aws-lambda-microvm/hooks_server.py``) is a stdlib-only
script that ships inside the MicroVM image, not an importable package module, so
we load ``_extract_identity_payload`` by path and exercise its shape tolerance:
the platform's /run body framing is not contractually fixed, so the parser must
recover the identity map whether it arrives wrapped, bare, or as a JSON string.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_HOOKS = Path(__file__).resolve().parents[3] / "deploy" / "aws-lambda-microvm" / "hooks_server.py"


def _load_hooks() -> Any:
    spec = importlib.util.spec_from_file_location("_lambda_microvm_hooks", _HOOKS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_hooks = _load_hooks()

_IDENTITY = {
    "OMNIGENT_SERVER": "https://srv.example.com",
    "OMNIGENT_HOST_ID": "host_abc",
    "OMNIGENT_HOST_NAME": "managed-abc",
    "OMNIGENT_HOST_TOKEN": "tok-xyz",
    "ANTHROPIC_API_KEY": "sk-test",
}


def test_extract_wrapped_string_payload() -> None:
    """Shape 1: the launcher's {microvmId, runHookPayload: '<json string>'}."""
    body = json.dumps({"microvmId": "mv-1", "runHookPayload": json.dumps(_IDENTITY)}).encode()
    assert _hooks._extract_identity_payload(body) == _IDENTITY


def test_extract_wrapped_object_payload() -> None:
    """Shape 1 variant: runHookPayload delivered as an object, not a string."""
    body = json.dumps({"microvmId": "mv-1", "runHookPayload": _IDENTITY}).encode()
    assert _hooks._extract_identity_payload(body) == _IDENTITY


def test_extract_bare_identity_map() -> None:
    """Shape 2: the identity map delivered directly as the JSON body."""
    body = json.dumps(_IDENTITY).encode()
    assert _hooks._extract_identity_payload(body) == _IDENTITY


def test_extract_inner_json_string_as_whole_body() -> None:
    """Shape 3: the inner runHookPayload JSON string delivered as the body."""
    body = json.dumps(json.dumps(_IDENTITY)).encode()
    assert _hooks._extract_identity_payload(body) == _IDENTITY


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2, 3]", b"123"])
def test_extract_non_dict_body_returns_none(raw: bytes) -> None:
    """Non-JSON or non-dict bodies yield None (host is not started)."""
    assert _hooks._extract_identity_payload(raw) is None


@pytest.mark.parametrize("raw", [b"", b"{}", b'{"microvmId": "mv-1"}'])
def test_extract_identity_less_body_never_starts_host(raw: bytes) -> None:
    """An empty or identity-less dict body (bare {}, or {microvmId} with no
    payload) must NOT satisfy the identity gate — the required keys are absent,
    so whether the extractor returns {} or None, no host is started."""
    payload = _hooks._extract_identity_payload(raw)
    assert payload is None or any(
        not (payload or {}).get(key) for key in _hooks._REQUIRED_IDENTITY
    )


def test_spawn_failure_clears_started_flag_so_retry_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Popen must reset ``_host_started`` so a platform /run retry
    re-attempts the spawn. A sticky flag would strand the VM host-less: the
    guard at the top of ``_start_host_from_payload`` short-circuits every
    subsequent call."""
    monkeypatch.setattr(_hooks, "_host_started", False)
    body = json.dumps(_IDENTITY).encode()

    calls = {"n": 0}

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("start_host.sh not found")
        return object()  # second attempt "succeeds"

    monkeypatch.setattr(_hooks.subprocess, "Popen", _boom)

    # First /run: spawn raises, flag must be cleared back to False.
    _hooks._start_host_from_payload(body)
    assert _hooks._host_started is False

    # Retry: guard doesn't short-circuit, spawn is attempted again and sticks.
    _hooks._start_host_from_payload(body)
    assert calls["n"] == 2
    assert _hooks._host_started is True
