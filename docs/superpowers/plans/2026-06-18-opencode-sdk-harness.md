# OpenCode SDK Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `harness: opencode` to Omnigent that drives OpenCode through a persistent `opencode serve` process over HTTP/SSE using the `opencode-ai` Python SDK, with mid-turn interrupt, live message queue, an in-process MCP tool-bridge, and token-usage reporting.

**Architecture:** A per-conversation `OpenCodeExecutor` (inner `Executor`) lazily spawns one `opencode serve` subprocess, connects an `AsyncOpencode` client to it, and per turn sends a prompt via `session.chat` while consuming the global SSE event stream and translating events into Omnigent `ExecutorEvent`s. Spec tools are exposed to OpenCode through an in-process FastMCP server whose handlers round-trip through the adapter's `_tool_executor`. Wrapped by the existing `ExecutorAdapter`/`create_app` harness scaffold and wired through the standard spawn-env / registry / onboarding / CLI surfaces, matching upstream PR #183 but on the SDK transport.

**Tech Stack:** Python 3.12, `opencode-ai` (Stainless httpx SDK, `AsyncOpencode`), `mcp.server.fastmcp.FastMCP` (core dep), FastAPI/uvicorn (harness scaffold), pytest + pytest-asyncio.

## Global Constraints

- Python 3.12+; follow existing module/docstring style (Sphinx `:param:`/`:returns:`, `from __future__ import annotations`).
- No compound bash in steps where avoidable; never append `2>&1`.
- New runtime dependency: `opencode-ai` is a **pre-release** package — install with `pip install --pre opencode-ai`; pin floor `opencode-ai>=0.1.0a36`.
- `mcp` (FastMCP), `httpx`, `anyio` are already core deps — do **not** re-add.
- Harness registry key, spec allowlist spelling, CLI choice, and env-var prefix are all the literal string `opencode` / `HARNESS_OPENCODE_*`.
- Env var names (verbatim): `HARNESS_OPENCODE_MODEL`, `HARNESS_OPENCODE_CWD`, `HARNESS_OPENCODE_PATH`, `HARNESS_OPENCODE_THINKING`, `HARNESS_OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS`, `HARNESS_OPENCODE_GATEWAY_PROVIDER`, `HARNESS_OPENCODE_GATEWAY_BASE_URL`, `HARNESS_OPENCODE_GATEWAY_API_KEY`, `HARNESS_OPENCODE_MCP_SERVERS`, `HARNESS_OPENCODE_DATABRICKS_PROFILE`, and the OpenCode-native `OPENCODE_CONFIG_CONTENT` / `OPENCODE_DISABLE_PROJECT_CONFIG`.
- `opencode serve` announces its URL on **stdout** as `opencode server listening on http://127.0.0.1:<port>`.
- Reference (do not copy the transport, but match the surface wiring): upstream PR #183 diff saved at `docs/superpowers/plans/pr183-reference.diff`, plus the saved analysis in `docs/OPENCODE_SDK_DESIGN.md`.

---

### Task 1: Dependency + registry/allowlist plumbing

**Files:**
- Modify: `pyproject.toml:23-81` (add `opencode-ai` to `dependencies`)
- Modify: `omnigent/runtime/harnesses/__init__.py:67-68` (registry entry)
- Modify: `omnigent/spec/_omnigent_compat.py:83-90` (harness allowlist)
- Modify: `omnigent/runtime/workflow.py:142` (`AgentHarnessType` literal)
- Test: `tests/inner/test_opencode_registry.py` (new)

**Interfaces:**
- Produces: registry mapping `"opencode" -> "omnigent.inner.opencode_harness"`; `opencode` accepted by `AgentSpec` validation.

- [ ] **Step 1: Write the failing test**

```python
# tests/inner/test_opencode_registry.py
from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_opencode_registered():
    assert _HARNESS_MODULES["opencode"] == "omnigent.inner.opencode_harness"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/inner/test_opencode_registry.py -v`
Expected: FAIL with `KeyError: 'opencode'`.

- [ ] **Step 3: Add the registry entry**

In `omnigent/runtime/harnesses/__init__.py`, inside `_HARNESS_MODULES`, after the `databricks_supervisor` entry:

```python
    # OpenCode harness wrap. See omnigent/inner/opencode_harness.py.
    # Drives OpenCode (https://opencode.ai) via a persistent
    # ``opencode serve`` process talked to over HTTP/SSE with the
    # ``opencode-ai`` Python SDK.
    "opencode": "omnigent.inner.opencode_harness",
```

- [ ] **Step 4: Add the spec allowlist + harness type + dependency**

In `omnigent/spec/_omnigent_compat.py`, add `"opencode",` to the harness set (keep alphabetical, after `"open-responses"`).

In `omnigent/runtime/workflow.py`, change `AgentHarnessType`:

```python
AgentHarnessType = Literal[
    "claude-sdk", "codex", "pi", "openai-agents-sdk", "antigravity", "opencode"
]
```

In `pyproject.toml`, append to `dependencies` (after `"openai-agents>=0.0.17",`):

```python
    # OpenCode harness transport: the Stainless-generated httpx SDK
    # for talking to a persistent ``opencode serve`` process. Pre-release
    # package (install with `pip install --pre`); the `opencode` binary
    # itself is installed separately via npm `opencode-ai` or opencode.ai/install.
    "opencode-ai>=0.1.0a36",
```

- [ ] **Step 5: Install the dependency**

Run: `pip install --pre "opencode-ai>=0.1.0a36"`
Expected: installs `opencode_ai` and `httpx` (already present).

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/inner/test_opencode_registry.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml omnigent/runtime/harnesses/__init__.py omnigent/spec/_omnigent_compat.py omnigent/runtime/workflow.py tests/inner/test_opencode_registry.py
git commit -m "feat(opencode): register opencode harness + add opencode-ai dep"
```

---

### Task 2: Pure helpers — model split, truthy, binary resolve, user text

**Files:**
- Create: `omnigent/inner/opencode_executor.py` (helpers only this task)
- Test: `tests/inner/test_opencode_helpers.py` (new)

**Interfaces:**
- Produces:
  - `_parse_truthy(raw: str | None) -> bool`
  - `_resolve_opencode_binary() -> str` (reads `HARNESS_OPENCODE_PATH`, else `shutil.which("opencode")`, raises `FileNotFoundError`)
  - `_split_provider_model(model: str | None) -> tuple[str | None, str | None]` — splits `"anthropic/claude-sonnet-4-5"` → `("anthropic", "claude-sonnet-4-5")`; no slash → `(None, model)`; `None` → `(None, None)`
  - `_latest_user_text(messages: list[Message]) -> str`
  - Module env-var constants `_ENV_MODEL`, `_ENV_CWD`, `_ENV_OPENCODE_PATH`, `_ENV_THINKING`, `_ENV_SKIP_PERMISSIONS`, `_ENV_GATEWAY_PROVIDER`, `_ENV_GATEWAY_BASE_URL`, `_ENV_GATEWAY_API_KEY`, `_ENV_MCP_SERVERS`, `_OPENCODE_CONFIG_CONTENT_ENV`, `_OPENCODE_DISABLE_PROJECT_CONFIG_ENV`

- [ ] **Step 1: Write the failing tests**

```python
# tests/inner/test_opencode_helpers.py
import pytest
from omnigent.inner.opencode_executor import (
    _parse_truthy,
    _split_provider_model,
    _latest_user_text,
)


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("", False), (None, False), ("nope", False),
])
def test_parse_truthy(raw, expected):
    assert _parse_truthy(raw) is expected


