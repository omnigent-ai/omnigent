"""Unit tests for the createos os_env provider (`type='createos'`).

No live control plane: a stub `_Http` records calls and returns canned
responses, so the sync impls and the async delegation are exercised in
isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.createos_os_env import CreateosOSEnvironment, _Http
from omnigent.inner.datamodel import OSEnvSpec


class _StubHttp:
    """Stand-in for `_Http`; records calls, returns scripted values."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.exec_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.closed = False

    def get_bytes(self, path: str, params: dict[str, str]) -> bytes:
        key = params["path"]
        if key not in self.files:
            import httpx

            req = httpx.Request("GET", "http://x")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("not found", request=req, response=resp)
        return self.files[key]

    def put_bytes(self, path: str, data: bytes, params: dict[str, str]) -> None:
        self.files[params["path"]] = data

    def post(self, path: str, body: Any = None) -> Any:
        self.exec_calls.append(body)
        return {"result": {"stdout": "hi\n", "stderr": "", "exit_code": 0}, "exec_ms": 1}

    def delete(self, path: str) -> None:
        self.deleted.append(path)

    def close(self) -> None:
        self.closed = True


def _env(http: _StubHttp, cwd: str = "/root") -> CreateosOSEnvironment:
    return CreateosOSEnvironment(
        spec=OSEnvSpec(type="createos"),
        cwd=Path(cwd),
        _sandbox_id="sbx_test",
        _http=http,  # type: ignore[arg-type]
    )


def test_create_sync_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key (no spec field, no env var) raises a clear ValueError."""
    monkeypatch.delenv("CREATEOS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires an API key"):
        CreateosOSEnvironment.create_sync(OSEnvSpec(type="createos"))


def test_abs_resolves_relative_against_cwd() -> None:
    env = _env(_StubHttp(), cwd="/work")
    assert env._abs("a/b.txt") == "/work/a/b.txt"
    assert env._abs("/etc/hosts") == "/etc/hosts"


async def test_write_then_read_roundtrip() -> None:
    http = _StubHttp()
    env = _env(http)
    w = await env.write("notes.txt", "line1\nline2\n")
    assert w["ok"] is True
    assert w["bytes_written"] == len("line1\nline2\n".encode())
    assert http.files["/root/notes.txt"] == b"line1\nline2\n"

    r = await env.read("notes.txt")
    assert r["ok"] is True
    assert r["content"] == "line1\nline2\n"
    assert r["total_lines"] == 2


async def test_read_offset_and_limit() -> None:
    http = _StubHttp()
    http.files["/root/f.txt"] = b"a\nb\nc\nd\n"
    env = _env(http)
    r = await env.read("f.txt", offset=2, limit=2)
    assert r["content"] == "b\nc\n"
    assert r["offset"] == 2


async def test_read_missing_file_returns_error() -> None:
    env = _env(_StubHttp())
    r = await env.read("nope.txt")
    assert "error" in r
    assert "not found" in r["error"].lower()


async def test_edit_replaces_text() -> None:
    http = _StubHttp()
    http.files["/root/c.txt"] = b"hello world\n"
    env = _env(http)
    res = await env.edit("c.txt", old_text="world", new_text="createos")
    assert res["ok"] is True
    assert http.files["/root/c.txt"] == b"hello createos\n"


async def test_edit_missing_old_text_errors() -> None:
    http = _StubHttp()
    http.files["/root/c.txt"] = b"hello\n"
    env = _env(http)
    res = await env.edit("c.txt", old_text="absent", new_text="x")
    assert "error" in res


async def test_shell_wraps_in_bash_with_cwd() -> None:
    http = _StubHttp()
    env = _env(http, cwd="/srv/app")
    res = await env.shell("echo hi")
    assert res["exit_code"] == 0
    assert res["stdout"] == "hi\n"
    call = http.exec_calls[0]
    assert call["cmd"] == "bash"
    assert call["args"][0] == "-c"
    assert call["args"][1] == "cd /srv/app && echo hi"


async def test_shell_nonzero_exit_sets_error() -> None:
    http = _StubHttp()

    def _post(path: str, body: Any = None) -> Any:
        return {"result": {"stdout": "", "stderr": "boom", "exit_code": 2}}

    http.post = _post  # type: ignore[assignment]
    env = _env(http)
    res = await env.shell("false")
    assert res["exit_code"] == 2
    assert "status 2" in res["error"]


def test_close_is_idempotent_and_destroys() -> None:
    http = _StubHttp()
    env = _env(http)
    env.close()
    env.close()
    assert http.deleted == ["/v1/sandboxes/sbx_test"]
    assert http.closed is True


def test_http_unwrap_jsend_envelope() -> None:
    import httpx

    req = httpx.Request("GET", "http://x")
    resp = httpx.Response(200, json={"status": "success", "data": {"id": "z"}}, request=req)
    http = _Http("http://x", "k")
    assert http._unwrap(resp) == {"id": "z"}
