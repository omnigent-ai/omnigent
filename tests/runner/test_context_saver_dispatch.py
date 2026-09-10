"""Runner-dispatch coverage for Context Saver interception and Focused Read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime import _globals
from omnigent.runtime.caps import RuntimeCaps
from omnigent.runtime.context_saver import (
    FocusedReadWorker,
    FocusedReadWorkerResult,
    parse_context_saver_settings,
)
from omnigent.spec.types import AgentSpec


class _FakeOsEnvironment:
    def __init__(self, content: str) -> None:
        self.content = content
        self.read_calls: list[str] = []
        self.metadata_calls: list[str] = []
        self.shell_calls: list[str] = []

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
        max_binary_bytes: int | None = None,
        max_text_bytes: int | None = None,
    ) -> dict[str, object]:
        del max_binary_bytes
        self.read_calls.append(path)
        total_bytes = len(self.content.encode("utf-8"))
        if max_text_bytes is not None and total_bytes > max_text_bytes:
            return {"error": f"text file exceeds the {max_text_bytes}-byte read limit"}
        lines = self.content.splitlines(keepends=True)
        effective_limit = len(lines) if limit is None else limit
        returned = lines[offset - 1 : offset - 1 + effective_limit]
        result: dict[str, object] = {
            "path": path,
            "content": "".join(returned),
            "encoding": "utf-8",
            "offset": offset,
            "limit": effective_limit,
            "returned_lines": len(returned),
            "total_lines": len(lines),
        }
        if max_text_bytes is not None:
            result["total_bytes"] = total_bytes
        return result

    async def read_metadata(self, path: str) -> dict[str, object]:
        self.metadata_calls.append(path)
        return {
            "path": path,
            "encoding": "utf-8",
            "total_lines": len(self.content.splitlines()),
            "total_bytes": len(self.content.encode("utf-8")),
        }

    async def shell(self, command: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.shell_calls.append(command)
        return {"stdout": self.content, "exit_code": 0}

    def close(self) -> None:
        return None


class _FocusedWorker:
    async def focus(self, **kwargs: object) -> FocusedReadWorkerResult:
        return FocusedReadWorkerResult(
            content=(
                '{"answer":"The target is on line 6.","sources":['
                '{"path":"large.py","ranges":[{"start":6,"end":6,'
                '"excerpt":"line 6"}]}]}'
            ),
            reported_model="databricks-glm-5-2",
        )


class _ServerWorkerClient:
    def __init__(
        self,
        os_env: _FakeOsEnvironment,
        *,
        available: bool = True,
        preflight_status: int = 200,
        reported_model: str = "databricks-glm-5-2",
    ) -> None:
        self.os_env = os_env
        self.available = available
        self.preflight_status = preflight_status
        self.reported_model = reported_model
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def get(self, path: str, *, timeout: float) -> httpx.Response:
        del timeout
        assert self.os_env.read_calls == []
        self.gets.append(path)
        return httpx.Response(
            self.preflight_status,
            json={"available": self.available},
            request=httpx.Request("GET", path),
        )

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        del timeout
        self.posts.append((path, json))
        return httpx.Response(
            200,
            json={
                "content": (
                    '{"answer":"The target is on line 6.","sources":['
                    '{"path":"large.py","ranges":[{"start":6,"end":6,'
                    '"excerpt":"line 6"}]}]}'
                ),
                "input_tokens": 20,
                "output_tokens": 10,
                "model": self.reported_model,
            },
            request=httpx.Request("POST", path),
        )


def _caps(worker: FocusedReadWorker | None = None) -> RuntimeCaps:
    return RuntimeCaps(
        context_saver=parse_context_saver_settings(
            {
                "enabled": True,
                "techniques": {"focused_read": {"min_lines": 5}},
            }
        ),
        context_saver_worker=worker,
    )


def _agent_with_filesystem_capability() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(type="caller_process", cwd="."),
    )


@pytest.mark.asyncio
async def test_runner_disabled_context_saver_preserves_legacy_read_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("one\ntwo\n")
    disabled_settings = parse_context_saver_settings({"enabled": False})
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(
        "omnigent.runtime.context_saver.load_context_saver_settings",
        lambda _workspace: disabled_settings,
    )
    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(context_saver=disabled_settings),
    )

    raw = await execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "source.txt"}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw) == {
        "path": "source.txt",
        "content": "one\ntwo\n",
        "encoding": "utf-8",
        "offset": 1,
        "limit": 2_000,
        "returned_lines": 2,
        "total_lines": 2,
    }
    assert fake.metadata_calls == []


@pytest.mark.asyncio
async def test_runner_redirects_large_read_before_returning_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"secret line {i}\n" for i in range(10)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps())

    raw = await execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "large.py"}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )
    result = json.loads(raw)

    assert result["context_saver"] == "redirect"
    assert "secret line" not in raw
    assert result["paths"] == ["large.py"]
    assert fake.metadata_calls == ["large.py"]
    assert fake.read_calls == []


@pytest.mark.asyncio
async def test_runner_allows_broad_read_below_context_saver_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("one\ntwo\nthree\nfour\n")
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps())

    raw = await execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "small.py"}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw) == {
        "path": "small.py",
        "content": fake.content,
        "encoding": "utf-8",
        "offset": 1,
        "limit": 2_000,
        "returned_lines": 4,
        "total_lines": 4,
    }
    assert fake.metadata_calls == ["small.py"]
    assert fake.read_calls == ["small.py"]


@pytest.mark.asyncio
async def test_project_config_cannot_override_unavailable_context_saver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"line {i}\n" for i in range(10)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(context_saver_available=False),
    )
    monkeypatch.setattr(
        "omnigent.runtime.context_saver.load_context_saver_settings",
        lambda _workspace: parse_context_saver_settings(
            {
                "enabled": True,
                "techniques": {"focused_read": {"min_lines": 5}},
            }
        ),
    )

    raw = await execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "large.py"}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw)["content"] == fake.content


@pytest.mark.asyncio
async def test_context_read_rejects_project_enablement_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(context_saver_available=False),
    )
    monkeypatch.setattr(
        "omnigent.runtime.context_saver.load_context_saver_settings",
        lambda _workspace: parse_context_saver_settings({"enabled": True}),
    )

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw) == {"error": "Context Saver is unavailable in this deployment"}


@pytest.mark.asyncio
async def test_runner_allows_targeted_read_of_large_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"line {i}\n" for i in range(10)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps())

    raw = await execute_tool(
        tool_name="sys_os_read",
        arguments=json.dumps({"path": "large.py", "offset": 6, "limit": 1}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )
    result = json.loads(raw)

    assert result["content"] == "line 5\n"


@pytest.mark.asyncio
async def test_runner_focused_read_uses_injected_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"line {i}\n" for i in range(1, 11)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps(_FocusedWorker()))

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )
    result = json.loads(raw)

    assert result["failure"] is None
    assert result["model_routing"] == {
        "primary_models": "all",
        "worker_route": "databricks/context-saver-cheap",
        "worker_model_reported": "databricks-glm-5-2",
        "route_provider": "databricks",
        "non_databricks_source_sharing_allowed": False,
    }
    assert result["relevant_line_ranges"] == {"large.py": [[6, 6]]}
    assert "large.py:6-6" in result["content"]
    assert fake.metadata_calls == ["large.py"]
    assert fake.read_calls == ["large.py"]


@pytest.mark.asyncio
async def test_runner_rejects_file_changed_after_focused_read_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _ShrinkingOsEnvironment(_FakeOsEnvironment):
        async def read_metadata(self, path: str) -> dict[str, object]:
            result = await super().read_metadata(path)
            self.content = self.content[:-1]
            return result

    fake = _ShrinkingOsEnvironment("".join(f"line {i}\n" for i in range(1, 11)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps(_FocusedWorker()))

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )
    result = json.loads(raw)

    assert result["failure"] == "file_read_failed:file changed while it was being read"


@pytest.mark.asyncio
async def test_runner_focused_read_proxies_server_worker_before_local_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"line {i}\n" for i in range(1, 11)))
    server_client = _ServerWorkerClient(
        fake,
        reported_model="gpt-4o-mini-2024-07-18",
    )
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(
            context_saver=parse_context_saver_settings(
                {
                    "enabled": True,
                    "techniques": {
                        "focused_read": {
                            "worker_model": "openai/gpt-4o-mini",
                            "worker_provider": "runner-local-provider",
                            "allow_source_upload": True,
                        }
                    },
                }
            )
        ),
    )

    def _unexpected_local_credentials(*args: object, **kwargs: object) -> None:
        raise AssertionError("local worker credentials must not be resolved")

    monkeypatch.setattr(
        "omnigent.runtime.focused_read.resolve_configured_focused_read_connection",
        _unexpected_local_credentials,
    )

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        server_client=server_client,  # type: ignore[arg-type]
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )
    result = json.loads(raw)

    assert result["failure"] is None
    assert result["worker_input_tokens"] == 20
    assert result["model_routing"] == {
        "primary_models": "all",
        "worker_route": "openai/gpt-4o-mini",
        "worker_model_reported": "gpt-4o-mini-2024-07-18",
        "route_provider": "openai",
        "non_databricks_source_sharing_allowed": True,
    }
    assert server_client.gets == ["/v1/sessions/conv_test/context-saver/focused-read"]
    assert server_client.posts[0][1]["files"] == [{"path": "large.py", "content": fake.content}]
    assert server_client.posts[0][1]["model"] == "openai/gpt-4o-mini"
    assert server_client.posts[0][1]["allow_source_upload"] is True


@pytest.mark.asyncio
async def test_runner_resolves_local_worker_before_read_when_server_has_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("secret source\n")
    server_client = _ServerWorkerClient(fake, available=False)
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(
            context_saver=parse_context_saver_settings(
                {
                    "enabled": True,
                    "techniques": {
                        "focused_read": {
                            "worker_model": "openai/gpt-4o-mini",
                            "worker_provider": "missing-worker",
                            "allow_source_upload": True,
                        }
                    },
                }
            )
        ),
    )

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        server_client=server_client,  # type: ignore[arg-type]
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert "missing-worker" in json.loads(raw)["error"]
    assert fake.read_calls == []
    assert fake.metadata_calls == []
    assert server_client.posts == []


@pytest.mark.asyncio
async def test_runner_does_not_fallback_when_server_disables_context_saver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("secret source\n")
    server_client = _ServerWorkerClient(fake, preflight_status=403)
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps())

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        server_client=server_client,  # type: ignore[arg-type]
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw) == {"error": "Context Saver server worker preflight returned 403"}
    assert fake.read_calls == []
    assert fake.metadata_calls == []
    assert server_client.posts == []


@pytest.mark.asyncio
async def test_runner_resolves_worker_destination_before_reading_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("secret source\n")
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(
            context_saver=parse_context_saver_settings(
                {
                    "enabled": True,
                    "techniques": {
                        "focused_read": {
                            "worker_model": "openai/gpt-4o-mini",
                            "worker_provider": "missing-worker",
                            "allow_source_upload": True,
                        }
                    },
                }
            )
        ),
    )

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        agent_spec=_agent_with_filesystem_capability(),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert "missing-worker" in json.loads(raw)["error"]
    assert fake.read_calls == []
    assert fake.metadata_calls == []


@pytest.mark.asyncio
async def test_runner_rejects_context_read_without_filesystem_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_globals, "_caps", _caps(_FocusedWorker()))

    raw = await execute_tool(
        tool_name="sys_context_read",
        arguments=json.dumps({"paths": ["large.py"], "question": "Where is the target?"}),
        agent_spec=AgentSpec(spec_version=1),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw) == {
        "error": "sys_context_read requires an authorized filesystem capability"
    }


@pytest.mark.asyncio
async def test_runner_redirects_broad_shell_read_before_shell_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"secret line {i}\n" for i in range(10)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps())

    raw = await execute_tool(
        tool_name="sys_os_shell",
        arguments=json.dumps({"command": "cat large.py | grep secret"}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw)["context_saver"] == "redirect"
    assert fake.metadata_calls == ["large.py"]
    assert fake.read_calls == []
    assert fake.shell_calls == []


@pytest.mark.asyncio
async def test_runner_resolves_broad_shell_read_after_directory_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeOsEnvironment("".join(f"secret line {i}\n" for i in range(10)))
    monkeypatch.setattr("omnigent.inner.os_env.create_os_environment", lambda spec: fake)
    monkeypatch.setattr(_globals, "_caps", _caps())

    raw = await execute_tool(
        tool_name="sys_os_shell",
        arguments=json.dumps({"command": "cd sub && cat large.py"}),
        conversation_id="conv_test",
        runner_workspace=tmp_path,
    )

    assert json.loads(raw)["context_saver"] == "redirect"
    assert fake.metadata_calls == ["sub/large.py"]
    assert fake.read_calls == []
    assert fake.shell_calls == []