def test_split_provider_model_with_slash():
    assert _split_provider_model("anthropic/claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")


def test_split_provider_model_no_slash():
    assert _split_provider_model("gpt-5") == (None, "gpt-5")


def test_split_provider_model_none():
    assert _split_provider_model(None) == (None, None)


def test_latest_user_text_plain_string():
    msgs = [{"role": "user", "content": "hello"}]
    assert _latest_user_text(msgs) == "hello"


def test_latest_user_text_blocks():
    msgs = [{"role": "user", "content": [
        {"type": "input_text", "text": "a"},
        {"type": "input_text", "text": "b"},
    ]}]
    assert _latest_user_text(msgs) == "a\nb"


def test_latest_user_text_prefers_last_user():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert _latest_user_text(msgs) == "second"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/inner/test_opencode_helpers.py -v`
Expected: FAIL with `ImportError` (module/functions not defined).

- [ ] **Step 3: Write the module header + helpers**

Create `omnigent/inner/opencode_executor.py` with module docstring describing the SDK/serve transport (summarise `docs/OPENCODE_SDK_DESIGN.md`), then:

```python
from __future__ import annotations

import logging
import os
import shutil
from collections.abc import AsyncIterator
from typing import Any

from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_OPENCODE_MODEL"
_ENV_CWD = "HARNESS_OPENCODE_CWD"
_ENV_OPENCODE_PATH = "HARNESS_OPENCODE_PATH"
_ENV_THINKING = "HARNESS_OPENCODE_THINKING"
_ENV_SKIP_PERMISSIONS = "HARNESS_OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS"
_ENV_GATEWAY_PROVIDER = "HARNESS_OPENCODE_GATEWAY_PROVIDER"
_ENV_GATEWAY_BASE_URL = "HARNESS_OPENCODE_GATEWAY_BASE_URL"
_ENV_GATEWAY_API_KEY = "HARNESS_OPENCODE_GATEWAY_API_KEY"
_ENV_MCP_SERVERS = "HARNESS_OPENCODE_MCP_SERVERS"
_OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
_OPENCODE_DISABLE_PROJECT_CONFIG_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"

_SERVER_BOOT_TIMEOUT_S = 30.0
_STDERR_CHUNK_LIMIT = 65536


def _parse_truthy(raw: str | None) -> bool:
    """Decode the truthy-env-var convention shared across harness wraps.

    :param raw: Raw env-var value or ``None``.
    :returns: ``True`` for ``"1"``/``"true"``/``"yes"``/``"on"`` (case-insensitive).
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_opencode_binary() -> str:
    """Return the absolute path to the ``opencode`` binary.

    :returns: ``HARNESS_OPENCODE_PATH`` if set, else ``shutil.which("opencode")``.
    :raises FileNotFoundError: No ``opencode`` binary located.
    """
    explicit = os.environ.get(_ENV_OPENCODE_PATH, "").strip()
    if explicit:
        return explicit
    found = shutil.which("opencode")
    if not found:
        raise FileNotFoundError(
            "opencode CLI not found on PATH. Install it from "
            "https://opencode.ai or set HARNESS_OPENCODE_PATH."
        )
    return found


def _split_provider_model(model: str | None) -> tuple[str | None, str | None]:
    """Split an OpenCode ``provider/model`` id into its parts.

    :param model: e.g. ``"anthropic/claude-sonnet-4-5"`` or ``"gpt-5"`` or ``None``.
    :returns: ``(provider_id, model_id)``; provider is ``None`` when no slash.
    """
    if not model:
        return (None, None)
    if "/" in model:
        provider, _, rest = model.partition("/")
        return (provider or None, rest or None)
    return (None, model)


def _latest_user_text(messages: list[Message]) -> str:
    """Extract the most recent user message as plain text.

    Multimodal blocks are dropped with a warning (deferred — see design doc).

    :param messages: Inner ``Message`` list for the turn.
    :returns: Latest user text, or ``""`` when none present.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"input_text", "text"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif block.get("type") in {"input_image", "input_file", "input_audio"}:
                    logger.warning(
                        "opencode harness: dropping %s block; multimodal input "
                        "not yet plumbed through the SDK.",
                        block.get("type"),
                    )
            if parts:
                return "\n".join(parts)
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/inner/test_opencode_helpers.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_executor.py tests/inner/test_opencode_helpers.py
git commit -m "feat(opencode): pure helpers for model split + user text"
```

---

### Task 3: Config synthesis (gateway + MCP) helpers

**Files:**
- Modify: `omnigent/inner/opencode_executor.py` (add config helpers)
- Test: `tests/inner/test_opencode_config.py` (new)

**Interfaces:**
- Consumes: env-var constants from Task 2.
- Produces:
  - `_resolve_mcp_servers_env() -> dict[str, Any]` — decodes `HARNESS_OPENCODE_MCP_SERVERS` JSON object; raises `ValueError` on non-object.
  - `_build_opencode_config_content(mcp_extra: dict[str, Any] | None = None) -> dict[str, Any] | None` — merges gateway provider override + env MCP map + `mcp_extra` (the in-process bridge entry); returns `None` when nothing configured.

- [ ] **Step 1: Write the failing tests**

```python
# tests/inner/test_opencode_config.py
import json
import pytest
from omnigent.inner.opencode_executor import (
    _build_opencode_config_content,
    _resolve_mcp_servers_env,
    _ENV_GATEWAY_BASE_URL,
    _ENV_GATEWAY_API_KEY,
    _ENV_GATEWAY_PROVIDER,
    _ENV_MCP_SERVERS,
)


def test_config_none_when_unset(monkeypatch):
    for var in (_ENV_GATEWAY_BASE_URL, _ENV_GATEWAY_API_KEY, _ENV_MCP_SERVERS):
        monkeypatch.delenv(var, raising=False)
    assert _build_opencode_config_content() is None


def test_config_gateway_default_provider(monkeypatch):
    monkeypatch.setenv(_ENV_GATEWAY_BASE_URL, "https://gw/serving-endpoints")
    monkeypatch.setenv(_ENV_GATEWAY_API_KEY, "sk-test")
    monkeypatch.delenv(_ENV_GATEWAY_PROVIDER, raising=False)
    monkeypatch.delenv(_ENV_MCP_SERVERS, raising=False)
    payload = _build_opencode_config_content()
    assert payload == {
        "provider": {"anthropic": {"options": {
            "baseURL": "https://gw/serving-endpoints", "apiKey": "sk-test"}}}
    }


def test_config_merges_mcp_extra(monkeypatch):
    for var in (_ENV_GATEWAY_BASE_URL, _ENV_GATEWAY_API_KEY, _ENV_MCP_SERVERS):
        monkeypatch.delenv(var, raising=False)
    payload = _build_opencode_config_content(
        mcp_extra={"omnigent": {"type": "remote", "url": "http://127.0.0.1:9/mcp"}}
    )
    assert payload == {"mcp": {"omnigent": {"type": "remote", "url": "http://127.0.0.1:9/mcp"}}}


