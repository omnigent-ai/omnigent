"""End-to-end regression: a pinned executor variant must reach the OpenCode serve.

Journey: an agent bundle pins ``executor.model`` + ``executor.variant`` for the
``opencode-native`` harness, and a turn on that session sends a prompt. The
serve's ``POST /session/{id}/prompt_async`` body schema accepts a top-level
``variant: string`` (verified against a real ``opencode serve`` 1.18.25 inside
the supported pin), so the pin must survive spec parse and land in that body —
including a ``provider/model#suffix`` id contributing its suffix as the
``variant`` instead of leaking it verbatim into ``modelID``.

Needs no opencode binary, credentials, or live inference: the wire body is
captured from the real transport + client over an ``httpx.MockTransport``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx

from omnigent.harnesses.opencode_native.client import OpenCodeClient
from omnigent.harnesses.opencode_native.http_transport import OpenCodeHttpTransport
from omnigent.native.native_server_transport import NativePrompt
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec
from omnigent.spec.validator import validate

_BUNDLE_YAML = """\
spec_version: 1
name: opencode-variant-pin
description: opencode-native bundle pinning a model variant
executor:
  type: omnigent
  model: opencode-go/deepseek-v4.1-flash
  variant: max
  config:
    harness: opencode-native
prompt: |
  You are a test agent.
os_env:
  type: caller_process
  cwd: .
"""


def _parse_variant_bundle(tmp_path: Path) -> AgentSpec:
    (tmp_path / "config.yaml").write_text(_BUNDLE_YAML, encoding="utf-8")
    spec = parse(tmp_path, expand_env=False)
    assert not validate(spec).errors, f"variant bundle failed validation: {validate(spec).errors}"
    return spec


async def _captured_prompt_body(prompt: NativePrompt) -> dict[str, object]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_e2e", "directory": "/w", "title": "t"})
        return httpx.Response(200, json={"id": "msg_1"})

    http_client = httpx.AsyncClient(
        base_url="http://opencode.invalid", transport=httpx.MockTransport(handler)
    )
    client = OpenCodeClient("http://opencode.invalid", client=http_client)
    transport = OpenCodeHttpTransport(client_factory=lambda: client)
    try:
        await transport.send_prompt("ses_e2e", prompt)
    finally:
        await client.aclose()
    posts = [r for r in captured if r.method == "POST" and r.url.path.endswith("/prompt_async")]
    assert posts, f"no prompt POST captured; saw {[(r.method, r.url.path) for r in captured]}"
    body = json.loads(posts[-1].content)
    assert isinstance(body, dict)
    return body


def test_bundle_executor_variant_survives_parse(tmp_path: Path) -> None:
    spec = _parse_variant_bundle(tmp_path)
    assert spec.executor.model == "opencode-go/deepseek-v4.1-flash"
    variant = getattr(spec.executor, "variant", None)
    assert variant == "max", (
        f"executor.variant pin was silently dropped at parse: variant={variant!r}, "
        f"executor.config={spec.executor.config!r}"
    )


async def test_pinned_variant_reaches_prompt_wire_body(tmp_path: Path) -> None:
    spec = _parse_variant_bundle(tmp_path)
    prompt = NativePrompt(text="hello", model=spec.executor.model)
    variant = getattr(spec.executor, "variant", None)
    if variant is not None:
        prompt = dataclasses.replace(prompt, variant=variant)
    body = await _captured_prompt_body(prompt)
    assert body.get("model") == {
        "providerID": "opencode-go",
        "modelID": "deepseek-v4.1-flash",
    }, f"unexpected model object on the wire: {body.get('model')!r}"
    assert body.get("variant") == "max", (
        f"pinned executor.variant never reached the prompt wire body: {body!r}"
    )


async def test_model_hash_suffix_splits_into_variant() -> None:
    prompt = NativePrompt(text="hello", model="opencode-go/muse-spark-1.3-contributor#xhigh")
    body = await _captured_prompt_body(prompt)
    assert body.get("model") == {
        "providerID": "opencode-go",
        "modelID": "muse-spark-1.3-contributor",
    }, f"'#suffix' must not leak into modelID: {body.get('model')!r}"
    assert body.get("variant") == "xhigh", (
        f"'#suffix' was not lifted to a top-level variant: {body!r}"
    )