def test_resolve_mcp_servers_env_bad_json(monkeypatch):
    monkeypatch.setenv(_ENV_MCP_SERVERS, "[]")
    with pytest.raises(ValueError):
        _resolve_mcp_servers_env()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/inner/test_opencode_config.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the helpers**

Append to `omnigent/inner/opencode_executor.py`:

```python
import json


def _resolve_mcp_servers_env() -> dict[str, Any]:
    """Decode ``HARNESS_OPENCODE_MCP_SERVERS`` into the OpenCode MCP map.

    :returns: Decoded ``{server_name: info}`` map, or ``{}`` when unset.
    :raises ValueError: When set but not a JSON object.
    """
    raw = os.environ.get(_ENV_MCP_SERVERS, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_ENV_MCP_SERVERS} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{_ENV_MCP_SERVERS} must be a JSON object")
    return parsed


def _build_opencode_config_content(
    mcp_extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Synthesise the ``OPENCODE_CONFIG_CONTENT`` payload, if any.

    Combines the gateway provider override (``provider.<id>.options.baseURL`` /
    ``apiKey``) with the merged MCP map (env-supplied servers + the in-process
    bridge entry *mcp_extra*).

    :param mcp_extra: In-process bridge entries to merge into the ``mcp`` map.
    :returns: A config dict, or ``None`` when nothing is configured.
    """
    base_url = os.environ.get(_ENV_GATEWAY_BASE_URL, "").strip()
    api_key = os.environ.get(_ENV_GATEWAY_API_KEY, "").strip()
    mcp_servers = dict(_resolve_mcp_servers_env())
    if mcp_extra:
        mcp_servers.update(mcp_extra)

    if not base_url and not api_key and not mcp_servers:
        return None

    payload: dict[str, Any] = {}
    if base_url or api_key:
        provider_id = os.environ.get(_ENV_GATEWAY_PROVIDER, "").strip() or "anthropic"
        options: dict[str, Any] = {}
        if base_url:
            options["baseURL"] = base_url
        if api_key:
            options["apiKey"] = api_key
        payload["provider"] = {provider_id: {"options": options}}
    if mcp_servers:
        payload["mcp"] = mcp_servers
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/inner/test_opencode_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_executor.py tests/inner/test_opencode_config.py
git commit -m "feat(opencode): OPENCODE_CONFIG_CONTENT synthesis (gateway + MCP)"
```

---

### Task 4: Event translation + usage extraction

**Files:**
- Modify: `omnigent/inner/opencode_executor.py` (add `_PartTracker`, `_translate_part_event`, `_tokens_to_usage`)
- Test: `tests/inner/test_opencode_translate.py` (new)

**Interfaces:**
- Consumes: inner event classes; SDK part/event types.
- Produces:
  - `_PartTracker` — tracks per-part seen text length; `.text_delta(part_id, full_text) -> str` returns only the unseen suffix.
  - `_translate_part_event(part: dict[str, Any], tracker: _PartTracker, *, emit_reasoning: bool) -> list[ExecutorEvent]` — maps a `message.part.updated` part dict (already `model_dump`'d) into inner events.
  - `_tokens_to_usage(tokens: dict[str, Any]) -> dict[str, Any]` — maps OpenCode `tokens` → Omnigent usage map.

Note: parts are consumed as plain dicts (`part.model_dump(by_alias=False)` on the SDK object) so tests don't need SDK constructors.

- [ ] **Step 1: Write the failing tests**

```python
# tests/inner/test_opencode_translate.py
from omnigent.inner.executor import (
    TextChunk, ReasoningChunk, ToolCallRequest, ToolCallComplete, ToolCallStatus,
)
from omnigent.inner.opencode_executor import (
    _PartTracker, _translate_part_event, _tokens_to_usage,
)


def test_text_delta_emits_only_suffix():
    tracker = _PartTracker()
    p = {"id": "p1", "type": "text", "text": "Hello"}
    out1 = _translate_part_event(p, tracker, emit_reasoning=False)
    assert [e.text for e in out1 if isinstance(e, TextChunk)] == ["Hello"]
    p2 = {"id": "p1", "type": "text", "text": "Hello world"}
    out2 = _translate_part_event(p2, tracker, emit_reasoning=False)
    assert [e.text for e in out2 if isinstance(e, TextChunk)] == [" world"]


def test_reasoning_gated(tracker_off=False):
    tracker = _PartTracker()
    p = {"id": "r1", "type": "reasoning", "text": "thinking"}
    assert _translate_part_event(p, tracker, emit_reasoning=False) == []
    tracker2 = _PartTracker()
    out = _translate_part_event(p, tracker2, emit_reasoning=True)
    assert any(isinstance(e, ReasoningChunk) for e in out)


def test_tool_completed_emits_request_and_complete():
    tracker = _PartTracker()
    p = {
        "id": "t1", "type": "tool", "tool": "bash", "callID": "c1",
        "state": {"status": "completed", "input": {"command": "ls"},
                  "output": "file.txt", "title": "ls", "metadata": {}},
    }
    out = _translate_part_event(p, tracker, emit_reasoning=False)
    req = [e for e in out if isinstance(e, ToolCallRequest)]
    comp = [e for e in out if isinstance(e, ToolCallComplete)]
    assert req and req[0].name == "bash" and req[0].args == {"command": "ls"}
    assert comp and comp[0].status == ToolCallStatus.SUCCESS
    assert comp[0].result == "file.txt"


def test_tool_running_emits_request_only():
    tracker = _PartTracker()
    p = {"id": "t2", "type": "tool", "tool": "edit", "callID": "c2",
         "state": {"status": "running", "input": {"path": "x"}}}
    out = _translate_part_event(p, tracker, emit_reasoning=False)
    assert [type(e).__name__ for e in out] == ["ToolCallRequest"]
    # Re-emitting the same running part again must not duplicate the request.
    assert _translate_part_event(p, tracker, emit_reasoning=False) == []


def test_tool_error_status():
    tracker = _PartTracker()
    p = {"id": "t3", "type": "tool", "tool": "bash", "callID": "c3",
         "state": {"status": "error", "error": "boom", "input": {}}}
    out = _translate_part_event(p, tracker, emit_reasoning=False)
    comp = [e for e in out if isinstance(e, ToolCallComplete)]
    assert comp and comp[0].status == ToolCallStatus.ERROR and comp[0].error == "boom"


def test_tokens_to_usage():
    tokens = {"input": 100, "output": 50, "reasoning": 10,
              "cache": {"read": 20, "write": 5}}
    assert _tokens_to_usage(tokens) == {
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5,
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/inner/test_opencode_translate.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement translation**

Append to `omnigent/inner/opencode_executor.py`:

```python
class _PartTracker:
    """Per-turn state for diffing streamed parts.

    OpenCode re-sends the full part on every ``message.part.updated``; this
    tracks the last-seen text length per part id (for text/reasoning deltas)
    and which tool parts have already emitted a ``ToolCallRequest``.
    """

    def __init__(self) -> None:
        self._text_len: dict[str, int] = {}
        self._tool_requested: set[str] = set()
        self._tool_completed: set[str] = set()

    def text_delta(self, part_id: str, full_text: str) -> str:
        """Return only the unseen suffix of *full_text* for *part_id*."""
        seen = self._text_len.get(part_id, 0)
        if len(full_text) <= seen:
            return ""
        self._text_len[part_id] = len(full_text)
        return full_text[seen:]

    def mark_tool_requested(self, part_id: str) -> bool:
        """Return ``True`` the first time a tool part id is seen."""
        if part_id in self._tool_requested:
            return False
        self._tool_requested.add(part_id)
        return True

    def mark_tool_completed(self, part_id: str) -> bool:
        """Return ``True`` the first time a tool part completes/errors."""
        if part_id in self._tool_completed:
            return False
        self._tool_completed.add(part_id)
        return True


def _tokens_to_usage(tokens: dict[str, Any]) -> dict[str, Any]:
    """Map an OpenCode ``tokens`` object onto the Omnigent usage map.

    :param tokens: ``{"input", "output", "reasoning", "cache": {"read","write"}}``.
    :returns: ``{"input_tokens","output_tokens","total_tokens",
        "cache_read_input_tokens","cache_creation_input_tokens"}``.
    """
    cache = tokens.get("cache") or {}
    inp = int(tokens.get("input", 0) or 0)
    out = int(tokens.get("output", 0) or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cache_read_input_tokens": int(cache.get("read", 0) or 0),
        "cache_creation_input_tokens": int(cache.get("write", 0) or 0),
    }


def _translate_part_event(
    part: dict[str, Any],
    tracker: _PartTracker,
    *,
    emit_reasoning: bool,
) -> list[ExecutorEvent]:
    """Translate one ``message.part.updated`` part dict into inner events.

    :param part: The part, dumped to a dict (snake_case keys; ``callID`` alias
        preserved as ``callID`` or ``call_id``).
    :param tracker: Per-turn diff state.
    :param emit_reasoning: When ``False``, reasoning parts are dropped.
    :returns: Zero or more inner :class:`ExecutorEvent` instances.
    """
    ptype = part.get("type")
    pid = part.get("id") or ""

    if ptype == "text":
        delta = tracker.text_delta(pid, part.get("text") or "")
        return [TextChunk(text=delta)] if delta else []

    if ptype == "reasoning":
        if not emit_reasoning:
            return []
        delta = tracker.text_delta(pid, part.get("text") or "")
        return [ReasoningChunk(delta=delta, event_type="reasoning_text")] if delta else []

    if ptype == "tool":
        tool_name = part.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []
        call_id = part.get("callID") or part.get("call_id") or pid
        state = part.get("state") or {}
        status_str = state.get("status")
        events: list[ExecutorEvent] = []
        metadata = {"call_id": call_id}
        if tracker.mark_tool_requested(pid):
            events.append(
                ToolCallRequest(
                    name=tool_name,
                    args=state.get("input") if isinstance(state.get("input"), dict) else {},
                    metadata=dict(metadata),
                )
            )
        if status_str in {"completed", "error"} and tracker.mark_tool_completed(pid):
            status = ToolCallStatus.ERROR if status_str == "error" else ToolCallStatus.SUCCESS
            events.append(
                ToolCallComplete(
                    name=tool_name,
                    status=status,
                    result=state.get("output"),
                    error=state.get("error") if status == ToolCallStatus.ERROR else None,
                    metadata=dict(metadata),
                )
            )
        return events

    return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/inner/test_opencode_translate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_executor.py tests/inner/test_opencode_translate.py
git commit -m "feat(opencode): event translation + token usage mapping"
```

---

### Task 5: Server lifecycle (`_OpenCodeServer`)

**Files:**
- Modify: `omnigent/inner/opencode_executor.py` (add `_OpenCodeServer`)
- Test: `tests/inner/test_opencode_server.py` (new — uses a fake subprocess + fake `AsyncOpencode`)

**Interfaces:**
- Consumes: `_resolve_opencode_binary`, env constants, `_SERVER_BOOT_TIMEOUT_S`.
- Produces:
  - `_OpenCodeServer` with `async start(self, *, cwd: str | None, extra_env: dict[str,str]) -> None`, attribute `base_url: str | None`, attribute `client` (an `AsyncOpencode`), and `async close(self) -> None`.
  - `_parse_listen_url(line: str) -> str | None` — extracts `http://127.0.0.1:<port>` from the announce line.

- [ ] **Step 1: Write the failing tests**

```python
# tests/inner/test_opencode_server.py
from omnigent.inner.opencode_executor import _parse_listen_url


def test_parse_listen_url():
    line = "opencode server listening on http://127.0.0.1:4096"
    assert _parse_listen_url(line) == "http://127.0.0.1:4096"


def test_parse_listen_url_none():
    assert _parse_listen_url("Warning: something unrelated") is None
```

(Full `_OpenCodeServer.start` is exercised by the e2e test in Task 11; here we
unit-test only the pure parse helper to avoid mocking the whole subprocess +
SDK client surface.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/inner/test_opencode_server.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `_OpenCodeServer`**

Append to `omnigent/inner/opencode_executor.py`:

```python
import asyncio
import contextlib
import re

_LISTEN_RE = re.compile(r"listening on (http://\S+)")


def _parse_listen_url(line: str) -> str | None:
    """Extract the base URL OpenCode announces on stdout.

    :param line: A stdout line, e.g.
        ``"opencode server listening on http://127.0.0.1:4096"``.
    :returns: The URL, or ``None`` when the line isn't the announce line.
    """
    match = _LISTEN_RE.search(line)
    return match.group(1) if match else None


class _OpenCodeServer:
    """Owns one ``opencode serve`` subprocess + its ``AsyncOpencode`` client."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self.base_url: str | None = None
        self.client: Any | None = None

    async def start(self, *, cwd: str | None, extra_env: dict[str, str]) -> None:
        """Spawn ``opencode serve``, discover its URL, build the SDK client.

        :param cwd: Working directory for the server (``--dir`` equivalent).
        :param extra_env: Extra env vars (e.g. ``OPENCODE_CONFIG_CONTENT``).
        :raises RuntimeError: If the server doesn't announce a URL in time.
        """
        from opencode_ai import AsyncOpencode  # lazy: optional dep

        binary = _resolve_opencode_binary()
        env = dict(os.environ)
        env.update(extra_env)
        self._proc = await asyncio.create_subprocess_exec(
            binary, "serve", "--port", "0", "--hostname", "127.0.0.1", "--print-logs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env=env,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        async def _await_url() -> str:
            assert self._proc is not None and self._proc.stdout is not None
            while True:
                line_bytes = await self._proc.stdout.readline()
                if not line_bytes:
                    raise RuntimeError("opencode serve exited before announcing a URL")
                url = _parse_listen_url(line_bytes.decode("utf-8", "replace"))
                if url:
                    return url

        try:
            self.base_url = await asyncio.wait_for(_await_url(), timeout=_SERVER_BOOT_TIMEOUT_S)
        except (asyncio.TimeoutError, RuntimeError) as exc:
            await self.close()
            raise RuntimeError(f"opencode serve failed to start: {exc}") from exc
        self.client = AsyncOpencode(base_url=self.base_url)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            chunk = await self._proc.stderr.read(4096)
            if not chunk:
                return

    async def close(self) -> None:
        """Close the SDK client and terminate the server subprocess."""
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
            self.client = None
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        self._proc = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/inner/test_opencode_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_executor.py tests/inner/test_opencode_server.py
git commit -m "feat(opencode): opencode serve lifecycle + SDK client"
```

---

### Task 6: In-process MCP tool-bridge (`_OmnigentToolBridge`)

**Files:**
- Modify: `omnigent/inner/opencode_executor.py` (add `_OmnigentToolBridge`)
- Test: `tests/inner/test_opencode_mcp_bridge.py` (new)

**Interfaces:**
- Consumes: `ToolSpec`, the adapter's `_tool_executor` callback signature `async (name: str, args: dict) -> dict`.
- Produces:
  - `_OmnigentToolBridge(tools: list[ToolSpec], tool_executor)` with `async start() -> str` (returns `http://127.0.0.1:<port>/mcp`), `async close() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/inner/test_opencode_mcp_bridge.py
import pytest
from omnigent.inner.opencode_executor import _OmnigentToolBridge


@pytest.mark.asyncio
async def test_bridge_starts_and_reports_url():
    called = {}

    async def fake_executor(name, args):
        called["name"] = name
        called["args"] = args
        return {"ok": True}

    tools = [{"name": "echo", "description": "echo", "parameters": {
        "type": "object", "properties": {"msg": {"type": "string"}}}}]
    bridge = _OmnigentToolBridge(tools, fake_executor)
    url = await bridge.start()
    try:
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/mcp")
    finally:
        await bridge.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/inner/test_opencode_mcp_bridge.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the bridge**

Append to `omnigent/inner/opencode_executor.py`. Use FastMCP's streamable-HTTP app served by a uvicorn server on an ephemeral port:

```python
import socket
from collections.abc import Awaitable, Callable

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _pick_free_port() -> int:
    """Bind an ephemeral port, release it, and return the number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _OmnigentToolBridge:
    """In-process FastMCP server exposing Omnigent spec tools to OpenCode.

    Each spec tool becomes one MCP tool whose handler round-trips through the
    adapter-supplied *tool_executor* (which dispatches through Omnigent policy
    + execution and emits the function_call events).
    """

    def __init__(self, tools: list[ToolSpec], tool_executor: ToolExecutor) -> None:
        self._tools = tools
        self._tool_executor = tool_executor
        self._server: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None

    async def start(self) -> str:
        """Boot the MCP server on an ephemeral port; return its ``/mcp`` URL."""
        from mcp.server.fastmcp import FastMCP

        self._port = _pick_free_port()
        mcp = FastMCP("omnigent", host="127.0.0.1", port=self._port)

        for spec in self._tools:
            self._register_tool(mcp, spec)

        app = mcp.streamable_http_app()
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=self._port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Wait until uvicorn flips started.
        for _ in range(100):
            if getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.05)
        return f"http://127.0.0.1:{self._port}/mcp"

    def _register_tool(self, mcp: Any, spec: ToolSpec) -> None:
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            return
        description = spec.get("description") or ""
        schema = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
        executor = self._tool_executor

        async def _handler(**kwargs: Any) -> Any:
            return await executor(name, kwargs)

        # FastMCP infers the schema from the function signature by default; we
        # register with the explicit JSON schema so arbitrary tool params work.
        mcp.add_tool(
            _handler,
            name=name,
            description=description,
            structured_output=False,
        )

    async def close(self) -> None:
        """Shut the MCP server down."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5.0)
        self._server = None
        self._task = None
```

Note for implementer: verify `FastMCP.add_tool` signature against the installed `mcp` version (`python -c "import inspect, mcp.server.fastmcp as m; print(inspect.signature(m.FastMCP.add_tool))"`). If `add_tool` does not accept an explicit JSON schema in this version, wrap each tool with a thin function whose signature is built from `parameters` via `functools` / annotations, or fall back to FastMCP's `@mcp.tool()` with `**kwargs`. Adjust `_register_tool` accordingly; keep the handler→`tool_executor` round-trip identical.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/inner/test_opencode_mcp_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_executor.py tests/inner/test_opencode_mcp_bridge.py
git commit -m "feat(opencode): in-process FastMCP tool-bridge"
```

---

### Task 7: `OpenCodeExecutor` — turn loop, interrupt, enqueue, lifecycle

**Files:**
- Modify: `omnigent/inner/opencode_executor.py` (add `OpenCodeExecutor`)
- Test: `tests/inner/test_opencode_executor.py` (new — fakes server + client)

**Interfaces:**
- Consumes: all helpers + classes above; the adapter sets `executor._tool_executor`.
- Produces: `OpenCodeExecutor(Executor)` implementing `run_turn`, `interrupt_session`, `enqueue_session_message`, `supports_live_message_queue`, `handles_tools_internally`, `close_session`, `close`.

- [ ] **Step 1: Write the failing tests** (drive the loop with an injected fake server/client)

```python
# tests/inner/test_opencode_executor.py
import asyncio
import pytest
from omnigent.inner.executor import TextChunk, TurnComplete, ExecutorConfig
from omnigent.inner.opencode_executor import OpenCodeExecutor


class _FakeStream:
    def __init__(self, events): self._events = list(events)
    def __aiter__(self): return self
    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)
    async def close(self): pass


class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)
    def model_dump(self, **kw): return self.__dict__


def _part_event(part):
    return _Obj(type="message.part.updated", properties=_Obj(part=_Obj(**part)))


def _idle(session_id):
    return _Obj(type="session.idle", properties=_Obj(session_id=session_id))


@pytest.mark.asyncio
async def test_run_turn_streams_text_and_completes(monkeypatch):
    ex = OpenCodeExecutor()

    class _FakeSessionRes:
        async def create(self, **kw): return _Obj(id="sess-1")
        async def chat(self, **kw):
            return _Obj(parts=[], tokens={"input": 5, "output": 3, "cache": {}},
                        model_id="m", provider_id="p")
        async def abort(self, **kw): return _Obj(ok=True)

    class _FakeEventRes:
        async def list(self):
            return _FakeStream([
                _part_event({"id": "p1", "type": "text", "text": "Hi"}),
                _idle("sess-1"),
            ])

    class _FakeClient:
        session = _FakeSessionRes()
        event = _FakeEventRes()
        async def close(self): pass

    async def _fake_ensure_server(self):
        self._server_started = True
        self._client = _FakeClient()

    monkeypatch.setattr(OpenCodeExecutor, "_ensure_server", _fake_ensure_server)

    events = []
    async for e in ex.run_turn(
        [{"role": "user", "content": "hello", "session_id": "k1"}],
        tools=[], system_prompt="", config=ExecutorConfig(),
    ):
        events.append(e)
    assert any(isinstance(e, TextChunk) and e.text == "Hi" for e in events)
    tc = [e for e in events if isinstance(e, TurnComplete)]
    assert tc and tc[0].response == "Hi"
    assert tc[0].usage == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def test_capabilities():
    ex = OpenCodeExecutor()
    assert ex.handles_tools_internally() is True
    assert ex.supports_streaming() is True
    assert ex.supports_live_message_queue() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/inner/test_opencode_executor.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `OpenCodeExecutor`**

Append to `omnigent/inner/opencode_executor.py`. Key points: lazy `_ensure_server` builds the MCP bridge (when `_tool_executor` set + tools present) → synthesises config → starts `_OpenCodeServer`; per-`session_key` OpenCode session-id cache; subscribe-then-prompt; end on `session.idle` for our id or when `chat()` returns; `interrupt_session`→`session.abort`; `enqueue_session_message`→fire-and-forget `session.chat`.

```python
import uuid


class OpenCodeExecutor(Executor):
    """Drive OpenCode via a persistent ``opencode serve`` + the Python SDK."""

    def __init__(self) -> None:
        self._model = os.environ.get(_ENV_MODEL, "").strip() or None
        self._cwd = os.environ.get(_ENV_CWD, "").strip() or None
        self._thinking = _parse_truthy(os.environ.get(_ENV_THINKING))
        skip_raw = os.environ.get(_ENV_SKIP_PERMISSIONS)
        self._skip_permissions = True if skip_raw is None else _parse_truthy(skip_raw)
        self._server = _OpenCodeServer()
        self._server_started = False
        self._client: Any | None = None
        self._bridge: _OmnigentToolBridge | None = None
        self._session_ids: dict[str, str] = {}
        # Set by ExecutorAdapter before the first run_turn.
        self._tool_executor: ToolExecutor | None = None
        self._lock = asyncio.Lock()

    async def _ensure_server(self, tools: list[ToolSpec]) -> None:
        async with self._lock:
            if self._server_started:
                return
            mcp_extra: dict[str, Any] | None = None
            if tools and self._tool_executor is not None:
                self._bridge = _OmnigentToolBridge(tools, self._tool_executor)
                url = await self._bridge.start()
                mcp_extra = {"omnigent": {"type": "remote", "url": url}}
            extra_env: dict[str, str] = {}
            payload = _build_opencode_config_content(mcp_extra=mcp_extra)
            if payload is not None:
                extra_env[_OPENCODE_CONFIG_CONTENT_ENV] = json.dumps(payload, separators=(",", ":"))
                extra_env[_OPENCODE_DISABLE_PROJECT_CONFIG_ENV] = "1"
            await self._server.start(cwd=self._cwd, extra_env=extra_env)
            self._client = self._server.client
            self._server_started = True

    def _session_key_for(self, messages: list[Message]) -> str | None:
        for message in messages:
            sid = message.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return None

    async def _opencode_session_id(self, session_key: str | None) -> str:
        key = session_key or "default"
        if key in self._session_ids:
            return self._session_ids[key]
        created = await self._client.session.create()
        sid = created.id
        self._session_ids[key] = sid
        return sid

    @staticmethod
    def _as_dict(obj: Any) -> dict[str, Any]:
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump(by_alias=False)
        return getattr(obj, "__dict__", {})

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        await self._ensure_server(tools)
        session_key = self._session_key_for(messages)
        prompt = _latest_user_text(messages)
        if not prompt:
            yield ExecutorError(message="opencode harness: no user message in request")
            return
        session_id = await self._opencode_session_id(session_key)
        model = config.model if (config and config.model) else self._model
        provider_id, model_id = _split_provider_model(model)

        tracker = _PartTracker()
        stream = await self._client.event.list()
        chat_kwargs: dict[str, Any] = {
            "id": session_id,
            "parts": [{"type": "text", "text": prompt}],
        }
        if provider_id:
            chat_kwargs["provider_id"] = provider_id
        if model_id:
            chat_kwargs["model_id"] = model_id
        if system_prompt:
            chat_kwargs["system"] = system_prompt
        chat_task = asyncio.create_task(self._client.session.chat(**chat_kwargs))

        final_text: list[str] = []
        usage: dict[str, Any] | None = None
        try:
            async for raw in stream:
                evt = self._as_dict(raw)
                etype = evt.get("type")
                props = self._as_dict(evt.get("properties"))
                ev_session = props.get("session_id") or props.get("sessionID")
                if etype == "message.part.updated":
                    part = self._as_dict(props.get("part"))
                    if part.get("session_id") and part.get("session_id") != session_id:
                        continue
                    for out in _translate_part_event(part, tracker, emit_reasoning=self._thinking):
                        if isinstance(out, TextChunk):
                            final_text.append(out.text)
                        yield out
                elif etype == "session.error" and (ev_session in (None, session_id)):
                    err = self._as_dict(props.get("error"))
                    yield ExecutorError(message=f"opencode: {err.get('name') or err or 'error'}")
                    return
                elif etype == "session.idle" and ev_session == session_id:
                    break
        finally:
            with contextlib.suppress(Exception):
                await stream.close()

        try:
            result = await chat_task
            tokens = self._as_dict(result).get("tokens")
            if isinstance(tokens, dict):
                usage = _tokens_to_usage(tokens)
        except Exception as exc:  # noqa: BLE001
            yield ExecutorError(message=f"opencode: chat failed: {exc}")
            return

        yield TurnComplete(response="".join(final_text) or None, usage=usage)

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        return None

    async def interrupt_session(self, session_key: str) -> bool:
        sid = self._session_ids.get(session_key)
        if not sid or self._client is None:
            return False
        with contextlib.suppress(Exception):
            await self._client.session.abort(id=sid)
            return True
        return False

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        sid = self._session_ids.get(session_key)
        if not sid or self._client is None:
            return False
        text = content if isinstance(content, str) else str(content)
        provider_id, model_id = _split_provider_model(self._model)
        kwargs: dict[str, Any] = {"id": sid, "parts": [{"type": "text", "text": text}]}
        if provider_id:
            kwargs["provider_id"] = provider_id
        if model_id:
            kwargs["model_id"] = model_id
        asyncio.create_task(self._client.session.chat(**kwargs))
        return True

    async def close_session(self, session_key: str) -> None:
        self._session_ids.pop(session_key, None)

    async def close(self) -> None:
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None
        await self._server.close()
        self._server_started = False
        self._client = None
```

Implementer note: the SDK's `session.chat` requires `provider_id` + `model_id` (not `NOT_GIVEN`-optional in practice). If a turn has no model pinned, fetch the OpenCode default once via `self._client.app.get()` / config and fill both; only omit when the SDK accepts absence. Verify against `python -c "import inspect; from opencode_ai.resources.session import AsyncSessionResource as S; print(inspect.signature(S.chat))"` and adjust the `if provider_id`/`if model_id` gating to always supply both when the API rejects partial input.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/inner/test_opencode_executor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_executor.py tests/inner/test_opencode_executor.py
git commit -m "feat(opencode): SDK-driven turn loop, interrupt, live queue"
```

---

### Task 8: Harness wrap (`create_app`)

**Files:**
- Create: `omnigent/inner/opencode_harness.py`
- Test: `tests/inner/test_opencode_harness.py` (new)

**Interfaces:**
- Consumes: `OpenCodeExecutor`, `ExecutorAdapter`.
- Produces: `create_app() -> FastAPI`; `_build_opencode_executor() -> Executor`.

- [ ] **Step 1: Write the failing test**

```python
# tests/inner/test_opencode_harness.py
from fastapi import FastAPI
from omnigent.inner.opencode_harness import create_app, _build_opencode_executor
from omnigent.inner.opencode_executor import OpenCodeExecutor


def test_build_executor():
    assert isinstance(_build_opencode_executor(), OpenCodeExecutor)


def test_create_app():
    assert isinstance(create_app(), FastAPI)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/inner/test_opencode_harness.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the wrap**

Create `omnigent/inner/opencode_harness.py` with the env-var contract docstring (copy the env list from PR #183's harness docstring, adjusting the transport sentence to "persistent `opencode serve` + Python SDK"):

```python
from __future__ import annotations

import logging

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.opencode_executor import OpenCodeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)


def _build_opencode_executor() -> Executor:
    """Construct the inner :class:`OpenCodeExecutor` (server spawned lazily)."""
    return OpenCodeExecutor()


def create_app() -> FastAPI:
    """Build the OpenCode harness's FastAPI app (per the harness contract)."""
    adapter = ExecutorAdapter(executor_factory=_build_opencode_executor)
    return adapter.build()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/inner/test_opencode_harness.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/opencode_harness.py tests/inner/test_opencode_harness.py
git commit -m "feat(opencode): harness wrap create_app"
```

---

### Task 9: Workflow spawn-env builder + provider/Databricks routing

**Files:**
- Modify: `omnigent/runtime/workflow.py` (add `_build_opencode_spawn_env`, `_apply_provider_to_opencode`, `_apply_databricks_profile_to_opencode`, `_materialise_one_shot_token`; extend `_HARNESS_GATEWAY_FLAG` map + `configure_agent_harness_with_provider` dispatch)
- Modify: `omnigent/runner/app.py` (model-override env map + dispatch branch)
- Test: `tests/runtime/test_opencode_spawn_env.py` (new)

**Interfaces:**
- Consumes: `AgentSpec`, `ProviderEntry`, `FamilyConfig`, `ApiKeyAuth`, `DatabricksAuth`, `_resolve_spec_model`, `_resolve_provider_for_build`, `configure_agent_harness_with_provider`, `ANTHROPIC_FAMILY`, `OPENAI_FAMILY`.
- Produces: `_build_opencode_spawn_env(spec, *, workdir=None) -> dict[str,str]`.

This task reproduces the workflow additions from PR #183 verbatim (they are transport-agnostic). Use the exact code from the PR diff sections for `_apply_databricks_profile_to_opencode`, `_apply_provider_to_opencode`, `_materialise_one_shot_token`, `_build_opencode_spawn_env`, the `_HARNESS_GATEWAY_FLAG` `"opencode"` entry, and the two dispatch branches in `configure_agent_harness_with_provider` (the `if harness_type == "opencode"` early-return inside the gateway-transport block, and the standalone `if harness_type == "opencode": _apply_provider_to_opencode(...)` branch). Reference: `docs/superpowers/plans/pr183-reference.diff` lines 1493-1783 (also reproduced in `docs/OPENCODE_SDK_DESIGN.md` surface list).

- [ ] **Step 1: Write the failing tests**

```python
# tests/runtime/test_opencode_spawn_env.py
from omnigent.runtime.workflow import _build_opencode_spawn_env
from omnigent.spec.omnigent import AgentSpec  # adjust import to the real AgentSpec location


def _spec(**executor):
    return AgentSpec.model_validate(
        {"name": "oc", "prompt": "hi", "executor": {"harness": "opencode", **executor}}
    )


def test_model_and_cwd(tmp_path):
    env = _build_opencode_spawn_env(_spec(model="anthropic/claude-sonnet-4-5"), workdir=tmp_path)
    assert env["HARNESS_OPENCODE_MODEL"] == "anthropic/claude-sonnet-4-5"
    assert env["HARNESS_OPENCODE_CWD"] == str(tmp_path)


def test_api_key_auth_bakes_gateway_key():
    env = _build_opencode_spawn_env(
        _spec(auth={"type": "api_key", "api_key": "sk-xyz"})
    )
    assert env["HARNESS_OPENCODE_GATEWAY_API_KEY"] == "sk-xyz"
```

Implementer: confirm `AgentSpec` import path and the exact auth dict shape with `grep -n "class ApiKeyAuth\|class DatabricksAuth\|class AgentSpec" omnigent/spec/*.py`. Mirror the assertions used in PR #183's `tests/runtime/test_provider_spawn_env.py` additions (see `docs/superpowers/plans/pr183-reference.diff` line 2894+).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/runtime/test_opencode_spawn_env.py -v`
Expected: FAIL (`_build_opencode_spawn_env` not defined).

- [ ] **Step 3: Add the workflow functions**

Paste the four functions + the `_HARNESS_GATEWAY_FLAG` `"opencode"` entry + the two `configure_agent_harness_with_provider` dispatch branches from `docs/superpowers/plans/pr183-reference.diff` (lines 1493-1783) into `omnigent/runtime/workflow.py`. These are transport-independent — they only write `HARNESS_OPENCODE_*` env vars consumed by Tasks 2-3.

- [ ] **Step 4: Wire runner/app.py**

In `omnigent/runner/app.py`, add to the `_HARNESS_MODEL_ENV_KEY` map:

```python
    "opencode": "HARNESS_OPENCODE_MODEL",
```

In `_build_spawn_env_from_spec`, add `_build_opencode_spawn_env` to the import tuple and the dispatch:

```python
        elif harness == "opencode":
            env = _build_opencode_spawn_env(spec, workdir=workdir)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/runtime/test_opencode_spawn_env.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add omnigent/runtime/workflow.py omnigent/runner/app.py tests/runtime/test_opencode_spawn_env.py
git commit -m "feat(opencode): spawn-env builder + provider/databricks routing"
```

---

### Task 10: Model catalog/override + onboarding install/readiness

**Files:**
- Modify: `omnigent/model_catalog.py` (identity entry)
- Modify: `omnigent/model_override.py:26-28` (add to `_SDK_MODEL_OVERRIDE_HARNESSES`)
- Modify: `omnigent/onboarding/harness_install.py` (`OPENCODE_KEY` + install spec + required-CLI map)
- Modify: `omnigent/onboarding/harness_readiness.py` (`OPENCODE_SURFACE` + branches)
- Test: `tests/onboarding/test_opencode_onboarding.py` (new)

**Interfaces:**
- Produces: `OPENCODE_KEY = "opencode"`, `OPENCODE_SURFACE = "opencode"`; `harness_is_configured("opencode")` resolvable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/onboarding/test_opencode_onboarding.py
from omnigent.onboarding.harness_install import (
    OPENCODE_KEY, HARNESS_INSTALL_SPECS, required_cli_for_harness,
)
from omnigent.onboarding.harness_readiness import OPENCODE_SURFACE, configured_harness_map


def test_install_spec_present():
    spec = HARNESS_INSTALL_SPECS[OPENCODE_KEY]
    assert spec.package == "opencode-ai"
    assert spec.binary == "opencode"


def test_required_cli_for_opencode():
    assert required_cli_for_harness("opencode") is not None


def test_configured_map_includes_opencode():
    assert OPENCODE_SURFACE in configured_harness_map()
```

Implementer: confirm the real symbol names (`HARNESS_INSTALL_SPECS`, `required_cli_for_harness`, `HarnessInstallSpec` field names) via `grep -n "HARNESS_INSTALL_SPECS\|def required_cli_for_harness\|class HarnessInstallSpec" omnigent/onboarding/harness_install.py`. Adjust the test to match.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/onboarding/test_opencode_onboarding.py -v`
Expected: FAIL.

- [ ] **Step 3: Apply the onboarding + catalog + override edits**

Apply the exact additions from `docs/superpowers/plans/pr183-reference.diff`:
- `model_catalog.py`: add `"opencode": "opencode",` to the harness-identity map (diff lines 1299-1309).
- `model_override.py`: add `"opencode"` to `_SDK_MODEL_OVERRIDE_HARNESSES` (line 1320).
- `harness_install.py`: add `OPENCODE_KEY = "opencode"`, the `HarnessInstallSpec("OpenCode", "opencode", "opencode-ai", login_args=("auth","login"), logout_args=("auth","logout"))` entry, and the required-CLI map entry (diff lines 1325-1374).
- `harness_readiness.py`: add `OPENCODE_SURFACE = "opencode"`, the `_install_key` branch, the `harness_is_configured` branch, and `spellings.add(OPENCODE_SURFACE)` (diff lines 1377-1443).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/onboarding/test_opencode_onboarding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/model_catalog.py omnigent/model_override.py omnigent/onboarding/harness_install.py omnigent/onboarding/harness_readiness.py tests/onboarding/test_opencode_onboarding.py
git commit -m "feat(opencode): model catalog/override + onboarding install/readiness"
```

---

### Task 11: CLI subcommand + choices + first-run + docs/example/frontend

**Files:**
- Modify: `omnigent/cli.py` (subcommand, command list, harness choices/help, default prompt, first-run plan)
- Modify: `README.md`, `docs/AGENT_YAML_SPEC.md`, `ap-web/src/components/AgentCard.tsx`
- Create: `examples/opencode_hello.yaml`
- Test: `tests/cli/test_opencode_cli.py` (new)

**Interfaces:**
- Produces: `omnigent opencode` Click command forwarding to `run --harness opencode`.

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_opencode_cli.py
from click.testing import CliRunner
from omnigent.cli import cli


def test_opencode_in_help():
    result = CliRunner().invoke(cli, ["--help"])
    assert "opencode" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/cli/test_opencode_cli.py -v`
Expected: FAIL.

- [ ] **Step 3: Apply CLI + docs + example + frontend edits**

Apply the exact additions from `docs/superpowers/plans/pr183-reference.diff`:
- `cli.py`: the `opencode` command (lines 363-399), the command-list entry `"opencode",` (line 355), the first-run plan branch (lines 339-347; the `harness_cli_installed(OPENCODE_KEY)` proxy), the `_HARNESS_CHOICES_HELP` addition, and the `opencode` default-prompt entry (lines 414-422).
- `README.md`, `docs/AGENT_YAML_SPEC.md`, `AgentCard.tsx`: lines 1-95 of the diff.
- `examples/opencode_hello.yaml`: lines 277-330 of the diff, but change the description/comments to say "driven via a persistent `opencode serve` process + the Python SDK" instead of "per turn".

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/cli/test_opencode_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/cli.py README.md docs/AGENT_YAML_SPEC.md ap-web/src/components/AgentCard.tsx examples/opencode_hello.yaml tests/cli/test_opencode_cli.py
git commit -m "feat(opencode): omnigent opencode CLI + docs + example"
```

---

### Task 12: End-to-end test (opt-in, real `opencode`)

**Files:**
- Create: `tests/e2e/test_opencode_executor_e2e.py`

**Interfaces:**
- Consumes: `OpenCodeExecutor`, real `opencode` binary + configured provider.

- [ ] **Step 1: Write the gated e2e test**

```python
# tests/e2e/test_opencode_executor_e2e.py
import os
import pytest
from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.inner.opencode_executor import OpenCodeExecutor

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE") != "1",
    reason="set OMNIGENT_E2E_OPENCODE=1 and have `opencode` configured to run",
)


@pytest.mark.asyncio
async def test_opencode_server_roundtrip():
    ex = OpenCodeExecutor()
    try:
        texts = []
        usage = None
        async for e in ex.run_turn(
            [{"role": "user", "content": "Reply with exactly: PONG", "session_id": "e2e"}],
            tools=[], system_prompt="", config=None,
        ):
            if isinstance(e, TextChunk):
                texts.append(e.text)
            if isinstance(e, TurnComplete):
                usage = e.usage
        assert "PONG" in "".join(texts)
        assert usage is None or "input_tokens" in usage
    finally:
        await ex.close()
```

- [ ] **Step 2: Run it (gated off by default → skips)**

Run: `python -m pytest tests/e2e/test_opencode_executor_e2e.py -v`
Expected: SKIPPED.

- [ ] **Step 3: Run it for real (manual verification)**

Run: `OMNIGENT_E2E_OPENCODE=1 python -m pytest tests/e2e/test_opencode_executor_e2e.py -v`
Expected: PASS (requires `opencode` on PATH + a configured provider via `opencode auth login`).

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_opencode_executor_e2e.py
git commit -m "test(opencode): opt-in serve/SDK e2e"
```

---

### Task 13: Full verification + design-doc followups

**Files:**
- Modify: `docs/OPENCODE_SDK_DESIGN.md` (append a "Status: implemented" note + any remaining deferrals: multimodal input, `--continue`, dedicated glyph)

- [ ] **Step 1: Run the full opencode unit suite**

Run: `python -m pytest tests/inner/test_opencode_helpers.py tests/inner/test_opencode_config.py tests/inner/test_opencode_translate.py tests/inner/test_opencode_server.py tests/inner/test_opencode_mcp_bridge.py tests/inner/test_opencode_executor.py tests/inner/test_opencode_harness.py tests/inner/test_opencode_registry.py tests/runtime/test_opencode_spawn_env.py tests/onboarding/test_opencode_onboarding.py tests/cli/test_opencode_cli.py -v`
Expected: all PASS.

- [ ] **Step 2: Lint/type-check per repo conventions**

Run the repo's configured linters (check `pyproject.toml`/`Makefile`/`.pre-commit-config.yaml` for the exact commands; likely `ruff check omnigent/inner/opencode_executor.py omnigent/inner/opencode_harness.py` and `mypy omnigent/inner/opencode_executor.py`).
Expected: clean (fix any issues found).

- [ ] **Step 3: Real e2e (manual)**

Run: `OMNIGENT_E2E_OPENCODE=1 python -m pytest tests/e2e/test_opencode_executor_e2e.py -v`
Then: `omnigent opencode -p "list files in the current directory"` (manual smoke).
Expected: streamed reply; for MCP, verify a spec tool round-trips with a tools-bearing agent YAML.

- [ ] **Step 4: Update design doc + commit**

Append implementation status + remaining deferrals to `docs/OPENCODE_SDK_DESIGN.md`.

```bash
git add docs/OPENCODE_SDK_DESIGN.md
git commit -m "docs(opencode): mark SDK harness implemented + note deferrals"
```

---

## Self-Review notes

- **Spec coverage:** scope = full PR-#183 parity on SDK transport + interrupt (Task 7) + live queue (Task 7) + in-process MCP bridge (Task 6) + token usage (Tasks 4, 7). Surfaces: registry/allowlist/type (Task 1), executor+server+bridge (Tasks 2-7), wrap (Task 8), spawn-env/provider/databricks/runner (Task 9), catalog/override/onboarding (Task 10), CLI/docs/example/frontend (Task 11), tests (Tasks 1-12).
- **Deferred (documented, matches PR followups):** multimodal input, native TUI (`opencode-native`), dedicated glyph asset, `--continue` flag.
- **Verification risks to watch during execution:** (1) exact `session.chat` required-arg behavior (provider_id/model_id) — verify signature, fill defaults if partial input is rejected; (2) `FastMCP.add_tool` schema-passing across `mcp` versions — verify and adjust `_register_tool`; (3) real SDK event field names (`session_id` alias vs `sessionID`) — the `_as_dict` + dual-key reads handle both, but confirm against a live stream in Task 12; (4) `AgentSpec` / auth-class import paths in Tasks 9-10.
